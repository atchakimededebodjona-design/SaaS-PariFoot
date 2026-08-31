"""
api/app/ai/safety/schemas.py — Phase 9.1 : contrat de données pur (§2/§3/
§14 du prompt). Aucune dataclasse ici n'accède à la DB, au réseau, ni au
disque. RESEARCH/SAFETY ONLY — jamais importé par main.py/scheduler.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# ---------------------------------------------------------------------------
# §2 : vocabulaire d'état — SEULES "ENABLED"/"TRIGGERED" sont des valeurs
# RÉELLEMENT PERSISTÉES dans le fichier d'état (un seul champ source de
# vérité, jamais deux qui pourraient diverger — même principe que
# FEATURE_REGISTRY.traffic_light(), Phase 8A). "RESET_REQUIRED" est le
# libellé DÉRIVÉ (jamais stocké séparément) affiché à l'appelant quand
# state == "TRIGGERED" : un switch déclenché est TOUJOURS, par construction,
# en attente d'un reset explicite (§16) — il n'existe pas d'état
# intermédiaire distinct où il serait "triggered mais pas encore
# reset_required". Les 3 tokens du prompt (§2 : "ou vocabulaire équivalent")
# restent donc tous les 3 des valeurs réellement observables par un
# appelant (via `state` ou `effective_status`), sans dupliquer la source de
# vérité.
# ---------------------------------------------------------------------------

KILL_SWITCH_PERSISTED_STATES = ("ENABLED", "TRIGGERED")
KILL_SWITCH_STATES = ("ENABLED", "TRIGGERED", "RESET_REQUIRED")

# §3 : modes d'échec de LECTURE — distincts du vocabulaire d'état ci-dessus.
# Un échec de lecture n'est JAMAIS un état "ENABLED" par défaut (§3 :
# "jamais continuer par défaut").
KILL_SWITCH_READ_FAILURE_MODES = ("UNKNOWN", "CORRUPTED", "UNREADABLE", "INVALID")

# ---------------------------------------------------------------------------
# §14 : triggers minimum — remplace/étend le sous-ensemble documenté en
# Phase 9 (api/app/ai/readiness/schemas.py::KILL_SWITCH_TRIGGERS, qui ne
# décrivait qu'un DESIGN conceptuel, jamais un mécanisme réel). Cette liste
# est désormais la source canonique — voir readiness/schemas.py qui
# continue de porter sa propre liste historique (rapport Phase 9, jamais
# réécrit rétroactivement) mais renvoie explicitement ici pour le mécanisme réel.
# ---------------------------------------------------------------------------

KILL_SWITCH_TRIGGERS = (
    "TEMPORAL_LEAK", "MODEL_MISMATCH", "FEATURE_MISMATCH", "PROBABILITY_MISMATCH",
    "DECISION_MISMATCH", "PROVENANCE_MISSING", "STORE_CORRUPTION", "DATABASE_MUTATION",
    "UNEXPECTED_MODEL_VERSION", "INVALID_PROBABILITY", "PIPELINE_CRITICAL_ERROR", "MANUAL_OPERATOR_TRIGGER",
)

# §15 : AUTOMATIC vs MANUAL — seul MANUAL_OPERATOR_TRIGGER est manuel par nature ;
# tous les autres DOIVENT pouvoir être déclenchés automatiquement par un appelant.
MANUAL_TRIGGERS = ("MANUAL_OPERATOR_TRIGGER",)
AUTOMATIC_TRIGGERS = tuple(t for t in KILL_SWITCH_TRIGGERS if t not in MANUAL_TRIGGERS)

# ---------------------------------------------------------------------------
# §5 : scope — ce que le Kill Switch peut bloquer. SHADOW_RESEARCH n'est
# JAMAIS bloqué par ce mécanisme (§5 : "le Shadow research peut continuer à
# fonctionner si cela ne crée aucun risque") — absent volontairement de
# BLOCKABLE_SCOPES.
# ---------------------------------------------------------------------------

BLOCKABLE_SCOPES = ("PRODUCTION_PREDICTION_ACTIVATION", "MODEL_PROMOTION", "VALUE_SIGNAL_PRODUCTION")
NEVER_BLOCKED_SCOPES = ("SHADOW_RESEARCH",)

# ---------------------------------------------------------------------------
# §8 : codes de blocage — jamais un secret, jamais un message ambigu.
# ---------------------------------------------------------------------------

BLOCK_CODES = (
    "KILL_SWITCH_ACTIVE", "KILL_SWITCH_UNREADABLE", "KILL_SWITCH_CORRUPTED", "KILL_SWITCH_INVALID",
    "CRITICAL_READINESS_GATE_NOT_PASS", "SCOPE_BLOCKED",
)

# ---------------------------------------------------------------------------
# §27 : audit trail — event_type vocabulaire.
# ---------------------------------------------------------------------------

AUDIT_EVENT_TYPES = (
    "TRIGGERED", "BLOCK", "RESET_REQUESTED", "RESET_APPROVED", "RESET_DENIED",
    "ROLLBACK_EXECUTED", "ROLLBACK_NOOP", "ROLLBACK_DENIED",
)


@dataclass(frozen=True)
class KillSwitchState:
    """§2 : snapshot immuable de l'état persisté — jamais muté en place (une
    nouvelle instance à chaque changement, même discipline que
    ShadowResolution, Phase 8K)."""
    state: str                              # KILL_SWITCH_PERSISTED_STATES
    triggered_at: Optional[datetime] = None
    trigger_code: Optional[str] = None      # KILL_SWITCH_TRIGGERS, si state == TRIGGERED
    trigger_reason: Optional[str] = None
    trigger_scope: Optional[str] = None     # BLOCKABLE_SCOPES, si applicable
    trigger_actor: Optional[str] = None
    trigger_automatic: Optional[bool] = None

    @property
    def effective_status(self) -> str:
        """§2 : le 3e token du vocabulaire (RESET_REQUIRED), DÉRIVÉ, jamais stocké séparément."""
        return "RESET_REQUIRED" if self.state == "TRIGGERED" else "ENABLED"


@dataclass(frozen=True)
class AuditEvent:
    event_type: str      # AUDIT_EVENT_TYPES
    scope: Optional[str]
    code: Optional[str]  # KILL_SWITCH_TRIGGERS ou BLOCK_CODES, selon event_type
    reason: str
    actor: str
    timestamp: datetime
    model_version: Optional[str] = None


@dataclass(frozen=True)
class ProductionAllowedResult:
    """§7 : résultat de assert_production_allowed — ALLOW ou BLOCK explicite, jamais un troisième état ambigu."""
    allowed: bool
    code: Optional[str] = None       # BLOCK_CODES si allowed=False, None si allowed=True
    reason: Optional[str] = None
    scope: Optional[str] = None
    timestamp: Optional[datetime] = None


@dataclass(frozen=True)
class ActivationGuardResult:
    """§12 : can_activate_production — combine Kill Switch + gates critiques Phase 9."""
    allowed: bool
    blocking_reasons: list[str] = field(default_factory=list)
    checked_critical_gates: dict = field(default_factory=dict)
    kill_switch_status: Optional[str] = None


# ---------------------------------------------------------------------------
# §18-23 : rollback.
# ---------------------------------------------------------------------------

ROLLBACK_STATUSES = ("ROLLBACK_AVAILABLE", "ROLLBACK_NOT_AVAILABLE")


@dataclass(frozen=True)
class RollbackReadiness:
    status: str  # ROLLBACK_STATUSES
    model_type: str
    target_version_id: Optional[int] = None
    target_version_name: Optional[str] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class RollbackResult:
    status: str  # "EXECUTED" | "NOOP_ALREADY_ACTIVE" | "DENIED"
    model_type: str
    restored_version_id: Optional[int] = None
    previous_active_version_id: Optional[int] = None
    reason: Optional[str] = None
