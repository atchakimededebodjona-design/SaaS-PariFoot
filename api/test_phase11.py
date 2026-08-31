"""
test_phase11.py — Phase 11 : tests de api/app/ai/shadow/internal_operation.py
(les SEULES fonctions nouvelles de cette phase : assert_mode_1_only,
evaluate_mode2_conditions, compare_to_phase10_baseline) et vérification
structurelle de scripts/internal_shadow_operation.py. Sert aussi de
re-démonstration EMPIRIQUE du rollback (§28, isolée, jamais api/app.db).

Toutes les briques DÉJÀ testées ailleurs (discover/capture/resolve/
monitoring/track record/évidence/readiness/preflight/kill switch/rollback/
longitudinal watch/human review) sont réutilisées TELLES QUELLES — ce
fichier vérifie que la couche d'opération interne contrôlée (mode
enforcement, MODE_2 evaluation documentaire, comparaison Phase 10) tient,
jamais une réécriture des garanties déjà prouvées en Phase 8K-10.

Base isolée dédiée (jamais api/app.db), Shadow Store / lock / Kill Switch
store / Evidence History store isolés dédiés (jamais les fichiers réels
sous reports/).

Usage : python api/test_phase11.py
"""

import inspect
import os
import sys
import tempfile
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_phase11.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match
from app.models.model_prediction import ModelPrediction
from app.models.prediction_log import PredictionLog
from app.models.team_rating import ModelVersion, next_version_name

from app.ai.pipeline.schemas import PipelineInput, CalibrationInput, FeatureSnapshotInput, TemporalMetadataInput

from app.ai.arena.promotion import apply_promotion, get_active_version
from app.ai.historical.eligibility import is_model_available_at

from app.ai.shadow.tracking import ShadowDecisionStore
from app.ai.shadow.schemas import ShadowDecisionRecord, ShadowResolution, pending_resolution
from app.ai.shadow.live import check_production_consistency
from app.ai.shadow.resolution import resolve_record
from app.ai.shadow.metrics import compute_shadow_track_record, classify_maturity, value_tracking_status
from app.ai.shadow.monitoring import compute_shadow_health, classify_prediction_timing
from app.ai.shadow.evidence import compute_model_version_tracking, compute_breakdown, compute_temporal_drift, build_activation_matrix
from app.ai.shadow.prospective import run_prospective_capture, acquire_capture_lock, is_real_prospective, backup_store, restore_and_validate
from app.ai.shadow.watch import EvidenceHistoryStore, filter_real_prospective_entries, compute_blocker_evolution, readiness_blockers

import app.ai.shadow.internal_operation as internal_operation
from app.ai.shadow.internal_operation import (
    OPERATING_MODE, assert_mode_1_only, evaluate_mode2_conditions, compare_to_phase10_baseline,
)
from app.ai.shadow.operations import run_preflight_safety

from app.ai.readiness.matrix import evaluate_production_readiness
from app.ai.readiness.human_review import human_review_gate

from app.ai.safety.kill_switch import KillSwitchStore, trigger, assert_production_allowed
from app.ai.safety.rollback import evaluate_rollback_readiness, execute_rollback

init_db()
UTC = timezone.utc
SCRIPT_SOURCE = (Path(__file__).parent.parent / "scripts" / "internal_shadow_operation.py").read_text(encoding="utf-8")


def _temp_store() -> ShadowDecisionStore:
    fd, name = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    tmp = Path(name)
    tmp.unlink()
    return ShadowDecisionStore(path=tmp)


def _temp_lock() -> Path:
    fd, name = tempfile.mkstemp(suffix=".lock")
    os.close(fd)
    p = Path(name)
    p.unlink()
    return p


def _temp_switch() -> KillSwitchStore:
    fd1, p1 = tempfile.mkstemp(suffix=".json"); os.close(fd1); Path(p1).unlink()
    fd2, p2 = tempfile.mkstemp(suffix=".json"); os.close(fd2); Path(p2).unlink()
    return KillSwitchStore(state_path=Path(p1), audit_path=Path(p2))


def _cleanup_switch(store: KillSwitchStore) -> None:
    Path(store.state_path).unlink(missing_ok=True)
    Path(store.audit_path).unlink(missing_ok=True)


def _temp_history() -> EvidenceHistoryStore:
    fd, name = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    tmp = Path(name)
    tmp.unlink()
    return EvidenceHistoryStore(path=tmp)


def _cleanup_history(store: EvidenceHistoryStore) -> None:
    Path(store.path).unlink(missing_ok=True)


def _seed_active(session, model_type, status="active", is_active=True, trained_at=None):
    v = ModelVersion(name=next_version_name(session, f"xfoot-{model_type}"), model_type=model_type,
                      trained_at=trained_at or (datetime.now(UTC) - timedelta(days=10)), is_active=is_active, status=status)
    session.add(v); session.commit(); session.refresh(v)
    return v


def _seed_pending_prediction(session, league="Ligue1", match_date=None, home="A", away="B",
                              model_type="xgboost", predicted_at=None, is_active=True):
    match_date = match_date or (date.today() + timedelta(days=2))
    version = ModelVersion(name=next_version_name(session, f"xfoot-{model_type}"), model_type=model_type,
                            trained_at=datetime.now(UTC) - timedelta(days=30), is_active=is_active, status="active" if is_active else "candidate")
    session.add(version); session.commit(); session.refresh(version)
    mp = ModelPrediction(league=league, match_date=match_date, home_team=home, away_team=away, model_type=model_type,
                          model_version_id=version.id, source="live", status="pending",
                          predicted_at=predicted_at or (datetime.now(UTC) - timedelta(hours=1)),
                          prob_home=0.5, prob_draw=0.3, prob_away=0.2, pick_1x2="home_win")
    session.add(mp); session.commit(); session.refresh(mp)
    return mp, version


def _clear_all(session):
    for model in (ModelPrediction, ModelVersion, PredictionLog, Match):
        for row in session.exec(select(model)).all():
            session.delete(row)
    session.commit()


def _make_record(**overrides) -> ShadowDecisionRecord:
    base = dict(
        shadow_id="x", match_id=None, league="Ligue1", home_team="A", away_team="B",
        kickoff=datetime.now(UTC) + timedelta(days=1), as_of=datetime.now(UTC), model_type="xgboost", model_version="v1",
        calibration_source=None, market="1X2", selection="home_win", raw_probability=0.5, calibrated_probability=None,
        market_probabilities_raw={"home_win": 0.5, "draw": 0.3, "away_win": 0.2}, market_probabilities_calibrated=None,
        probability_source="RAW", quality={}, confidence="UNKNOWN", eligibility="UNKNOWN", value_status=None,
        odds_source=None, odds_timestamp=None, temporal_status="UNKNOWN", provenance={}, status="UNKNOWN",
        created_at=datetime.now(UTC), data_marking="REAL",
    )
    base.update(overrides)
    return ShadowDecisionRecord(**base)


def _fake_pi(model="xgboost", model_version="xfoot-xgboost-v1", market="1X2", probabilities=None):
    return PipelineInput(
        match_id=1, league="Ligue1", kickoff=datetime.now(UTC), as_of=datetime.now(UTC), model=model,
        market=market, selection="home_win", probabilities=probabilities or {"home_win": 0.5, "draw": 0.3, "away_win": 0.2},
        calibration=CalibrationInput(), feature_snapshot=FeatureSnapshotInput(), temporal_metadata=TemporalMetadataInput(),
        model_version=model_version,
    )


_passed = _failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


def section(name):
    print(f"\n=== {name} ===")


# ---------------------------------------------------------------------------
# 1-4. mode enforcement.
# ---------------------------------------------------------------------------

def test_mode_1_enforced():
    section("1. mode 1 enforced")
    check("OPERATING_MODE constant is MODE_1_SHADOW_ONLY", OPERATING_MODE == "MODE_1_SHADOW_ONLY")
    assert_mode_1_only(OPERATING_MODE)  # ne doit jamais lever.
    check("assert_mode_1_only accepts MODE_1_SHADOW_ONLY", True)
    check("script never registers a --mode CLI argument (a documentary mention explaining why is not a violation)", 'add_argument("--mode"' not in SCRIPT_SOURCE)


def test_mode_2_rejected():
    section("2. mode 2 rejected")
    try:
        assert_mode_1_only("MODE_2_LIMITED_INTERNAL")
        check("MODE_2 rejected", False)
    except ValueError:
        check("MODE_2 rejected", True)


def test_mode_3_rejected():
    section("3. mode 3 rejected")
    try:
        assert_mode_1_only("MODE_3_LIMITED_PRODUCTION")
        check("MODE_3 rejected", False)
    except ValueError:
        check("MODE_3 rejected", True)


def test_mode_4_rejected():
    section("4. mode 4 rejected")
    try:
        assert_mode_1_only("MODE_4_FULL_PRODUCTION")
        check("MODE_4 rejected", False)
    except ValueError:
        check("MODE_4 rejected", True)


# ---------------------------------------------------------------------------
# 5. preflight.
# ---------------------------------------------------------------------------

def test_preflight():
    section("5. preflight (reused from Phase 9.4, PASS on healthy state)")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        ks = _temp_switch()
        try:
            result = run_preflight_safety(s, store, ks, datetime.now(UTC))
            check("status PASS", result["status"] == "PASS")
            check("mode is MODE_1_SHADOW_ONLY", result["checks"]["mode"]["value"] == "MODE_1_SHADOW_ONLY")
        finally:
            _cleanup_switch(ks)


# ---------------------------------------------------------------------------
# 6. kill switch.
# ---------------------------------------------------------------------------

def test_kill_switch():
    section("6. kill switch (ENABLED default, TRIGGERED on isolated store)")
    store = _temp_switch()
    try:
        check("default ENABLED", store.read().state == "ENABLED")
        trigger(store, "MANUAL_OPERATOR_TRIGGER", "test-only, isolated store", actor="tester", automatic=False)
        check("TRIGGERED after trigger()", store.read().state == "TRIGGERED")
        result = assert_production_allowed(store, "PRODUCTION_PREDICTION_ACTIVATION")
        check("production blocked while TRIGGERED", result.allowed is False and result.code == "KILL_SWITCH_ACTIVE")
    finally:
        _cleanup_switch(store)


# ---------------------------------------------------------------------------
# 7. readiness.
# ---------------------------------------------------------------------------

def test_readiness():
    section("7. readiness (evaluate_production_readiness reused)")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        readiness = evaluate_production_readiness(s, store, datetime.now(UTC))
        check("gates present", len(readiness.gates) > 0)


# ---------------------------------------------------------------------------
# 8. future discovery.
# ---------------------------------------------------------------------------

def test_future_discovery():
    section("8. future discovery")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        outcome = run_prospective_capture(s, store, datetime.now(UTC), dry_run=True, lock_path=lock)
        check("candidate discovered", outcome["candidates"] == 1)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 9. no-data.
# ---------------------------------------------------------------------------

def test_no_data():
    section("9. no-data (0 future fixtures -> NO_DATA)")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        lock = _temp_lock()
        outcome = run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        check("0 candidates", outcome["candidates"] == 0)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 10. pending-only.
# ---------------------------------------------------------------------------

def test_pending_only():
    section("10. pending-only (resolved production predictions never re-discovered)")
    with Session(engine) as s:
        _clear_all(s)
        mp, _ = _seed_pending_prediction(s)
        mp.status = "resolved"
        s.add(mp); s.commit()
        store = _temp_store()
        lock = _temp_lock()
        outcome = run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        check("0 candidates", outcome["candidates"] == 0)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 11. capture eligibility.
# ---------------------------------------------------------------------------

def test_capture_eligibility():
    section("11. capture eligibility (prediction_timestamp <= as_of < kickoff strictly enforced)")
    with Session(engine) as s:
        _clear_all(s)
        match_date = date.today() + timedelta(days=2)
        as_of = datetime.now(UTC)
        _seed_pending_prediction(s, match_date=match_date, predicted_at=as_of + timedelta(hours=1))
        store = _temp_store()
        lock = _temp_lock()
        outcome = run_prospective_capture(s, store, as_of, lock_path=lock)
        check("rejected (prediction after as_of)", outcome["candidates"] == 1 and outcome["captured"] == 0 and len(outcome["rejected"]) == 1)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 12. kickoff unknown.
# ---------------------------------------------------------------------------

def test_kickoff_unknown():
    section("12. kickoff unknown -> UNKNOWN, never a positive claim")
    status, _ = is_real_prospective(capture_timestamp=datetime.now(UTC), match_date=None, prediction_timestamp=datetime.now(UTC), as_of=datetime.now(UTC))
    check("UNKNOWN", status == "UNKNOWN")


# ---------------------------------------------------------------------------
# 13. late prediction.
# ---------------------------------------------------------------------------

def test_late_prediction():
    section("13. late prediction (kickoff time structurally unknown -> KICKOFF_TIME_UNKNOWN, never fabricated)")
    with Session(engine) as s:
        _clear_all(s)
        mp, _ = _seed_pending_prediction(s)
        timing = classify_prediction_timing(mp, datetime.now(UTC), kickoff_time_known=False)
        check("KICKOFF_TIME_UNKNOWN", timing["late_status"] == "KICKOFF_TIME_UNKNOWN")


# ---------------------------------------------------------------------------
# 14. future information.
# ---------------------------------------------------------------------------

def test_future_information():
    section("14. future information (as_of at/after kickoff placeholder -> TIMING_VIOLATION)")
    match_date = date.today()
    kickoff_placeholder = datetime.combine(match_date, datetime.min.time(), tzinfo=UTC)
    status, _ = is_real_prospective(
        capture_timestamp=kickoff_placeholder - timedelta(hours=1), match_date=match_date,
        prediction_timestamp=kickoff_placeholder - timedelta(hours=2), as_of=kickoff_placeholder + timedelta(hours=1),
    )
    check("TIMING_VIOLATION", status == "TIMING_VIOLATION")


# ---------------------------------------------------------------------------
# 15. model version gate.
# ---------------------------------------------------------------------------

def test_model_version_gate():
    section("15. model version gate (trained_at > as_of -> rejected, never used)")
    as_of = datetime.now(UTC)
    check("trained_at <= as_of -> AVAILABLE", is_model_available_at(as_of - timedelta(days=1), as_of) == "AVAILABLE")
    check("trained_at > as_of -> TRAINED_AFTER_AS_OF", is_model_available_at(as_of + timedelta(days=1), as_of) == "TRAINED_AFTER_AS_OF")
    check("trained_at missing -> UNKNOWN, never AVAILABLE", is_model_available_at(None, as_of) == "UNKNOWN")


# ---------------------------------------------------------------------------
# 16. production consistency.
# ---------------------------------------------------------------------------

def test_production_consistency():
    section("16. production consistency (VERIFY stage, reused, never auto-corrected)")
    with Session(engine) as s:
        _clear_all(s)
        mp, _ = _seed_pending_prediction(s, model_type="xgboost")
        pi = _fake_pi(model="xgboost", probabilities={"home_win": 0.5, "draw": 0.3, "away_win": 0.2})
        mismatches = check_production_consistency(mp, pi, None)
        check("no mismatch for a consistent snapshot", mismatches == [])


# ---------------------------------------------------------------------------
# 17. model mismatch.
# ---------------------------------------------------------------------------

def test_model_mismatch():
    section("17. model mismatch")
    with Session(engine) as s:
        _clear_all(s)
        mp, _ = _seed_pending_prediction(s, model_type="xgboost")
        pi = _fake_pi(model="lightgbm")
        mismatches = check_production_consistency(mp, pi, None)
        check("MODEL_TYPE_MISMATCH detected", "MODEL_TYPE_MISMATCH" in mismatches)


# ---------------------------------------------------------------------------
# 18. probability mismatch.
# ---------------------------------------------------------------------------

def test_probability_mismatch():
    section("18. probability mismatch")
    with Session(engine) as s:
        _clear_all(s)
        mp, _ = _seed_pending_prediction(s, model_type="xgboost")
        pi = _fake_pi(model="xgboost", probabilities={"home_win": 0.99, "draw": 0.005, "away_win": 0.005})
        mismatches = check_production_consistency(mp, pi, None)
        check("PROBABILITY_MISMATCH detected", "PROBABILITY_MISMATCH" in mismatches)


# ---------------------------------------------------------------------------
# 19. decision mismatch.
# ---------------------------------------------------------------------------

def test_decision_mismatch():
    section("19. decision mismatch")
    with Session(engine) as s:
        _clear_all(s)
        mp, _ = _seed_pending_prediction(s, model_type="xgboost")
        pi = _fake_pi(model="xgboost", probabilities={"home_win": 0.5, "draw": 0.3, "away_win": 0.2})
        fake_assessment = SimpleNamespace(decision=SimpleNamespace(), prediction={"probabilities": {"home_win": 0.1, "draw": 0.1, "away_win": 0.8}})
        mismatches = check_production_consistency(mp, pi, fake_assessment)
        check("DECISION_INPUT_MISMATCH detected", "DECISION_INPUT_MISMATCH" in mismatches)


# ---------------------------------------------------------------------------
# 20. provenance.
# ---------------------------------------------------------------------------

def test_provenance():
    section("20. provenance (documented, never fabricated)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        for r, _ in store.all():
            check(f"provenance present for {r.shadow_id}", isinstance(r.provenance, dict) and len(r.provenance) > 0)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 21. temporal classification.
# ---------------------------------------------------------------------------

def test_temporal_classification():
    section("21. temporal classification (real capture -> CONSISTENT_WITH_PLACEHOLDER_KICKOFF)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        outcome = run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        check("prospective_status consistent", outcome["captured_records"][0]["prospective_status"] == "CONSISTENT_WITH_PLACEHOLDER_KICKOFF")
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 22. real prospective inclusion.
# ---------------------------------------------------------------------------

def test_real_prospective_inclusion():
    section("22. real prospective inclusion")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC), dry_run=False, lock_path=lock)
        entries = store.all()
        filtered = filter_real_prospective_entries(s, entries)
        check("real capture included", len(filtered) == len(entries) == 1)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 23. historical exclusion.
# ---------------------------------------------------------------------------

def test_historical_exclusion():
    section("23. historical exclusion")
    with Session(engine) as s:
        _clear_all(s)
        past_kickoff = datetime.now(UTC) - timedelta(days=5)
        record = _make_record(shadow_id="hist-1", kickoff=past_kickoff, as_of=past_kickoff + timedelta(days=1), created_at=past_kickoff + timedelta(days=1))
        entries = [(record, pending_resolution())]
        filtered = filter_real_prospective_entries(s, entries)
        check("HISTORICAL excluded", len(filtered) == 0)


# ---------------------------------------------------------------------------
# 24. synthetic exclusion.
# ---------------------------------------------------------------------------

def test_synthetic_exclusion():
    section("24. synthetic exclusion")
    with Session(engine) as s:
        _clear_all(s)
        record = _make_record(shadow_id="synth-1", data_marking="SYNTHETIC")
        entries = [(record, pending_resolution())]
        filtered = filter_real_prospective_entries(s, entries)
        check("SYNTHETIC excluded", len(filtered) == 0)


# ---------------------------------------------------------------------------
# 25. duplicate prevention.
# ---------------------------------------------------------------------------

def test_duplicate_prevention():
    section("25. duplicate prevention")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        as_of = datetime.now(UTC)
        first = run_prospective_capture(s, store, as_of, lock_path=lock)
        second = run_prospective_capture(s, store, as_of, lock_path=lock)
        check("first captures 1", first["captured"] == 1)
        check("second captures 0, duplicate prevented", second["captured"] == 0 and second["duplicates_prevented"] == 1)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 26. multi-as_of.
# ---------------------------------------------------------------------------

def test_multi_as_of():
    section("26. multi-as_of (distinct as_of -> distinct observations, never merged)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s, predicted_at=datetime.now(UTC) - timedelta(days=1))
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC) - timedelta(hours=20), lock_path=lock)
        run_prospective_capture(s, store, datetime.now(UTC) - timedelta(hours=8), lock_path=lock)
        check("2 distinct observations", len(store.all()) == 2)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 27. immutable snapshot.
# ---------------------------------------------------------------------------

def test_immutable_snapshot():
    section("27. immutable snapshot (ShadowDecisionRecord frozen)")
    record = _make_record(shadow_id="frozen-1")
    try:
        record.model_type = "lightgbm"
        check("mutation raises FrozenInstanceError", False)
    except FrozenInstanceError:
        check("mutation raises FrozenInstanceError", True)


# ---------------------------------------------------------------------------
# 28. atomic store.
# ---------------------------------------------------------------------------

def test_atomic_store():
    section("28. atomic store (save produces valid, reloadable JSON)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        reloaded = ShadowDecisionStore(path=store.path)
        reloaded.load()
        check("reload succeeds with same count", len(reloaded.all()) == len(store.all()))
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 29. corruption detection.
# ---------------------------------------------------------------------------

def test_corruption_detection():
    section("29. corruption detection (never silently accepted)")
    fd, name = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    Path(name).write_text("{not valid json", encoding="utf-8")
    store = ShadowDecisionStore(path=Path(name))
    try:
        store.load()
        check("corrupted store raises ValueError", False)
    except ValueError:
        check("corrupted store raises ValueError", True)
    finally:
        Path(name).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 30. backup/recovery.
# ---------------------------------------------------------------------------

def test_backup_recovery():
    section("30. backup/recovery")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            backup_path = backup_store(store, tmpdir)
            restored = restore_and_validate(backup_path, tmpdir / "restored.json")
            check("restored count matches", len(restored.all()) == len(store.all()))
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 31. concurrency.
# ---------------------------------------------------------------------------

def test_concurrency():
    section("31. concurrency (exclusive lock still enforced)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        with acquire_capture_lock(lock) as held:
            check("first acquisition succeeds", held is True)
            outcome = run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
            check("concurrent run blocked", outcome["blocked"] is True and outcome["reason"] == "LOCK_HELD")
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 32. resolution.
# ---------------------------------------------------------------------------

def test_resolution():
    section("32. resolution (PENDING -> RESOLVED)")
    with Session(engine) as s:
        _clear_all(s)
        match_date = date.today() + timedelta(days=1)
        _seed_pending_prediction(s, match_date=match_date, predicted_at=datetime.now(UTC) - timedelta(hours=2))
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC) - timedelta(hours=1), dry_run=False, lock_path=lock)
        m = Match(league="Ligue1", date=datetime.combine(match_date, datetime.min.time()), home_team="A", away_team="B", home_goals=2, away_goals=0)
        s.add(m); s.commit()
        record, resolution = store.all()[0]
        new_resolution = resolve_record(s, record, resolution)
        check("RESOLVED", new_resolution.result_status == "RESOLVED")
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 33. conflict.
# ---------------------------------------------------------------------------

def test_conflict():
    section("33. conflict (disagreeing sources -> CONFLICT, never an arbitrary pick)")
    with Session(engine) as s:
        _clear_all(s)
        league, match_date, home, away = "Ligue1", date.today() - timedelta(days=1), "A", "B"
        version = ModelVersion(name=next_version_name(s, "xfoot-xgboost"), model_type="xgboost",
                                trained_at=datetime.now(UTC) - timedelta(days=30), is_active=True, status="active")
        s.add(version); s.commit(); s.refresh(version)
        mp_resolved = ModelPrediction(league=league, match_date=match_date, home_team=home, away_team=away, model_type="xgboost",
                                       model_version_id=version.id, source="live", status="resolved",
                                       predicted_at=datetime.now(UTC) - timedelta(days=2),
                                       prob_home=0.5, prob_draw=0.3, prob_away=0.2, pick_1x2="home_win",
                                       result_home_goals=2, result_away_goals=0)
        s.add(mp_resolved)
        m = Match(league=league, date=datetime.combine(match_date, datetime.min.time()), home_team=home, away_team=away, home_goals=0, away_goals=0)
        s.add(m); s.commit()
        record = _make_record(shadow_id="conflict-1", league=league, home_team=home, away_team=away,
                               kickoff=datetime.combine(match_date, datetime.min.time(), tzinfo=UTC), model_version=version.name)
        result = resolve_record(s, record, pending_resolution())
        check("CONFLICT", result.result_status == "CONFLICT")
        check("all values carried, no arbitrary pick", result.conflict_sources is not None and len(result.conflict_sources) >= 2)


# ---------------------------------------------------------------------------
# 34. resolved immutability.
# ---------------------------------------------------------------------------

def test_resolved_immutability():
    section("34. resolved immutability (store never overwrites a non-PENDING resolution)")
    with Session(engine) as s:
        _clear_all(s)
        match_date = date.today() + timedelta(days=1)
        _seed_pending_prediction(s, match_date=match_date, predicted_at=datetime.now(UTC) - timedelta(hours=2))
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC) - timedelta(hours=1), dry_run=False, lock_path=lock)
        m = Match(league="Ligue1", date=datetime.combine(match_date, datetime.min.time()), home_team="A", away_team="B", home_goals=1, away_goals=1)
        s.add(m); s.commit()
        record, resolution = store.all()[0]
        first_resolution = resolve_record(s, record, resolution)
        check("first applied", store.update_resolution(record.shadow_id, first_resolution) is True)
        bogus = ShadowResolution(result_status="RESOLVED", actual_home_goals=9, actual_away_goals=9, actual_outcome="away_win")
        check("second rejected", store.update_resolution(record.shadow_id, bogus) is False)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 35. track record.
# ---------------------------------------------------------------------------

def test_track_record():
    section("35. track record (REAL_PROSPECTIVE + RESOLVED only)")
    with Session(engine) as s:
        _clear_all(s)
        match_date = date.today() + timedelta(days=1)
        _seed_pending_prediction(s, match_date=match_date, predicted_at=datetime.now(UTC) - timedelta(hours=2))
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC) - timedelta(hours=1), dry_run=False, lock_path=lock)
        m = Match(league="Ligue1", date=datetime.combine(match_date, datetime.min.time()), home_team="A", away_team="B", home_goals=2, away_goals=0)
        s.add(m); s.commit()
        record, resolution = store.all()[0]
        new_resolution = resolve_record(s, record, resolution)
        store.update_resolution(record.shadow_id, new_resolution)
        filtered = filter_real_prospective_entries(s, store.all())
        tr = compute_shadow_track_record(filtered, market="1X2")
        check("sample_size == 1", tr.get("sample_size") == 1)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 36. maturity.
# ---------------------------------------------------------------------------

def test_maturity():
    section("36. maturity")
    check("0 -> NO_DATA", classify_maturity(0) == "NO_DATA")
    check("5 -> EARLY_DATA", classify_maturity(5) == "EARLY_DATA")
    check("15 -> TRACKING", classify_maturity(15) == "TRACKING")
    check("150 -> STATISTICALLY_INFORMATIVE", classify_maturity(150) == "STATISTICALLY_INFORMATIVE")


# ---------------------------------------------------------------------------
# 37. temporal drift.
# ---------------------------------------------------------------------------

def test_temporal_drift():
    section("37. temporal drift (small windows -> INSUFFICIENT_DATA, never average-of-averages)")
    drift = compute_temporal_drift([], market="1X2")
    check("INSUFFICIENT_DATA on empty entries", drift["status"] == "INSUFFICIENT_DATA")


# ---------------------------------------------------------------------------
# 38. model version breakdown.
# ---------------------------------------------------------------------------

def test_model_version_breakdown():
    section("38. model version breakdown (never merged silently)")
    r1 = _make_record(shadow_id="v1", model_type="xgboost", model_version="xfoot-xgboost-v1")
    r2 = _make_record(shadow_id="v2", model_type="xgboost", model_version="xfoot-xgboost-v2")
    entries = [(r1, pending_resolution()), (r2, pending_resolution())]
    tracking = compute_model_version_tracking(entries)
    check("multi-version detected", "xgboost" in tracking["multi_version_models_detected"])


# ---------------------------------------------------------------------------
# 39. league breakdown.
# ---------------------------------------------------------------------------

def test_league_breakdown():
    section("39. league breakdown (insufficient sample -> INSUFFICIENT_DATA)")
    r1 = _make_record(shadow_id="l1", league="Ligue1")
    entries = [(r1, ShadowResolution(result_status="RESOLVED", actual_outcome="home_win", candidate_correct=True))]
    breakdown = compute_breakdown(entries, market="1X2", min_sample_size=10)
    check("INSUFFICIENT_DATA", breakdown["by_league"]["Ligue1"]["status"] == "INSUFFICIENT_DATA")


# ---------------------------------------------------------------------------
# 40. market breakdown.
# ---------------------------------------------------------------------------

def test_market_breakdown():
    section("40. market breakdown (independent per market)")
    b1 = compute_breakdown([], market="1X2")
    b2 = compute_breakdown([], market="BTTS")
    check("1X2 well-formed", "global" in b1)
    check("BTTS well-formed", "global" in b2)


# ---------------------------------------------------------------------------
# 41. monitoring.
# ---------------------------------------------------------------------------

def test_monitoring():
    section("41. monitoring (compute_shadow_health reused)")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        health = compute_shadow_health(s, store, datetime.now(UTC))
        check("NO_DATA on empty DB", health["status"] == "NO_DATA")


# ---------------------------------------------------------------------------
# 42. evidence history.
# ---------------------------------------------------------------------------

def test_evidence_history():
    section("42. evidence history (append-only, MÊME mécanisme que Phase 9.5, jamais écrasé)")
    history = _temp_history()
    try:
        history.append({"future_fixtures": 1, "blockers": ["A"]})
        history.append({"future_fixtures": 2, "blockers": ["A", "B"]})
        recovered = EvidenceHistoryStore(path=history.path).read()
        check("2 snapshots persisted, in order", [h["future_fixtures"] for h in recovered] == [1, 2])
    finally:
        _cleanup_history(history)


# ---------------------------------------------------------------------------
# 43. blocker evolution.
# ---------------------------------------------------------------------------

def test_blocker_evolution():
    section("43. blocker evolution (NEW/PERSISTING/CLEARED/REGRESSED)")
    history = [["A", "B"], ["B", "C"]]
    current = ["B", "D", "A"]
    result = compute_blocker_evolution(history, current)
    check("D is NEW", "D" in result["new"])
    check("B is PERSISTING", "B" in result["persisting"])
    check("C is CLEARED", "C" in result["cleared"])
    check("A is REGRESSED", "A" in result["regressed"])


# ---------------------------------------------------------------------------
# 44. readiness reassessment.
# ---------------------------------------------------------------------------

def test_readiness_reassessment():
    section("44. readiness reassessment (before/after, never modified)")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        before = evaluate_production_readiness(s, store, datetime.now(UTC))
        after = evaluate_production_readiness(s, store, datetime.now(UTC))
        check("gate names identical (read-only)", {g.name for g in before.gates} == {g.name for g in after.gates})
        check("readiness_blockers well-formed", isinstance(readiness_blockers(after), list))


# ---------------------------------------------------------------------------
# 45. odds unavailable.
# ---------------------------------------------------------------------------

def test_odds_unavailable():
    section("45. odds unavailable")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        for r, _ in store.all():
            check(f"odds_source None for {r.shadow_id}", r.odds_source is None)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 46. value unavailable.
# ---------------------------------------------------------------------------

def test_value_unavailable():
    section("46. value unavailable")
    status = value_tracking_status([])
    check("NOT_AVAILABLE", status["status"] == "NOT_AVAILABLE")


# ---------------------------------------------------------------------------
# 47/48/49. rollback — isolated fixture only.
# ---------------------------------------------------------------------------

def test_rollback_isolated():
    section("47. rollback isolated (dedicated db, never api/app.db)")
    check("DB_PATH is dedicated test db", "test_phase11.db" in str(DB_PATH) and "app.db" not in str(DB_PATH))


def test_rollback_idempotence():
    section("48. rollback idempotence (A -> B -> A -> A, deterministic)")
    with Session(engine) as s:
        _clear_all(s)
        v1 = _seed_active(s, "dixon_coles")
        v2 = ModelVersion(name=next_version_name(s, "xfoot-dixon-coles"), model_type="dixon_coles",
                           trained_at=datetime.now(UTC), is_active=False, status="candidate")
        s.add(v2); s.commit(); s.refresh(v2)
        apply_promotion(s, v2); s.commit()
        check("v2 active after promotion", get_active_version(s, "dixon_coles").id == v2.id)

        readiness = evaluate_rollback_readiness(s, "dixon_coles", target_version_id=v1.id)
        check("rollback available", readiness.status == "ROLLBACK_AVAILABLE")

        r1 = execute_rollback(s, "dixon_coles", actor="tester", target_version_id=v1.id)
        r2 = execute_rollback(s, "dixon_coles", actor="tester", target_version_id=v1.id)
        r3 = execute_rollback(s, "dixon_coles", actor="tester", target_version_id=v1.id)
        check("first EXECUTED (B -> A)", r1.status == "EXECUTED")
        check("second NOOP (A -> A, idempotent)", r2.status == "NOOP_ALREADY_ACTIVE")
        check("third NOOP (A -> A, still idempotent)", r3.status == "NOOP_ALREADY_ACTIVE")
        check("active is v1", get_active_version(s, "dixon_coles").id == v1.id)


def test_rollback_audit():
    section("49. rollback audit (auditable, no history deletion)")
    audit_store = _temp_switch()
    try:
        with Session(engine) as s:
            _clear_all(s)
            v1 = _seed_active(s, "elo")
            v2 = ModelVersion(name=next_version_name(s, "xfoot-elo"), model_type="elo", trained_at=datetime.now(UTC), is_active=False, status="candidate")
            s.add(v2); s.commit(); s.refresh(v2)
            apply_promotion(s, v2); s.commit()
            execute_rollback(s, "elo", actor="tester", target_version_id=v1.id, audit_store=audit_store)
            all_versions = s.exec(select(ModelVersion).where(ModelVersion.model_type == "elo")).all()
            check("both versions still exist (no deletion)", {v.id for v in all_versions} == {v1.id, v2.id})
        log = audit_store.read_audit_log()
        check("audit trail records ROLLBACK_EXECUTED", any(e.get("event_type") == "ROLLBACK_EXECUTED" for e in log))
    finally:
        _cleanup_switch(audit_store)


# ---------------------------------------------------------------------------
# 50. DB purity.
# ---------------------------------------------------------------------------

def test_db_purity():
    section("50. DB purity (internal_operation.py never writes to the DB)")
    source = inspect.getsource(internal_operation)
    check("no session.add(", "session.add(" not in source)
    check("no session.commit(", "session.commit(" not in source)


# ---------------------------------------------------------------------------
# 51. no network.
# ---------------------------------------------------------------------------

def test_no_network():
    section("51. no network")
    source = inspect.getsource(internal_operation)
    check("no httpx/requests/urllib in internal_operation.py", all(x not in source for x in ("import httpx", "import requests", "import urllib")))
    check("no httpx/requests/urllib in script", all(x not in SCRIPT_SOURCE for x in ("import httpx", "import requests", "import urllib")))


# ---------------------------------------------------------------------------
# 52. no model call.
# ---------------------------------------------------------------------------

def test_no_model_call():
    section("52. no model call")
    source = inspect.getsource(internal_operation)
    check("no xgboost/lightgbm/dixon_coles import", all(x not in source for x in ("import xgboost", "import lightgbm", "dixon_coles")))
    check("script never calls .predict(", ".predict(" not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 53. no training.
# ---------------------------------------------------------------------------

def test_no_training():
    section("53. no training")
    check("internal_operation.py: no train_/fit", "train_" not in inspect.getsource(internal_operation) and ".fit(" not in inspect.getsource(internal_operation))
    check("script: no train_/fit", "train_" not in SCRIPT_SOURCE and ".fit(" not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 54. no promotion.
# ---------------------------------------------------------------------------

def test_no_promotion():
    section("54. no promotion")
    check("internal_operation.py: no apply_promotion( call", "apply_promotion(" not in inspect.getsource(internal_operation))
    check("script: no apply_promotion(/execute_rollback( call", "apply_promotion(" not in SCRIPT_SOURCE and "execute_rollback(" not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 55. no scheduler modification.
# ---------------------------------------------------------------------------

def test_no_scheduler():
    section("55. no scheduler modification")
    check("internal_operation.py: scheduler never imported", "arena.scheduler" not in inspect.getsource(internal_operation))
    check("script: scheduler never imported", "arena.scheduler" not in SCRIPT_SOURCE and "import scheduler" not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 56. no frontend modification.
# ---------------------------------------------------------------------------

def test_no_frontend():
    section("56. no frontend modification")
    check("internal_operation.py: no frontend import", "import frontend" not in inspect.getsource(internal_operation))
    check("script: no frontend import", "import frontend" not in SCRIPT_SOURCE and "from frontend" not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 57. no automatic activation.
# ---------------------------------------------------------------------------

def test_no_automatic_activation():
    section("57. no automatic activation")
    with Session(engine) as s:
        _clear_all(s)
        mp, version = _seed_pending_prediction(s)
        version_id = version.id
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        s.expire_all()
        refreshed = s.get(ModelVersion, version_id)
        check("ModelVersion.is_active unchanged", refreshed.is_active is True)
        lock.unlink(missing_ok=True)
    check("script never claims MODE_2/3/4 as operating mode", '"mode": "MODE_2' not in SCRIPT_SOURCE and '"mode": "MODE_3' not in SCRIPT_SOURCE and '"mode": "MODE_4' not in SCRIPT_SOURCE)
    check("internal_operation.py never triggers/resets the Kill Switch", ".trigger(" not in inspect.getsource(internal_operation) and ".reset(" not in inspect.getsource(internal_operation))


# ---------------------------------------------------------------------------
# 58. deterministic output.
# ---------------------------------------------------------------------------

def test_deterministic_output():
    section("58. deterministic output (pure functions)")
    check("build_activation_matrix deterministic", build_activation_matrix() == build_activation_matrix())
    kwargs = dict(current_readiness_verdict="NO_GO", baseline_readiness_verdict="NO_GO", current_real_prospective_count=0,
                  baseline_real_prospective_count=0, current_track_record_sample_size=0, baseline_track_record_sample_size=0,
                  current_provenance_complete=0, baseline_provenance_complete=0, current_gate_statuses={"A": "PASS"}, baseline_gate_statuses={"A": "PASS"})
    c1 = compare_to_phase10_baseline(**kwargs)
    c2 = compare_to_phase10_baseline(**kwargs)
    check("compare_to_phase10_baseline deterministic", c1 == c2)


# ---------------------------------------------------------------------------
# 59. report generation.
# ---------------------------------------------------------------------------

def test_report_generation():
    section("59. report generation (building blocks well-formed, incl. MODE_2 evaluation and Phase 10 comparison)")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        readiness = evaluate_production_readiness(s, store, datetime.now(UTC))
        mode2 = evaluate_mode2_conditions(readiness)
        check("mode2 evaluation is DOCUMENTARY_ONLY", mode2["documentary_only"] is True and mode2["activated"] is False)
        check("mode2 evaluation well-formed", all(k in mode2 for k in ("conditions_met", "required_gates", "unmet_gates")))
        comparison = compare_to_phase10_baseline(
            current_readiness_verdict="NO_GO", baseline_readiness_verdict="NO_GO", current_real_prospective_count=0,
            baseline_real_prospective_count=0, current_track_record_sample_size=0, baseline_track_record_sample_size=0,
            current_provenance_complete=0, baseline_provenance_complete=0, current_gate_statuses={}, baseline_gate_statuses={},
        )
        check("comparison well-formed", all(k in comparison for k in ("readiness_verdict", "real_prospective_evidence", "critical_gates")))
        check("human_review_gate result well-formed", human_review_gate(maturity="NO_DATA", blockers=[], readiness_verdict="NO_GO") in ("READY_FOR_HUMAN_REVIEW", "NOT_READY_FOR_HUMAN_REVIEW"))


# ---------------------------------------------------------------------------
# 60. error isolation.
# ---------------------------------------------------------------------------

def test_error_isolation():
    section("60. error isolation (one bad candidate never crashes the whole run)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        outcome = run_prospective_capture(s, store, datetime.now(UTC), market="UNKNOWN_MARKET_X", lock_path=lock)
        check("unknown market handled gracefully, never raises", outcome.get("blocked") is True and "UNKNOWN_MARKET" in outcome.get("reason", ""))
        outcome_ok = run_prospective_capture(s, store, datetime.now(UTC), market="1X2", lock_path=lock)
        check("run recovers normally afterwards", outcome_ok.get("blocked") is False)
        check("errors field always a list (structural)", isinstance(outcome_ok.get("errors"), list))
        lock.unlink(missing_ok=True)


def run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{_passed} passed, {_failed} failed (sur {_passed + _failed} assertions, {len(tests)} scénarios)")
    cleanup_db(DB_PATH)
    if _failed:
        sys.exit(1)


if __name__ == "__main__":
    run_all()
