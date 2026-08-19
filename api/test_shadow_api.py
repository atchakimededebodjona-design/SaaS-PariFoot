"""
test_shadow_api.py — Phase 11 : GET /models/shadow/status et
GET /models/shadow/comparison — publics (mesure pure, aucune décision
appliquée), "insufficient LIVE data" honnête, jamais de "meilleur modèle"
annoncé sous le seuil.

Base isolée dédiée (jamais api/app.db). Usage : python api/test_shadow_api.py
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_shadow_api.db")

from fastapi.testclient import TestClient
from sqlmodel import Session, select
from main import app, engine
from app.core.database import init_db
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion
from app.ai.arena.prediction_logging import PredictionRecord, log_prediction, resolve_prediction
from app.ai.arena import promotion

init_db()
client = TestClient(app)
BASE_DATE = date(2026, 1, 1)


def _clean_all():
    with Session(engine) as session:
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


def _log_n_resolved(session, model_type, version_id, n, p_true, *, role):
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


def test_shadow_status_is_public_and_lists_active_and_shadow():
    _clean_all()
    with Session(engine) as session:
        active = _make_version(session, "xgboost", is_active=True, status="active")
        shadow = _make_version(session, "xgboost", is_active=False, status="shadow")
        _log_n_resolved(session, "xgboost", active.id, 5, 0.6, role="active")
        _log_n_resolved(session, "xgboost", shadow.id, 3, 0.7, role="shadow")

    r = client.get("/models/shadow/status?model_type=xgboost")
    assert r.status_code == 200, r.text
    body = r.json()
    xgb = body["models"]["xgboost"]
    assert xgb["active"]["predictions_resolved"] == 5
    assert len(xgb["shadow"]) == 1
    assert xgb["shadow"][0]["predictions_resolved"] == 3


def test_shadow_status_honest_when_no_shadow_active():
    _clean_all()
    with Session(engine) as session:
        _make_version(session, "elo", is_active=True, status="active")
    r = client.get("/models/shadow/status?model_type=elo")
    assert r.status_code == 200
    assert r.json()["models"]["elo"]["shadow"] == []


def test_shadow_comparison_insufficient_data_never_claims_best_model():
    _clean_all()
    with Session(engine) as session:
        active = _make_version(session, "lightgbm", is_active=True, status="active")
        shadow = _make_version(session, "lightgbm", is_active=False, status="shadow")
        _log_n_resolved(session, "lightgbm", active.id, 5, 0.6, role="active")
        _log_n_resolved(session, "lightgbm", shadow.id, 5, 0.9, role="shadow")

    r = client.get("/models/shadow/comparison?model_type=lightgbm&market=1X2")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "insufficient_matched_sample"
    assert "best" not in str(body).lower() or "best_model" not in body, (
        "aucune clé 'best_model' ne doit apparaître sous le seuil de significativité"
    )
    assert body["active"]["log_loss"] is None and body["shadow"]["log_loss"] is None, (
        "aucune métrique ne doit être publiée pour un échantillon matched insuffisant"
    )


def test_shadow_comparison_ok_with_sufficient_matched_sample():
    _clean_all()
    with Session(engine) as session:
        active = _make_version(session, "xgboost", is_active=True, status="active")
        shadow = _make_version(session, "xgboost", is_active=False, status="shadow")
        for i in range(120):
            d = BASE_DATE + timedelta(days=i)
            for version_id, role, p_true in ((active.id, "active", 0.55), (shadow.id, "shadow", 0.9)):
                other = (1.0 - p_true) / 2
                record = PredictionRecord(
                    league="Ligue1", match_date=d, home_team=f"H{i}", away_team=f"A{i}",
                    model_type="xgboost", prob_home=p_true, prob_draw=other, prob_away=other,
                    source="live", role=role,
                )
                row = log_prediction(session, record, version_id)
                resolve_prediction(row, 2, 0)
                session.add(row)
        session.commit()

    r = client.get("/models/shadow/comparison?model_type=xgboost&market=1X2")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["matched_sample_size"] == 120
    assert body["deltas"]["log_loss"] < 0


def test_shadow_comparison_rejects_unknown_model_type():
    r = client.get("/models/shadow/comparison?model_type=not_a_model&market=1X2")
    assert r.status_code == 400


UNIT_TESTS = [
    test_shadow_status_is_public_and_lists_active_and_shadow,
    test_shadow_status_honest_when_no_shadow_active,
    test_shadow_comparison_insufficient_data_never_claims_best_model,
    test_shadow_comparison_ok_with_sufficient_matched_sample,
    test_shadow_comparison_rejects_unknown_model_type,
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
