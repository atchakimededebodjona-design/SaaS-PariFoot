"""
api/app/ai/arena/scheduler.py — Phase 9, Partie A/F : scheduler de
prédictions LIVE pour les matchs à venir.

Logique de prédiction ENTIÈREMENT déléguée à ModelOrchestrator.predict_all()
(Phase 7, inchangé) — ce module n'implémente AUCUNE deuxième logique de
prédiction, seulement : sélection des fixtures à traiter, construction du
MatchContext, et (Phase 9 nouveauté) une passe optionnelle "shadow" pour les
ModelVersion en observation (status="shadow", jamais utilisées par
l'orchestrateur — voir app/ai/arena/promotion.py::set_shadow).

Idempotence (§7 du ticket) : entièrement héritée de log_prediction()
(contrainte UNIQUE existante sur model_predictions, Phase 6) — ce module ne
contient AUCUNE logique de déduplication propre. Relancer generate_live_
predictions() deux fois sur les mêmes fixtures ne produit jamais de doublon.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app.models.team_rating import ModelVersion

from .models_common import MatchContext, PredictionOutcome, _MLPredictionModel
from .orchestrator import ModelOrchestrator
from .prediction_logging import PredictionRecord, log_prediction

logger = logging.getLogger("xfoot.arena.scheduler")

# Fenêtre par défaut (§8 du ticket) — configurable via variable
# d'environnement, utilisée UNIQUEMENT comme valeur par défaut du CLI
# (scripts/generate_live_predictions.py) : generate_live_predictions()
# elle-même prend toujours une liste de fixtures déjà résolue par
# l'appelant, jamais cette variable directement (fonction pure, testable).
DEFAULT_LIVE_PREDICTION_WINDOW_HOURS = int(os.environ.get("LIVE_PREDICTION_WINDOW_HOURS", "48"))


@dataclass
class UpcomingFixture:
    league: str
    home_team: str
    away_team: str
    match_date: datetime


@dataclass
class SchedulerRunResult:
    fixtures_considered: int = 0
    fixtures_skipped_past: int = 0
    active_outcomes: dict[str, list[PredictionOutcome]] = field(default_factory=dict)
    shadow_predictions_logged: int = 0
    shadow_errors: list[str] = field(default_factory=list)


def generate_live_predictions(
    session: Session,
    orchestrator: ModelOrchestrator,
    fixtures: list[UpcomingFixture],
    *,
    persist: bool = True,
    include_shadow: bool = True,
    now: Optional[datetime] = None,
) -> SchedulerRunResult:
    """
    Pour chaque fixture :
      1. Ignore silencieusement (mais compte, §8 : "ne pas générer de
         prédictions pour des matchs déjà commencés ou terminés") toute
         fixture dont `match_date` n'est plus dans le futur au moment de
         l'appel — re-vérification défensive, même si l'appelant (le
         fetch de fixtures) a déjà normalement filtré sur status="NS".
      2. Appelle ModelOrchestrator.predict_all() SANS MODIFICATION —
         produit des ModelPrediction role="active" (valeur par défaut de
         PredictionRecord), exactement comme un appel direct à
         /predictions/*.
      3. Si `include_shadow` : pour chaque ModelVersion status="shadow" dont
         le model_type est actuellement enregistré dans l'orchestrateur (et
         supporte l'inférence par version explicite, voir
         models_common.py::_MLPredictionModel.predict_for_shadow), produit
         une prédiction supplémentaire role="shadow" — jamais mélangée aux
         prédictions actives (voir docstring ModelPrediction.role).
    """
    now = now or datetime.now(timezone.utc)
    result = SchedulerRunResult()

    shadow_versions: list[ModelVersion] = []
    if include_shadow:
        shadow_versions = session.exec(select(ModelVersion).where(ModelVersion.status == "shadow")).all()

    shadow_models = {
        m.model_type: m for m in getattr(orchestrator, "_models", []) if isinstance(m, _MLPredictionModel)
    }

    for fixture in fixtures:
        result.fixtures_considered += 1
        if fixture.match_date <= now:
            result.fixtures_skipped_past += 1
            logger.info(
                f"fixture_skipped_past league={fixture.league} "
                f"{fixture.home_team} vs {fixture.away_team} match_date={fixture.match_date.isoformat()}"
            )
            continue

        ctx = MatchContext(
            league=fixture.league, home_team=fixture.home_team, away_team=fixture.away_team,
            match_date=fixture.match_date.date() if hasattr(fixture.match_date, "date") else fixture.match_date,
        )
        outcomes = orchestrator.predict_all(session, ctx, persist=persist)
        result.active_outcomes[f"{fixture.league}:{fixture.home_team} vs {fixture.away_team}"] = list(outcomes.values())

        if not shadow_versions:
            continue

        for shadow_version in shadow_versions:
            model = shadow_models.get(shadow_version.model_type)
            if model is None:
                continue  # ce model_type n'est pas géré en shadow (seuls XGBoost/LightGBM le sont, §20)
            try:
                outcome = model.predict_for_shadow(session, ctx, shadow_version)
            except Exception as e:
                msg = f"shadow_prediction_failed model_type={shadow_version.model_type} version_id={shadow_version.id} error={e!r}"
                logger.warning(msg)
                result.shadow_errors.append(msg)
                continue

            if outcome.status != "ok" or outcome.record is None:
                logger.info(
                    f"shadow_prediction_unavailable model_type={shadow_version.model_type} "
                    f"version_id={shadow_version.id} reason={outcome.reason}"
                )
                continue

            shadow_record = PredictionRecord(
                league=outcome.record.league, match_date=outcome.record.match_date,
                home_team=outcome.record.home_team, away_team=outcome.record.away_team,
                model_type=outcome.record.model_type,
                prob_home=outcome.record.prob_home, prob_draw=outcome.record.prob_draw, prob_away=outcome.record.prob_away,
                prob_btts_yes=outcome.record.prob_btts_yes, prob_btts_no=outcome.record.prob_btts_no,
                prob_over_2_5=outcome.record.prob_over_2_5, prob_under_2_5=outcome.record.prob_under_2_5,
                confidence=outcome.record.confidence, features_version=outcome.record.features_version,
                source="live", role="shadow",
            )
            if persist:
                log_prediction(session, shadow_record, shadow_version.id)
                session.commit()
            result.shadow_predictions_logged += 1

    return result
