"""
api/app/ai/readiness/schemas.py — Phase 9 : contrat de données pur (§2/§3
du prompt). Aucune dataclasse ici n'accède à la DB, au réseau, ni à un
fournisseur externe. RESEARCH ONLY — jamais importé par main.py/scheduler.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# ---------------------------------------------------------------------------
# §3 : vocabulaire de gate — UNKNOWN n'est JAMAIS converti en PASS ailleurs.
# ---------------------------------------------------------------------------

GATE_STATUSES = ("PASS", "FAIL", "UNKNOWN", "NOT_AVAILABLE", "CONDITIONAL", "NOT_APPLICABLE")

# §2 : les 20 dimensions minimales de la matrice, + API_EXPOSURE/FRONTEND_EXPOSURE
# (§29/§30 du prompt — hors de la liste des 20, ajoutés en plus du minimum imposé),
# + KILL_SWITCH (Phase 9.1 — le mécanisme n'existait pas encore quand cette liste a
# été écrite en Phase 9 ; ajouté ici plutôt que dans un doublon de matrice, §34 Phase 9.1).
GATE_DIMENSIONS = (
    "MODEL", "MODEL_VERSION", "DATA", "FEATURES", "TEMPORAL_INTEGRITY", "CALIBRATION",
    "SAMPLE_SIZE", "STATISTICAL_EVIDENCE", "ODDS", "VALUE", "DECISION", "SHADOW",
    "TRACK_RECORD", "MONITORING", "PROVENANCE", "DATABASE_SAFETY", "ROLLBACK",
    "OBSERVABILITY", "SECURITY", "LEGAL_PROVIDER_STATUS",
    "API_EXPOSURE", "FRONTEND_EXPOSURE", "KILL_SWITCH",
)

# §5 : gates critiques (minimum imposé par le prompt) — un FAIL/UNKNOWN/
# NOT_AVAILABLE sur l'un d'eux bloque à lui seul PRODUCTION_READY (§4/§52 :
# "no compensation"). ODDS n'est critique QUE si le Value Engine/betting
# doit être activé (§15) — traité séparément dans verdict.py, jamais ajouté
# ici en dur (dépend du mode d'activation visé).
# KILL_SWITCH ajouté aux gates critiques en Phase 9.1 : une fois le mécanisme réel
# disponible, aucune activation ne doit pouvoir l'ignorer (§12 Phase 9.1 : "si un
# seul critical gate échoue -> BLOCK").
CRITICAL_GATES = (
    "TEMPORAL_INTEGRITY", "MODEL", "MODEL_VERSION", "PROVENANCE",
    "DATABASE_SAFETY", "ROLLBACK", "MONITORING", "TRACK_RECORD", "KILL_SWITCH",
)


@dataclass(frozen=True)
class ProductionGate:
    name: str                          # un de GATE_DIMENSIONS
    status: str                        # un de GATE_STATUSES
    evidence: dict = field(default_factory=dict)
    blocking_reason: Optional[str] = None
    required_action: Optional[str] = None
    critical: bool = False


# ---------------------------------------------------------------------------
# §4 : verdict final.
# ---------------------------------------------------------------------------

FINAL_VERDICTS = ("PRODUCTION_READY", "CONDITIONALLY_READY", "NO_GO", "BLOCKED")

# ---------------------------------------------------------------------------
# §22 : modes d'activation — conceptuels, jamais appliqués automatiquement.
# ---------------------------------------------------------------------------

ACTIVATION_MODES = (
    "MODE_0_DISABLED",
    "MODE_1_SHADOW_ONLY",
    "MODE_2_INTERNAL_RESEARCH",
    "MODE_3_LIMITED_PRODUCTION",
    "MODE_4_FULL_PRODUCTION",
)

# ---------------------------------------------------------------------------
# §7 : promotion — jamais automatique.
# ---------------------------------------------------------------------------

PROMOTION_ELIGIBILITY_STATUSES = ("PROMOTION_ELIGIBLE", "PROMOTION_NOT_ELIGIBLE")


@dataclass(frozen=True)
class ModelPromotionReadiness:
    model_type: str
    status: str  # PROMOTION_ELIGIBILITY_STATUSES
    reasons: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# §24 : kill switch — triggers connus, JAMAIS déclenchés automatiquement ici
# (évaluation uniquement : le mécanisme d'exécution réel est un TRAVAIL
# FUTUR, explicitement absent du code aujourd'hui — voir gates.py::gate_
# observability).
# ---------------------------------------------------------------------------

KILL_SWITCH_TRIGGERS = (
    "TEMPORAL_LEAK", "MODEL_MISMATCH", "PROVENANCE_MISSING", "STORE_CORRUPTION",
    "PROBABILITY_MISMATCH", "DECISION_MISMATCH", "PRODUCTION_DB_MUTATION", "ODDS_TIMESTAMP_INVALID",
)

# ---------------------------------------------------------------------------
# §39 : checklist d'activation — AUCUNE case cochée sans preuve (`evidence`).
# ---------------------------------------------------------------------------

CHECKLIST_ITEMS = (
    "model_approved", "artifact_verified", "feature_set_verified", "temporal_integrity_verified",
    "calibration_verified", "statistical_evidence_sufficient", "prospective_track_record_sufficient",
    "odds_verified", "value_engine_validated", "decision_layer_validated", "shadow_healthy",
    "monitoring_healthy", "rollback_tested", "kill_switch_tested", "database_isolation_verified",
    "api_exposure_verified", "frontend_exposure_verified", "security_verified", "legal_provider_review_complete",
)


@dataclass(frozen=True)
class ChecklistItem:
    key: str                   # un de CHECKLIST_ITEMS
    checked: bool
    evidence: str               # JAMAIS vide — la preuve (ou l'absence documentée) qui justifie l'état


# ---------------------------------------------------------------------------
# §38/§40 : rapport final.
# ---------------------------------------------------------------------------

@dataclass
class ProductionReadinessAssessment:
    generated_at: datetime
    as_of: datetime
    gates: list[ProductionGate]
    critical_gate_failures: list[str]
    final_verdict: str                              # FINAL_VERDICTS
    recommended_mode: str                             # ACTIVATION_MODES
    promotion_readiness: list[ModelPromotionReadiness]
    checklist: list[ChecklistItem]
    open_risks: list[str] = field(default_factory=list)
    blocking_conditions: list[str] = field(default_factory=list)
    # §40 : REQUIRED NOW / REQUIRED BEFORE PRODUCTION / OPTIONAL FUTURE — jamais fusionnées.
    conditions_required_now: list[str] = field(default_factory=list)
    conditions_required_before_production: list[str] = field(default_factory=list)
    conditions_optional_future: list[str] = field(default_factory=list)
    phase10_readiness: dict = field(default_factory=dict)
    db_safety: dict = field(default_factory=dict)     # §27/§44 : matrice READ/WRITE/FORBIDDEN + comparaison avant/après
