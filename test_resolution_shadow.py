"""
test_resolution_shadow.py — Phase 9, §3/§25 : confirme que
fetch_daily_results.py (INCHANGÉ par la Phase 9) résout déjà correctement
les ModelPrediction role="shadow" exactement comme role="active" — sa
requête de sélection des lignes "pending" ne filtre jamais sur `role`
(vérifié en lisant le code, garde de régression ici plutôt qu'une
modification du script).

Même précaution que test_fetch_daily_results.py : base isolée dédiée,
jamais le vrai réseau (httpx.get mocké).

Usage : python test_resolution_shadow.py
"""

import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

DB_PATH = Path(__file__).parent / "test_resolution_shadow.db"
if DB_PATH.exists():
    DB_PATH.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["API_FOOTBALL_KEY"] = "test_dummy_key_never_sent_over_real_network"

sys.path.insert(0, str(Path(__file__).parent / "api"))

import fetch_daily_results
from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion
from app.ai.arena.prediction_logging import PredictionRecord, log_prediction
import app.core.api_football_config as api_football_config

TARGET_DATE = date(2026, 8, 16)

init_db()


class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _make_fixture(home: str, away: str, home_goals: int, away_goals: int, league_id: int) -> dict:
    return {
        "league": {"id": league_id},
        "teams": {"home": {"name": home}, "away": {"name": away}},
        "goals": {"home": home_goals, "away": away_goals},
    }


def _clean_all():
    with Session(engine) as session:
        for row in session.exec(select(ModelPrediction)).all():
            session.delete(row)
        for v in session.exec(select(ModelVersion)).all():
            session.delete(v)
        session.commit()


def _make_version(session, model_type: str, status: str, is_active: bool) -> ModelVersion:
    v = ModelVersion(
        name=f"test-{model_type}-{status}-{datetime.now(timezone.utc).timestamp()}",
        model_type=model_type, trained_at=datetime.now(timezone.utc), is_active=is_active, status=status,
    )
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


def _log_pending(session, model_type, version_id, role, home_team, away_team, league="Ligue1"):
    record = PredictionRecord(
        league=league, match_date=TARGET_DATE, home_team=home_team, away_team=away_team,
        model_type=model_type, prob_home=0.5, prob_draw=0.3, prob_away=0.2, source="live", role=role,
    )
    row = log_prediction(session, record, version_id)
    session.commit()
    session.refresh(row)
    return row


def test_shadow_prediction_resolved_by_the_same_unmodified_query():
    _clean_all()
    with Session(engine) as session:
        active_v = _make_version(session, "xgboost", status="active", is_active=True)
        shadow_v = _make_version(session, "xgboost", status="shadow", is_active=False)
        active_row_id = _log_pending(session, "xgboost", active_v.id, "active", "Paris SG", "Marseille").id
        shadow_row_id = _log_pending(session, "xgboost", shadow_v.id, "shadow", "Paris SG", "Marseille").id

    fixture = _make_fixture("Paris SG", "Marseille", 2, 0, league_id=api_football_config.API_FOOTBALL_LEAGUE_IDS["Ligue1"])

    def fake_get(url, params=None, headers=None, timeout=None):
        return FakeResponse({"response": [fixture], "paging": {"current": 1, "total": 1}})

    with patch("httpx.get", side_effect=fake_get):
        exit_code = fetch_daily_results.run(target_date=TARGET_DATE)
    assert exit_code == 0, "les deux lignes (active + shadow) auraient dû être rapprochées"

    with Session(engine) as session:
        active_after = session.get(ModelPrediction, active_row_id)
        shadow_after = session.get(ModelPrediction, shadow_row_id)

    assert active_after.status == "resolved"
    assert active_after.role == "active"
    assert (active_after.result_home_goals, active_after.result_away_goals) == (2, 0)

    assert shadow_after.status == "resolved", "la ligne shadow n'a jamais été résolue"
    assert shadow_after.role == "shadow"
    assert (shadow_after.result_home_goals, shadow_after.result_away_goals) == (2, 0)
    assert shadow_after.correct_1x2 is True  # pick "home" (0.5 >= 0.3,0.2), résultat 2-0 -> home

    print("  [OK] prédiction shadow résolue par fetch_daily_results.py sans aucune modification du script")


def test_active_and_shadow_resolved_independently_never_conflated():
    """Deux model_version_id différents (une active, une shadow) pour le
    MÊME match -> deux lignes model_predictions distinctes (contrainte
    UNIQUE incluant model_version_id), chacune résolue séparément."""
    _clean_all()
    with Session(engine) as session:
        active_v = _make_version(session, "lightgbm", status="active", is_active=True)
        shadow_v = _make_version(session, "lightgbm", status="shadow", is_active=False)
        _log_pending(session, "lightgbm", active_v.id, "active", "Lyon", "Nice")
        _log_pending(session, "lightgbm", shadow_v.id, "shadow", "Lyon", "Nice")

        rows = session.exec(select(ModelPrediction).where(ModelPrediction.model_type == "lightgbm")).all()
        assert len(rows) == 2, "une ligne active et une ligne shadow distinctes sont attendues"

    fixture = _make_fixture("Lyon", "Nice", 1, 1, league_id=api_football_config.API_FOOTBALL_LEAGUE_IDS["Ligue1"])

    def fake_get(url, params=None, headers=None, timeout=None):
        return FakeResponse({"response": [fixture], "paging": {"current": 1, "total": 1}})

    with patch("httpx.get", side_effect=fake_get):
        exit_code = fetch_daily_results.run(target_date=TARGET_DATE)
    assert exit_code == 0

    with Session(engine) as session:
        rows = session.exec(select(ModelPrediction).where(ModelPrediction.model_type == "lightgbm")).all()
        assert all(r.status == "resolved" for r in rows)
        roles = {r.role for r in rows}
        assert roles == {"active", "shadow"}

    print("  [OK] prédictions active et shadow du même match résolues indépendamment, jamais confondues")


TESTS = [
    test_shadow_prediction_resolved_by_the_same_unmodified_query,
    test_active_and_shadow_resolved_independently_never_conflated,
]


if __name__ == "__main__":
    failures = 0
    for t in TESTS:
        print(f"\n=== {t.__name__} ===")
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"  [ECHEC] {e}")

    total = len(TESTS)
    try:
        if DB_PATH.exists():
            DB_PATH.unlink()
    except PermissionError:
        pass  # verrou Windows bref sur le fichier SQLite — sans conséquence (même précaution que _test_support.cleanup_db)
    print(f"\n{'='*60}\n{total-failures}/{total} tests reussis\n{'='*60}")
    sys.exit(1 if failures else 0)
