"""
api/app/ai/decision/schemas.py — Phase 8I : XFOOT PREDICTION QUALITY &
DECISION LAYER V1.

Contrat de données PUR (§2/§17 du prompt). Aucune dataclasse ici n'accède à
la DB, au réseau, ni à un fournisseur externe. RESEARCH + SHADOW ONLY —
aucune de ces classes n'est importée par la production (main.py,
scheduler.py, orchestrator.py, service.py, ensemble.py, models_common.py,
promotion.py, api/app/ai/value/).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# ---------------------------------------------------------------------------
# §3 : dimensions de qualité, évaluées INDÉPENDAMMENT.
# ---------------------------------------------------------------------------

QUALITY_DIMENSION_NAMES = (
    "model_quality", "calibration_quality", "data_quality",
    "temporal_quality", "sample_quality", "market_quality",
)


@dataclass(frozen=True)
class QualityDimensions:
    model_quality: str          # HIGH | MEDIUM | LOW | UNKNOWN
    calibration_quality: str    # CALIBRATED | UNCALIBRATED | INSUFFICIENT_DATA | UNKNOWN
    data_quality: str           # HIGH | MEDIUM | LOW | UNKNOWN
    temporal_quality: str       # TEMPORALLY_VERIFIED | HISTORICAL_UNVERIFIED | FUTURE_INFORMATION | UNKNOWN
    sample_quality: str         # SUFFICIENT | LIMITED | INSUFFICIENT | UNKNOWN
    market_quality: str         # HIGH | MEDIUM | LOW | NOT_AVAILABLE | UNKNOWN


# ---------------------------------------------------------------------------
# §10/§11 : synthèse de confiance — jamais confidence=edge/probability/score.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PredictionConfidence:
    quality_dimensions: QualityDimensions
    overall_status: str  # HIGH | MEDIUM | LOW | UNKNOWN | INELIGIBLE
    research_score: Optional[float] = None  # §26 : EXPÉRIMENTAL uniquement, jamais "confidence", jamais production


# ---------------------------------------------------------------------------
# §14/§15 : hard gates.
# ---------------------------------------------------------------------------

GATE_STATUSES = ("PASS", "FAIL", "UNKNOWN", "NOT_APPLICABLE")
GATE_NAMES = ("GATE_DATA", "GATE_MODEL", "GATE_CALIBRATION", "GATE_TEMPORAL", "GATE_SAMPLE", "GATE_MARKET")


@dataclass(frozen=True)
class Gate:
    name: str            # un de GATE_NAMES
    status: str          # un de GATE_STATUSES
    reason: Optional[str] = None  # decision.REJECTION_REASONS, None si status == PASS/NOT_APPLICABLE


# ---------------------------------------------------------------------------
# §13 : éligibilité.
# ---------------------------------------------------------------------------

DECISION_ELIGIBILITY_STATUSES = ("ELIGIBLE", "RESEARCH_ONLY", "INELIGIBLE", "INSUFFICIENT_DATA", "UNKNOWN")


@dataclass(frozen=True)
class DecisionEligibility:
    status: str  # un de DECISION_ELIGIBILITY_STATUSES
    gates: list[Gate]
    reasons: list[str] = field(default_factory=list)  # TOUTES les raisons trouvées, jamais seulement la première (§15)


# ---------------------------------------------------------------------------
# §17 : objet de sortie unique.
# ---------------------------------------------------------------------------

@dataclass
class DecisionAssessment:
    prediction_id: Optional[int]
    model: Optional[str]
    market: str
    probability: Optional[float]
    confidence: PredictionConfidence
    quality_dimensions: QualityDimensions
    gates: list[Gate]
    eligibility: str  # DECISION_ELIGIBILITY_STATUSES
    reasons: list[str]
    research_only: bool
    timestamp: datetime  # timestamp D'ÉVALUATION, fourni ou généré explicitement — jamais utilisé pour fabriquer une donnée historique (§17)


# ---------------------------------------------------------------------------
# §27 : seuils — RESEARCH_DEFAULT, jamais des seuils de production.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DecisionThresholds:
    min_sample_size: int
    limited_sample_floor: int    # sous ce seuil : INSUFFICIENT plutôt que LIMITED
    max_feature_age_hours: float
    min_probability: float


RESEARCH_DEFAULT_THRESHOLDS = DecisionThresholds(
    min_sample_size=100,           # même ordre de grandeur que MIN_BENCHMARK_SAMPLE_SIZE (Phase 5), jamais un seuil de production dérivé de ce module
    limited_sample_floor=30,
    max_feature_age_hours=float("inf"),
    min_probability=0.0,
)
