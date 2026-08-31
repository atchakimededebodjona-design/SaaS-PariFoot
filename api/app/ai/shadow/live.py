"""
api/app/ai/shadow/live.py — Phase 8M : découverte et capture LIVE (§4-§11,
§33-§38 du prompt).

RÉUTILISE TEL QUEL (jamais réimplémenté) :
  - api/app/ai/pipeline/orchestrator.py::run_pipeline (Phase 8J).
  - api/app/ai/features/snapshot.py::build_feature_snapshot/snapshot_coverage (Phase 8A).
  - api/app/ai/shadow/tracking.py::capture_shadow_decision/dedup_key (Phase 8K).

Source PRIORITAIRE (§4/§8) : `model_predictions` (status="pending",
match_date >= as_of) — la production DÉJÀ produite, jamais un nouvel appel
modèle, jamais un nouvel appel réseau. Ce module ne fait QUE LIRE
match_predictions/match/match_stats — aucune écriture, y compris sur les
lignes snapshotées elles-mêmes (§8/§58).
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Optional

from sqlmodel import Session, select

from app.models.match import Match
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion

from app.ai.features.snapshot import build_feature_snapshot, snapshot_coverage
from app.ai.pipeline.schemas import PipelineInput, CalibrationInput, FeatureSnapshotInput, TemporalMetadataInput

_MARKET_TO_PROB_FIELDS = {
    "1X2": {"home_win": "prob_home", "draw": "prob_draw", "away_win": "prob_away"},
    "BTTS": {"yes": "prob_btts_yes", "no": "prob_btts_no"},
    "OVER_UNDER_2_5": {"over": "prob_over_2_5", "under": "prob_under_2_5"},
}


def discover_live_candidates(session: Session, as_of: datetime) -> list[ModelPrediction]:
    """
    §4 : model_predictions, status="pending", match_date >= as_of.date() —
    AUCUN appel réseau, AUCUN appel à fetch_upcoming_fixtures (§4/§57).
    Triée de façon déterministe (§53), jamais l'ordre implicite d'une
    requête SQL non explicitement triée.
    """
    stmt = (
        select(ModelPrediction)
        .where(ModelPrediction.status == "pending", ModelPrediction.match_date >= as_of.date())
        .order_by(ModelPrediction.match_date, ModelPrediction.league, ModelPrediction.home_team, ModelPrediction.away_team, ModelPrediction.model_type)
    )
    return session.exec(stmt).all()


def assess_capture_eligibility(mp: ModelPrediction, as_of: datetime) -> tuple[bool, Optional[str]]:
    """
    §5/§6/§34/§35 : (capturable, reason_si_rejeté).
    - kickoff <= as_of -> REJECTED "TOO_LATE" (§5/§34, JAMAIS une capture rétroactive).
    - predicted_at > as_of -> REJECTED "PREDICTION_TIMESTAMP_AFTER_AS_OF" (§35 : la prédiction n'existait pas
      encore à as_of, ne jamais prétendre le contraire).
    """
    kickoff = datetime.combine(mp.match_date, time.min, tzinfo=timezone.utc)
    a = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
    if kickoff <= a:
        return False, "TOO_LATE"
    predicted_at = mp.predicted_at if mp.predicted_at.tzinfo else mp.predicted_at.replace(tzinfo=timezone.utc)
    if predicted_at > a:
        return False, "PREDICTION_TIMESTAMP_AFTER_AS_OF"
    return True, None


def build_pipeline_input_for_live(session: Session, mp: ModelPrediction, market: str, selection: str, as_of: datetime) -> tuple[Optional[PipelineInput], dict]:
    """
    §1/§9 : snapshotte la prédiction PRODUCTION déjà existante (`mp`) — ne
    RECALCULE JAMAIS de probabilité modèle. `as_of` doit être fourni
    explicitement par l'appelant (§6 : jamais `datetime.now()` implicite
    dans une fonction pure).
    """
    diagnostics: dict = {"match": f"{mp.league}:{mp.match_date}:{mp.home_team}-{mp.away_team}", "model_type": mp.model_type}

    capturable, reject_reason = assess_capture_eligibility(mp, as_of)
    if not capturable:
        diagnostics["reason"] = reject_reason
        return None, diagnostics

    field_map = _MARKET_TO_PROB_FIELDS.get(market)
    if field_map is None:
        diagnostics["reason"] = "unknown_market"
        return None, diagnostics
    probs = {sel: getattr(mp, attr) for sel, attr in field_map.items()}
    if any(v is None for v in probs.values()) or selection not in probs:
        diagnostics["reason"] = "MARKET_NOT_MODELED_BY_THIS_MODEL"
        return None, diagnostics

    version = session.get(ModelVersion, mp.model_version_id)
    # §30/§13 Phase 8L, réaffirmé ici (§36) : la ModelVersion snapshotée doit exister et être cohérente —
    # aucune substitution silencieuse par la version actuellement active si elle a changé depuis `predicted_at`.
    if version is None:
        diagnostics["reason"] = "MODEL_VERSION_MISSING"
        return None, diagnostics

    kickoff = datetime.combine(mp.match_date, time.min, tzinfo=timezone.utc)
    snapshot = build_feature_snapshot(session, mp.league, mp.home_team, mp.away_team, cutoff=as_of.date())
    coverage = snapshot_coverage(snapshot)

    pi = PipelineInput(
        match_id=mp.id, league=mp.league, kickoff=kickoff, as_of=as_of, model=mp.model_type,
        model_version=version.name, market=market, selection=selection, probabilities=probs,
        calibration=CalibrationInput(probabilities=None, source="RAW", calibration_result=None, calibration_method_label=None),
        feature_snapshot=FeatureSnapshotInput(coverage=coverage, generated_at=None, snapshot_id=f"{mp.league}:{mp.match_date}:{mp.home_team}-{mp.away_team}@{as_of.isoformat()}", team_mapping_confident=None),
        temporal_metadata=TemporalMetadataInput(cutoff_timestamp=as_of, match_kickoff=kickoff),
        odds_input=None,  # §11 : odds optionnelles — jamais fabriquées, aucun fournisseur intégré (The Odds API hors scope)
        selection_decision=None, sample_size=None,
    )
    diagnostics["reason"] = "OK"
    diagnostics["feature_coverage_ratio"] = coverage.get("coverage_ratio")
    return pi, diagnostics


def check_production_consistency(mp: ModelPrediction, pi: PipelineInput, assessment) -> list[str]:
    """
    §36/§37/§38 : vérifie que le PipelineInput/PipelineAssessment construits
    correspondent EXACTEMENT au snapshot production (`mp`) — jamais une
    correction silencieuse en cas d'écart (§37 : "NE PAS corriger
    automatiquement"). Retourne la liste des mismatches trouvés (vide si
    cohérent).
    """
    mismatches = []
    version_name_prefix = mp.model_type
    if pi.model != mp.model_type:
        mismatches.append("MODEL_TYPE_MISMATCH")
    if pi.model_version is None or not pi.model_version.startswith(f"xfoot-{version_name_prefix}"):
        mismatches.append("MODEL_VERSION_MISMATCH")
    snapshot_probs = pi.probabilities or {}
    field_map = _MARKET_TO_PROB_FIELDS.get(pi.market, {})
    for sel, attr in field_map.items():
        production_value = getattr(mp, attr)
        if production_value is not None and snapshot_probs.get(sel) != production_value:
            mismatches.append("PROBABILITY_MISMATCH")
            break
    if assessment is not None and assessment.decision is not None:
        # auto-cohérence : le PipelineAssessment doit porter EXACTEMENT les probabilités du snapshot (jamais recalculées).
        if assessment.prediction.get("probabilities") != pi.probabilities:
            mismatches.append("DECISION_INPUT_MISMATCH")
    return mismatches
