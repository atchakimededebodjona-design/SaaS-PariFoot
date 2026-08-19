"""
test_shadow_mode.py — Phase 9, Partie F/G : mode SHADOW
(app/ai/arena/promotion.py::set_shadow) — une version shadow ne doit JAMAIS
entrer dans un chemin de décision Phase 5-8 existant (ensemble, meilleure
version active, get_or_create_active_model_version), et ses prédictions
(role="shadow") doivent rester exclues des agrégats par défaut.

Base isolée dédiée (jamais api/app.db) — même précaution que les suites de
tests précédentes (voir _test_support.py).

Usage : python api/test_shadow_mode.py
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_shadow_mode.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion
from app.ai.arena.prediction_logging import (
    PredictionRecord, log_prediction, resolve_prediction, get_or_create_active_model_version,
)
from app.ai.arena.ensemble import _active_version, compute_market_weights
from app.ai.arena import monitoring, promotion

init_db()

BASE_DATE = date(2026, 1, 1)


def _clean_all():
    with Session(engine) as session:
        for row in session.exec(select(ModelPrediction)).all():
            session.delete(row)
        for v in session.exec(select(ModelVersion)).all():
            session.delete(v)
        session.commit()


def _make_version(session, model_type: str, *, is_active=False, status="active") -> ModelVersion:
    v = ModelVersion(
        name=f"test-{model_type}-{datetime.now(timezone.utc).timestamp()}",
        model_type=model_type, trained_at=datetime.now(timezone.utc), is_active=is_active, status=status,
    )
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


def _log_resolved(session, model_type, version_id, match_date, p_true, *, role="active", home_team=None, away_team=None):
    home_team = home_team or f"Home-{match_date.isoformat()}-{role}"
    away_team = away_team or f"Away-{match_date.isoformat()}-{role}"
    other = (1.0 - p_true) / 2
    record = PredictionRecord(
        league="Ligue1", match_date=match_date, home_team=home_team, away_team=away_team,
        model_type=model_type, prob_home=p_true, prob_draw=other, prob_away=other,
        source="live", role=role,
    )
    row = log_prediction(session, record, version_id)
    resolve_prediction(row, 2, 0)
    session.add(row)
    session.commit()
    return row


def test_set_shadow_flips_status_but_never_is_active():
    _clean_all()
    with Session(engine) as session:
        v = _make_version(session, "lightgbm", is_active=False, status="candidate")
        result = promotion.set_shadow(session, v.id)
        session.commit()
        session.refresh(v)
        assert result.status == "shadow"
        assert v.status == "shadow"
        assert v.is_active is False


def test_shadow_version_excluded_from_active_version_lookup():
    _clean_all()
    with Session(engine) as session:
        active = _make_version(session, "lightgbm", is_active=True, status="active")
        shadow_candidate = _make_version(session, "lightgbm", is_active=False, status="candidate")
        promotion.set_shadow(session, shadow_candidate.id)
        session.commit()

        found = _active_version(session, "lightgbm")
        assert found is not None and found.id == active.id, "un shadow ne doit jamais être trouvé comme version active"


def test_shadow_version_excluded_from_ensemble_weights():
    """Une version shadow, même avec des prédictions résolues, ne doit
    jamais apparaître dans compute_market_weights (qui ne considère que les
    versions actives, voir ensemble.py::_active_version)."""
    _clean_all()
    with Session(engine) as session:
        active = _make_version(session, "lightgbm", is_active=True, status="active")
        shadow = _make_version(session, "lightgbm", is_active=False, status="candidate")
        promotion.set_shadow(session, shadow.id)
        session.commit()

        for i in range(150):
            _log_resolved(session, "lightgbm", active.id, BASE_DATE + timedelta(days=i), p_true=0.6, role="active")
        # Le shadow accumule AUSSI ses propres prédictions résolues (c'est
        # tout l'intérêt du mode shadow) — mais ne doit jamais peser dans
        # l'ensemble tant qu'il n'est pas promu.
        for i in range(150):
            _log_resolved(session, "lightgbm", shadow.id, BASE_DATE + timedelta(days=i), p_true=0.9, role="shadow")

        weights = compute_market_weights(session, "1X2", until=date.today() + timedelta(days=1), min_sample_size=50)
        assert "lightgbm" in weights.weights
        used_version_id = weights.weights["lightgbm"].model_version_id
        assert used_version_id == active.id, "compute_market_weights a utilisé la version shadow au lieu de l'active"


def test_get_or_create_active_model_version_never_returns_a_shadow():
    _clean_all()
    with Session(engine) as session:
        shadow = _make_version(session, "elo", is_active=False, status="candidate")
        promotion.set_shadow(session, shadow.id)
        session.commit()

        # Aucune version active -> get_or_create_active_model_version doit en
        # CRÉER une nouvelle plutôt que de réutiliser le shadow existant.
        created = get_or_create_active_model_version(session, "elo", "xfoot-elo", "bootstrap test")
        assert created.id != shadow.id
        assert created.is_active is True


def test_shadow_predictions_excluded_from_default_live_monitoring():
    _clean_all()
    with Session(engine) as session:
        active = _make_version(session, "xgboost", is_active=True, status="active")
        shadow = _make_version(session, "xgboost", is_active=False, status="candidate")
        promotion.set_shadow(session, shadow.id)
        session.commit()

        for i in range(20):
            _log_resolved(session, "xgboost", active.id, BASE_DATE + timedelta(days=i), p_true=0.7, role="active")
        for i in range(20):
            _log_resolved(session, "xgboost", shadow.id, BASE_DATE + timedelta(days=i), p_true=0.1, role="shadow")

        default_monitoring = monitoring.get_live_monitoring(session, "xgboost", "1X2", min_sample_size=1)
        assert default_monitoring["ALL_TIME"].sample_size == 20, "les prédictions shadow ont fuité dans l'agrégat par défaut"

        shadow_monitoring = monitoring.get_live_monitoring(session, "xgboost", "1X2", min_sample_size=1, role="shadow")
        assert shadow_monitoring["ALL_TIME"].sample_size == 20


def test_shadow_resolved_via_same_status_pending_query_as_active():
    """§25 : fetch_daily_results.py ne filtre jamais sur `role` — une
    prédiction shadow status='pending' doit rester sélectionnable par la
    même requête que les prédictions actives (aucune régression attendue,
    voir test_resolution_shadow.py pour le test bout-en-bout du script)."""
    _clean_all()
    with Session(engine) as session:
        shadow = _make_version(session, "xgboost", is_active=False, status="candidate")
        promotion.set_shadow(session, shadow.id)
        session.commit()

        record = PredictionRecord(
            league="Ligue1", match_date=BASE_DATE, home_team="A", away_team="B",
            model_type="xgboost", prob_home=0.5, prob_draw=0.3, prob_away=0.2,
            source="live", role="shadow",
        )
        row = log_prediction(session, record, shadow.id)
        session.commit()

        pending = session.exec(
            select(ModelPrediction).where(
                ModelPrediction.match_date == BASE_DATE, ModelPrediction.status == "pending",
            )
        ).all()
        assert any(p.id == row.id for p in pending)


UNIT_TESTS = [
    test_set_shadow_flips_status_but_never_is_active,
    test_shadow_version_excluded_from_active_version_lookup,
    test_shadow_version_excluded_from_ensemble_weights,
    test_get_or_create_active_model_version_never_returns_a_shadow,
    test_shadow_predictions_excluded_from_default_live_monitoring,
    test_shadow_resolved_via_same_status_pending_query_as_active,
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
