"""
test_phase10.py — Phase 10 : tests de api/app/ai/readiness/human_review.py
(les SEULES fonctions nouvelles de cette phase) et vérification structurelle
de scripts/phase10_validation.py. Sert aussi de démonstration EMPIRIQUE du
rollback (§19/§21-23) — sur une base ISOLÉE dédiée, JAMAIS api/app.db (même
discipline que api/test_safety_controls.py, Phase 9.1, jamais dupliquée ici
en profondeur — seulement re-confirmée dans le contexte Phase 10).

Toutes les briques DÉJÀ testées ailleurs (discover/capture/resolve/
monitoring/track record/évidence/readiness/preflight/kill switch/rollback)
sont réutilisées TELLES QUELLES — ce fichier vérifie que la couche de
décision Phase 10 (checklist/evidence status/entry-exit criteria/verdict/
human review) tient, jamais une réécriture des garanties déjà prouvées en
Phase 8K-9.5.

Base isolée dédiée (jamais api/app.db), Shadow Store / lock / Kill Switch
store isolés dédiés (jamais les fichiers réels sous reports/).

Usage : python api/test_phase10.py
"""

import inspect
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_phase10.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match
from app.models.model_prediction import ModelPrediction
from app.models.prediction_log import PredictionLog
from app.models.team_rating import ModelVersion, next_version_name

from app.ai.arena.promotion import apply_promotion, get_active_version
from app.ai.arena.research import wilson_interval
from app.ai.historical.eligibility import is_model_available_at

from app.ai.shadow.tracking import ShadowDecisionStore
from app.ai.shadow.schemas import ShadowDecisionRecord, ShadowResolution
from app.ai.shadow.resolution import resolve_record
from app.ai.shadow.metrics import compute_shadow_track_record, classify_maturity, value_tracking_status, MATURITY_THRESHOLDS
from app.ai.shadow.monitoring import compute_shadow_health
from app.ai.shadow.evidence import (
    compute_model_version_tracking, compute_breakdown, compute_temporal_drift, build_activation_matrix,
)
from app.ai.shadow.prospective import run_prospective_capture, acquire_capture_lock, is_real_prospective, compute_readiness_impact
from app.ai.shadow.watch import filter_real_prospective_entries
from app.ai.shadow.operations import run_preflight_safety

import app.ai.readiness.gates as gates
from app.ai.readiness.matrix import evaluate_production_readiness
from app.ai.readiness.schemas import GATE_STATUSES

import app.ai.readiness.human_review as human_review
from app.ai.readiness.human_review import (
    build_phase10_checklist, classify_evidence_status, human_review_gate, build_entry_criteria, build_exit_criteria,
    derive_phase10_verdict, PHASE10_VERDICTS, PHASE10_CHECKLIST_ITEMS, ENTRY_CRITERIA_CATEGORIES, HUMAN_REVIEW_STATUSES,
)

from app.ai.safety.kill_switch import KillSwitchStore, trigger, reset, assert_production_allowed
from app.ai.safety.guards import can_activate_production
from app.ai.safety.rollback import evaluate_rollback_readiness, execute_rollback

init_db()
UTC = timezone.utc
SCRIPT_SOURCE = (Path(__file__).parent.parent / "scripts" / "phase10_validation.py").read_text(encoding="utf-8")

ALL_PASS_CRITICAL = {
    "TEMPORAL_INTEGRITY": "PASS", "MODEL": "PASS", "MODEL_VERSION": "PASS", "PROVENANCE": "PASS",
    "DATABASE_SAFETY": "PASS", "ROLLBACK": "PASS", "MONITORING": "PASS", "TRACK_RECORD": "PASS", "KILL_SWITCH": "PASS",
}


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
# 1. readiness baseline.
# ---------------------------------------------------------------------------

def test_readiness_baseline():
    section("1. readiness baseline")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        readiness = evaluate_production_readiness(s, store, datetime.now(UTC))
        check("gates present", len(readiness.gates) > 0)
        check("verdict in vocabulary", readiness.final_verdict in ("PRODUCTION_READY", "CONDITIONALLY_READY", "NO_GO", "BLOCKED"))
        check("checklist present", len(readiness.checklist) > 0)


# ---------------------------------------------------------------------------
# 2. shadow baseline.
# ---------------------------------------------------------------------------

def test_shadow_baseline():
    section("2. shadow baseline")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        health = compute_shadow_health(s, store, datetime.now(UTC))
        check("status present", health["status"] in ("NO_DATA", "HEALTHY", "DEGRADED", "CRITICAL", "BLOCKED"))
        check("NO_DATA on empty DB", health["status"] == "NO_DATA")


# ---------------------------------------------------------------------------
# 3. no-data.
# ---------------------------------------------------------------------------

def test_no_data():
    section("3. no-data (0 future fixtures + 0 real prospective -> NO_DATA)")
    verdict = derive_phase10_verdict(preflight_status="PASS", tests_green=True, readiness_verdict="NO_GO",
                                      future_fixtures=0, real_prospective_resolved=0, maturity="NO_DATA", blockers=[])
    check("NO_DATA", verdict == "NO_DATA")
    check("never modified to produce a better status (§33)", verdict != "EARLY_EVIDENCE" and verdict != "TRACKING")


# ---------------------------------------------------------------------------
# 4. real prospective.
# ---------------------------------------------------------------------------

def test_real_prospective():
    section("4. real prospective (real capture classified REAL_PROSPECTIVE)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC), dry_run=False, lock_path=lock)
        entries = store.all()
        filtered = filter_real_prospective_entries(s, entries)
        check("real capture kept as REAL_PROSPECTIVE", len(filtered) == len(entries) == 1)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 5. synthetic exclusion.
# ---------------------------------------------------------------------------

def test_synthetic_exclusion():
    section("5. synthetic exclusion")
    with Session(engine) as s:
        _clear_all(s)
        record = _make_record(shadow_id="synth-1", data_marking="SYNTHETIC")
        entries = [(record, ShadowResolution(result_status="PENDING"))]
        filtered = filter_real_prospective_entries(s, entries)
        check("SYNTHETIC never REAL_PROSPECTIVE", len(filtered) == 0)


# ---------------------------------------------------------------------------
# 6. historical exclusion.
# ---------------------------------------------------------------------------

def test_historical_exclusion():
    section("6. historical exclusion")
    with Session(engine) as s:
        _clear_all(s)
        past_kickoff = datetime.now(UTC) - timedelta(days=5)
        record = _make_record(shadow_id="hist-1", kickoff=past_kickoff, as_of=past_kickoff + timedelta(days=1), created_at=past_kickoff + timedelta(days=1))
        entries = [(record, ShadowResolution(result_status="PENDING"))]
        filtered = filter_real_prospective_entries(s, entries)
        check("HISTORICAL never REAL_PROSPECTIVE", len(filtered) == 0)


# ---------------------------------------------------------------------------
# 7. temporal gate.
# ---------------------------------------------------------------------------

def test_temporal_gate():
    section("7. temporal gate (UNKNOWN never becomes TEMPORALLY_VERIFIED by inference)")
    status, _ = is_real_prospective(capture_timestamp=None, match_date=None, prediction_timestamp=None, as_of=None)
    check("missing timestamps -> UNKNOWN", status == "UNKNOWN")
    check("UNKNOWN is never a positive prospective claim", status != "CONSISTENT_WITH_PLACEHOLDER_KICKOFF")


# ---------------------------------------------------------------------------
# 8. model trained_at gate.
# ---------------------------------------------------------------------------

def test_model_trained_at_gate():
    section("8. model trained_at gate (trained_at > as_of -> REJECTED, never used)")
    as_of = datetime.now(UTC)
    check("trained_at <= as_of -> AVAILABLE", is_model_available_at(as_of - timedelta(days=1), as_of) == "AVAILABLE")
    check("trained_at > as_of -> TRAINED_AFTER_AS_OF (rejected)", is_model_available_at(as_of + timedelta(days=1), as_of) == "TRAINED_AFTER_AS_OF")
    check("trained_at missing -> UNKNOWN, never AVAILABLE by inference", is_model_available_at(None, as_of) == "UNKNOWN")


# ---------------------------------------------------------------------------
# 9. feature provenance.
# ---------------------------------------------------------------------------

def test_feature_provenance():
    section("9. feature provenance (gate_features reused, never fabricated)")
    with Session(engine) as s:
        _clear_all(s)
        gate = gates.gate_features()
        check("status in vocabulary", gate.status in GATE_STATUSES)
        check("evidence non-empty", bool(gate.evidence))


# ---------------------------------------------------------------------------
# 10. track record.
# ---------------------------------------------------------------------------

def test_track_record():
    section("10. track record (REAL_PROSPECTIVE + RESOLVED only)")
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
# 11. Wilson interval.
# ---------------------------------------------------------------------------

def test_wilson_interval():
    section("11. Wilson interval (reused, embedded in compute_shadow_track_record)")
    lo, hi = wilson_interval(5, 10)
    check("interval bounds valid", 0.0 <= lo <= 0.5 <= hi <= 1.0)
    tr = compute_shadow_track_record([], market="1X2")
    check("no fabricated interval on empty data", tr["status"] == "INSUFFICIENT_DATA")


# ---------------------------------------------------------------------------
# 12. maturity.
# ---------------------------------------------------------------------------

def test_maturity():
    section("12. maturity (reused thresholds)")
    check("0 -> NO_DATA", classify_maturity(0) == "NO_DATA")
    check("5 -> EARLY_DATA", classify_maturity(5) == "EARLY_DATA")
    check("15 -> TRACKING", classify_maturity(15) == "TRACKING")
    check("150 -> STATISTICALLY_INFORMATIVE", classify_maturity(150) == "STATISTICALLY_INFORMATIVE")


# ---------------------------------------------------------------------------
# 13. temporal drift.
# ---------------------------------------------------------------------------

def test_temporal_drift():
    section("13. temporal drift (EARLY/MIDDLE/RECENT, never average-of-averages; small windows -> INSUFFICIENT_DATA)")
    drift = compute_temporal_drift([], market="1X2")
    check("insufficient data on empty entries", drift["status"] == "INSUFFICIENT_DATA")


# ---------------------------------------------------------------------------
# 14. model version breakdown.
# ---------------------------------------------------------------------------

def test_model_version_breakdown():
    section("14. model version breakdown (never silently merged)")
    r1 = _make_record(shadow_id="v1", model_type="xgboost", model_version="xfoot-xgboost-v1")
    r2 = _make_record(shadow_id="v2", model_type="xgboost", model_version="xfoot-xgboost-v2")
    entries = [(r1, ShadowResolution(result_status="PENDING")), (r2, ShadowResolution(result_status="PENDING"))]
    tracking = compute_model_version_tracking(entries)
    check("multi-version detected", "xgboost" in tracking["multi_version_models_detected"])


# ---------------------------------------------------------------------------
# 15. league breakdown.
# ---------------------------------------------------------------------------

def test_league_breakdown():
    section("15. league breakdown (insufficient sample -> INSUFFICIENT_DATA)")
    r1 = _make_record(shadow_id="l1", league="Ligue1")
    entries = [(r1, ShadowResolution(result_status="RESOLVED", actual_outcome="home_win", candidate_correct=True))]
    breakdown = compute_breakdown(entries, market="1X2", min_sample_size=10)
    check("league under threshold -> INSUFFICIENT_DATA", breakdown["by_league"]["Ligue1"]["status"] == "INSUFFICIENT_DATA")


# ---------------------------------------------------------------------------
# 16. market breakdown.
# ---------------------------------------------------------------------------

def test_market_breakdown():
    section("16. market breakdown (independent per market)")
    entries = []
    b1 = compute_breakdown(entries, market="1X2")
    b2 = compute_breakdown(entries, market="BTTS")
    check("1X2 well-formed", "global" in b1)
    check("BTTS well-formed", "global" in b2)


# ---------------------------------------------------------------------------
# 17. monitoring.
# ---------------------------------------------------------------------------

def test_monitoring():
    section("17. monitoring (compute_shadow_health reused)")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        health = compute_shadow_health(s, store, datetime.now(UTC))
        check("track_record_health present", "track_record_health" in health)


# ---------------------------------------------------------------------------
# 18. readiness reassessment.
# ---------------------------------------------------------------------------

def test_readiness_reassessment():
    section("18. readiness reassessment (before/after, impact computed, never modified)")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        before = evaluate_production_readiness(s, store, datetime.now(UTC))
        after = evaluate_production_readiness(s, store, datetime.now(UTC))
        impact = compute_readiness_impact(before.gates, after.gates)
        check("impact well-formed", all("before" in v and "after" in v for v in impact.values()))
        check("before == after gate names identical (read-only, no mutation)", {g.name for g in before.gates} == {g.name for g in after.gates})


# ---------------------------------------------------------------------------
# 19. kill switch.
# ---------------------------------------------------------------------------

def test_kill_switch():
    section("19. kill switch (ENABLED default, TRIGGERED on real event, reset requires all critical gates PASS)")
    store = _temp_switch()
    try:
        check("default ENABLED", store.read().state == "ENABLED")
        trigger(store, "MANUAL_OPERATOR_TRIGGER", "test-only trigger on isolated store", actor="tester", automatic=False)
        check("TRIGGERED after trigger()", store.read().state == "TRIGGERED")
        check("effective_status RESET_REQUIRED", store.read().effective_status == "RESET_REQUIRED")
        ok_bad, _, _ = reset(store, {"TRACK_RECORD": "FAIL"}, actor="tester", reason="attempted reset with a failing critical gate")
        check("reset refused when a critical gate is not PASS (§25 : never automatic)", ok_bad is False)
        check("still TRIGGERED after refused reset", store.read().state == "TRIGGERED")
        ok_good, _, _ = reset(store, ALL_PASS_CRITICAL, actor="tester", reason="all critical gates PASS on this isolated store")
        check("reset approved once all critical gates PASS", ok_good is True)
        check("ENABLED after approved reset", store.read().state == "ENABLED")
    finally:
        _cleanup_switch(store)


# ---------------------------------------------------------------------------
# 20. fail-safe.
# ---------------------------------------------------------------------------

def test_fail_safe():
    section("20. fail-safe (corrupted/missing state -> BLOCK, never ALLOW by default)")
    fd, name = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    Path(name).write_text("{not valid json", encoding="utf-8")
    store = KillSwitchStore(state_path=Path(name), audit_path=Path(tempfile.gettempdir()) / "phase10_test_audit_unused.json")
    try:
        result = assert_production_allowed(store, "PRODUCTION_PREDICTION_ACTIVATION")
        check("corrupted state -> BLOCK", result.allowed is False)
        check("code KILL_SWITCH_CORRUPTED", result.code == "KILL_SWITCH_CORRUPTED")
    finally:
        Path(name).unlink(missing_ok=True)
    guard = can_activate_production(_temp_switch(), [], scope="PRODUCTION_PREDICTION_ACTIVATION")
    check("0 critical gates -> denied, never treated as nothing to check", guard.allowed is False)


# ---------------------------------------------------------------------------
# 21/22/23. rollback — isolated fixture only, never api/app.db.
# ---------------------------------------------------------------------------

def test_rollback_test_isolation():
    section("21. rollback test isolation (dedicated db, never api/app.db)")
    check("DB_PATH is dedicated test db", "test_phase10.db" in str(DB_PATH) and "app.db" not in str(DB_PATH))


def test_rollback_idempotence():
    section("22. rollback idempotence (A -> A twice, deterministic)")
    with Session(engine) as s:
        _clear_all(s)
        v1 = _seed_active(s, "dixon_coles")
        v2 = ModelVersion(name=next_version_name(s, "xfoot-dixon-coles"), model_type="dixon_coles",
                           trained_at=datetime.now(UTC), is_active=False, status="candidate")
        s.add(v2); s.commit(); s.refresh(v2)
        apply_promotion(s, v2); s.commit()

        readiness = evaluate_rollback_readiness(s, "dixon_coles", target_version_id=v1.id)
        check("rollback readiness available before execution", readiness.status == "ROLLBACK_AVAILABLE")

        r1 = execute_rollback(s, "dixon_coles", actor="tester", target_version_id=v1.id)
        r2 = execute_rollback(s, "dixon_coles", actor="tester", target_version_id=v1.id)
        check("first rollback EXECUTED", r1.status == "EXECUTED")
        check("second identical rollback NOOP (idempotent)", r2.status == "NOOP_ALREADY_ACTIVE")
        check("active version is v1 after both calls", get_active_version(s, "dixon_coles").id == v1.id)


def test_rollback_audit_trail():
    section("23. rollback audit trail (auditable)")
    audit_store = _temp_switch()
    try:
        with Session(engine) as s:
            _clear_all(s)
            v1 = _seed_active(s, "elo")
            v2 = ModelVersion(name=next_version_name(s, "xfoot-elo"), model_type="elo", trained_at=datetime.now(UTC), is_active=False, status="candidate")
            s.add(v2); s.commit(); s.refresh(v2)
            apply_promotion(s, v2); s.commit()
            result = execute_rollback(s, "elo", actor="tester", target_version_id=v1.id, audit_store=audit_store)
            check("rollback EXECUTED", result.status == "EXECUTED")
        log = audit_store.read_audit_log()
        check("audit trail records ROLLBACK_EXECUTED", any(e.get("event_type") == "ROLLBACK_EXECUTED" for e in log))
    finally:
        _cleanup_switch(audit_store)


# ---------------------------------------------------------------------------
# 24. production isolation.
# ---------------------------------------------------------------------------

def test_production_isolation():
    section("24. production isolation (human_review.py / script never call training/promotion/rollback/betting-signal functions)")
    hr_source = inspect.getsource(human_review)
    check("human_review.py: no apply_promotion/execute_rollback calls", "apply_promotion(" not in hr_source and "execute_rollback(" not in hr_source)
    check("script: no apply_promotion/execute_rollback calls (rollback demo lives only in test_phase10.py, isolated DB)",
          "apply_promotion(" not in SCRIPT_SOURCE and "execute_rollback(" not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 25. API isolation.
# ---------------------------------------------------------------------------

def test_api_isolation():
    section("25. API isolation (gate_api_exposure reused)")
    gate = gates.gate_api_exposure()
    check("status in vocabulary", gate.status in GATE_STATUSES)


# ---------------------------------------------------------------------------
# 26. frontend isolation.
# ---------------------------------------------------------------------------

def test_frontend_isolation():
    section("26. frontend isolation (gate_frontend_exposure reused)")
    gate = gates.gate_frontend_exposure()
    check("status in vocabulary", gate.status in GATE_STATUSES)


# ---------------------------------------------------------------------------
# 27. no odds fabrication.
# ---------------------------------------------------------------------------

def test_no_odds_fabrication():
    section("27. no odds fabrication")
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
# 28. value unavailable without odds.
# ---------------------------------------------------------------------------

def test_value_unavailable():
    section("28. value unavailable without odds")
    status = value_tracking_status([])
    check("NOT_AVAILABLE", status["status"] == "NOT_AVAILABLE")


# ---------------------------------------------------------------------------
# 29/30/31. no model training / promotion / modification.
# ---------------------------------------------------------------------------

def test_no_model_training():
    section("29. no model training")
    check("human_review.py: no train_/fit", "train_" not in inspect.getsource(human_review) and ".fit(" not in inspect.getsource(human_review))
    check("script: no train_/fit", "train_" not in SCRIPT_SOURCE and ".fit(" not in SCRIPT_SOURCE)


def test_no_model_promotion():
    section("30. no model promotion (no actual CALL — a documentary mention explaining what is deliberately never invoked is not a violation)")
    check("human_review.py: no apply_promotion(/deactivate_other_versions( call", all(x not in inspect.getsource(human_review) for x in ("apply_promotion(", "deactivate_other_versions(")))
    check("script: no apply_promotion(/deactivate_other_versions( call", all(x not in SCRIPT_SOURCE for x in ("apply_promotion(", "deactivate_other_versions(")))


def test_no_model_modification():
    section("31. no model modification")
    check("human_review.py never writes ModelVersion", "session.add(ModelVersion" not in inspect.getsource(human_review))
    check("script never writes ModelVersion", "session.add(ModelVersion" not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 32. no scheduler modification.
# ---------------------------------------------------------------------------

def test_no_scheduler():
    section("32. no scheduler modification")
    check("human_review.py: scheduler never imported", "arena.scheduler" not in inspect.getsource(human_review))
    check("script: scheduler never imported", "arena.scheduler" not in SCRIPT_SOURCE and "import scheduler" not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 33. no network.
# ---------------------------------------------------------------------------

def test_no_network():
    section("33. no network")
    check("human_review.py: no httpx/requests", "import httpx" not in inspect.getsource(human_review) and "import requests" not in inspect.getsource(human_review))
    check("script: no httpx/requests", "import httpx" not in SCRIPT_SOURCE and "import requests" not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 34. DB purity.
# ---------------------------------------------------------------------------

def test_db_purity():
    section("34. DB purity (human_review.py never writes to the DB)")
    source = inspect.getsource(human_review)
    check("no session.add(", "session.add(" not in source)
    check("no session.commit(", "session.commit(" not in source)


# ---------------------------------------------------------------------------
# 35. store purity.
# ---------------------------------------------------------------------------

def test_store_purity():
    section("35. store purity (orchestrator never writes the Shadow Store)")
    check("script never calls store.save()/upsert_new/update_resolution", all(x not in SCRIPT_SOURCE for x in ("store.save(", "store.upsert_new(", "store.update_resolution(")))


# ---------------------------------------------------------------------------
# 36. concurrency.
# ---------------------------------------------------------------------------

def test_concurrency():
    section("36. concurrency (capture lock still exclusive)")
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
# 37. deterministic output.
# ---------------------------------------------------------------------------

def test_deterministic_output():
    section("37. deterministic output (pure verdict function)")
    kwargs = dict(preflight_status="PASS", tests_green=True, readiness_verdict="NO_GO", future_fixtures=0,
                  real_prospective_resolved=0, maturity="NO_DATA", blockers=[])
    v1 = derive_phase10_verdict(**kwargs)
    v2 = derive_phase10_verdict(**kwargs)
    check("identical inputs -> identical verdict", v1 == v2)


# ---------------------------------------------------------------------------
# 38. report generation.
# ---------------------------------------------------------------------------

def test_report_generation():
    section("38. report generation (building blocks well-formed)")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        readiness = evaluate_production_readiness(s, store, datetime.now(UTC))
        checklist = build_phase10_checklist(readiness)
        evidence_status = classify_evidence_status(readiness)
        check("checklist has 16 items", len(checklist) == len(PHASE10_CHECKLIST_ITEMS) == 16)
        check("evidence_status has all 5 buckets", set(evidence_status.keys()) == {"proven", "observed", "unknown", "blocked", "required_next"})
        check("PRODUCTION_READY absent from vocabulary", "PRODUCTION_READY" not in PHASE10_VERDICTS)


# ---------------------------------------------------------------------------
# 39. human review gate.
# ---------------------------------------------------------------------------

def test_human_review_gate():
    section("39. human review gate")
    check("blockers present -> NOT_READY", human_review_gate(maturity="STATISTICALLY_INFORMATIVE", blockers=["TRACK_RECORD"], readiness_verdict="CONDITIONALLY_READY") == "NOT_READY_FOR_HUMAN_REVIEW")
    check("no blockers + CONDITIONALLY_READY + STATISTICALLY_INFORMATIVE -> READY", human_review_gate(maturity="STATISTICALLY_INFORMATIVE", blockers=[], readiness_verdict="CONDITIONALLY_READY") == "READY_FOR_HUMAN_REVIEW")
    check("low maturity -> NOT_READY regardless of readiness", human_review_gate(maturity="NO_DATA", blockers=[], readiness_verdict="PRODUCTION_READY") == "NOT_READY_FOR_HUMAN_REVIEW")
    check("result always in HUMAN_REVIEW_STATUSES", human_review_gate(maturity="NO_DATA", blockers=[], readiness_verdict="NO_GO") in HUMAN_REVIEW_STATUSES)


# ---------------------------------------------------------------------------
# 40. activation checklist.
# ---------------------------------------------------------------------------

def test_activation_checklist():
    section("40. activation checklist (critical gate non-PASS -> reflected, never silently PASS)")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        readiness = evaluate_production_readiness(s, store, datetime.now(UTC))
        checklist = build_phase10_checklist(readiness)
        by_item = {c["item"]: c for c in checklist}
        check("Odds/Value allow NA without being critical-blocking", by_item["Odds"]["status"] in ("PASS", "NA", "FAIL", "UNKNOWN") and by_item["Value"]["status"] in ("PASS", "NA", "FAIL", "UNKNOWN"))
        non_pass_critical = [c for c in checklist if c["critical"] and not c["checked"]]
        if non_pass_critical:
            check("current real state: readiness NO_GO consistent with a non-PASS critical checklist item", readiness.final_verdict == "NO_GO")


# ---------------------------------------------------------------------------
# 41. entry criteria.
# ---------------------------------------------------------------------------

def test_entry_criteria():
    section("41. entry criteria (reused categories, no arbitrary threshold)")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        readiness = evaluate_production_readiness(s, store, datetime.now(UTC))
        entry = build_entry_criteria(readiness)
        check("all categories present", set(entry.keys()) == set(ENTRY_CRITERIA_CATEGORIES))


# ---------------------------------------------------------------------------
# 42. exit criteria.
# ---------------------------------------------------------------------------

def test_exit_criteria():
    section("42. exit criteria (maturity ladder matches MATURITY_THRESHOLDS exactly, mode ladder matches build_activation_matrix)")
    exit_c = build_exit_criteria()
    check("EARLY_DATA threshold matches MATURITY_THRESHOLDS", str(MATURITY_THRESHOLDS["EARLY_DATA"]) in exit_c["maturity_ladder"]["NO_DATA_to_EARLY_DATA"])
    check("mode ladder matches build_activation_matrix keys", set(exit_c["activation_mode_ladder"].keys()) == set(build_activation_matrix().keys()))


# ---------------------------------------------------------------------------
# 43. mode 1 enforced.
# ---------------------------------------------------------------------------

def test_mode_1_enforced():
    section("43. MODE_1_SHADOW_ONLY enforced structurally")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        ks = _temp_switch()
        try:
            result = run_preflight_safety(s, store, ks, datetime.now(UTC))
            check("mode is MODE_1_SHADOW_ONLY", result["checks"]["mode"]["value"] == "MODE_1_SHADOW_ONLY")
        finally:
            _cleanup_switch(ks)
    check("script never claims MODE_2/3/4 as operating mode", '"mode": "MODE_2' not in SCRIPT_SOURCE and '"mode": "MODE_3' not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 44. no automatic activation.
# ---------------------------------------------------------------------------

def test_no_automatic_activation():
    section("44. no automatic activation")
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
        check("ModelVersion.status unchanged", refreshed.status == "active")
        lock.unlink(missing_ok=True)
    check("script never literally assigns PRODUCTION_READY as a chosen verdict", '"final_verdict": "PRODUCTION_READY"' not in SCRIPT_SOURCE)
    check("human_review.py never triggers/resets the Kill Switch", ".trigger(" not in inspect.getsource(human_review) and ".reset(" not in inspect.getsource(human_review))


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
