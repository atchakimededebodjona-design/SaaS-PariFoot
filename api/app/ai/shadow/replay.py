"""
api/app/ai/shadow/replay.py — Phase 8K : sourcing de données RÉELLES,
lecture seule (§8/§9/§10/§27-§32 du prompt).

Priorité de source (§8, jamais téléchargée) :
  1. `model_predictions` déjà existante (Phase 6, source="backtest" pour un
     match historique déjà résolu — la SEULE probabilité RÉELLEMENT produite
     par un modèle Xfoot pour ce match, jamais recalculée ici).
  2. Feature snapshot (Phase 8A, api/app/ai/features/snapshot.py::
     build_feature_snapshot — réutilisé tel quel, anti-fuite déjà garanti
     par construction : Match.date < as_of, §7 Phase 8A).
  3. Métadonnées modèle (`model_versions.trained_at`, vérifié <= as_of, §30).

AUCUN appel réseau, AUCUNE nouvelle donnée téléchargée. `selection_decision`/
`calibration_result` restent None (UNKNOWN, jamais fabriqués) : aucune
SelectionDecision/CalibrationResult n'est persistée par match dans ce dépôt
(Phase 6 les calcule par FENÊTRE via un script de recherche, jamais stockée
par match individuel) — limitation documentée, pas contournée.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Optional

from sqlmodel import Session, func, select

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


def measure_data_reality(session: Session) -> dict:
    """§9 : mesure RÉELLE de ce qui existe — jamais supposée. `measured_at`
    est le SEUL usage de l'heure système dans tout ce module (un relevé
    d'état environnemental, jamais une donnée injectée dans une décision —
    voir docstring module)."""
    now = datetime.now(timezone.utc)
    total_matches = session.exec(select(func.count()).select_from(Match)).one()
    future_matches = session.exec(select(func.count()).select_from(Match).where(Match.date > now)).one()
    pending_predictions = session.exec(select(func.count()).select_from(ModelPrediction).where(ModelPrediction.status == "pending")).one()
    resolved_predictions = session.exec(select(func.count()).select_from(ModelPrediction).where(ModelPrediction.status == "resolved")).one()
    backtest_predictions = session.exec(select(func.count()).select_from(ModelPrediction).where(ModelPrediction.source == "backtest")).one()

    return {
        "measured_at": now.isoformat(),
        "total_matches_in_db": total_matches,
        "future_fixtures": future_matches,
        "pending_model_predictions": pending_predictions,
        "resolved_model_predictions": resolved_predictions,
        "backtest_model_predictions_available_as_shadow_candidates": backtest_predictions,
        "shadow_live_data": "NONE_AVAILABLE" if future_matches == 0 else "AVAILABLE",
    }


def find_replay_candidates(session: Session, limit: int) -> list[ModelPrediction]:
    """§8 : source #1 — prédictions RÉELLEMENT déjà produites (source=
    "backtest", déjà résolues), triées de façon déterministe (§43 : jamais
    l'ordre implicite d'une requête SQL non explicitement triée)."""
    stmt = (
        select(ModelPrediction)
        .where(ModelPrediction.source == "backtest", ModelPrediction.status == "resolved")
        .order_by(ModelPrediction.match_date.desc(), ModelPrediction.league, ModelPrediction.home_team, ModelPrediction.away_team, ModelPrediction.model_type)
        .limit(limit)
    )
    return session.exec(stmt).all()


def build_pipeline_input_for_replay(session: Session, mp: ModelPrediction, market: str, selection: str) -> tuple[Optional[PipelineInput], dict]:
    """
    §10/§27-§32 : reconstruit un PipelineInput pour le match/marché de `mp`
    (une ModelPrediction RÉELLE déjà en base), `as_of = kickoff du match`
    (la borne la plus stricte : évalué EXACTEMENT au coup d'envoi, aucune
    donnée du match lui-même n'est utilisée — build_feature_snapshot,
    Phase 8A, applique déjà `Match.date < as_of` strictement).

    Retourne (PipelineInput|None, diagnostics) — None si le marché n'est pas
    modélisé par ce modèle (probabilité absente, jamais fabriquée) ou si la
    ModelVersion a été entraînée APRÈS as_of (§30 : fuite de version modèle,
    rejetée).
    """
    diagnostics: dict = {"match": f"{mp.league}:{mp.match_date}:{mp.home_team}-{mp.away_team}", "model_type": mp.model_type}
    field_map = _MARKET_TO_PROB_FIELDS.get(market)
    if field_map is None:
        diagnostics["reason"] = "unknown_market"
        return None, diagnostics

    probs = {sel: getattr(mp, attr) for sel, attr in field_map.items()}
    if any(v is None for v in probs.values()):
        diagnostics["reason"] = "MARKET_NOT_MODELED_BY_THIS_MODEL"
        return None, diagnostics
    if selection not in probs:
        diagnostics["reason"] = "SELECTION_NOT_IN_MARKET"
        return None, diagnostics

    as_of = datetime.combine(mp.match_date, time.min, tzinfo=timezone.utc)  # ModelPrediction.match_date est un `date` pur (voir app/models/model_prediction.py)

    # §30 : fuite de version modèle — une ModelVersion entraînée APRÈS as_of ne doit jamais être utilisée.
    version = session.get(ModelVersion, mp.model_version_id)
    model_version_leakage = None
    if version is not None and version.trained_at is not None:
        trained_at = version.trained_at if version.trained_at.tzinfo else version.trained_at.replace(tzinfo=timezone.utc)
        if trained_at > as_of:
            diagnostics["reason"] = "MODEL_VERSION_TRAINED_AFTER_AS_OF"
            diagnostics["trained_at"] = trained_at.isoformat()
            return None, diagnostics
        model_version_leakage = "SAFE"

    # §27 : feature snapshot RÉEL (Phase 8A, réutilisé) — anti-fuite déjà garanti (Match.date < as_of).
    snapshot = build_feature_snapshot(session, mp.league, mp.home_team, mp.away_team, cutoff=as_of.date())
    coverage = snapshot_coverage(snapshot)

    pi = PipelineInput(
        match_id=None, league=mp.league, kickoff=as_of, as_of=as_of, model=mp.model_type,
        model_version=version.name if version else None, market=market, selection=selection,
        probabilities=probs,
        calibration=CalibrationInput(probabilities=None, source="RAW", calibration_result=None, calibration_method_label=None),
        feature_snapshot=FeatureSnapshotInput(coverage=coverage, generated_at=None, snapshot_id=f"{mp.league}:{mp.match_date}:{mp.home_team}-{mp.away_team}@{as_of.isoformat()}", team_mapping_confident=None),
        temporal_metadata=TemporalMetadataInput(cutoff_timestamp=as_of, match_kickoff=as_of),
        odds_input=None,  # §18/§42 du prompt Phase 8K : aucune odds — The Odds API SUPPORT_REQUIRED (Phase 8G.2)
        selection_decision=None,  # UNKNOWN, jamais fabriqué (voir docstring module)
        sample_size=None,
    )
    diagnostics["reason"] = "OK"
    diagnostics["model_version_leakage_check"] = model_version_leakage or "UNKNOWN (ModelVersion.trained_at absent)"
    diagnostics["feature_coverage_ratio"] = coverage.get("coverage_ratio")
    return pi, diagnostics
