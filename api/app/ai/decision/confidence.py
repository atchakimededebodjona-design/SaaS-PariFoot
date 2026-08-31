"""
api/app/ai/decision/confidence.py — Phase 8I : Confidence Framework (§2/§10-
§12/§20/§26 du prompt).

PRINCIPE (§2, jamais violé ici) : confidence != edge, confidence !=
probability, confidence != model_score. `overall_status` est une synthèse
CONSERVATIVE des 6 dimensions indépendantes de quality.py — AUCUNE
compensation n'est permise (§12) : une dimension critique dégradée ne peut
jamais être rattrapée par une autre dimension excellente.
"""

from __future__ import annotations

from typing import Optional

from app.ai.decision.quality import (
    assess_model_quality, assess_calibration_quality, assess_data_quality,
    assess_sample_quality, assess_market_quality, classify_temporal_status,
)
from app.ai.decision.schemas import PredictionConfidence, QualityDimensions

OVERALL_CONFIDENCE_STATUSES = ("HIGH", "MEDIUM", "LOW", "UNKNOWN", "INELIGIBLE")


def compute_overall_confidence(dims: QualityDimensions) -> str:
    """
    §10/§11/§12 — ordre de décision FIXE, documenté, jamais réordonné à la
    volée (déterminisme, §35) :

    1. temporal_quality == FUTURE_INFORMATION -> INELIGIBLE (fuite avérée,
       aucune autre dimension ne peut compenser, §12/§33).
    2. temporal_quality == UNKNOWN -> INELIGIBLE (§11 : règle explicite,
       jamais une confiance calculée sur un statut temporel inconnu).
    3. model_quality == LOW -> LOW (un modèle jugé instable ne peut JAMAIS
       produire une confiance HIGH/MEDIUM, quelle que soit la qualité des
       autres dimensions — §11 exemple exact du prompt).
    4. N'IMPORTE QUELLE dimension restante == UNKNOWN (model/calibration/
       data/sample) -> UNKNOWN (§11 : ne jamais augmenter artificiellement
       la confiance faute d'information — jamais un défaut optimiste).
    5. temporal_quality == HISTORICAL_UNVERIFIED -> plafonné à MEDIUM au
       mieux (§41 : historical_unverified != temporally_verified, jamais
       promu à HIGH même si tout le reste est excellent) ; MEDIUM
       uniquement si model/data=HIGH et sample=SUFFICIENT, sinon LOW.
    6. calibration_quality != CALIBRATED -> plafonné à MEDIUM (§5 : sans
       calibration statistiquement validée, jamais "high confidence") ;
       MEDIUM uniquement si model=HIGH, data in (HIGH,MEDIUM), sample in
       (SUFFICIENT,LIMITED), sinon LOW.
    7. Sinon (temporal=TEMPORALLY_VERIFIED, calibration=CALIBRATED) :
       HIGH seulement si model=HIGH ET data=HIGH ET sample=SUFFICIENT ;
       MEDIUM si model/data au moins MEDIUM et sample au moins LIMITED ;
       sinon LOW.

    market_quality n'intervient PAS dans cette synthèse par construction
    (§9 : NOT_AVAILABLE est l'état ATTENDU tant qu'aucun fournisseur d'odds
    n'est connecté — le pénaliser dégraderait systématiquement TOUTE
    prédiction aujourd'hui, ce qui masquerait le signal des 5 autres
    dimensions, réellement disponibles).
    """
    if dims.temporal_quality == "FUTURE_INFORMATION":
        return "INELIGIBLE"
    if dims.temporal_quality == "UNKNOWN":
        return "INELIGIBLE"
    if dims.model_quality == "LOW":
        return "LOW"
    if any(d == "UNKNOWN" for d in (dims.model_quality, dims.calibration_quality, dims.data_quality, dims.sample_quality)):
        return "UNKNOWN"

    strong_core = dims.model_quality == "HIGH" and dims.data_quality == "HIGH" and dims.sample_quality == "SUFFICIENT"
    decent_core = dims.model_quality in ("HIGH", "MEDIUM") and dims.data_quality in ("HIGH", "MEDIUM") and dims.sample_quality in ("SUFFICIENT", "LIMITED")

    if dims.temporal_quality == "HISTORICAL_UNVERIFIED":
        return "MEDIUM" if strong_core else "LOW"
    if dims.calibration_quality != "CALIBRATED":
        return "MEDIUM" if decent_core else "LOW"
    if strong_core:
        return "HIGH"
    if decent_core:
        return "MEDIUM"
    return "LOW"


def assess_prediction_quality(
    *,
    selection_decision=None, calibration_result=None,
    feature_coverage: Optional[dict] = None, team_mapping_confident: Optional[bool] = None,
    odds_timestamp=None, cutoff_timestamp=None, match_kickoff=None, has_measured_odds_timestamp: bool = False,
    sample_size: Optional[int] = None, min_sample_required: int = 100, limited_sample_floor: int = 30,
    odds_by_selection: Optional[dict[str, float]] = None, bookmaker_count: Optional[int] = None,
) -> PredictionConfidence:
    """§24 : assess_prediction_quality() — assemble les 6 dimensions
    indépendantes (quality.py, jamais recalculées différemment ici) puis la
    synthèse conservative (compute_overall_confidence). `research_score` est
    None par défaut (§26 : ne pas créer de score numérique sans besoin réel
    démontré) — voir compute_research_score() pour la version expérimentale
    explicite, jamais appelée automatiquement ici."""
    dims = QualityDimensions(
        model_quality=assess_model_quality(selection_decision),
        calibration_quality=assess_calibration_quality(calibration_result),
        data_quality=assess_data_quality(feature_coverage, team_mapping_confident),
        temporal_quality=classify_temporal_status(odds_timestamp, cutoff_timestamp, match_kickoff, has_measured_odds_timestamp),
        sample_quality=assess_sample_quality(sample_size, min_sample_required, limited_sample_floor),
        market_quality=assess_market_quality(odds_by_selection, bookmaker_count),
    )
    return PredictionConfidence(quality_dimensions=dims, overall_status=compute_overall_confidence(dims))


# ---------------------------------------------------------------------------
# §20 : model disagreement — INFORMATIF uniquement, jamais transformé en
# confiance.
# ---------------------------------------------------------------------------

def compute_model_disagreement(probabilities_by_model: dict[str, float]) -> Optional[float]:
    """§20 : max(p) - min(p) parmi les probabilités (même sélection/marché)
    de plusieurs modèles — None si moins de 2 modèles fournis (rien à
    comparer). Un désaccord FAIBLE n'est JAMAIS transformé automatiquement
    en confiance haute (§20 : dimension informative, jamais agrégée dans
    compute_overall_confidence ci-dessus)."""
    if len(probabilities_by_model) < 2:
        return None
    values = list(probabilities_by_model.values())
    return max(values) - min(values)


# ---------------------------------------------------------------------------
# §26 : research_score EXPÉRIMENTAL — jamais appelé "confidence", jamais
# utilisé en production, jamais présenté comme statistiquement validé.
# ---------------------------------------------------------------------------

_RESEARCH_SCORE_WEIGHTS = {"HIGH": 1.0, "MEDIUM": 0.5, "LOW": 0.0, "SUFFICIENT": 1.0, "LIMITED": 0.5, "INSUFFICIENT": 0.0, "CALIBRATED": 1.0, "UNCALIBRATED": 0.3, "INSUFFICIENT_DATA": 0.0, "TEMPORALLY_VERIFIED": 1.0, "HISTORICAL_UNVERIFIED": 0.4, "FUTURE_INFORMATION": 0.0, "UNKNOWN": 0.0, "NOT_AVAILABLE": None}


def compute_research_score(dims: QualityDimensions) -> Optional[float]:
    """
    §26 : score EXPÉRIMENTAL [0,1], moyenne non pondérée des 5 dimensions
    convertibles (market_quality exclu — NOT_AVAILABLE en est l'état
    attendu, voir compute_overall_confidence). AUCUNE validation statistique
    n'existe pour ces poids (choisis arbitrairement, documentés comme tels)
    — CE N'EST PAS UNE CONFIANCE, jamais utilisé par compute_overall_
    confidence ni par eligibility.py, jamais exposé comme un seuil de
    production. Fonction séparée, appelée explicitement uniquement si un
    besoin réel de tri approximatif existe (ex. exploration manuelle).
    """
    scores = [
        _RESEARCH_SCORE_WEIGHTS.get(dims.model_quality), _RESEARCH_SCORE_WEIGHTS.get(dims.calibration_quality),
        _RESEARCH_SCORE_WEIGHTS.get(dims.data_quality), _RESEARCH_SCORE_WEIGHTS.get(dims.temporal_quality),
        _RESEARCH_SCORE_WEIGHTS.get(dims.sample_quality),
    ]
    scores = [s for s in scores if s is not None]
    if not scores:
        return None
    return sum(scores) / len(scores)
