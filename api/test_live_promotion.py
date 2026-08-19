"""
test_live_promotion.py — Phase 10 : app/ai/arena/promotion.py::
evaluate_live_promotion — décision de promotion pilotée par les performances
LIVE réelles (candidat vs version active), DISTINCTE de evaluate_promotion
(offline/validation, inchangée, voir test_promotion.py).

Couvre les 5 issues réellement produites par cette fonction
(already_active/insufficient_data/rejected/no_clear_gain/eligible — "bootstrap"
sans baseline est une variante de "eligible"/"insufficient_data", testée à
part), ainsi que la garantie anti-mélange de versions (le vrai risque de
fuite ici, voir docstring live_validation.py).

Base isolée dédiée (jamais api/app.db). Usage : python api/test_live_promotion.py
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_live_promotion.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion
from app.ai.arena.prediction_logging import PredictionRecord, log_prediction, resolve_prediction
from app.ai.arena import promotion

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
    v = ModelVersion(name=f"test-{model_type}-{datetime.now(timezone.utc).timestamp()}",
                      model_type=model_type, trained_at=datetime.now(timezone.utc),
                      is_active=is_active, status=status)
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


def _log_n_resolved(session, model_type, version_id, n, p_true, *, role="active", start_offset=0):
    other = (1.0 - p_true) / 2
    for i in range(n):
        d = BASE_DATE + timedelta(days=start_offset + i)
        record = PredictionRecord(
            league="Ligue1", match_date=d, home_team=f"H-{model_type}-{version_id}-{d}",
            away_team=f"A-{model_type}-{version_id}-{d}", model_type=model_type,
            prob_home=p_true, prob_draw=other, prob_away=other, source="live", role=role,
        )
        row = log_prediction(session, record, version_id)
        resolve_prediction(row, 2, 0)  # home win : p_true doit être la proba HOME pour être "correcte"
        session.add(row)
    session.commit()


def test_already_active_when_candidate_is_the_active_version():
    _clean_all()
    with Session(engine) as session:
        v = _make_version(session, "xgboost", is_active=True, status="active")
        d = promotion.evaluate_live_promotion(session, v.id, "1X2")
        assert d.status == "already_active"


def test_insufficient_data_when_candidate_sample_too_small():
    _clean_all()
    with Session(engine) as session:
        active = _make_version(session, "xgboost", is_active=True, status="active")
        shadow = _make_version(session, "xgboost", is_active=False, status="shadow")
        _log_n_resolved(session, "xgboost", active.id, 150, p_true=0.9, role="active")
        _log_n_resolved(session, "xgboost", shadow.id, 10, p_true=0.95, role="shadow")  # < LIVE_MIN_SAMPLE_SIZE

        d = promotion.evaluate_live_promotion(session, shadow.id, "1X2")
        assert d.status == "insufficient_data"


def test_rejected_when_candidate_clearly_worse():
    _clean_all()
    with Session(engine) as session:
        active = _make_version(session, "xgboost", is_active=True, status="active")
        shadow = _make_version(session, "xgboost", is_active=False, status="shadow")
        _log_n_resolved(session, "xgboost", active.id, 120, p_true=0.9, role="active")
        _log_n_resolved(session, "xgboost", shadow.id, 120, p_true=0.4, role="shadow")

        d = promotion.evaluate_live_promotion(session, shadow.id, "1X2")
        assert d.status == "rejected"


def test_no_clear_gain_when_improvement_below_margin():
    """Le candidat est LÉGÈREMENT meilleur mais sous PROMOTION_MIN_IMPROVEMENT
    — ne doit jamais être promu sur un écart trop faible (§8 du ticket)."""
    _clean_all()
    with Session(engine) as session:
        active = _make_version(session, "xgboost", is_active=True, status="active")
        shadow = _make_version(session, "xgboost", is_active=False, status="shadow")
        _log_n_resolved(session, "xgboost", active.id, 120, p_true=0.60, role="active")
        _log_n_resolved(session, "xgboost", shadow.id, 120, p_true=0.605, role="shadow")

        d = promotion.evaluate_live_promotion(session, shadow.id, "1X2")
        assert d.status == "no_clear_gain", f"attendu no_clear_gain, obtenu {d.status} ({d.reason})"


def test_eligible_when_candidate_clearly_better_beyond_margin():
    _clean_all()
    with Session(engine) as session:
        active = _make_version(session, "xgboost", is_active=True, status="active")
        shadow = _make_version(session, "xgboost", is_active=False, status="shadow")
        _log_n_resolved(session, "xgboost", active.id, 120, p_true=0.55, role="active")
        _log_n_resolved(session, "xgboost", shadow.id, 120, p_true=0.9, role="shadow")

        d = promotion.evaluate_live_promotion(session, shadow.id, "1X2")
        assert d.status == "eligible", f"attendu eligible, obtenu {d.status} ({d.reason})"


def test_bootstrap_eligible_when_no_baseline_and_enough_sample():
    _clean_all()
    with Session(engine) as session:
        shadow = _make_version(session, "lightgbm", is_active=False, status="candidate")
        _log_n_resolved(session, "lightgbm", shadow.id, 120, p_true=0.8, role="shadow")

        d = promotion.evaluate_live_promotion(session, shadow.id, "1X2")
        assert d.status == "eligible"
        assert d.baseline_version_id is None


def test_two_successive_shadow_versions_never_mixed_in_decision():
    """Le vrai risque de fuite pour ce module : une v1 shadow déjà évaluée
    (rejetée) puis retirée, remplacée par une v2 shadow — la décision sur v2
    ne doit JAMAIS être influencée par les métriques de v1."""
    _clean_all()
    with Session(engine) as session:
        active = _make_version(session, "xgboost", is_active=True, status="active")
        v1 = _make_version(session, "xgboost", is_active=False, status="shadow")
        v2 = _make_version(session, "xgboost", is_active=False, status="shadow")

        _log_n_resolved(session, "xgboost", active.id, 120, p_true=0.6, role="active")
        _log_n_resolved(session, "xgboost", v1.id, 120, p_true=0.2, role="shadow")   # v1 : très mauvais
        _log_n_resolved(session, "xgboost", v2.id, 120, p_true=0.95, role="shadow")  # v2 : très bon

        d1 = promotion.evaluate_live_promotion(session, v1.id, "1X2")
        d2 = promotion.evaluate_live_promotion(session, v2.id, "1X2")

        assert d1.status == "rejected"
        assert d2.status == "eligible"
        assert d1.candidate_metrics["sample_size"] == 120
        assert d2.candidate_metrics["sample_size"] == 120
        assert d1.candidate_metrics["log_loss"] != d2.candidate_metrics["log_loss"]


def test_auto_promotion_disabled_by_default():
    assert promotion.AUTO_PROMOTION_ENABLED is False, "AUTO_PROMOTION_ENABLED doit être false par défaut (règle 10 du ticket)"


UNIT_TESTS = [
    test_already_active_when_candidate_is_the_active_version,
    test_insufficient_data_when_candidate_sample_too_small,
    test_rejected_when_candidate_clearly_worse,
    test_no_clear_gain_when_improvement_below_margin,
    test_eligible_when_candidate_clearly_better_beyond_margin,
    test_bootstrap_eligible_when_no_baseline_and_enough_sample,
    test_two_successive_shadow_versions_never_mixed_in_decision,
    test_auto_promotion_disabled_by_default,
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
