"""
test_monitoring.py — Phase 9, Partie C/D : LIVE performance monitoring
(app/ai/arena/monitoring.py) — fenêtres, séparation LIVE/BACKTEST et
active/shadow, insuffisance de données, détection de dégradation (Model
Health).

Base isolée dédiée (jamais api/app.db) — même précaution que les suites de
tests précédentes (voir _test_support.py).

Usage : python api/test_monitoring.py
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_monitoring.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion
from app.ai.arena.prediction_logging import PredictionRecord, log_prediction, resolve_prediction
from app.ai.arena.models_common import AvailabilityCheck, PredictionModel
from app.ai.arena import monitoring

init_db()

BASE_DATE = date(2026, 1, 1)


def _clean_all():
    with Session(engine) as session:
        for row in session.exec(select(ModelPrediction)).all():
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


def _log_resolved(session, model_type, version_id, match_date, p_true, *, home_team=None, away_team=None,
                   league="Ligue1", source="live", role="active"):
    """Loggue puis résout immédiatement une prédiction 1X2 dont l'issue
    réelle est TOUJOURS 'home' avec probabilité p_true — log_loss exact par
    construction (-log(p_true)), comme test_ensemble_engine.py::_log_resolved."""
    home_team = home_team or f"Home-{match_date.isoformat()}-{model_type}-{role}"
    away_team = away_team or f"Away-{match_date.isoformat()}-{model_type}-{role}"
    other = (1.0 - p_true) / 2
    record = PredictionRecord(
        league=league, match_date=match_date, home_team=home_team, away_team=away_team,
        model_type=model_type, prob_home=p_true, prob_draw=other, prob_away=other,
        source=source, role=role,
    )
    row = log_prediction(session, record, version_id)
    resolve_prediction(row, 2, 0)
    session.add(row)
    session.commit()
    return row


def _log_pending(session, model_type, version_id, match_date, *, league="Ligue1", source="live", role="active",
                  home_team=None, away_team=None):
    home_team = home_team or f"PendingHome-{match_date.isoformat()}"
    away_team = away_team or f"PendingAway-{match_date.isoformat()}"
    record = PredictionRecord(
        league=league, match_date=match_date, home_team=home_team, away_team=away_team,
        model_type=model_type, prob_home=0.5, prob_draw=0.3, prob_away=0.2, source=source, role=role,
    )
    return log_prediction(session, record, version_id)


# ---------------------------------------------------------------------------
# get_live_monitoring — fenêtres
# ---------------------------------------------------------------------------

def test_all_time_window_aggregates_everything():
    _clean_all()
    with Session(engine) as session:
        v = _make_version(session, "xgboost")
        for i in range(5):
            _log_resolved(session, "xgboost", v.id, BASE_DATE + timedelta(days=i), p_true=0.6)

        result = monitoring.get_live_monitoring(session, "xgboost", "1X2", min_sample_size=1)
        assert result["ALL_TIME"].status == "OK", result["ALL_TIME"]
        assert result["ALL_TIME"].sample_size == 5


def test_last_100_window_limits_by_count_most_recent_matches():
    _clean_all()
    with Session(engine) as session:
        v = _make_version(session, "xgboost")
        for i in range(120):
            _log_resolved(session, "xgboost", v.id, BASE_DATE + timedelta(days=i), p_true=0.6)

        result = monitoring.get_live_monitoring(session, "xgboost", "1X2", min_sample_size=1)
        assert result["LAST_100"].sample_size == 100, result["LAST_100"].sample_size
        assert result["ALL_TIME"].sample_size == 120


def test_last_30_days_window_filters_by_date():
    _clean_all()
    with Session(engine) as session:
        v = _make_version(session, "xgboost")
        # 5 matchs vieux de 60 jours (hors fenêtre), 5 matchs récents (dans la fenêtre)
        old_date = date.today() - timedelta(days=60)
        recent_date = date.today() - timedelta(days=5)
        for i in range(5):
            _log_resolved(session, "xgboost", v.id, old_date + timedelta(days=i), p_true=0.6)
        for i in range(5):
            _log_resolved(session, "xgboost", v.id, recent_date + timedelta(days=i), p_true=0.6)

        result = monitoring.get_live_monitoring(session, "xgboost", "1X2", min_sample_size=1)
        assert result["LAST_30_DAYS"].sample_size == 5, result["LAST_30_DAYS"].sample_size
        assert result["ALL_TIME"].sample_size == 10


def test_window_insufficient_data_when_below_min_sample_size():
    _clean_all()
    with Session(engine) as session:
        v = _make_version(session, "xgboost")
        for i in range(3):
            _log_resolved(session, "xgboost", v.id, BASE_DATE + timedelta(days=i), p_true=0.6)

        result = monitoring.get_live_monitoring(session, "xgboost", "1X2", min_sample_size=100)
        assert result["ALL_TIME"].status == "INSUFFICIENT_DATA"
        assert result["ALL_TIME"].metrics is None
        assert result["ALL_TIME"].sample_size == 3


def test_live_and_backtest_are_separated():
    _clean_all()
    with Session(engine) as session:
        v = _make_version(session, "xgboost")
        for i in range(10):
            _log_resolved(session, "xgboost", v.id, BASE_DATE + timedelta(days=i), p_true=0.6, source="live")
        for i in range(10):
            _log_resolved(session, "xgboost", v.id, BASE_DATE + timedelta(days=100 + i), p_true=0.9, source="backtest")

        live = monitoring.get_live_monitoring(session, "xgboost", "1X2", min_sample_size=1, source="live")
        backtest = monitoring.get_live_monitoring(session, "xgboost", "1X2", min_sample_size=1, source="backtest")
        assert live["ALL_TIME"].sample_size == 10
        assert backtest["ALL_TIME"].sample_size == 10
        # log_loss très différent (p_true 0.6 vs 0.9) -> preuve qu'aucune ligne
        # de l'autre source n'a fuité dans l'agrégat.
        assert live["ALL_TIME"].metrics.log_loss != backtest["ALL_TIME"].metrics.log_loss


def test_active_and_shadow_roles_are_separated():
    _clean_all()
    with Session(engine) as session:
        v_active = _make_version(session, "lightgbm", is_active=True)
        v_shadow = _make_version(session, "lightgbm", is_active=False)
        for i in range(10):
            _log_resolved(session, "lightgbm", v_active.id, BASE_DATE + timedelta(days=i), p_true=0.7, role="active")
        for i in range(10):
            _log_resolved(session, "lightgbm", v_shadow.id, BASE_DATE + timedelta(days=200 + i), p_true=0.2, role="shadow")

        active_only = monitoring.get_live_monitoring(session, "lightgbm", "1X2", min_sample_size=1, role="active")
        shadow_only = monitoring.get_live_monitoring(session, "lightgbm", "1X2", min_sample_size=1, role="shadow")
        assert active_only["ALL_TIME"].sample_size == 10
        assert shadow_only["ALL_TIME"].sample_size == 10
        assert active_only["ALL_TIME"].metrics.log_loss != shadow_only["ALL_TIME"].metrics.log_loss


# ---------------------------------------------------------------------------
# get_live_summary
# ---------------------------------------------------------------------------

def test_live_summary_counts_total_resolved_pending():
    _clean_all()
    with Session(engine) as session:
        v = _make_version(session, "elo")
        for i in range(3):
            _log_resolved(session, "elo", v.id, BASE_DATE + timedelta(days=i), p_true=0.5)
        for i in range(2):
            _log_pending(session, "elo", v.id, BASE_DATE + timedelta(days=100 + i))

        summary = monitoring.get_live_summary(session, "elo")
        assert summary.predictions_total == 5
        assert summary.predictions_resolved == 3
        assert summary.predictions_pending == 2
        assert summary.last_prediction_at is not None
        assert summary.last_resolved_at is not None


def test_live_summary_empty_model_has_no_last_timestamps():
    _clean_all()
    with Session(engine) as session:
        summary = monitoring.get_live_summary(session, "dixon_coles")
        assert summary.predictions_total == 0
        assert summary.last_prediction_at is None
        assert summary.last_resolved_at is None


# ---------------------------------------------------------------------------
# compute_model_health
# ---------------------------------------------------------------------------

def test_health_healthy_when_no_degradation():
    _clean_all()
    with Session(engine) as session:
        v = _make_version(session, "xgboost")
        old_date = date.today() - timedelta(days=200)
        recent_date = date.today() - timedelta(days=5)
        for i in range(60):
            _log_resolved(session, "xgboost", v.id, old_date + timedelta(days=i), p_true=0.7)
        for i in range(40):
            _log_resolved(session, "xgboost", v.id, recent_date + timedelta(days=i), p_true=0.7)

        health = monitoring.compute_model_health(session, "xgboost", "1X2", min_monitoring_sample=30)
        assert health.status == "HEALTHY", health
        assert health.delta is not None and abs(health.delta) < 1e-6


def test_health_warning_on_moderate_degradation():
    _clean_all()
    with Session(engine) as session:
        v = _make_version(session, "xgboost")
        old_date = date.today() - timedelta(days=200)
        recent_date = date.today() - timedelta(days=5)
        for i in range(60):
            _log_resolved(session, "xgboost", v.id, old_date + timedelta(days=i), p_true=0.75)
        for i in range(40):
            _log_resolved(session, "xgboost", v.id, recent_date + timedelta(days=i), p_true=0.60)

        health = monitoring.compute_model_health(
            session, "xgboost", "1X2", min_monitoring_sample=30, warning_delta=0.05, critical_delta=0.5,
        )
        assert health.status == "WARNING", health


def test_health_degraded_on_large_degradation():
    _clean_all()
    with Session(engine) as session:
        v = _make_version(session, "xgboost")
        old_date = date.today() - timedelta(days=200)
        recent_date = date.today() - timedelta(days=5)
        for i in range(60):
            _log_resolved(session, "xgboost", v.id, old_date + timedelta(days=i), p_true=0.9)
        for i in range(40):
            _log_resolved(session, "xgboost", v.id, recent_date + timedelta(days=i), p_true=0.15)

        health = monitoring.compute_model_health(
            session, "xgboost", "1X2", min_monitoring_sample=30, warning_delta=0.05, critical_delta=0.5,
        )
        assert health.status == "DEGRADED", health


def test_health_insufficient_data_below_min_monitoring_sample():
    _clean_all()
    with Session(engine) as session:
        v = _make_version(session, "xgboost")
        for i in range(5):
            _log_resolved(session, "xgboost", v.id, BASE_DATE + timedelta(days=i), p_true=0.6)

        health = monitoring.compute_model_health(session, "xgboost", "1X2", min_monitoring_sample=30)
        assert health.status == "INSUFFICIENT_DATA", health
        assert health.delta is None


def test_health_never_flags_degraded_on_a_handful_of_matches():
    """§17 : quelques matchs ne doivent jamais suffire à alerter sérieusement,
    même avec un delta de log_loss énorme sur ce petit échantillon."""
    _clean_all()
    with Session(engine) as session:
        v = _make_version(session, "xgboost")
        old_date = date.today() - timedelta(days=200)
        recent_date = date.today() - timedelta(days=1)
        for i in range(3):
            _log_resolved(session, "xgboost", v.id, old_date + timedelta(days=i), p_true=0.95)
        for i in range(2):
            _log_resolved(session, "xgboost", v.id, recent_date + timedelta(days=i), p_true=0.05)

        health = monitoring.compute_model_health(session, "xgboost", "1X2", min_monitoring_sample=30)
        assert health.status == "INSUFFICIENT_DATA", health


class _FakeUnavailableModel(PredictionModel):
    model_type = "xgboost"

    def predict(self, session, ctx):
        raise NotImplementedError

    def check_availability(self, session):
        return AvailabilityCheck(live_available=False, reason="Artefact injoignable (test).")


def test_health_unavailable_when_model_reports_not_live_available():
    _clean_all()
    with Session(engine) as session:
        v = _make_version(session, "xgboost")
        for i in range(60):
            _log_resolved(session, "xgboost", v.id, BASE_DATE + timedelta(days=i), p_true=0.7)

        health = monitoring.compute_model_health(
            session, "xgboost", "1X2", min_monitoring_sample=30, models=[_FakeUnavailableModel()],
        )
        assert health.status == "UNAVAILABLE", health
        assert "injoignable" in health.reason


# ---------------------------------------------------------------------------
# compute_ensemble_delta
# ---------------------------------------------------------------------------

def test_ensemble_delta_insufficient_data_when_no_ensemble_predictions():
    _clean_all()
    with Session(engine) as session:
        v = _make_version(session, "xgboost")
        for i in range(60):
            _log_resolved(session, "xgboost", v.id, BASE_DATE + timedelta(days=i), p_true=0.7)

        delta = monitoring.compute_ensemble_delta(session, "1X2")
        assert delta.benchmark_status == "INSUFFICIENT_DATA"
        assert delta.delta_log_loss is None


def test_ensemble_delta_negative_means_ensemble_better():
    """Ensemble p_true=0.9 (log_loss faible) vs meilleur individuel p_true=0.6
    (log_loss plus élevé) -> delta_log_loss < 0 (Ensemble meilleur)."""
    _clean_all()
    with Session(engine) as session:
        ens_v = _make_version(session, "ensemble")
        xgb_v = _make_version(session, "xgboost")
        elo_v = _make_version(session, "elo")
        for i in range(150):
            _log_resolved(session, "ensemble", ens_v.id, BASE_DATE + timedelta(days=i), p_true=0.9)
            _log_resolved(session, "xgboost", xgb_v.id, BASE_DATE + timedelta(days=i), p_true=0.6)
            _log_resolved(session, "elo", elo_v.id, BASE_DATE + timedelta(days=i), p_true=0.5)

        delta = monitoring.compute_ensemble_delta(session, "1X2")
        assert delta.benchmark_status == "STATISTICALLY_RELEVANT", delta
        assert delta.best_individual_model == "xgboost"  # meilleur des deux individuels (log_loss le plus bas)
        assert delta.delta_log_loss < 0, "Ensemble meilleur -> delta doit être négatif"
        assert delta.delta_accuracy is not None


def test_ensemble_delta_positive_means_ensemble_worse():
    _clean_all()
    with Session(engine) as session:
        ens_v = _make_version(session, "ensemble")
        xgb_v = _make_version(session, "xgboost")
        for i in range(150):
            _log_resolved(session, "ensemble", ens_v.id, BASE_DATE + timedelta(days=i), p_true=0.5)
            _log_resolved(session, "xgboost", xgb_v.id, BASE_DATE + timedelta(days=i), p_true=0.9)

        delta = monitoring.compute_ensemble_delta(session, "1X2")
        assert delta.delta_log_loss > 0, "Ensemble moins bon -> delta doit être positif"


def test_ensemble_delta_preliminary_below_relevant_threshold():
    _clean_all()
    with Session(engine) as session:
        ens_v = _make_version(session, "ensemble")
        xgb_v = _make_version(session, "xgboost")
        for i in range(25):  # >= preliminary(20), < relevant(100 par défaut)
            _log_resolved(session, "ensemble", ens_v.id, BASE_DATE + timedelta(days=i), p_true=0.8)
            _log_resolved(session, "xgboost", xgb_v.id, BASE_DATE + timedelta(days=i), p_true=0.6)

        delta = monitoring.compute_ensemble_delta(session, "1X2")
        assert delta.benchmark_status == "PRELIMINARY", delta


def test_ensemble_delta_never_concludes_on_a_handful_of_matches():
    _clean_all()
    with Session(engine) as session:
        ens_v = _make_version(session, "ensemble")
        xgb_v = _make_version(session, "xgboost")
        for i in range(5):
            _log_resolved(session, "ensemble", ens_v.id, BASE_DATE + timedelta(days=i), p_true=0.95)
            _log_resolved(session, "xgboost", xgb_v.id, BASE_DATE + timedelta(days=i), p_true=0.05)

        delta = monitoring.compute_ensemble_delta(session, "1X2")
        assert delta.benchmark_status == "INSUFFICIENT_DATA", delta


UNIT_TESTS = [
    test_all_time_window_aggregates_everything,
    test_last_100_window_limits_by_count_most_recent_matches,
    test_last_30_days_window_filters_by_date,
    test_window_insufficient_data_when_below_min_sample_size,
    test_live_and_backtest_are_separated,
    test_active_and_shadow_roles_are_separated,
    test_live_summary_counts_total_resolved_pending,
    test_live_summary_empty_model_has_no_last_timestamps,
    test_health_healthy_when_no_degradation,
    test_health_warning_on_moderate_degradation,
    test_health_degraded_on_large_degradation,
    test_health_insufficient_data_below_min_monitoring_sample,
    test_health_never_flags_degraded_on_a_handful_of_matches,
    test_health_unavailable_when_model_reports_not_live_available,
    test_ensemble_delta_insufficient_data_when_no_ensemble_predictions,
    test_ensemble_delta_negative_means_ensemble_better,
    test_ensemble_delta_positive_means_ensemble_worse,
    test_ensemble_delta_preliminary_below_relevant_threshold,
    test_ensemble_delta_never_concludes_on_a_handful_of_matches,
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
