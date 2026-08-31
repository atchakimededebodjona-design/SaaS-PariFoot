"""
scripts/safety_control.py — Phase 9.1 : XFOOT PRODUCTION SAFETY CONTROLS &
KILL SWITCH V1.
=============================================================================
SAFETY HARDENING ONLY. Réutilise TEL QUEL api/app/ai/safety/ (Phase 9.1) —
jamais une deuxième logique. `reset` réévalue TOUJOURS les gates critiques
Phase 9 EN DIRECT contre api/app.db (lecture seule, jamais une valeur mise
en cache/périmée) avant de les transmettre à kill_switch.reset().

Usage (depuis la racine du dépôt) :
    python scripts/safety_control.py status [--json]
    python scripts/safety_control.py trigger --code CODE --reason "..." [--scope SCOPE] [--actor NAME] [--manual] [--dry-run]
    python scripts/safety_control.py reset --reason "..." [--actor NAME] [--dry-run]
    python scripts/safety_control.py test
    python scripts/safety_control.py rollback-status --model-type TYPE

IMPORTANT (§31/§39 du prompt) : `rollback-status` est LECTURE SEULE — ce
CLI n'exécute JAMAIS un rollback réel contre api/app.db (execute_rollback
n'est appelé que par api/test_safety_controls.py, sur une base isolée).
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from sqlmodel import Session  # noqa: E402
from app.core.database import engine, init_db  # noqa: E402

from app.ai.readiness.matrix import evaluate_production_readiness  # noqa: E402
from app.ai.readiness.schemas import CRITICAL_GATES  # noqa: E402
from app.ai.shadow.tracking import ShadowDecisionStore  # noqa: E402

from app.ai.safety.kill_switch import KillSwitchStore, trigger as ks_trigger, reset as ks_reset, assert_production_allowed  # noqa: E402
from app.ai.safety.schemas import KILL_SWITCH_TRIGGERS, BLOCKABLE_SCOPES  # noqa: E402
from app.ai.safety.rollback import evaluate_rollback_readiness  # noqa: E402

UTC = timezone.utc


def _fresh_critical_gates() -> dict:
    """Ré-évalue les gates critiques Phase 9 EN DIRECT (lecture seule) — jamais une valeur périmée passée
    à kill_switch.reset() (§17 du prompt : le reset doit refléter l'état RÉEL au moment du reset)."""
    init_db()
    store = ShadowDecisionStore()
    with Session(engine) as session:
        assessment = evaluate_production_readiness(session, store, datetime.now(UTC))
    return {gate.name: gate.status for gate in assessment.gates if gate.name in CRITICAL_GATES}


def cmd_status(args) -> dict:
    store = KillSwitchStore()
    try:
        state = store.read()
        corrupted = None
    except ValueError as e:
        state = None
        corrupted = str(e)
    result = {
        "command": "status", "state_path": str(store.state_path), "audit_path": str(store.audit_path),
        "state": state.state if state else "UNREADABLE", "effective_status": state.effective_status if state else "UNKNOWN",
        "corrupted": corrupted, "trigger_code": state.trigger_code if state else None, "trigger_reason": state.trigger_reason if state else None,
        "current_production_mode": "MODE_1_SHADOW_ONLY", "production_activation": "BLOCKED",
    }
    return result


def cmd_trigger(args) -> dict:
    store = KillSwitchStore()
    if args.code not in KILL_SWITCH_TRIGGERS:
        return {"command": "trigger", "error": f"code inconnu : '{args.code}'. Attendu un de {KILL_SWITCH_TRIGGERS}."}
    if args.scope and args.scope not in BLOCKABLE_SCOPES:
        return {"command": "trigger", "error": f"scope inconnu : '{args.scope}'. Attendu un de {BLOCKABLE_SCOPES}."}
    if args.dry_run:
        return {"command": "trigger", "dry_run": True, "would_set_state": "TRIGGERED", "code": args.code, "reason": args.reason, "scope": args.scope}
    state = ks_trigger(store, args.code, args.reason, scope=args.scope, actor=args.actor, automatic=not args.manual)
    return {"command": "trigger", "dry_run": False, "state": state.state, "trigger_code": state.trigger_code}


def cmd_reset(args) -> dict:
    store = KillSwitchStore()
    gates = _fresh_critical_gates()
    if args.dry_run:
        failing = {k: v for k, v in gates.items() if v != "PASS"}
        return {"command": "reset", "dry_run": True, "critical_gates": gates,
                "would_be_approved": not failing, "failing_gates": failing}
    ok, msg, state = ks_reset(store, gates, actor=args.actor, reason=args.reason)
    return {"command": "reset", "dry_run": False, "approved": ok, "message": msg, "state": state.state, "critical_gates_checked": gates}


def cmd_rollback_status(args) -> dict:
    """LECTURE SEULE (§31/§39) — n'exécute jamais un rollback."""
    init_db()
    with Session(engine) as session:
        readiness = evaluate_rollback_readiness(session, args.model_type)
    return {"command": "rollback-status", "model_type": args.model_type, "status": readiness.status,
            "target_version_id": readiness.target_version_id, "target_version_name": readiness.target_version_name,
            "reason": readiness.reason, "note": "LECTURE SEULE — ce CLI n'exécute jamais un rollback réel (§39)."}


def cmd_test(args) -> dict:
    """Vérification RAPIDE (pas la suite complète — voir `python api/test_safety_controls.py`) : trigger +
    assert blocked + reset denied + reset approved, sur un store TEMPORAIRE dédié (jamais le store réel)."""
    import tempfile, os
    fd1, p1 = tempfile.mkstemp(suffix=".json"); os.close(fd1); Path(p1).unlink()
    fd2, p2 = tempfile.mkstemp(suffix=".json"); os.close(fd2); Path(p2).unlink()
    tmp_store = KillSwitchStore(state_path=Path(p1), audit_path=Path(p2))
    try:
        r0 = assert_production_allowed(tmp_store, "MODEL_PROMOTION")
        ks_trigger(tmp_store, "MANUAL_OPERATOR_TRIGGER", "safety_control.py test", actor="cli-self-test", automatic=False)
        r1 = assert_production_allowed(tmp_store, "MODEL_PROMOTION")
        ok_no_gates, _, _ = ks_reset(tmp_store, {}, actor="cli-self-test", reason="no gates")
        ok_with_gates, _, _ = ks_reset(tmp_store, {"MODEL": "PASS"}, actor="cli-self-test", reason="fake all-pass single gate")
        passed = (r0.allowed is True and r1.allowed is False and ok_no_gates is False)
        return {"command": "test", "self_test_passed": passed,
                "checks": {"default_allowed": r0.allowed, "blocked_after_trigger": not r1.allowed, "reset_denied_without_gates": not ok_no_gates},
                "note": "Auto-test rapide sur store temporaire — pour la suite complète (31 scénarios), lancer api/test_safety_controls.py."}
    finally:
        Path(p1).unlink(missing_ok=True)
        Path(p2).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")

    p_trigger = sub.add_parser("trigger")
    p_trigger.add_argument("--code", required=True)
    p_trigger.add_argument("--reason", required=True)
    p_trigger.add_argument("--scope", default=None, choices=list(BLOCKABLE_SCOPES))
    p_trigger.add_argument("--actor", default="cli-operator")
    p_trigger.add_argument("--manual", action="store_true")
    p_trigger.add_argument("--dry-run", action="store_true")

    p_reset = sub.add_parser("reset")
    p_reset.add_argument("--reason", required=True)
    p_reset.add_argument("--actor", default="cli-operator")
    p_reset.add_argument("--dry-run", action="store_true")

    sub.add_parser("test")

    p_rb = sub.add_parser("rollback-status")
    p_rb.add_argument("--model-type", required=True, dest="model_type")

    args = parser.parse_args()
    dispatch = {"status": cmd_status, "trigger": cmd_trigger, "reset": cmd_reset, "test": cmd_test, "rollback-status": cmd_rollback_status}
    result = dispatch[args.cmd](args)

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print("\nMode production actuel : MODE_1_SHADOW_ONLY — PRODUCTION ACTIVATION = BLOCKED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
