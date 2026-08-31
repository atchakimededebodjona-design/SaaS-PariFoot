"""
api/app/ai/safety/guards.py — Phase 9.1 : §12 — activation guard central.

`can_activate_production` combine DEUX sources indépendantes, jamais
fusionnées en une seule vérification opaque : le Kill Switch (ce module,
§7) et les gates critiques de Phase 9 (api/app/ai/readiness/, jamais
recalculés ici — l'appelant fournit la liste de ProductionGate déjà évaluée,
même séparation de responsabilité que kill_switch.reset()).
"""

from __future__ import annotations

from typing import Iterable

from app.ai.safety.kill_switch import KillSwitchStore, assert_production_allowed
from app.ai.safety.schemas import ActivationGuardResult


def can_activate_production(store: KillSwitchStore, readiness_gates: Iterable, *, scope: str) -> ActivationGuardResult:
    """
    §12 : vérifie au minimum readiness verdict (via les gates critiques
    fournis), Kill Switch, model eligibility (implicite : MODEL/MODEL_VERSION
    sont déjà dans CRITICAL_GATES de Phase 9), temporal integrity, provenance,
    monitoring, rollback readiness — TOUS déjà couverts par
    `readiness_gates` critiques (§13/§52 : "no compensation", un seul FAIL
    suffit à bloquer).

    `readiness_gates` : accepte tout objet avec `.name`/`.status`/`.critical`
    (duck-typing — évite un import direct de api.app.ai.readiness.schemas.
    ProductionGate ici, gardant ce module utilisable même si Phase 9 évolue
    sa représentation interne).
    """
    kill_switch_result = assert_production_allowed(store, scope)

    blocking_reasons: list[str] = []
    if not kill_switch_result.allowed:
        blocking_reasons.append(f"KILL_SWITCH: {kill_switch_result.code} — {kill_switch_result.reason}")

    checked = {}
    for gate in readiness_gates:
        if getattr(gate, "critical", False):
            checked[gate.name] = gate.status
            if gate.status != "PASS":
                blocking_reasons.append(f"{gate.name}: {gate.status}" + (f" — {gate.blocking_reason}" if getattr(gate, "blocking_reason", None) else ""))

    if not checked:
        # §3 : absence de preuve != preuve de sécurité — 0 gate critique fourni est traité comme UNKNOWN, jamais comme "rien à vérifier".
        blocking_reasons.append("READINESS: aucun gate critique fourni — évaluation Phase 9 absente ou incomplète.")

    return ActivationGuardResult(
        allowed=not blocking_reasons, blocking_reasons=blocking_reasons,
        checked_critical_gates=checked, kill_switch_status=kill_switch_result.code or "ENABLED",
    )
