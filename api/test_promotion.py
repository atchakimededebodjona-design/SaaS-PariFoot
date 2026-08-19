"""
test_promotion.py — Phase 9, Partie F/G : décision de promotion
(app/ai/arena/promotion.py) — bootstrap, comparaison à une baseline, rejet
sur échantillon insuffisant, rejet sur performance insuffisante,
apply_promotion (bascule is_active/status/activated_at/deactivated_at).

Base isolée dédiée (jamais api/app.db) — même précaution que les suites de
tests précédentes (voir _test_support.py).

Usage : python api/test_promotion.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_promotion.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.team_rating import ModelVersion
from app.ai.arena import promotion

init_db()


def _clean_all():
    with Session(engine) as session:
        for v in session.exec(select(ModelVersion)).all():
            session.delete(v)
        session.commit()


def _make_version(session, model_type: str, *, is_active=False, status="active",
                   sample_size=None, validation_log_loss=None, test_log_loss=None) -> ModelVersion:
    metrics = None
    if validation_log_loss is not None or test_log_loss is not None:
        metrics = json.dumps({
            "validation": {"log_loss": validation_log_loss} if validation_log_loss is not None else {},
            "test": {"log_loss": test_log_loss} if test_log_loss is not None else {},
        })
    v = ModelVersion(
        name=f"test-{model_type}-{datetime.now(timezone.utc).timestamp()}",
        model_type=model_type, trained_at=datetime.now(timezone.utc),
        is_active=is_active, status=status, sample_size=sample_size, metrics=metrics,
    )
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


def test_bootstrap_promotes_when_no_baseline_and_enough_sample():
    _clean_all()
    with Session(engine) as session:
        candidate = _make_version(session, "xgboost", status="candidate", sample_size=150, validation_log_loss=1.05)
        decision = promotion.evaluate_promotion(candidate, baseline=None)
        assert decision.promote is True, decision.reason
        assert decision.baseline_version_id is None


def test_bootstrap_rejects_when_sample_too_small():
    _clean_all()
    with Session(engine) as session:
        candidate = _make_version(session, "xgboost", status="candidate", sample_size=10, validation_log_loss=1.05)
        decision = promotion.evaluate_promotion(candidate, baseline=None)
        assert decision.promote is False
        assert "insuffisant" in decision.reason.lower()


def test_promotes_when_candidate_within_tolerance_of_baseline():
    _clean_all()
    with Session(engine) as session:
        baseline = _make_version(session, "xgboost", is_active=True, sample_size=300, validation_log_loss=1.031)
        candidate = _make_version(session, "xgboost", status="candidate", sample_size=150, validation_log_loss=1.032)
        decision = promotion.evaluate_promotion(candidate, baseline)
        assert decision.promote is True, decision.reason


def test_rejects_when_candidate_worse_than_tolerance():
    _clean_all()
    with Session(engine) as session:
        baseline = _make_version(session, "xgboost", is_active=True, sample_size=300, validation_log_loss=1.03)
        candidate = _make_version(session, "xgboost", status="candidate", sample_size=150, validation_log_loss=1.20)
        decision = promotion.evaluate_promotion(candidate, baseline)
        assert decision.promote is False, decision.reason


def test_a_successful_training_run_with_worse_performance_is_still_rejected():
    """§23 : un entraînement 'réussi' (candidat créé, métriques valides) mais
    moins bon que la tolérance n'est jamais promu malgré son 'succès'."""
    _clean_all()
    with Session(engine) as session:
        baseline = _make_version(session, "lightgbm", is_active=True, sample_size=300, validation_log_loss=1.0307)
        candidate = _make_version(session, "lightgbm", status="candidate", sample_size=200, validation_log_loss=1.15)
        decision = promotion.evaluate_promotion(candidate, baseline)
        assert decision.promote is False
        assert decision.candidate_log_loss == 1.15
        assert decision.baseline_log_loss == 1.0307


def test_rejects_when_metrics_missing_never_guesses():
    _clean_all()
    with Session(engine) as session:
        baseline = _make_version(session, "xgboost", is_active=True, sample_size=300, validation_log_loss=1.03)
        candidate = _make_version(session, "xgboost", status="candidate", sample_size=150)  # pas de metrics
        decision = promotion.evaluate_promotion(candidate, baseline)
        assert decision.promote is False
        assert "manquantes" in decision.reason.lower()


def test_promotion_never_reads_test_metrics_only_validation():
    """§22-23 : le candidat a un test log_loss EXCELLENT mais un validation
    log_loss MAUVAIS — la décision doit suivre validation, jamais test."""
    _clean_all()
    with Session(engine) as session:
        baseline = _make_version(session, "xgboost", is_active=True, sample_size=300, validation_log_loss=1.03, test_log_loss=1.03)
        candidate = _make_version(
            session, "xgboost", status="candidate", sample_size=150,
            validation_log_loss=1.50,  # mauvais -> doit rejeter
            test_log_loss=0.50,        # excellent -> ne doit jamais influencer la décision
        )
        decision = promotion.evaluate_promotion(candidate, baseline)
        assert decision.promote is False, "la décision a dû lire le test set au lieu de validation"
        assert decision.candidate_log_loss == 1.50


def test_apply_promotion_activates_candidate_and_retires_old_active():
    _clean_all()
    with Session(engine) as session:
        old_active = _make_version(session, "xgboost", is_active=True, status="active", sample_size=300, validation_log_loss=1.03)
        candidate = _make_version(session, "xgboost", is_active=False, status="candidate", sample_size=150, validation_log_loss=1.03)

        promotion.apply_promotion(session, candidate)
        session.commit()
        session.refresh(old_active)
        session.refresh(candidate)

        assert candidate.status == "active"
        assert candidate.is_active is True
        assert candidate.activated_at is not None

        assert old_active.status == "retired"
        assert old_active.is_active is False
        assert old_active.deactivated_at is not None


def test_apply_promotion_does_not_affect_other_model_types():
    _clean_all()
    with Session(engine) as session:
        other_type_active = _make_version(session, "lightgbm", is_active=True, status="active")
        candidate = _make_version(session, "xgboost", is_active=False, status="candidate", sample_size=150, validation_log_loss=1.03)

        promotion.apply_promotion(session, candidate)
        session.commit()
        session.refresh(other_type_active)

        assert other_type_active.is_active is True
        assert other_type_active.status == "active"


UNIT_TESTS = [
    test_bootstrap_promotes_when_no_baseline_and_enough_sample,
    test_bootstrap_rejects_when_sample_too_small,
    test_promotes_when_candidate_within_tolerance_of_baseline,
    test_rejects_when_candidate_worse_than_tolerance,
    test_a_successful_training_run_with_worse_performance_is_still_rejected,
    test_rejects_when_metrics_missing_never_guesses,
    test_promotion_never_reads_test_metrics_only_validation,
    test_apply_promotion_activates_candidate_and_retires_old_active,
    test_apply_promotion_does_not_affect_other_model_types,
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
