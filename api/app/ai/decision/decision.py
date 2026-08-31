"""
api/app/ai/decision/decision.py — Phase 8I : orchestration finale
(§17-§19, §23, §25, §29-§31 du prompt).

assess_decision() est le SEUL point d'entrée recommandé : il combine la
validation de probabilité (§18/§19), les 6 dimensions de qualité
(confidence.py, jamais recalculées différemment), et les hard gates
(eligibility.py) en un unique DecisionAssessment (§17).

RESEARCH + SHADOW ONLY — aucune fonction ici n'écrit en base, n'appelle un
fournisseur d'odds, ni ne connecte le Value Engine (api/app/ai/value/) :
to_value_engine_input() documente uniquement la FORME du contrat de sortie
(§30 : "ne pas connecter les modules").
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.ai.arena.ensemble import MARKET_OUTCOME_KEYS

from app.ai.decision.schemas import (
    DecisionAssessment, DecisionThresholds, RESEARCH_DEFAULT_THRESHOLDS,
)
from app.ai.decision.confidence import assess_prediction_quality
from app.ai.decision.eligibility import evaluate_eligibility

PROBABILITY_SOURCES = ("RAW", "CALIBRATED")
# §18 : tolérance de somme des probabilités d'un marché — RESEARCH_DEFAULT,
# explicitement non validée statistiquement, jamais un seuil de production.
PROBABILITY_SUM_TOLERANCE = 0.02


def validate_probability_value(p: Optional[float]) -> bool:
    """§18 : 0 <= p <= 1, jamais NaN."""
    return p is not None and isinstance(p, (int, float)) and 0.0 <= p <= 1.0 and p == p


def validate_market_probabilities(market: str, probabilities: dict[str, float], tolerance: float = PROBABILITY_SUM_TOLERANCE) -> Optional[str]:
    """
    §18/§19 — None si valide, sinon "INVALID_PROBABILITY". Ne corrige
    JAMAIS silencieusement une donnée invalide (§19) :
      - marché inconnu (hors MARKET_OUTCOME_KEYS, réutilisé de Phase 5) -> invalide.
      - les clés fournies ne correspondent pas EXACTEMENT aux issues attendues du marché -> invalide.
      - une probabilité hors [0,1] -> invalide.
      - somme des probabilités hors [1-tolerance, 1+tolerance] -> invalide.
    """
    if market not in MARKET_OUTCOME_KEYS:
        return "INVALID_PROBABILITY"
    expected_keys = set(MARKET_OUTCOME_KEYS[market])
    if set(probabilities) != expected_keys:
        return "INVALID_PROBABILITY"
    if not all(validate_probability_value(p) for p in probabilities.values()):
        return "INVALID_PROBABILITY"
    if abs(sum(probabilities.values()) - 1.0) > tolerance:
        return "INVALID_PROBABILITY"
    return None


def compute_feature_age_hours(evaluated_at: datetime, feature_generated_at: Optional[datetime]) -> Optional[float]:
    """§23 : feature_age, en HEURES. None si feature_generated_at est absent — jamais une date inventée."""
    if feature_generated_at is None:
        return None
    return (evaluated_at - feature_generated_at).total_seconds() / 3600.0


def is_feature_stale(feature_age_hours: Optional[float], max_age_hours: float) -> bool:
    """False si feature_age_hours est None (inconnu != périmé — ne jamais confondre les deux, laisse la
    dimension DATA_QUALITY globale gérer l'UNKNOWN plutôt que de fabriquer un verdict de fraîcheur)."""
    return feature_age_hours is not None and feature_age_hours > max_age_hours


def assess_decision(
    *,
    prediction_id: Optional[int], model: Optional[str], market: str,
    probabilities: Optional[dict[str, float]], selection: str, probability_source: str,
    selection_decision=None, calibration_result=None,
    feature_coverage: Optional[dict] = None, team_mapping_confident: Optional[bool] = None,
    feature_generated_at: Optional[datetime] = None,
    odds_timestamp: Optional[datetime] = None, cutoff_timestamp: Optional[datetime] = None,
    match_kickoff: Optional[datetime] = None, has_measured_odds_timestamp: bool = False,
    sample_size: Optional[int] = None, thresholds: DecisionThresholds = RESEARCH_DEFAULT_THRESHOLDS,
    odds_by_selection: Optional[dict[str, float]] = None, bookmaker_count: Optional[int] = None,
    evaluated_at: datetime,
) -> DecisionAssessment:
    """
    §25 : assess_decision() — orchestrateur unique.

    `evaluated_at` : timestamp D'ÉVALUATION, fourni explicitement par
    l'appelant (jamais `datetime.now()` implicite ici — §17/§35 : aucune
    dépendance à l'heure actuelle non fournie, reproductibilité).

    `probability_source` doit être "RAW" ou "CALIBRATED" (§22) — les deux
    valeurs ne sont JAMAIS interchangées silencieusement ; c'est à
    l'appelant de fournir `probabilities` déjà dans la source voulue
    (par défaut CALIBRATED si disponible, sinon RAW — même règle que
    ModelProbability.effective_probability(), Phase 8H).

    Ordre de priorité du statut final :
    1. probabilité manquante/invalide (§18/§19) -> INELIGIBLE, prime sur
       TOUT (une probabilité invalide rend toute autre évaluation sans objet).
    2. sinon : statut des hard gates (eligibility.py), inchangé.
    """
    if probability_source not in PROBABILITY_SOURCES:
        raise ValueError(f"probability_source doit être 'RAW' ou 'CALIBRATED', reçu {probability_source!r}")

    if not probabilities:
        prob_reason = "MISSING_PROBABILITY"
    else:
        prob_reason = validate_market_probabilities(market, probabilities)

    feature_age = compute_feature_age_hours(evaluated_at, feature_generated_at)
    feature_stale = is_feature_stale(feature_age, thresholds.max_feature_age_hours)

    confidence = assess_prediction_quality(
        selection_decision=selection_decision, calibration_result=calibration_result,
        feature_coverage=feature_coverage, team_mapping_confident=team_mapping_confident,
        odds_timestamp=odds_timestamp, cutoff_timestamp=cutoff_timestamp, match_kickoff=match_kickoff,
        has_measured_odds_timestamp=has_measured_odds_timestamp,
        sample_size=sample_size, min_sample_required=thresholds.min_sample_size,
        limited_sample_floor=thresholds.limited_sample_floor,
        odds_by_selection=odds_by_selection, bookmaker_count=bookmaker_count,
    )
    eligibility = evaluate_eligibility(
        confidence.quality_dimensions, feature_stale=feature_stale, model_provided=selection_decision is not None,
    )

    final_status = eligibility.status
    reasons = list(eligibility.reasons)
    if prob_reason is not None:
        reasons = [prob_reason] + reasons
        final_status = "INELIGIBLE"

    probability_for_selection = probabilities.get(selection) if probabilities else None

    return DecisionAssessment(
        prediction_id=prediction_id, model=model, market=market,
        probability=probability_for_selection, confidence=confidence,
        quality_dimensions=confidence.quality_dimensions, gates=eligibility.gates,
        eligibility=final_status, reasons=reasons,
        research_only=(final_status == "RESEARCH_ONLY"),
        timestamp=evaluated_at,
    )


def evaluate_shadow_prediction(**kwargs) -> DecisionAssessment:
    """§31 : SHADOW MODE — alias explicite de assess_decision(). Ne modifie
    AUCUNE table de production, ne génère AUCUN pari — retourne uniquement
    l'évaluation. Nom distinct conservé (plutôt qu'un simple alias `=`) pour
    que l'intention d'appel (évaluation shadow) reste lisible au site
    d'appel, même si l'implémentation est strictement identique."""
    return assess_decision(**kwargs)


# ---------------------------------------------------------------------------
# §30 : contrat GÉNÉRIQUE vers un futur Value Engine — AUCUNE connexion
# réelle ici (api/app/ai/value/ n'est PAS importé par ce module).
# ---------------------------------------------------------------------------

def to_value_engine_input(assessment: DecisionAssessment) -> dict:
    """
    §30 : DecisionAssessment -> dict portable, forme attendue par un futur
    Value Engine. Le Value Engine (api/app/ai/value/, Phase 8H) pourra
    ensuite refuser INELIGIBLE/UNKNOWN et n'accepter ELIGIBLE (ou
    RESEARCH_ONLY, selon SES propres règles, jamais décidées ici) — cette
    fonction ne fait AUCUN appel à api/app/ai/value/, elle documente
    uniquement la forme du contrat (§30 : "ne pas connecter les modules").
    """
    return {
        "prediction_id": assessment.prediction_id, "market": assessment.market,
        "probability": assessment.probability, "eligibility": assessment.eligibility,
        "confidence_overall_status": assessment.confidence.overall_status,
        "temporal_status": assessment.quality_dimensions.temporal_quality,
        "research_only": assessment.research_only, "timestamp": assessment.timestamp,
    }
