"""
test_retraining.py — Phase 9, Partie F : infrastructure de réentraînement
continu (app/ai/arena/retraining.py) — disponibilité des données, split
temporel, entraînement + candidate ModelVersion sur un petit jeu de données
SYNTHÉTIQUE (jamais build_ml_features_from_db(), ~35 min sur la base de dev
réelle — voir test_ml_stacking.py pour la même précaution).

Base isolée dédiée (jamais api/app.db) — même précaution que les suites de
tests précédentes (voir _test_support.py).

Usage : python api/test_retraining.py
"""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_retraining.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match, MatchStats
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion
from app.ai.arena.prediction_logging import PredictionRecord, log_prediction, resolve_prediction
from app.ai.engine.features import FEATURE_COLUMNS, CATEGORICAL_COLUMNS
from app.ai.arena import retraining

init_db()


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


def _insert_matches(session, n: int, *, start: date, leagues=("Ligue1",), with_stats=True):
    for i in range(n):
        league = leagues[i % len(leagues)]
        m = Match(
            league=league, date=start + timedelta(days=i),
            home_team=f"Home{i}", away_team=f"Away{i}",
            home_goals=1 + (i % 3), away_goals=i % 2,
        )
        session.add(m)
        session.flush()
        if with_stats:
            session.add(MatchStats(match_id=m.id, home_shots=10, away_shots=8,
                                    home_shots_target=5, away_shots_target=4,
                                    home_corners=6, away_corners=5))
    session.commit()


# ---------------------------------------------------------------------------
# check_training_data
# ---------------------------------------------------------------------------

def test_ready_when_all_conditions_met():
    _clean_all()
    with Session(engine) as session:
        _insert_matches(session, 600, start=date(2020, 1, 1), leagues=("Ligue1", "PremierLeague"))
        readiness = retraining.check_training_data(session, min_matches=500, min_leagues=1, min_period_days=90)
        assert readiness.ready is True, readiness.reason
        assert readiness.match_count == 600
        assert readiness.league_count == 2
        assert readiness.duplicate_count == 0
        assert readiness.leakage_detected is False


def test_not_ready_when_not_enough_matches():
    _clean_all()
    with Session(engine) as session:
        _insert_matches(session, 50, start=date(2020, 1, 1))
        readiness = retraining.check_training_data(session, min_matches=500)
        assert readiness.ready is False
        assert "match_count" in readiness.reason


def test_not_ready_when_not_enough_leagues():
    _clean_all()
    with Session(engine) as session:
        _insert_matches(session, 600, start=date(2020, 1, 1), leagues=("Ligue1",))
        readiness = retraining.check_training_data(session, min_matches=500, min_leagues=2)
        assert readiness.ready is False
        assert "league_count" in readiness.reason


def test_not_ready_when_period_too_short():
    _clean_all()
    with Session(engine) as session:
        # 600 matchs mais tous le même jour -> période nulle
        for i in range(600):
            session.add(Match(league="Ligue1", date=date(2020, 1, 1), home_team=f"H{i}", away_team=f"A{i}",
                               home_goals=1, away_goals=0))
        session.commit()
        readiness = retraining.check_training_data(session, min_matches=500, min_period_days=90)
        assert readiness.ready is False
        assert "Période" in readiness.reason


def test_missing_match_stats_reported_but_not_necessarily_blocking_reason():
    _clean_all()
    with Session(engine) as session:
        _insert_matches(session, 600, start=date(2020, 1, 1), with_stats=False)
        readiness = retraining.check_training_data(session, min_matches=500, min_period_days=90)
        assert readiness.missing_value_columns, "match_stats totalement absent doit être signalé"


def test_leakage_detected_on_future_match_date():
    _clean_all()
    with Session(engine) as session:
        _insert_matches(session, 600, start=date(2020, 1, 1))
        future = Match(league="Ligue1", date=date.today() + timedelta(days=10),
                        home_team="FutureHome", away_team="FutureAway", home_goals=0, away_goals=0)
        session.add(future)
        session.commit()
        readiness = retraining.check_training_data(session, min_matches=500, min_period_days=90)
        assert readiness.leakage_detected is True
        assert readiness.ready is False


# ---------------------------------------------------------------------------
# compute_temporal_split
# ---------------------------------------------------------------------------

def test_temporal_split_indices_and_boundaries():
    dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(1000)]
    split = retraining.compute_temporal_split(dates, n_test=300, n_val=100)
    assert split.n_train == 600
    assert split.n_validation == 100
    assert split.n_test == 300
    assert split.train_start == dates[0]
    assert split.train_end == dates[599]
    assert split.validation_start == dates[600]
    assert split.validation_end == dates[699]
    assert split.test_start == dates[700]
    assert split.test_end == dates[999]


def test_validation_after_training_period():
    dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(1000)]
    split = retraining.compute_temporal_split(dates, n_test=300, n_val=100)
    assert split.validation_start > split.train_end


def test_test_after_validation_period():
    dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(1000)]
    split = retraining.compute_temporal_split(dates, n_test=300, n_val=100)
    assert split.test_start > split.validation_end


def test_temporal_split_raises_when_not_enough_data():
    dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(50)]
    try:
        retraining.compute_temporal_split(dates, n_test=300, n_val=100)
        assert False, "devait lever ValueError"
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# fit_ml_model / create_candidate_version — jeu de données synthétique
# ---------------------------------------------------------------------------

def _synthetic_df(n: int, *, start: date, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = [start + timedelta(days=i) for i in range(n)]
    leagues = ["Ligue1", "PremierLeague"]
    rows = []
    for i in range(n):
        # Cible corrélée à une feature (home_form_points_avg) pour que
        # l'entraînement produise un log_loss non-trivial mais raisonnable,
        # jamais du bruit pur (qui rendrait early stopping/évaluation instables).
        home_form = rng.uniform(0, 3)
        home_wins = home_form > 1.6
        home_goals = 2 if home_wins else rng.integers(0, 2)
        away_goals = 0 if home_wins else rng.integers(0, 3)
        row = {
            "date": pd.Timestamp(dates[i]), "league": leagues[i % 2],
            "home_team": f"Home{i}", "away_team": f"Away{i}",
            "home_goals": int(home_goals), "away_goals": int(away_goals),
            "home_form_points_avg": home_form, "home_form_goals_scored_avg": rng.uniform(0, 3),
            "home_form_goals_conceded_avg": rng.uniform(0, 3),
            "away_form_points_avg": rng.uniform(0, 3), "away_form_goals_scored_avg": rng.uniform(0, 3),
            "away_form_goals_conceded_avg": rng.uniform(0, 3),
            "home_days_since_last_match": rng.integers(3, 10), "away_days_since_last_match": rng.integers(3, 10),
            "home_returning_from_break": 0, "away_returning_from_break": 0,
            "h2h_matches_found": rng.integers(0, 5), "h2h_home_win_rate": rng.uniform(0, 1),
            "dc_home_win": rng.uniform(0.2, 0.6), "dc_draw": rng.uniform(0.2, 0.3), "dc_away_win": rng.uniform(0.2, 0.5),
            "dc_over_2_5": rng.uniform(0.3, 0.6), "dc_under_2_5": rng.uniform(0.4, 0.7),
            "home_shots_diff_avg": rng.uniform(-3, 3), "away_shots_diff_avg": rng.uniform(-3, 3),
            "home_shots_target_diff_avg": rng.uniform(-2, 2), "away_shots_target_diff_avg": rng.uniform(-2, 2),
            "home_corners_diff_avg": rng.uniform(-2, 2), "away_corners_diff_avg": rng.uniform(-2, 2),
            "home_current_streak": rng.integers(-3, 3), "away_current_streak": rng.integers(-3, 3),
        }
        rows.append(row)
    return pd.DataFrame(rows)


def test_create_candidate_version_produces_candidate_never_active():
    _clean_all()
    with Session(engine) as session:
        df = _synthetic_df(160, start=date(2020, 1, 1))
        version = retraining.create_candidate_version(
            session, "xgboost", df, FEATURE_COLUMNS, CATEGORICAL_COLUMNS, n_test=30, n_val=20,
        )
        session.commit()

        assert version.status == "candidate"
        assert version.is_active is False
        assert version.feature_version == retraining.FEATURE_VERSION
        assert version.sample_size == 20  # taille de VALIDATION, jamais test
        assert version.artifact is not None and len(version.artifact) > 0


def test_candidate_metrics_json_has_both_validation_and_test():
    _clean_all()
    with Session(engine) as session:
        df = _synthetic_df(160, start=date(2020, 1, 1))
        version = retraining.create_candidate_version(
            session, "lightgbm", df, FEATURE_COLUMNS, CATEGORICAL_COLUMNS, n_test=30, n_val=20,
        )
        session.commit()

        parsed = json.loads(version.metrics)
        assert "validation" in parsed and "test" in parsed
        assert parsed["validation"]["sample_size"] == 20
        assert parsed["test"]["sample_size"] == 30
        for key in ("log_loss", "accuracy", "brier_score"):
            assert key in parsed["validation"] and key in parsed["test"]


def test_candidate_periods_are_chronologically_ordered():
    _clean_all()
    with Session(engine) as session:
        df = _synthetic_df(160, start=date(2020, 1, 1))
        version = retraining.create_candidate_version(
            session, "xgboost", df, FEATURE_COLUMNS, CATEGORICAL_COLUMNS, n_test=30, n_val=20,
        )
        session.commit()

        assert version.training_period_end < version.validation_period_start
        assert version.validation_period_end < version.test_period_start


def test_candidate_references_current_active_as_baseline():
    _clean_all()
    with Session(engine) as session:
        baseline = ModelVersion(
            name="xfoot-xgboost-v1", model_type="xgboost", trained_at=datetime.now(timezone.utc),
            is_active=True, status="active",
        )
        session.add(baseline)
        session.commit()
        session.refresh(baseline)

        df = _synthetic_df(160, start=date(2020, 1, 1))
        candidate = retraining.create_candidate_version(
            session, "xgboost", df, FEATURE_COLUMNS, CATEGORICAL_COLUMNS, n_test=30, n_val=20,
        )
        session.commit()

        assert candidate.baseline_version_id == baseline.id
        # la baseline elle-même reste totalement inchangée
        session.refresh(baseline)
        assert baseline.is_active is True
        assert baseline.status == "active"


def test_retraining_does_not_modify_old_predictions():
    """§43 : test_retraining_does_not_modify_old_predictions — créer un
    candidat ne doit jamais toucher aux ModelPrediction déjà résolues d'une
    version existante (baseline ou autre)."""
    _clean_all()
    with Session(engine) as session:
        old_version = ModelVersion(
            name="xfoot-xgboost-old", model_type="xgboost", trained_at=datetime.now(timezone.utc),
            is_active=True, status="active",
        )
        session.add(old_version)
        session.commit()
        session.refresh(old_version)

        record = PredictionRecord(
            league="Ligue1", match_date=date(2020, 1, 1), home_team="A", away_team="B",
            model_type="xgboost", prob_home=0.7, prob_draw=0.2, prob_away=0.1, source="live",
        )
        row = log_prediction(session, record, old_version.id)
        resolve_prediction(row, 2, 0)
        session.add(row)
        session.commit()
        snapshot = (row.prob_home, row.prob_draw, row.prob_away, row.status, row.correct_1x2)

        df = _synthetic_df(160, start=date(2020, 1, 1))
        retraining.create_candidate_version(
            session, "xgboost", df, FEATURE_COLUMNS, CATEGORICAL_COLUMNS, n_test=30, n_val=20,
        )
        session.commit()

        session.refresh(row)
        after = (row.prob_home, row.prob_draw, row.prob_away, row.status, row.correct_1x2)
        assert after == snapshot, "une prédiction historique a été modifiée par le réentraînement"


UNIT_TESTS = [
    test_ready_when_all_conditions_met,
    test_not_ready_when_not_enough_matches,
    test_not_ready_when_not_enough_leagues,
    test_not_ready_when_period_too_short,
    test_missing_match_stats_reported_but_not_necessarily_blocking_reason,
    test_leakage_detected_on_future_match_date,
    test_temporal_split_indices_and_boundaries,
    test_validation_after_training_period,
    test_test_after_validation_period,
    test_temporal_split_raises_when_not_enough_data,
    test_create_candidate_version_produces_candidate_never_active,
    test_candidate_metrics_json_has_both_validation_and_test,
    test_candidate_periods_are_chronologically_ordered,
    test_candidate_references_current_active_as_baseline,
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
