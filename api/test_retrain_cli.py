"""
test_retrain_cli.py — Phase 9, Partie G : run_retrain()
(app/ai/arena/retraining.py, cœur partagé par scripts/retrain_ml_models.py)
— dry-run n'écrit rien, données insuffisantes, création de candidat,
--force promeut ou rejette selon la règle, gestion d'erreur d'entraînement,
et un test bout-en-bout du script CLI lui-même (subprocess, --dry-run
uniquement — jamais un --force réel dans cette suite, voir docstring du
script).

Base isolée dédiée (jamais api/app.db) — même précaution que les suites de
tests précédentes (voir _test_support.py).

Usage : python api/test_retrain_cli.py
"""

import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_retrain_cli.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion
from app.ai.engine.features import FEATURE_COLUMNS
from app.ai.arena.retraining import run_retrain

init_db()

REPO_ROOT = Path(__file__).parent.parent


def _clean_all():
    with Session(engine) as session:
        for row in session.exec(select(ModelPrediction)).all():
            session.delete(row)
        for row in session.exec(select(Match)).all():
            session.delete(row)
        for v in session.exec(select(ModelVersion)).all():
            session.delete(v)
        session.commit()


def _insert_matches(session, n: int, *, start: date, leagues=("Ligue1", "PremierLeague")):
    for i in range(n):
        session.add(Match(league=leagues[i % len(leagues)], date=datetime.combine(start + timedelta(days=i), datetime.min.time()),
                           home_team=f"H{i}", away_team=f"A{i}", home_goals=1, away_goals=0))
    session.commit()


def _make_version(session, model_type: str, *, is_active=True, sample_size=None, validation_log_loss=None) -> ModelVersion:
    metrics = json.dumps({"validation": {"log_loss": validation_log_loss}}) if validation_log_loss is not None else None
    v = ModelVersion(
        name=f"test-{model_type}-{datetime.now(timezone.utc).timestamp()}",
        model_type=model_type, trained_at=datetime.now(timezone.utc),
        is_active=is_active, status="active", sample_size=sample_size, metrics=metrics,
    )
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


def _synthetic_df(n: int, *, start: date, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        home_form = rng.uniform(0, 3)
        home_wins = home_form > 1.6
        row = {
            "date": pd.Timestamp(start + timedelta(days=i)), "league": ["Ligue1", "PremierLeague"][i % 2],
            "home_team": f"H{i}", "away_team": f"A{i}",
            "home_goals": 2 if home_wins else int(rng.integers(0, 2)),
            "away_goals": 0 if home_wins else int(rng.integers(0, 3)),
            "home_form_points_avg": home_form,
        }
        for c in FEATURE_COLUMNS:
            if c not in row:
                row[c] = rng.uniform(0, 1)
        rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# run_retrain — dry_run
# ---------------------------------------------------------------------------

def test_dry_run_writes_nothing_to_the_database():
    _clean_all()
    with Session(engine) as session:
        _insert_matches(session, 600, start=date(2020, 1, 1))
        versions_before = session.exec(select(ModelVersion)).all()
        assert len(versions_before) == 0

        result = run_retrain(session, "xgboost", dry_run=True)
        session.commit()

        assert result.status == "dry_run"
        versions_after = session.exec(select(ModelVersion)).all()
        assert len(versions_after) == 0, "dry-run a écrit une ModelVersion en base"


def test_dry_run_reports_data_not_ready_without_writing():
    _clean_all()
    with Session(engine) as session:
        _insert_matches(session, 10, start=date(2020, 1, 1))
        result = run_retrain(session, "xgboost", dry_run=True)
        assert result.status == "data_not_ready"
        assert result.readiness is not None and result.readiness.ready is False
        assert len(session.exec(select(ModelVersion)).all()) == 0


def test_dry_run_message_mentions_split_and_promotion_rule():
    _clean_all()
    with Session(engine) as session:
        _insert_matches(session, 600, start=date(2020, 1, 1))
        result = run_retrain(session, "lightgbm", dry_run=True)
        assert "validation" in result.message.lower()
        assert "log_loss" in result.message.lower()


# ---------------------------------------------------------------------------
# run_retrain — real run (df_builder injecté, jamais build_ml_features_from_db)
# ---------------------------------------------------------------------------

def test_real_run_creates_candidate_without_promoting_by_default():
    _clean_all()
    with Session(engine) as session:
        _insert_matches(session, 600, start=date(2020, 1, 1))
        df = _synthetic_df(450, start=date(2020, 1, 1))  # > N_TEST(300)+N_VAL(100), défauts de run_retrain

        result = run_retrain(session, "xgboost", dry_run=False, force=False, df_builder=lambda: df)

        assert result.status == "candidate_created", result.message
        assert result.candidate_version_id is not None
        candidate = session.get(ModelVersion, result.candidate_version_id)
        assert candidate.status == "candidate"
        assert candidate.is_active is False


def test_real_run_data_not_ready_still_writes_nothing():
    _clean_all()
    with Session(engine) as session:
        _insert_matches(session, 5, start=date(2020, 1, 1))
        result = run_retrain(session, "xgboost", dry_run=False, force=False)
        assert result.status == "data_not_ready"
        assert len(session.exec(select(ModelVersion)).all()) == 0


def test_force_promotes_when_no_baseline_and_enough_validation_sample():
    _clean_all()
    with Session(engine) as session:
        _insert_matches(session, 600, start=date(2020, 1, 1))
        df = _synthetic_df(450, start=date(2020, 1, 1))  # n_val par défaut = 100 >= PROMOTION_MIN_VALIDATION_SAMPLE

        result = run_retrain(session, "xgboost", dry_run=False, force=True, df_builder=lambda: df)

        assert result.status == "promoted", result.message
        candidate = session.get(ModelVersion, result.candidate_version_id)
        assert candidate.is_active is True
        assert candidate.status == "active"


def test_force_rejects_and_leaves_baseline_active_when_candidate_worse():
    _clean_all()
    with Session(engine) as session:
        baseline = _make_version(session, "xgboost", is_active=True, sample_size=300, validation_log_loss=0.0001)
        _insert_matches(session, 600, start=date(2020, 1, 1))
        df = _synthetic_df(450, start=date(2020, 1, 1))

        result = run_retrain(session, "xgboost", dry_run=False, force=True, df_builder=lambda: df)

        # Un vrai modèle entraîné sur du bruit ne battra jamais un log_loss
        # quasi nul fabriqué pour ce test -> rejet garanti.
        assert result.status == "rejected", result.message
        session.refresh(baseline)
        assert baseline.is_active is True
        candidate = session.get(ModelVersion, result.candidate_version_id)
        assert candidate.is_active is False
        assert candidate.status == "candidate"  # jamais supprimé, historique conservé


def test_real_run_error_status_on_training_exception():
    _clean_all()
    with Session(engine) as session:
        _insert_matches(session, 600, start=date(2020, 1, 1))

        def _broken_builder():
            raise RuntimeError("panne simulée du pipeline de features")

        result = run_retrain(session, "xgboost", dry_run=False, force=False, df_builder=_broken_builder)
        assert result.status == "error"
        assert "panne simulée" in result.message


# ---------------------------------------------------------------------------
# CLI bout-en-bout (subprocess, --dry-run uniquement)
# ---------------------------------------------------------------------------

def test_cli_dry_run_exit_code_zero_and_mentions_dry_run():
    _clean_all()
    with Session(engine) as session:
        _insert_matches(session, 600, start=date(2020, 1, 1))

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "retrain_ml_models.py"), "--model", "xgboost", "--dry-run"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
        env={**__import__("os").environ, "DATABASE_URL": f"sqlite:///{DB_PATH.as_posix()}"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "dry-run" in (proc.stdout + proc.stderr).lower()


def test_cli_exit_code_one_on_insufficient_data():
    _clean_all()
    with Session(engine) as session:
        _insert_matches(session, 5, start=date(2020, 1, 1))

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "retrain_ml_models.py"), "--model", "xgboost", "--dry-run"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
        env={**__import__("os").environ, "DATABASE_URL": f"sqlite:///{DB_PATH.as_posix()}"},
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr


UNIT_TESTS = [
    test_dry_run_writes_nothing_to_the_database,
    test_dry_run_reports_data_not_ready_without_writing,
    test_dry_run_message_mentions_split_and_promotion_rule,
    test_real_run_creates_candidate_without_promoting_by_default,
    test_real_run_data_not_ready_still_writes_nothing,
    test_force_promotes_when_no_baseline_and_enough_validation_sample,
    test_force_rejects_and_leaves_baseline_active_when_candidate_worse,
    test_real_run_error_status_on_training_exception,
    test_cli_dry_run_exit_code_zero_and_mentions_dry_run,
    test_cli_exit_code_one_on_insufficient_data,
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
