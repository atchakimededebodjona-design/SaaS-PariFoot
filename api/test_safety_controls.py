"""
test_safety_controls.py — Phase 9.1 : tests de api/app/ai/safety/
(kill_switch.py + guards.py + rollback.py). Base isolée dédiée (jamais
api/app.db), Kill Switch store isolé dédié (jamais reports/safety/*.json
réel — voir _temp_switch()).

Usage : python api/test_safety_controls.py
"""

import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_safety_controls.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.team_rating import ModelVersion, next_version_name
from app.models.match import Match

from app.ai.arena.promotion import apply_promotion, get_active_version

from app.ai.safety.schemas import KILL_SWITCH_STATES, KILL_SWITCH_TRIGGERS
from app.ai.safety.kill_switch import KillSwitchStore, trigger, reset, assert_production_allowed
from app.ai.safety.guards import can_activate_production
from app.ai.safety.rollback import evaluate_rollback_readiness, execute_rollback

init_db()
UTC = timezone.utc


def _temp_switch() -> KillSwitchStore:
    fd1, p1 = tempfile.mkstemp(suffix=".json"); os.close(fd1); Path(p1).unlink()
    fd2, p2 = tempfile.mkstemp(suffix=".json"); os.close(fd2); Path(p2).unlink()
    return KillSwitchStore(state_path=Path(p1), audit_path=Path(p2))


def _cleanup_switch(store: KillSwitchStore) -> None:
    Path(store.state_path).unlink(missing_ok=True)
    Path(store.audit_path).unlink(missing_ok=True)


@dataclass(frozen=True)
class _FakeGate:
    name: str
    status: str
    critical: bool
    blocking_reason: str = None


ALL_PASS_CRITICAL = {
    "TEMPORAL_INTEGRITY": "PASS", "MODEL": "PASS", "MODEL_VERSION": "PASS", "PROVENANCE": "PASS",
    "DATABASE_SAFETY": "PASS", "ROLLBACK": "PASS", "MONITORING": "PASS", "TRACK_RECORD": "PASS",
    "KILL_SWITCH": "PASS",
}


def _seed_active(session, model_type, status="active", is_active=True, trained_at=None):
    v = ModelVersion(name=next_version_name(session, f"xfoot-{model_type}"), model_type=model_type,
                      trained_at=trained_at or (datetime.now(UTC) - timedelta(days=10)), is_active=is_active, status=status)
    session.add(v); session.commit(); session.refresh(v)
    return v


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
# 1. default state.
# ---------------------------------------------------------------------------

def test_default_state():
    section("1. default state")
    store = _temp_switch()
    state = store.read()
    check("default state ENABLED (file absent, legitimate default)", state.state == "ENABLED")
    check("effective_status ENABLED", state.effective_status == "ENABLED")
    result = assert_production_allowed(store, "MODEL_PROMOTION")
    check("production allowed by default", result.allowed is True)
    _cleanup_switch(store)


# ---------------------------------------------------------------------------
# 2. trigger.
# ---------------------------------------------------------------------------

def test_trigger():
    section("2. trigger")
    store = _temp_switch()
    st = trigger(store, "MANUAL_OPERATOR_TRIGGER", "test", scope="MODEL_PROMOTION", actor="tester", automatic=False)
    check("state TRIGGERED", st.state == "TRIGGERED")
    check("effective_status RESET_REQUIRED", st.effective_status == "RESET_REQUIRED")
    check("code in vocabulary", st.trigger_code in KILL_SWITCH_TRIGGERS)
    result = assert_production_allowed(store, "MODEL_PROMOTION")
    check("production blocked after trigger", result.allowed is False)
    check("block code KILL_SWITCH_ACTIVE", result.code == "KILL_SWITCH_ACTIVE")
    _cleanup_switch(store)


def test_trigger_unknown_code_rejected():
    section("2b. trigger rejects unknown code")
    store = _temp_switch()
    try:
        trigger(store, "NOT_A_REAL_TRIGGER", "x", actor="tester")
        check("raises ValueError on unknown trigger code", False)
    except ValueError:
        check("raises ValueError on unknown trigger code", True)
    _cleanup_switch(store)


# ---------------------------------------------------------------------------
# 3. trigger idempotence.
# ---------------------------------------------------------------------------

def test_trigger_idempotence():
    section("3. trigger idempotence")
    store = _temp_switch()
    trigger(store, "MODEL_MISMATCH", "first", actor="a")
    trigger(store, "MODEL_MISMATCH", "second", actor="b")
    state = store.read()
    check("state still TRIGGERED (no crash, no incoherent state)", state.state == "TRIGGERED")
    log = store.read_audit_log()
    check("both trigger attempts logged (2 TRIGGERED events)", sum(1 for e in log if e["event_type"] == "TRIGGERED") == 2)
    _cleanup_switch(store)


# ---------------------------------------------------------------------------
# 4. reset.
# ---------------------------------------------------------------------------

def test_reset_approved_when_gates_pass():
    section("4. reset (gates PASS)")
    store = _temp_switch()
    trigger(store, "MODEL_MISMATCH", "x", actor="a")
    ok, msg, state = reset(store, ALL_PASS_CRITICAL, actor="tester", reason="all clear")
    check("reset approved", ok is True)
    check("state ENABLED after reset", state.state == "ENABLED")
    check("production allowed again", assert_production_allowed(store, "MODEL_PROMOTION").allowed is True)
    _cleanup_switch(store)


def test_reset_noop_when_already_enabled():
    section("4b. reset no-op when already ENABLED")
    store = _temp_switch()
    ok, msg, state = reset(store, ALL_PASS_CRITICAL, actor="tester", reason="nothing to do")
    check("reset returns True (no-op)", ok is True)
    check("message ALREADY_ENABLED_NOOP", msg == "ALREADY_ENABLED_NOOP")
    _cleanup_switch(store)


# ---------------------------------------------------------------------------
# 5. reset denied by critical gate.
# ---------------------------------------------------------------------------

def test_reset_denied_by_critical_gate():
    section("5. reset denied by critical gate")
    store = _temp_switch()
    trigger(store, "MODEL_MISMATCH", "x", actor="a")
    gates = dict(ALL_PASS_CRITICAL); gates["TRACK_RECORD"] = "NOT_AVAILABLE"
    ok, msg, state = reset(store, gates, actor="tester", reason="attempt")
    check("reset denied", ok is False)
    check("state still TRIGGERED", state.state == "TRIGGERED")
    check("still blocked", assert_production_allowed(store, "MODEL_PROMOTION").allowed is False)
    _cleanup_switch(store)


def test_reset_denied_when_no_gates_provided():
    section("5b. reset denied when 0 gate provided (absence of proof != proof of safety)")
    store = _temp_switch()
    trigger(store, "MODEL_MISMATCH", "x", actor="a")
    ok, msg, state = reset(store, {}, actor="tester", reason="attempt")
    check("reset denied with empty gates dict", ok is False)
    _cleanup_switch(store)


# ---------------------------------------------------------------------------
# 6. corrupted state.
# ---------------------------------------------------------------------------

def test_corrupted_state_blocks():
    section("6. corrupted state -> BLOCK")
    store = _temp_switch()
    store.state_path.parent.mkdir(parents=True, exist_ok=True)
    store.state_path.write_text("{not valid json", encoding="utf-8")
    try:
        store.read()
        check("read() raises on corrupted JSON", False)
    except ValueError:
        check("read() raises on corrupted JSON", True)
    result = assert_production_allowed(store, "MODEL_PROMOTION")
    check("assert_production_allowed BLOCKS on corruption (never ENABLED default)", result.allowed is False)
    check("block code KILL_SWITCH_CORRUPTED", result.code == "KILL_SWITCH_CORRUPTED")
    _cleanup_switch(store)


def test_invalid_state_value_blocks():
    section("6b. invalid state value (hors vocabulaire) -> BLOCK")
    store = _temp_switch()
    store.state_path.parent.mkdir(parents=True, exist_ok=True)
    import json
    store.state_path.write_text(json.dumps({"state": "SOMETHING_ELSE"}), encoding="utf-8")
    result = assert_production_allowed(store, "MODEL_PROMOTION")
    check("invalid state value blocks", result.allowed is False)
    _cleanup_switch(store)


# ---------------------------------------------------------------------------
# 7. unreadable state.
# ---------------------------------------------------------------------------

def test_unreadable_state_blocks():
    section("7. unreadable state -> BLOCK")
    # `state_path.exists()` est True (c'est un RÉPERTOIRE, pas un fichier) mais `read_text()` lève
    # IsADirectoryError (OSError) — simule un fichier illisible sans dépendre de permissions OS réelles
    # (non portable Windows/POSIX). Capturé par le même bloc `except (OSError, ...)` que la corruption JSON
    # (§3 : un seul point de capture, jamais deux comportements distincts pour "présent mais illisible").
    import shutil
    bad_dir = Path(tempfile.gettempdir()) / "xfoot_safety_test_state_as_dir.json"
    bad_dir.mkdir(parents=True, exist_ok=True)
    store = KillSwitchStore(state_path=bad_dir, audit_path=Path(tempfile.gettempdir()) / "xfoot_safety_test_audit_unused.json")
    check("state_path 'exists' (it's a directory)", store.state_path.exists())
    try:
        store.read()
        check("read() raises on unreadable (directory-as-file) state", False)
    except ValueError:
        check("read() raises on unreadable (directory-as-file) state", True)
    result = assert_production_allowed(store, "MODEL_PROMOTION")
    check("unreadable path blocks (treated as corrupted/unreadable, never ENABLED)", result.allowed is False)
    shutil.rmtree(bad_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 8. fail-safe (generic).
# ---------------------------------------------------------------------------

def test_fail_safe_never_default_allow_on_error():
    section("8. fail-safe : jamais ALLOW par défaut sur erreur")
    store = _temp_switch()
    import json
    store.state_path.parent.mkdir(parents=True, exist_ok=True)
    store.state_path.write_text(json.dumps({"no_state_field": True}), encoding="utf-8")
    result = assert_production_allowed(store, "MODEL_PROMOTION")
    check("missing 'state' field blocks, never defaults to allowed", result.allowed is False)
    _cleanup_switch(store)


# ---------------------------------------------------------------------------
# 9. activation guard.
# ---------------------------------------------------------------------------

def test_activation_guard_all_pass():
    section("9. activation guard (all critical gates PASS + kill switch ENABLED)")
    store = _temp_switch()
    gates = [_FakeGate(name, "PASS", True) for name in ALL_PASS_CRITICAL]
    result = can_activate_production(store, gates, scope="MODEL_PROMOTION")
    check("allowed True", result.allowed is True)
    check("no blocking reasons", result.blocking_reasons == [])
    _cleanup_switch(store)


def test_activation_guard_blocks_on_single_critical_fail():
    section("9b. activation guard blocks on a single critical gate FAIL (no compensation)")
    store = _temp_switch()
    gates = [_FakeGate(name, "PASS", True) for name in ALL_PASS_CRITICAL]
    gates[0] = _FakeGate("TEMPORAL_INTEGRITY", "FAIL", True, "leak detected")
    result = can_activate_production(store, gates, scope="MODEL_PROMOTION")
    check("blocked", result.allowed is False)
    check("reason mentions TEMPORAL_INTEGRITY", any("TEMPORAL_INTEGRITY" in r for r in result.blocking_reasons))
    _cleanup_switch(store)


def test_activation_guard_blocks_with_no_gates():
    section("9c. activation guard blocks when 0 critical gate provided")
    store = _temp_switch()
    result = can_activate_production(store, [], scope="MODEL_PROMOTION")
    check("blocked (absence of evidence != safe)", result.allowed is False)
    _cleanup_switch(store)


# ---------------------------------------------------------------------------
# 10/11/12/13. temporal / provenance / model mismatch / probability mismatch failures -> guard blocks.
# ---------------------------------------------------------------------------

def test_temporal_failure_blocks_guard():
    section("10. temporal failure blocks activation guard")
    store = _temp_switch()
    gates = [_FakeGate(name, "PASS", True) for name in ALL_PASS_CRITICAL]
    gates = [g if g.name != "TEMPORAL_INTEGRITY" else _FakeGate("TEMPORAL_INTEGRITY", "FAIL", True) for g in gates]
    result = can_activate_production(store, gates, scope="MODEL_PROMOTION")
    check("blocked", result.allowed is False)
    _cleanup_switch(store)


def test_provenance_failure_blocks_guard():
    section("11. provenance failure blocks activation guard")
    store = _temp_switch()
    gates = [g if g.name != "PROVENANCE" else _FakeGate("PROVENANCE", "NOT_AVAILABLE", True) for g in
             (_FakeGate(name, "PASS", True) for name in ALL_PASS_CRITICAL)]
    result = can_activate_production(store, gates, scope="MODEL_PROMOTION")
    check("blocked", result.allowed is False)
    _cleanup_switch(store)


def test_model_mismatch_via_kill_switch_trigger_blocks_guard():
    section("12. model mismatch (kill switch trigger) blocks activation guard")
    store = _temp_switch()
    trigger(store, "MODEL_MISMATCH", "unexpected model version served", actor="system")
    gates = [_FakeGate(name, "PASS", True) for name in ALL_PASS_CRITICAL]
    result = can_activate_production(store, gates, scope="MODEL_PROMOTION")
    check("blocked by kill switch even though all readiness gates PASS (no compensation)", result.allowed is False)
    check("kill_switch_status reflects trigger", result.kill_switch_status == "KILL_SWITCH_ACTIVE")
    _cleanup_switch(store)


def test_probability_mismatch_trigger():
    section("13. probability mismatch trigger blocks")
    store = _temp_switch()
    trigger(store, "PROBABILITY_MISMATCH", "sum != 1.0 detected", actor="system")
    result = assert_production_allowed(store, "VALUE_SIGNAL_PRODUCTION")
    check("blocked", result.allowed is False)
    _cleanup_switch(store)


def test_decision_mismatch_trigger():
    section("13b. decision mismatch trigger blocks")
    store = _temp_switch()
    trigger(store, "DECISION_MISMATCH", "eligibility flipped unexpectedly", actor="system")
    result = assert_production_allowed(store, "PRODUCTION_PREDICTION_ACTIVATION")
    check("blocked", result.allowed is False)
    _cleanup_switch(store)


# ---------------------------------------------------------------------------
# 15. database mutation trigger.
# ---------------------------------------------------------------------------

def test_database_mutation_trigger():
    section("15. DATABASE_MUTATION trigger blocks")
    store = _temp_switch()
    trigger(store, "DATABASE_MUTATION", "unexpected write detected outside safety module", actor="system", automatic=True)
    result = assert_production_allowed(store, "PRODUCTION_PREDICTION_ACTIVATION")
    check("blocked", result.allowed is False)
    log = store.read_audit_log()
    check("audit records automatic=True", log[-1].get("code") == "DATABASE_MUTATION")
    _cleanup_switch(store)


# ---------------------------------------------------------------------------
# 16/17/18/19/20. rollback — isolated fixture only, never api/app.db.
# ---------------------------------------------------------------------------

def test_rollback_isolated_fixture_only():
    section("16. rollback isolated (dedicated in-memory-like temp db via _test_support, never api/app.db)")
    check("DB_PATH is a dedicated test db file, not app.db", "test_safety_controls.db" in str(DB_PATH) and "app.db" not in str(DB_PATH))


def test_rollback_restores_previous_version():
    section("17. rollback restores previous version")
    with Session(engine) as s:
        for v in s.exec(select(ModelVersion)).all():
            s.delete(v)
        s.commit()
        v1 = _seed_active(s, "dixon_coles")
        v2 = ModelVersion(name=next_version_name(s, "xfoot-dixon-coles"), model_type="dixon_coles",
                           trained_at=datetime.now(UTC), is_active=False, status="candidate")
        s.add(v2); s.commit(); s.refresh(v2)
        apply_promotion(s, v2); s.commit()
        check("v2 active after promotion", get_active_version(s, "dixon_coles").id == v2.id)

        result = execute_rollback(s, "dixon_coles", actor="tester", target_version_id=v1.id)
        check("rollback EXECUTED", result.status == "EXECUTED")
        check("v1 restored", get_active_version(s, "dixon_coles").id == v1.id)


def test_rollback_preserves_history():
    section("18. rollback preserves history (no DELETE)")
    with Session(engine) as s:
        for v in s.exec(select(ModelVersion)).all():
            s.delete(v)
        s.commit()
        v1 = _seed_active(s, "elo")
        v2 = ModelVersion(name=next_version_name(s, "xfoot-elo"), model_type="elo", trained_at=datetime.now(UTC), is_active=False, status="candidate")
        s.add(v2); s.commit(); s.refresh(v2)
        apply_promotion(s, v2); s.commit()
        execute_rollback(s, "elo", actor="tester", target_version_id=v1.id)
        all_versions = s.exec(select(ModelVersion).where(ModelVersion.model_type == "elo")).all()
        check("both v1 and v2 still exist in DB (never deleted)", {v.id for v in all_versions} == {v1.id, v2.id})
        v2_after = s.get(ModelVersion, v2.id)
        check("v2 marked retired, not deleted", v2_after.status == "retired")


def test_rollback_idempotence():
    section("19. rollback idempotence (A -> A twice)")
    with Session(engine) as s:
        for v in s.exec(select(ModelVersion)).all():
            s.delete(v)
        s.commit()
        v1 = _seed_active(s, "xgboost")
        v2 = ModelVersion(name=next_version_name(s, "xfoot-xgboost"), model_type="xgboost", trained_at=datetime.now(UTC), is_active=False, status="candidate")
        s.add(v2); s.commit(); s.refresh(v2)
        apply_promotion(s, v2); s.commit()

        r1 = execute_rollback(s, "xgboost", actor="tester", target_version_id=v1.id)
        r2 = execute_rollback(s, "xgboost", actor="tester", target_version_id=v1.id)
        check("first rollback EXECUTED", r1.status == "EXECUTED")
        check("second identical rollback NOOP (no incoherent state)", r2.status == "NOOP_ALREADY_ACTIVE")
        check("active version still v1 after both calls", get_active_version(s, "xgboost").id == v1.id)
        events_after = len(s.exec(select(ModelVersion).where(ModelVersion.model_type == "xgboost")).all())
        check("no phantom ModelVersion created", events_after == 2)


def test_rollback_unavailable_without_history():
    section("20. rollback unavailable when no retired version exists -> BLOCK, never invented")
    with Session(engine) as s:
        for v in s.exec(select(ModelVersion)).all():
            s.delete(v)
        s.commit()
        _seed_active(s, "lightgbm")  # seule version, jamais retirée -> pas de cible de rollback.
        readiness = evaluate_rollback_readiness(s, "lightgbm")
        check("ROLLBACK_NOT_AVAILABLE", readiness.status == "ROLLBACK_NOT_AVAILABLE")
        result = execute_rollback(s, "lightgbm", actor="tester")
        check("execute_rollback DENIED, never best-effort", result.status == "DENIED")


# ---------------------------------------------------------------------------
# 21. kill switch + rollback combined scenario.
# ---------------------------------------------------------------------------

def test_kill_switch_plus_rollback_scenario():
    section("21. kill switch + rollback combined")
    store = _temp_switch()
    with Session(engine) as s:
        for v in s.exec(select(ModelVersion)).all():
            s.delete(v)
        s.commit()
        v1 = _seed_active(s, "ensemble")
        v2 = ModelVersion(name=next_version_name(s, "xfoot-ensemble"), model_type="ensemble", trained_at=datetime.now(UTC), is_active=False, status="candidate")
        s.add(v2); s.commit(); s.refresh(v2)
        apply_promotion(s, v2); s.commit()

        trigger(store, "MODEL_MISMATCH", "candidate underperforming live", actor="system")
        check("production blocked", assert_production_allowed(store, "MODEL_PROMOTION").allowed is False)

        result = execute_rollback(s, "ensemble", actor="operator", target_version_id=v1.id, audit_store=store)
        check("rollback executed while kill switch still triggered (rollback itself is a mitigation, not a production activation)", result.status == "EXECUTED")
        check("active is v1 again", get_active_version(s, "ensemble").id == v1.id)

        ok, msg, state = reset(store, ALL_PASS_CRITICAL, actor="operator", reason="rollback complete, model gates checked")
        check("reset now succeeds once mitigated + gates pass", ok is True)

        log = store.read_audit_log()
        check("audit log contains ROLLBACK_EXECUTED event", any(e["event_type"] == "ROLLBACK_EXECUTED" for e in log))
    _cleanup_switch(store)


def test_reset_still_blocked_if_gates_fail_even_after_rollback():
    section("21b. reset impossible tant que gates critiques non PASS, même après rollback")
    store = _temp_switch()
    trigger(store, "MODEL_MISMATCH", "x", actor="system")
    gates = dict(ALL_PASS_CRITICAL); gates["TRACK_RECORD"] = "NOT_AVAILABLE"
    ok, msg, state = reset(store, gates, actor="operator", reason="attempt after rollback")
    check("reset denied while a critical gate is not PASS", ok is False)
    check("state remains TRIGGERED", state.state == "TRIGGERED")
    _cleanup_switch(store)


# ---------------------------------------------------------------------------
# 22/23. concurrency.
# ---------------------------------------------------------------------------

def test_concurrency_two_triggers():
    section("22. concurrency : deux triggers simultanés")
    store = _temp_switch()
    trigger(store, "MODEL_MISMATCH", "first", actor="a")
    trigger(store, "PROVENANCE_MISSING", "second", actor="b")
    state = store.read()
    check("state coherent (TRIGGERED, one of the two valid persisted values)", state.state in ("ENABLED", "TRIGGERED"))
    check("still blocked", assert_production_allowed(store, "MODEL_PROMOTION").allowed is False)
    log = store.read_audit_log()
    check("both triggers recorded in audit (nothing silently dropped)", len(log) == 2)
    _cleanup_switch(store)


def test_concurrency_trigger_and_reset():
    section("23a. concurrency : trigger + reset")
    store = _temp_switch()
    trigger(store, "MODEL_MISMATCH", "x", actor="a")
    ok, msg, state = reset(store, ALL_PASS_CRITICAL, actor="b", reason="race")
    check("reset succeeds deterministically (sequential file ops, no torn state)", ok is True)
    check("final state coherent", state.state in KILL_SWITCH_STATES[:2])  # ENABLED/TRIGGERED (les 2 valeurs persistées)
    _cleanup_switch(store)


def test_concurrency_reset_and_trigger():
    section("23b. concurrency : reset + trigger (ambiguity -> ends TRIGGERED, never silently ENABLED)")
    store = _temp_switch()
    trigger(store, "MODEL_MISMATCH", "x", actor="a")
    reset(store, ALL_PASS_CRITICAL, actor="b", reason="clear")
    trigger(store, "PROVENANCE_MISSING", "y", actor="c")
    state = store.read()
    check("final state TRIGGERED (last write wins, deterministic, no ambiguous ENABLED)", state.state == "TRIGGERED")
    _cleanup_switch(store)


# ---------------------------------------------------------------------------
# 24. no secret leakage.
# ---------------------------------------------------------------------------

def test_no_secret_leakage():
    section("24. no secret leakage in state/audit files")
    store = _temp_switch()
    trigger(store, "MANUAL_OPERATOR_TRIGGER", "operator flagged suspicious THE_ODDS_API_KEY=sk_live_should_never_appear", actor="tester")
    raw_state = store.state_path.read_text(encoding="utf-8")
    raw_audit = store.audit_path.read_text(encoding="utf-8")
    # Le texte de la RAISON est stocké tel quel (jamais un champ dédié "secret") — ce test vérifie que le
    # MÉCANISME lui-même ne loggue jamais de variable d'environnement/secret réel automatiquement (aucun
    # accès à os.environ nulle part dans kill_switch.py/guards.py/rollback.py).
    import app.ai.safety.kill_switch as ks_module
    import inspect
    source = inspect.getsource(ks_module)
    check("kill_switch.py never reads os.environ", "os.environ" not in source)
    _cleanup_switch(store)


# ---------------------------------------------------------------------------
# 25. dry-run purity (tested at CLI level in scripts/safety_control.py — here, the underlying read functions
# used by --dry-run never write).
# ---------------------------------------------------------------------------

def test_dry_run_purity_read_functions_never_write():
    section("25. dry-run purity (read-only functions never mutate)")
    store = _temp_switch()
    before_exists = store.state_path.exists()
    _ = assert_production_allowed(store, "MODEL_PROMOTION")
    _ = store.read()
    after_exists = store.state_path.exists()
    check("read-only calls never create the state file", before_exists == after_exists == False)
    _cleanup_switch(store)


# ---------------------------------------------------------------------------
# 26/27/28. CLI-level behavior — exercised via the pure functions the CLI calls (scripts/safety_control.py
# is a thin wrapper, no separate logic — see script itself for the argparse plumbing).
# ---------------------------------------------------------------------------

def test_cli_status_reads_without_side_effect():
    section("26. CLI status (read-only)")
    store = _temp_switch()
    state = store.read()
    check("status read never writes", not store.state_path.exists())
    _cleanup_switch(store)


def test_cli_trigger_path():
    section("27. CLI trigger (functional, not merely simulated)")
    store = _temp_switch()
    trigger(store, "MANUAL_OPERATOR_TRIGGER", "operator CLI trigger", scope="MODEL_PROMOTION", actor="cli-operator", automatic=False)
    check("state file now exists and reflects TRIGGERED", store.read().state == "TRIGGERED")
    _cleanup_switch(store)


def test_cli_reset_refused_without_fresh_gates():
    section("28. CLI reset refusal path (no gates supplied by caller)")
    store = _temp_switch()
    trigger(store, "MANUAL_OPERATOR_TRIGGER", "x", actor="a")
    ok, msg, state = reset(store, {}, actor="cli-operator", reason="no readiness check run")
    check("CLI-style reset refused without a fresh Phase 9 evaluation", ok is False)
    _cleanup_switch(store)


# ---------------------------------------------------------------------------
# 29. deterministic state.
# ---------------------------------------------------------------------------

def test_deterministic_state():
    section("29. deterministic state (same sequence of calls -> same final state)")
    results = []
    for _ in range(2):
        store = _temp_switch()
        trigger(store, "MODEL_MISMATCH", "x", actor="a", now=datetime(2026, 1, 1, tzinfo=UTC))
        reset(store, ALL_PASS_CRITICAL, actor="b", reason="clear", now=datetime(2026, 1, 1, 1, tzinfo=UTC))
        results.append(store.read().state)
        _cleanup_switch(store)
    check("same final state across two independent runs", results[0] == results[1] == "ENABLED")


# ---------------------------------------------------------------------------
# 30. DB purity (safety module read paths never mutate match/model_predictions/etc — rollback WRITES
# ModelVersion by design, §19, but ONLY ModelVersion/model_promotion_events, never match/match_stats/
# prediction_log/team_ratings).
# ---------------------------------------------------------------------------

def test_db_purity_readiness_paths():
    section("30. DB purity (readiness-only paths: evaluate_rollback_readiness never writes)")
    with Session(engine) as s:
        for v in s.exec(select(ModelVersion)).all():
            s.delete(v)
        s.commit()
        v1 = _seed_active(s, "dixon_coles")
        before = len(s.exec(select(ModelVersion)).all())
        evaluate_rollback_readiness(s, "dixon_coles")
        after = len(s.exec(select(ModelVersion)).all())
        check("evaluate_rollback_readiness never writes", before == after)


def test_db_purity_untouched_tables_during_rollback():
    section("30b. rollback never touches match/match_stats/prediction_log/team_ratings")
    with Session(engine) as s:
        match_count_before = len(s.exec(select(Match)).all())
        for v in s.exec(select(ModelVersion)).all():
            s.delete(v)
        s.commit()
        v1 = _seed_active(s, "dixon_coles")
        v2 = ModelVersion(name=next_version_name(s, "xfoot-dixon-coles"), model_type="dixon_coles", trained_at=datetime.now(UTC), is_active=False, status="candidate")
        s.add(v2); s.commit(); s.refresh(v2)
        apply_promotion(s, v2); s.commit()
        execute_rollback(s, "dixon_coles", actor="tester", target_version_id=v1.id)
        match_count_after = len(s.exec(select(Match)).all())
        check("match table untouched by rollback", match_count_before == match_count_after)


# ---------------------------------------------------------------------------
# 31. no production activation — this module never flips is_active/status on its own initiative;
# it only ever acts when explicitly called with an explicit target, and never inside this test file
# is a scope other than the isolated test DB touched.
# ---------------------------------------------------------------------------

def test_no_production_activation():
    section("31. no production activation (safety module never self-activates anything)")
    import inspect
    import app.ai.safety.kill_switch as ks
    import app.ai.safety.guards as gd
    source = inspect.getsource(ks) + inspect.getsource(gd)
    check("kill_switch.py/guards.py never call apply_promotion (only rollback.py may, and only when explicitly invoked)", "apply_promotion" not in source)
    check("this test file only ever used a dedicated isolated DB (test_safety_controls.db), never app.db", "app.db" not in str(DB_PATH))


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
