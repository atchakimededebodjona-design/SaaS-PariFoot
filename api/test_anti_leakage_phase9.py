"""
test_anti_leakage_phase9.py — Phase 9, §43 : suite dédiée regroupant, SOUS
LEUR NOM EXACT demandé par le ticket, les garanties anti-fuite qui
traversent tout Phase 9 (scheduler, monitoring, retraining, promotion).

Certaines propriétés sont DÉJÀ testées ailleurs (ex. test_live_features.py::
test_future_result_not_used, test_retraining.py::test_leakage_detected_on_
future_match_date) — ce fichier ne les réimplémente pas depuis zéro mais les
revérifie sous le nom exact exigé par le ticket, avec un fixture minimal et
autonome, pour qu'elles restent découvrables par nom en un seul endroit.

Base isolée dédiée (jamais api/app.db) — même précaution que les suites de
tests précédentes (voir _test_support.py).

Usage : python api/test_anti_leakage_phase9.py
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_anti_leakage_phase9.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match, MatchStats
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion
from app.ai.engine.live_features import build_live_features
from app.ai.arena.prediction_logging import PredictionRecord, log_prediction, resolve_prediction
from app.ai.arena.ensemble import compute_market_weights
from app.ai.arena import retraining, service

init_db()

BASE_DATE = date(2025, 1, 1)


def _clean_all():
    with Session(engine) as session:
        for row in session.exec(select(ModelPrediction)).all():
            session.delete(row)
        for row in session.exec(select(MatchStats)).all():
            session.delete(row)
        for row in session.exec(select(Match)).all():
            session.delete(row)
        for v in session.exec(select(ModelVersion)).all():
            session.delete(v)
        session.commit()


def _make_version(session, model_type: str, is_active: bool = True) -> ModelVersion:
    v = ModelVersion(
        name=f"test-{model_type}-{datetime.now(timezone.utc).timestamp()}",
        model_type=model_type, trained_at=datetime.now(timezone.utc), is_active=is_active,
    )
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


def _log_resolved(session, model_type, version_id, match_date, p_true, *, source="live"):
    other = (1.0 - p_true) / 2
    record = PredictionRecord(
        league="Ligue1", match_date=match_date,
        home_team=f"Home-{match_date.isoformat()}-{source}", away_team=f"Away-{match_date.isoformat()}-{source}",
        model_type=model_type, prob_home=p_true, prob_draw=other, prob_away=other, source=source,
    )
    row = log_prediction(session, record, version_id)
    resolve_prediction(row, 2, 0)
    session.add(row)
    session.commit()
    return row


# ---------------------------------------------------------------------------
# 1. test_no_future_results_in_features
# ---------------------------------------------------------------------------

def test_no_future_results_in_features():
    """build_live_features(as_of=X) ne doit JAMAIS changer si un match
    postérieur à X est ajouté à la base ensuite — voir aussi
    test_live_features.py::test_future_result_not_used (même garantie,
    fixture plus large)."""
    _clean_all()
    with Session(engine) as session:
        session.add(Match(league="TestLeague", date=datetime.combine(BASE_DATE, datetime.min.time()),
                           home_team="A", away_team="B", home_goals=1, away_goals=0))
        session.add(Match(league="TestLeague", date=datetime.combine(BASE_DATE + timedelta(days=7), datetime.min.time()),
                           home_team="A", away_team="C", home_goals=2, away_goals=1))
        session.commit()
        as_of = BASE_DATE + timedelta(days=10)
        before = build_live_features(session, None, "TestLeague", "A", "D", as_of)

    with Session(engine) as session:
        # Match "futur" par rapport à as_of, avec un score extrême — s'il
        # fuitait dans les moyennes de forme de A, before != after.
        session.add(Match(league="TestLeague", date=datetime.combine(as_of + timedelta(days=1), datetime.min.time()),
                           home_team="A", away_team="E", home_goals=9, away_goals=0))
        session.commit()
        after = build_live_features(session, None, "TestLeague", "A", "D", as_of)

    for key in ("home_form_points_avg", "home_form_goals_scored_avg", "home_days_since_last_match"):
        b, a = before[key], after[key]
        same = (b == a) or (b != b and a != a)  # NaN == NaN est False en Python
        assert same, f"{key} a changé après insertion d'un match futur : {b} -> {a}"


# ---------------------------------------------------------------------------
# 2. test_no_future_matches_in_training_features
# ---------------------------------------------------------------------------

def test_no_future_matches_in_training_features():
    """check_training_data() doit détecter et bloquer sur un match dont la
    date est dans le futur — voir aussi test_retraining.py::
    test_leakage_detected_on_future_match_date."""
    _clean_all()
    with Session(engine) as session:
        for i in range(10):
            session.add(Match(league="Ligue1", date=datetime.combine(BASE_DATE + timedelta(days=i), datetime.min.time()),
                               home_team=f"H{i}", away_team=f"A{i}", home_goals=1, away_goals=0))
        session.add(Match(league="Ligue1", date=datetime.combine(date.today() + timedelta(days=30), datetime.min.time()),
                           home_team="Future", away_team="Team", home_goals=0, away_goals=0))
        session.commit()

        readiness = retraining.check_training_data(session, min_matches=1, min_leagues=1, min_period_days=1)
        assert readiness.leakage_detected is True
        assert readiness.ready is False


# ---------------------------------------------------------------------------
# 3-4. split temporel (voir aussi test_retraining.py, même garantie)
# ---------------------------------------------------------------------------

def test_validation_after_training_period():
    dates = [BASE_DATE + timedelta(days=i) for i in range(500)]
    split = retraining.compute_temporal_split(dates, n_test=100, n_val=50)
    assert split.validation_start > split.train_end, "la validation chevauche ou précède l'entraînement"


def test_test_after_validation_period():
    dates = [BASE_DATE + timedelta(days=i) for i in range(500)]
    split = retraining.compute_temporal_split(dates, n_test=100, n_val=50)
    assert split.test_start > split.validation_end, "le test chevauche ou précède la validation"


# ---------------------------------------------------------------------------
# 5. test_no_future_prediction_in_benchmark
# ---------------------------------------------------------------------------

def test_no_future_prediction_in_benchmark():
    """compute_market_weights(until=X) ne doit jamais inclure une prédiction
    postérieure à X dans son échantillon (§9 Phase 7, revérifié ici sous le
    nom exigé par le ticket Phase 9)."""
    _clean_all()
    with Session(engine) as session:
        v = _make_version(session, "xgboost")
        cutoff = BASE_DATE + timedelta(days=50)
        for i in range(120):
            _log_resolved(session, "xgboost", v.id, cutoff - timedelta(days=i + 1), p_true=0.6)  # avant/au cutoff
        # Prédictions APRÈS le cutoff — ne doivent jamais compter.
        for i in range(30):
            _log_resolved(session, "xgboost", v.id, cutoff + timedelta(days=i + 1), p_true=0.9)

        weights = compute_market_weights(session, "1X2", until=cutoff, min_sample_size=1)
        assert "xgboost" in weights.weights
        assert weights.weights["xgboost"].sample_size == 120, (
            f"des prédictions postérieures à `until` ont fuité dans le benchmark : "
            f"{weights.weights['xgboost'].sample_size} != 120"
        )


# ---------------------------------------------------------------------------
# 6. test_live_and_backtest_are_separated
# ---------------------------------------------------------------------------

def test_live_and_backtest_are_separated():
    """GET /models/benchmark (service.get_models_benchmark) avec
    prediction_source explicite ne doit jamais mélanger live/backtest — voir
    aussi test_monitoring.py::test_live_and_backtest_are_separated (même
    garantie, au niveau monitoring plutôt que benchmark)."""
    _clean_all()
    with Session(engine) as session:
        v = _make_version(session, "xgboost")
        for i in range(5):
            _log_resolved(session, "xgboost", v.id, BASE_DATE + timedelta(days=i), p_true=0.6, source="live")
        for i in range(5):
            _log_resolved(session, "xgboost", v.id, BASE_DATE + timedelta(days=100 + i), p_true=0.6, source="backtest")

        live_bench = service.get_models_benchmark(session, league_models={}, market="1X2", prediction_source="live")
        backtest_bench = service.get_models_benchmark(session, league_models={}, market="1X2", prediction_source="backtest")

        live_entry = next(m for m in live_bench.markets["1X2"].models if m.model_type == "xgboost")
        backtest_entry = next(m for m in backtest_bench.markets["1X2"].models if m.model_type == "xgboost")
        assert live_entry.sample_size == 5
        assert backtest_entry.sample_size == 5


# ---------------------------------------------------------------------------
# 7. test_prediction_probability_immutable
# ---------------------------------------------------------------------------

def test_prediction_probability_immutable():
    _clean_all()
    with Session(engine) as session:
        v = _make_version(session, "xgboost")
        record = PredictionRecord(
            league="Ligue1", match_date=BASE_DATE, home_team="A", away_team="B",
            model_type="xgboost", prob_home=0.55, prob_draw=0.25, prob_away=0.20, source="live",
        )
        row = log_prediction(session, record, v.id)
        session.commit()
        original = (row.prob_home, row.prob_draw, row.prob_away)

        resolve_prediction(row, 2, 0)
        session.add(row)
        session.commit()
        session.refresh(row)
        assert (row.prob_home, row.prob_draw, row.prob_away) == original, "resolve_prediction a modifié les probabilités"

        try:
            resolve_prediction(row, 0, 0)
            assert False, "une deuxième résolution aurait dû lever RuntimeError"
        except RuntimeError:
            pass
        session.refresh(row)
        assert (row.prob_home, row.prob_draw, row.prob_away) == original


# ---------------------------------------------------------------------------
# 8. test_retraining_does_not_modify_old_predictions (voir aussi
#    test_retraining.py, même nom, fixture différente)
# ---------------------------------------------------------------------------

def test_retraining_does_not_modify_old_predictions():
    _clean_all()
    with Session(engine) as session:
        old_version = _make_version(session, "lightgbm", is_active=True)
        row = _log_resolved(session, "lightgbm", old_version.id, BASE_DATE, p_true=0.65)
        snapshot = (row.prob_home, row.prob_draw, row.prob_away, row.status, row.result_home_goals)

        import pandas as pd
        import numpy as np
        from app.ai.engine.features import FEATURE_COLUMNS, CATEGORICAL_COLUMNS

        rng = np.random.default_rng(1)
        rows = []
        for i in range(160):
            rows.append({
                "date": pd.Timestamp(BASE_DATE + timedelta(days=i)), "league": "Ligue1",
                "home_team": f"H{i}", "away_team": f"A{i}",
                "home_goals": int(rng.integers(0, 3)), "away_goals": int(rng.integers(0, 3)),
                **{c: rng.uniform(0, 1) for c in FEATURE_COLUMNS},
            })
        df = pd.DataFrame(rows)
        retraining.create_candidate_version(session, "lightgbm", df, FEATURE_COLUMNS, CATEGORICAL_COLUMNS, n_test=30, n_val=20)
        session.commit()

        session.refresh(row)
        after = (row.prob_home, row.prob_draw, row.prob_away, row.status, row.result_home_goals)
        assert after == snapshot


UNIT_TESTS = [
    test_no_future_results_in_features,
    test_no_future_matches_in_training_features,
    test_validation_after_training_period,
    test_test_after_validation_period,
    test_no_future_prediction_in_benchmark,
    test_live_and_backtest_are_separated,
    test_prediction_probability_immutable,
    test_retraining_does_not_modify_old_predictions,
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
