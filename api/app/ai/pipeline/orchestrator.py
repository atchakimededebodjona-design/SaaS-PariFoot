"""
api/app/ai/pipeline/orchestrator.py — Phase 8J : run_pipeline() (§7-§16 du
prompt).

RÉUTILISE TEL QUEL (jamais réimplémenté, §1/§48) :
  - api/app/ai/decision/decision.py::assess_decision (Phase 8I) — Quality +
    Decision stages en UN seul appel (assess_decision appelle déjà
    assess_prediction_quality en interne, voir Phase 8I §7 : "aucune
    logique parallèle de qualité" — ce module ne le rappelle donc PAS une
    deuxième fois, il réutilise decision.confidence).
  - api/app/ai/value/core.py::build_value_signal (Phase 8H) — Value stage,
    appelé UNIQUEMENT si des odds sont fournies ET si la Decision l'autorise
    (§13 : jamais si INELIGIBLE/INSUFFICIENT_DATA/UNKNOWN).

Ce module n'écrit JAMAIS en base, n'appelle aucun fournisseur d'odds.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.ai.decision.decision import assess_decision
from app.ai.value.core import build_value_signal
from app.ai.value.schemas import ModelProbability, OddsSnapshot

from app.ai.pipeline.schemas import PipelineAssessment, PipelineInput, Provenance


def _resolve_evaluated_at(pi: PipelineInput) -> datetime:
    """§17/§28/§44 : timestamp D'ÉVALUATION — jamais `datetime.now()` implicite.
    `as_of` prime (c'est le cutoff explicite d'évaluation) ; à défaut `kickoff`
    (un match déjà résolu, évalué à son propre coup d'envoi) ; sinon erreur
    explicite plutôt qu'une heure système fabriquée."""
    evaluated_at = pi.as_of or pi.kickoff
    if evaluated_at is None:
        raise ValueError(
            "PipelineInput.as_of (ou kickoff) doit être fourni explicitement — "
            "jamais déduit de l'heure système (§17/§28/§44 du prompt Phase 8J)."
        )
    return evaluated_at


def _run_value_stage(pi: PipelineInput, decision, evaluated_at: datetime):
    """§9/§10/§13/§15/§16 : le Value Engine (Phase 8H, jamais modifié) n'est
    appelé QUE si des odds sont fournies ET si la Decision l'autorise
    (ELIGIBLE ou RESEARCH_ONLY uniquement — §13 : jamais POSITIVE_VALUE sur
    une Decision INELIGIBLE/INSUFFICIENT_DATA/UNKNOWN, §15 : aucun faux
    positif). Retourne (ValueSignal|None, VALUE_STAGE_STATUSES)."""
    if pi.odds_input is None or not pi.odds_input.odds_by_selection:
        return None, "SKIPPED_NO_ODDS"
    if decision.eligibility == "INELIGIBLE":
        return None, "SKIPPED_DECISION_INELIGIBLE"
    if decision.eligibility == "INSUFFICIENT_DATA":
        return None, "SKIPPED_DECISION_INSUFFICIENT_DATA"
    if decision.eligibility == "UNKNOWN":
        return None, "SKIPPED_DECISION_UNKNOWN"
    # eligibility in ("ELIGIBLE", "RESEARCH_ONLY") à partir d'ici.
    if not pi.probabilities or pi.selection not in pi.probabilities:
        return None, "SKIPPED_NO_MODEL_PROBABILITY"

    calibrated = (pi.calibration.probabilities or {}).get(pi.selection) if pi.calibration.source == "CALIBRATED" else None
    model_probability = ModelProbability(
        market=pi.market, selection=pi.selection, raw_probability=pi.probabilities[pi.selection],
        calibrated_probability=calibrated, source_model_type=pi.model,
    )
    market_odds = {
        sel: OddsSnapshot(
            market=pi.market, selection=sel, decimal_odds=odds, bookmaker=pi.odds_input.bookmaker,
            odds_timestamp=pi.odds_input.odds_timestamp, has_measured_timestamp=pi.odds_input.has_measured_timestamp,
        )
        for sel, odds in pi.odds_input.odds_by_selection.items()
    }
    signal = build_value_signal(
        match_id=pi.match_id, market=pi.market, selection=pi.selection,
        model_probability=model_probability, market_odds=market_odds,
        cutoff_timestamp=pi.temporal_metadata.cutoff_timestamp or pi.as_of,
        match_kickoff=pi.temporal_metadata.match_kickoff or pi.kickoff,
        # §44 : jamais une confiance fabriquée — Phase 8I ne produit pas de score numérique par défaut
        # (research_score est expérimental, jamais nommé "confidence", jamais transmis ici).
        confidence=None,
    )
    return signal, "EVALUATED"


def _derive_final_status(decision, value_signal, value_stage_status: str) -> tuple[str, list[str]]:
    """
    §14 : précédence explicite, réutilisant le vocabulaire RÉEL de Phase
    8H/8I partout où il existe déjà — jamais un statut masquant une cause
    de rejet plus importante (§15) :

    1. decision.eligibility == INELIGIBLE -> "INELIGIBLE" (couvre déjà
       FUTURE_INFORMATION, TEMPORAL_UNKNOWN, MODEL_UNSTABLE, INVALID_
       PROBABILITY — tous encodés dans decision.reasons, Phase 8I).
    2. decision.eligibility == UNKNOWN -> "UNKNOWN".
    3. decision.eligibility == INSUFFICIENT_DATA -> "INSUFFICIENT_DATA".
    4. decision.eligibility == RESEARCH_ONLY -> "RESEARCH_ONLY", TOUJOURS
       (même si Value = POSITIVE_VALUE — jamais promu à VALUE_CANDIDATE,
       §26 du prompt Phase 8I : jamais production eligible).
    5. decision.eligibility == ELIGIBLE :
       a. value_stage_status != EVALUATED -> "NOT_AVAILABLE" (§16 : absence
          d'odds/probabilité ne devient JAMAIS "NO_VALUE").
       b. value.status == POSITIVE_VALUE -> "VALUE_CANDIDATE" (jamais "BET").
       c. value.status in (NEUTRAL, NEGATIVE_VALUE) -> "NO_VALUE".
       d. sinon (INSUFFICIENT_DATA, TEMPORALLY_UNSAFE, INVALID_ODDS,
          Phase 8H) -> propagé TEL QUEL.
    """
    reasons = list(decision.reasons)
    if decision.eligibility == "INELIGIBLE":
        return "INELIGIBLE", reasons
    if decision.eligibility == "UNKNOWN":
        return "UNKNOWN", reasons
    if decision.eligibility == "INSUFFICIENT_DATA":
        return "INSUFFICIENT_DATA", reasons
    if decision.eligibility == "RESEARCH_ONLY":
        return "RESEARCH_ONLY", reasons

    # ELIGIBLE à partir d'ici.
    if value_stage_status != "EVALUATED":
        return "NOT_AVAILABLE", reasons + [value_stage_status]
    if value_signal.status == "POSITIVE_VALUE":
        return "VALUE_CANDIDATE", reasons
    if value_signal.status in ("NEUTRAL", "NEGATIVE_VALUE"):
        return "NO_VALUE", reasons
    return value_signal.status, reasons + ([value_signal.reason] if value_signal.reason else [])


def run_pipeline(pi: PipelineInput) -> PipelineAssessment:
    """§7-§16 : point d'entrée unique pour UN match/marché/sélection."""
    evaluated_at = _resolve_evaluated_at(pi)
    cutoff_timestamp = pi.temporal_metadata.cutoff_timestamp or pi.as_of
    match_kickoff = pi.temporal_metadata.match_kickoff or pi.kickoff

    probability_source = pi.calibration.source
    probabilities_for_decision = (
        pi.calibration.probabilities if (probability_source == "CALIBRATED" and pi.calibration.probabilities) else pi.probabilities
    )

    odds_timestamp = pi.odds_input.odds_timestamp if pi.odds_input else None
    has_measured = pi.odds_input.has_measured_timestamp if pi.odds_input else False
    odds_by_selection = pi.odds_input.odds_by_selection if pi.odds_input else None
    bookmaker_count = 1 if (pi.odds_input and pi.odds_input.bookmaker) else None

    # --- Quality + Decision stages (§7/§8) : UN seul appel à Phase 8I. ---
    decision = assess_decision(
        prediction_id=pi.prediction_id, model=pi.model, market=pi.market,
        probabilities=probabilities_for_decision, selection=pi.selection, probability_source=probability_source,
        selection_decision=pi.selection_decision, calibration_result=pi.calibration.calibration_result,
        feature_coverage=pi.feature_snapshot.coverage, team_mapping_confident=pi.feature_snapshot.team_mapping_confident,
        feature_generated_at=pi.feature_snapshot.generated_at,
        odds_timestamp=odds_timestamp, cutoff_timestamp=cutoff_timestamp,
        match_kickoff=match_kickoff, has_measured_odds_timestamp=has_measured,
        sample_size=pi.sample_size, odds_by_selection=odds_by_selection, bookmaker_count=bookmaker_count,
        evaluated_at=evaluated_at,
    )

    # --- Value stage (§9/§10/§13) : Phase 8H, jamais modifié. ---
    value_signal, value_stage_status = _run_value_stage(pi, decision, evaluated_at)

    final_status, reasons = _derive_final_status(decision, value_signal, value_stage_status)

    provenance = Provenance(
        model_source=pi.model, model_version=pi.model_version,
        calibration_source=pi.calibration.calibration_method_label or pi.calibration.source,
        feature_snapshot=pi.feature_snapshot.snapshot_id,
        odds_source=pi.odds_input.source_label if pi.odds_input else None,
        odds_timestamp=odds_timestamp, cutoff_timestamp=cutoff_timestamp,
    )

    return PipelineAssessment(
        match_id=pi.match_id, market=pi.market,
        prediction={"selection": pi.selection, "probability_source": probability_source, "probabilities": probabilities_for_decision},
        quality=decision.confidence, decision=decision, value=value_signal, value_stage_status=value_stage_status,
        final_status=final_status, reasons=reasons, provenance=provenance, evaluated_at=evaluated_at,
    )
