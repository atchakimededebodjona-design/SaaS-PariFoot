"""
test_promotion_api.py — Phase 10 : endpoints GET/POST /models/promotion/* —
auth admin (ADMIN_EMAILS), traçabilité (model_promotion_events écrit dans
tous les cas, y compris rejet), idempotence de promote, evaluate n'applique
jamais de promotion.

Base isolée dédiée (jamais api/app.db). Usage : python api/test_promotion_api.py
"""

import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db, register_and_login

DB_PATH = configure_test_env("test_promotion_api.db")

from fastapi.testclient import TestClient
from sqlmodel import Session, select
from main import app, engine
from app.core.database import init_db
from app.models.model_prediction import ModelPrediction
from app.models.model_promotion_event import ModelPromotionEvent
from app.models.team_rating import ModelVersion
from app.ai.arena.prediction_logging import PredictionRecord, log_prediction, resolve_prediction

init_db()
client = TestClient(app)
BASE_DATE = date(2026, 1, 1)

ADMIN_EMAIL = "admin@xfoot-test.example.com"
USER_EMAIL = "user@xfoot-test.example.com"


def _clean_all():
    with Session(engine) as session:
        for row in session.exec(select(ModelPromotionEvent)).all():
            session.delete(row)
        for row in session.exec(select(ModelPrediction)).all():
            session.delete(row)
        for v in session.exec(select(ModelVersion)).all():
            session.delete(v)
        session.commit()


def _make_version(session, model_type: str, *, is_active=False, status="active") -> ModelVersion:
    v = ModelVersion(name=f"test-{model_type}-{datetime.now(timezone.utc).timestamp()}",
                      model_type=model_type, trained_at=datetime.now(timezone.utc),
                      is_active=is_active, status=status)
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


def _log_n_resolved(session, model_type, version_id, n, p_true, *, role="active"):
    other = (1.0 - p_true) / 2
    for i in range(n):
        d = BASE_DATE + timedelta(days=i)
        record = PredictionRecord(
            league="Ligue1", match_date=d, home_team=f"H-{model_type}-{version_id}-{d}",
            away_team=f"A-{model_type}-{version_id}-{d}", model_type=model_type,
            prob_home=p_true, prob_draw=other, prob_away=other, source="live", role=role,
        )
        row = log_prediction(session, record, version_id)
        resolve_prediction(row, 2, 0)
        session.add(row)
    session.commit()


def _setup_eligible_shadow():
    """Crée une version active médiocre + une version shadow clairement
    meilleure, échantillon suffisant des deux côtés."""
    with Session(engine) as session:
        active = _make_version(session, "xgboost", is_active=True, status="active")
        shadow = _make_version(session, "xgboost", is_active=False, status="shadow")
        _log_n_resolved(session, "xgboost", active.id, 120, p_true=0.55, role="active")
        _log_n_resolved(session, "xgboost", shadow.id, 120, p_true=0.9, role="shadow")
        return active.id, shadow.id


def _auth_headers() -> tuple[dict, dict]:
    os.environ["ADMIN_EMAILS"] = ADMIN_EMAIL
    _, admin_token = register_and_login(client, ADMIN_EMAIL, "password123")
    _, user_token = register_and_login(client, USER_EMAIL, "password123")
    return {"Authorization": f"Bearer {admin_token}"}, {"Authorization": f"Bearer {user_token}"}


ADMIN_HEADERS, USER_HEADERS = _auth_headers()


def test_status_and_history_are_public():
    _clean_all()
    r = client.get("/models/promotion/status")
    assert r.status_code == 200, r.text
    r = client.get("/models/promotion/history")
    assert r.status_code == 200, r.text
    assert r.json()["events"] == []


def test_evaluate_requires_authentication():
    _clean_all()
    _, shadow_id = _setup_eligible_shadow()
    r = client.post("/models/promotion/evaluate", json={"model_version_id": shadow_id})
    assert r.status_code == 401, r.text


def test_evaluate_forbidden_for_non_admin():
    _clean_all()
    _, shadow_id = _setup_eligible_shadow()
    r = client.post("/models/promotion/evaluate", json={"model_version_id": shadow_id}, headers=USER_HEADERS)
    assert r.status_code == 403, r.text


def test_evaluate_never_applies_promotion_but_logs_history():
    _clean_all()
    active_id, shadow_id = _setup_eligible_shadow()

    r = client.post("/models/promotion/evaluate", json={"model_version_id": shadow_id}, headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "eligible"

    with Session(engine) as session:
        shadow = session.get(ModelVersion, shadow_id)
        active = session.get(ModelVersion, active_id)
        assert shadow.is_active is False, "evaluate ne doit JAMAIS appliquer la promotion"
        assert active.is_active is True

    h = client.get("/models/promotion/history").json()
    assert h["count"] == 1
    assert h["events"][0]["decision"] == "eligible"
    assert h["events"][0]["automatic"] is False
    assert h["events"][0]["actor"] == ADMIN_EMAIL


def test_promote_applies_and_is_idempotent_then_rejects():
    _clean_all()
    active_id, shadow_id = _setup_eligible_shadow()

    r = client.post("/models/promotion/promote", json={"model_version_id": shadow_id}, headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "promoted"
    assert r.json()["previous_model_version_id"] == active_id

    with Session(engine) as session:
        shadow = session.get(ModelVersion, shadow_id)
        active = session.get(ModelVersion, active_id)
        assert shadow.is_active is True and shadow.status == "active"
        assert active.is_active is False and active.status == "retired"

    # Deuxième appel : la version est désormais déjà active -> rejeté, jamais
    # une double promotion silencieuse.
    r2 = client.post("/models/promotion/promote", json={"model_version_id": shadow_id}, headers=ADMIN_HEADERS)
    assert r2.status_code == 400, r2.text
    assert r2.json()["detail"]["decision"]["status"] == "already_active"

    h = client.get("/models/promotion/history").json()
    decisions = [e["decision"] for e in h["events"]]
    assert "promoted" in decisions
    assert "already_active" in decisions, "le second appel rejeté doit rester tracé, jamais silencieux"


def test_promote_forbidden_for_non_admin():
    _clean_all()
    _, shadow_id = _setup_eligible_shadow()
    r = client.post("/models/promotion/promote", json={"model_version_id": shadow_id}, headers=USER_HEADERS)
    assert r.status_code == 403, r.text


UNIT_TESTS = [
    test_status_and_history_are_public,
    test_evaluate_requires_authentication,
    test_evaluate_forbidden_for_non_admin,
    test_evaluate_never_applies_promotion_but_logs_history,
    test_promote_applies_and_is_idempotent_then_rejects,
    test_promote_forbidden_for_non_admin,
]


if __name__ == "__main__":
    failures = 0
    for t in UNIT_TESTS:
        print(f"\n=== {t.__name__} ===")
        try:
            t()
            print("  [OK]")
        except AssertionError as e:
            failures += 1
            print(f"  [ECHEC] {e}")

    total = len(UNIT_TESTS)
    cleanup_db(DB_PATH)
    print(f"\n{'='*60}\n{total-failures}/{total} tests reussis\n{'='*60}")
    sys.exit(1 if failures else 0)
