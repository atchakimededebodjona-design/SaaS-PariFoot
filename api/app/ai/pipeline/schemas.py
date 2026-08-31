"""
api/app/ai/pipeline/schemas.py — Phase 8J : XFOOT END-TO-END SHADOW DECISION
PIPELINE V1 — contrat de données (§3-§6 du prompt).

Ce module ne fait QUE définir des structures. Il n'appelle ni ne modifie
Phase 6 (model_selection/calibration_engine), Phase 7 (track_record),
Phase 8A (features), Phase 8H (value) ou Phase 8I (decision) — ces phases
sont réutilisées TELLES QUELLES par orchestrator.py/shadow.py, jamais
réimplémentées (§1/§48 : inspecter avant de créer, ne jamais dupliquer).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.ai.arena.model_selection import SelectionDecision
    from app.ai.arena.calibration_engine import CalibrationResult
    from app.ai.decision.schemas import PredictionConfidence, DecisionAssessment
    from app.ai.value.schemas import ValueSignal

# ---------------------------------------------------------------------------
# §3 : PipelineInput.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OddsInput:
    """§3 : `odds_input` reste OPTIONNEL — le pipeline doit fonctionner sans."""
    odds_by_selection: dict[str, float]
    odds_timestamp: Optional[datetime] = None
    has_measured_timestamp: bool = False
    bookmaker: Optional[str] = None
    source_label: str = "UNKNOWN"  # ex. "SYNTHETIC" (§44) — jamais "the_odds_api" tant que non intégré


@dataclass(frozen=True)
class CalibrationInput:
    """§22 (héritage Phase 8I) : raw et calibrated jamais échangées silencieusement."""
    probabilities: Optional[dict[str, float]] = None  # probabilités CALIBRÉES, si disponibles
    source: str = "RAW"  # "RAW" | "CALIBRATED" — quelle version alimente la Decision/Value stage
    calibration_result: Optional["CalibrationResult"] = None  # Phase 6 — pour calibration_quality (Phase 8I)
    calibration_method_label: Optional[str] = None  # ex. "isotonic" — provenance uniquement (§5/§27)


@dataclass(frozen=True)
class FeatureSnapshotInput:
    coverage: Optional[dict] = None  # snapshot_coverage() — Phase 8A
    generated_at: Optional[datetime] = None
    snapshot_id: Optional[str] = None  # provenance uniquement (§5)
    team_mapping_confident: Optional[bool] = None


@dataclass(frozen=True)
class TemporalMetadataInput:
    cutoff_timestamp: Optional[datetime] = None
    match_kickoff: Optional[datetime] = None


@dataclass(frozen=True)
class PipelineInput:
    match_id: Optional[int]
    league: Optional[str]
    kickoff: Optional[datetime]
    as_of: Optional[datetime]  # cutoff d'évaluation — utilisé comme evaluated_at si fourni
    model: Optional[str]
    market: str
    selection: str
    probabilities: Optional[dict[str, float]]  # RAW — source canonique, jamais réécrite silencieusement
    calibration: CalibrationInput = field(default_factory=CalibrationInput)
    feature_snapshot: FeatureSnapshotInput = field(default_factory=FeatureSnapshotInput)
    temporal_metadata: TemporalMetadataInput = field(default_factory=TemporalMetadataInput)
    odds_input: Optional[OddsInput] = None
    selection_decision: Optional["SelectionDecision"] = None  # Phase 6 — model_quality
    sample_size: Optional[int] = None
    model_version: Optional[str] = None
    prediction_id: Optional[int] = None


# ---------------------------------------------------------------------------
# §5 : Provenance — NULL/UNKNOWN si absent, jamais fabriquée.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Provenance:
    model_source: Optional[str]
    model_version: Optional[str]
    calibration_source: Optional[str]
    feature_snapshot: Optional[str]
    odds_source: Optional[str]
    odds_timestamp: Optional[datetime]
    cutoff_timestamp: Optional[datetime]


# ---------------------------------------------------------------------------
# §6/§9/§14 : statuts d'étape et statut final — réutilisent le vocabulaire
# RÉEL de Phase 8H/8I partout où le concept existe déjà ; seuls "NOT_AVAILABLE"
# (aucune odds fournie), "NO_VALUE" (marché évalué, sans signal positif ni
# négatif) et "REJECTED" (erreur de traitement pipeline, batch, §30/§31)
# sont de VÉRITABLES ajouts, faute d'équivalent exact ailleurs.
# ---------------------------------------------------------------------------

VALUE_STAGE_STATUSES = (
    "EVALUATED",                       # Value Engine (Phase 8H) réellement appelé.
    "SKIPPED_NO_ODDS",                 # §10/§21 : odds_input absent — jamais une valeur fabriquée.
    "SKIPPED_DECISION_INELIGIBLE",     # §13 : Decision=INELIGIBLE -> Value jamais calculé.
    "SKIPPED_DECISION_INSUFFICIENT_DATA",
    "SKIPPED_DECISION_UNKNOWN",
    "SKIPPED_NO_MODEL_PROBABILITY",
)

PIPELINE_FINAL_STATUSES = (
    "VALUE_CANDIDATE",   # Decision=ELIGIBLE + Value=POSITIVE_VALUE (jamais "BET", §25 Phase 8H réutilisé).
    "NO_VALUE",           # Decision=ELIGIBLE + Value évalué mais NEUTRAL/NEGATIVE_VALUE.
    "RESEARCH_ONLY",       # Decision=RESEARCH_ONLY (Phase 8I) — jamais promu, quel que soit le Value.
    "INSUFFICIENT_DATA",    # Decision=INSUFFICIENT_DATA (Phase 8I), ou Value=INSUFFICIENT_DATA (Phase 8H).
    "INELIGIBLE",             # Decision=INELIGIBLE (Phase 8I) — fuite temporelle, modèle instable, probabilité invalide.
    "UNKNOWN",                 # Decision=UNKNOWN (Phase 8I).
    "NOT_AVAILABLE",             # Decision=ELIGIBLE mais Value jamais évalué (pas d'odds/probabilité manquante).
    "TEMPORALLY_UNSAFE",           # Value=TEMPORALLY_UNSAFE (Phase 8H) — ne devrait survenir que si odds/decision divergent.
    "INVALID_ODDS",                  # Value=INVALID_ODDS (Phase 8H).
    "REJECTED",                        # Erreur de traitement PIPELINE (exception isolée, §30/§31) — jamais un verdict de donnée.
)


# ---------------------------------------------------------------------------
# §4 : PipelineAssessment.
# ---------------------------------------------------------------------------

@dataclass
class PipelineAssessment:
    match_id: Optional[int]
    market: str
    prediction: dict  # {"selection", "probability_source", "probabilities"}
    quality: Optional["PredictionConfidence"]     # Phase 8I, réutilisé tel quel — None seulement si error != None
    decision: Optional["DecisionAssessment"]      # Phase 8I, réutilisé tel quel — None seulement si error != None
    value: Optional["ValueSignal"]                # Phase 8H, réutilisé tel quel — None si non évalué
    value_stage_status: str                        # VALUE_STAGE_STATUSES
    final_status: str                                # PIPELINE_FINAL_STATUSES
    reasons: list[str]
    provenance: Provenance
    evaluated_at: datetime
    error: Optional[str] = None                        # §30/§31 : isolation d'erreur batch — repr() de l'exception, None si succès
