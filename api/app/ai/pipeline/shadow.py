"""
api/app/ai/pipeline/shadow.py — Phase 8J : run_shadow_batch() (§30/§31) et
adaptateur Track Record (§32).

RÈGLE ABSOLUE : aucune fonction ici n'écrit en base — un batch ne fait que
retourner une liste de PipelineAssessment, jamais une persistance.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.ai.pipeline.orchestrator import run_pipeline
from app.ai.pipeline.schemas import PipelineAssessment, PipelineInput, Provenance


def run_shadow_batch(inputs: list[PipelineInput]) -> list[PipelineAssessment]:
    """
    §30/§31 : évalue CHAQUE PipelineInput indépendamment. Une exception sur
    UN match ne doit JAMAIS interrompre les autres (§31 : "A=invalide -> A=
    REJECTED mais B et C continuent normalement") — capturée ici, jamais
    laissée remonter, jamais silencieusement ignorée (l'erreur complète est
    conservée dans `PipelineAssessment.error` et `reasons`).
    """
    results: list[PipelineAssessment] = []
    for pi in inputs:
        try:
            results.append(run_pipeline(pi))
        except Exception as e:  # noqa: BLE001 — isolation volontaire, §31 : jamais interrompre le batch
            fallback_evaluated_at = pi.as_of or pi.kickoff or datetime.now(timezone.utc)
            results.append(PipelineAssessment(
                match_id=pi.match_id, market=pi.market, prediction={}, quality=None, decision=None, value=None,
                value_stage_status="SKIPPED_NO_ODDS", final_status="REJECTED",
                reasons=[f"PIPELINE_ERROR: {e!r}"],
                provenance=Provenance(
                    model_source=pi.model, model_version=pi.model_version, calibration_source=None,
                    feature_snapshot=None, odds_source=None, odds_timestamp=None, cutoff_timestamp=None,
                ),
                evaluated_at=fallback_evaluated_at, error=repr(e),
            ))
    return results


# ---------------------------------------------------------------------------
# §32 : adaptateur Track Record — LIMITÉ à la FORME de l'observation.
# ---------------------------------------------------------------------------

def pipeline_assessment_to_observation(assessment: PipelineAssessment, actual_outcome: str) -> Optional[dict]:
    """
    §32 : PipelineAssessment -> observation, dans le MÊME format
    {"p_true", "probs", "actual", "correct"} déjà utilisé partout dans
    l'Arena (api/app/ai/arena/service.py::_market_observation,
    research.py::dc_observation, track_record.py::_shadow_observation) —
    jamais un format inventé (§1/§48).

    `actual_outcome` doit être fourni EXPLICITEMENT par l'appelant (résultat
    RÉEL déjà connu, ex. un match historique déjà joué) — jamais déduit ni
    fabriqué ici (§44). None si la Decision n'était pas au moins RESEARCH_
    ONLY, ou si la sélection/probabilités sont absentes — jamais une
    observation fabriquée à partir d'un rejet.

    === LIMITATION DOCUMENTÉE (§32 : "si l'adaptation n'est pas possible
    proprement, documenter précisément pourquoi") ===

    Cet adaptateur s'arrête au niveau de la FORME de l'observation, JAMAIS à
    sa PERSISTANCE. api/app/ai/arena/track_record.py (compute_track_record,
    compute_cumulative_track_record, compute_selection_distribution,
    compute_stability_tracking, compute_calibration_tracking) interrogent
    TOUTES directement la table `shadow_selection_predictions` via une
    Session SQLModel (`_decisions_query`/`_query_resolved_rows`) — aucune de
    ces fonctions n'accepte une liste d'observations externes en paramètre.
    Les connecter proprement nécessiterait d'ÉCRIRE une ligne dans
    `shadow_selection_predictions` (le mécanisme réel de Phase 6/7, voir
    scripts/model_selection_shadow.py) — explicitement HORS PÉRIMÈTRE de
    cette phase (§ "aucune écriture production", "aucune migration DB sauf
    nécessité absolue").

    Ce que cette fonction PROUVE (voir api/test_end_to_end_pipeline.py) :
    l'observation qu'elle produit est structurellement COMPATIBLE avec
    api/app/ai/arena/service.py::_compute_market_metrics — la fonction
    RÉELLEMENT partagée par TOUT le pipeline de statistiques de l'Arena
    (Phase 5-7), réutilisée telle quelle, jamais réimplémentée. La
    persistance réelle vers Track Record reste la responsabilité EXCLUSIVE
    de scripts/model_selection_shadow.py, jamais dupliquée ici.
    """
    if assessment.decision is None or assessment.decision.eligibility not in ("ELIGIBLE", "RESEARCH_ONLY"):
        return None
    probs = assessment.prediction.get("probabilities") if assessment.prediction else None
    if not probs or actual_outcome not in probs:
        return None
    pick = max(probs, key=probs.get)
    return {"p_true": probs[actual_outcome], "probs": probs, "actual": actual_outcome, "correct": pick == actual_outcome}
