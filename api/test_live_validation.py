"""
test_live_validation.py — Phase 10 : app/ai/arena/live_validation.py —
métriques LIVE scopées par model_version_id précise (jamais mélangées entre
deux versions du même model_type, contrairement à monitoring.py qui agrège
par `role`), prédictions pending exclues, période min/max correcte.

Base isolée dédiée (jamais api/app.db) — même précaution que les suites de
tests précédentes (voir _test_support.py).

Usage : python api/test_live_validation.py
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_live_validation.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion
from app.ai.arena.prediction_logging import PredictionRecord, log_prediction, resolve_prediction
from app.ai.arena import live_validation

init_db()

BASE_DATE = date(2026, 1, 1)


def _clean_all():
    with Session(engine) as session:
        for row in session.exec(select(ModelPrediction)).all():
            session.delete(row)
        for v in session.exec(select(ModelVersion)).all():
            session.delete(v)
        session.commit()


def _make_version(session, model_type: str) -> ModelVersion:
    v = ModelVersion(name=f"test-{model_type}-{datetime.now(timezone.utc).timestamp()}",
                      model_type=model_type, trained_at=datetime.now(timezone.utc))
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


def _log(session, model_type, version_id, match_date, p_true, *, resolve=True):
    other = (1.0 - p_true) / 2
    record = PredictionRecord(
        league="Ligue1", match_date=match_date,
        home_team=f"Home-{match_date.isoformat()}", away_team=f"Away-{match_date.isoformat()}",
        model_type=model_type, prob_home=p_true, prob_draw=other, prob_away=other, source="live",
    )
    row = log_prediction(session, record, version_id)
    if resolve:
        resolve_prediction(row, 2, 0)
        session.add(row)
    session.commit()
    return row


def test_metrics_scoped_to_single_version_never_mixed():
    """Deux versions shadow successives du même model_type ne doivent jamais
    partager un échantillon — c'est précisément ce que monitoring.py
    n'offre pas (il agrège par role, pas par model_version_id)."""
    _clean_all()
    with Session(engine) as session:
        v1 = _make_version(session, "xgboost")
        v2 = _make_version(session, "xgboost")
        for i in range(10):
            _log(session, "xgboost", v1.id, BASE_DATE + timedelta(days=i), p_true=0.9)
        for i in range(5):
            _log(session, "xgboost", v2.id, BASE_DATE + timedelta(days=100 + i), p_true=0.2)

        m1 = live_validation.compute_live_model_metrics(session, "xgboost", v1.id, "1X2")
        m2 = live_validation.compute_live_model_metrics(session, "xgboost", v2.id, "1X2")

        assert m1.sample_size == 10
        assert m2.sample_size == 5
        assert m1.log_loss != m2.log_loss


def test_pending_predictions_excluded_from_metrics_but_counted():
    _clean_all()
    with Session(engine) as session:
        v = _make_version(session, "lightgbm")
        for i in range(5):
            _log(session, "lightgbm", v.id, BASE_DATE + timedelta(days=i), p_true=0.7, resolve=True)
        for i in range(3):
            _log(session, "lightgbm", v.id, BASE_DATE + timedelta(days=50 + i), p_true=0.7, resolve=False)

        m = live_validation.compute_live_model_metrics(session, "lightgbm", v.id, "1X2")
        assert m.predictions_resolved == 5
        assert m.predictions_pending == 3
        assert m.sample_size == 5, "les predictions pending ne doivent jamais entrer dans sample_size/métriques"


def test_period_reflects_min_max_match_date_across_all_rows():
    _clean_all()
    with Session(engine) as session:
        v = _make_version(session, "elo")
        _log(session, "elo", v.id, BASE_DATE, p_true=0.6)
        _log(session, "elo", v.id, BASE_DATE + timedelta(days=30), p_true=0.6, resolve=False)

        m = live_validation.compute_live_model_metrics(session, "elo", v.id, "1X2")
        assert m.period_start == BASE_DATE
        assert m.period_end == BASE_DATE + timedelta(days=30)


def test_no_predictions_returns_honest_zero_never_fabricated():
    _clean_all()
    with Session(engine) as session:
        v = _make_version(session, "dixon_coles")
        m = live_validation.compute_live_model_metrics(session, "dixon_coles", v.id, "1X2")
        assert m.sample_size == 0
        assert m.log_loss is None
        assert m.accuracy is None
        assert m.period_start is None and m.period_end is None


UNIT_TESTS = [
    test_metrics_scoped_to_single_version_never_mixed,
    test_pending_predictions_excluded_from_metrics_but_counted,
    test_period_reflects_min_max_match_date_across_all_rows,
    test_no_predictions_returns_honest_zero_never_fabricated,
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
