"""
Model Orchestrator — Phase 7 (§8-§9-§27-§28 du ticket).

Point d'entrée UNIQUE pour appeler « tous les modèles connus » sur un même
match — main.py (et scripts/walk_forward_ensemble.py) ne parlent jamais
directement à DixonColesPredictionModel/EloPredictionModel/etc., toujours à
travers ModelOrchestrator.predict_all(), pour qu'il n'existe qu'un seul
endroit où « la liste des modèles » et « ce qu'on fait quand l'un d'eux
échoue » sont définis (§8 : responsabilités listées dans le ticket).

Gestion des erreurs (§9) : un modèle qui lève une exception, retourne
"unavailable" ou "error" n'empêche JAMAIS les autres de produire leur
prédiction — chaque appel est isolé dans son propre try/except, et le
résultat (succès ou échec typé) est toujours renvoyé pour CHAQUE modèle du
registre, jamais silencieusement omis.

Cache (§27, révisé Phase 8 §31) : LEAGUE_MODELS (Dixon-Coles) est déjà chargé
une fois au démarrage du service web (voir main.py) ; Elo ne lit que de
petites requêtes indexées (team_ratings/model_versions) par appel, jamais un
ré-entraînement. XGBoost/LightGBM, eux, désérialisent un booster à chaque
ModelVersion active différente — trop coûteux pour le refaire à CHAQUE
requête : voir models_common.py::_MLPredictionModel._get_booster, qui garde
en cache le dernier booster chargé PAR INSTANCE, invalidé dès qu'une version
différente est demandée. L'orchestrateur, lui, réutilise les MÊMES instances
de PredictionModel entre appels (voir main.py : `default_models(...)` n'est
PAS reconstruit à chaque requête) — condition nécessaire pour que ce cache
serve à quelque chose.
"""

from __future__ import annotations

import logging

from sqlmodel import Session

from .models_common import (
    DixonColesPredictionModel,
    EloPredictionModel,
    LightGBMPredictionModel,
    MatchContext,
    PredictionModel,
    PredictionOutcome,
    XGBoostPredictionModel,
)
from .prediction_logging import log_prediction

logger = logging.getLogger("xfoot.arena.orchestrator")


def default_models(league_models: dict) -> list[PredictionModel]:
    """
    Registre par défaut des modèles connus (§2 : Dixon-Coles, Elo, XGBoost,
    LightGBM) — un futur modèle s'ajoute UNIQUEMENT ici, jamais en dupliquant
    ModelOrchestrator ou en modifiant les appelants (main.py,
    scripts/walk_forward_ensemble.py, tests).
    """
    return [
        DixonColesPredictionModel(league_models),
        EloPredictionModel(),
        XGBoostPredictionModel(league_models),
        LightGBMPredictionModel(league_models),
    ]


class ModelOrchestrator:
    def __init__(self, models: list[PredictionModel]):
        self._models = models

    def predict_all(self, session: Session, ctx: MatchContext, persist: bool = True) -> dict[str, PredictionOutcome]:
        """
        Appelle CHAQUE modèle du registre sur `ctx`, journalise chaque
        prédiction réussie dans model_predictions (source="live", via le
        logger commun — §6 du ticket Phase 6, jamais un second mécanisme
        d'écriture), puis retourne un résultat PAR model_type, succès ou
        échec, jamais partiel. `persist=False` (tests/walk-forward) permet
        d'obtenir les PredictionOutcome sans écrire en base.
        """
        match_label = f"{ctx.league}:{ctx.home_team} vs {ctx.away_team} ({ctx.match_date.isoformat()})"
        logger.info(f"ensemble_prediction_started match={match_label}")

        results: dict[str, PredictionOutcome] = {}
        for model in self._models:
            try:
                outcome = model.predict(session, ctx)
            except Exception as e:
                # Un modèle qui plante ne doit jamais interrompre les autres (§9).
                logger.warning(f"model_prediction_failed model_type={model.model_type} match={match_label} error={e!r}")
                results[model.model_type] = PredictionOutcome(
                    model.model_type, "error", reason=f"Exception inattendue : {e}",
                )
                continue

            if outcome.status == "ok" and outcome.record is not None:
                logger.info(
                    f"model_prediction_success model_type={model.model_type} "
                    f"model_version_id={outcome.model_version_id} match={match_label}"
                )
                if persist:
                    try:
                        log_prediction(session, outcome.record, outcome.model_version_id)
                        session.commit()
                    except Exception as e:
                        logger.warning(
                            f"model_prediction_failed model_type={model.model_type} match={match_label} "
                            f"error=persist_failed:{e!r}"
                        )
            else:
                logger.info(
                    f"model_excluded model_type={model.model_type} status={outcome.status} "
                    f"match={match_label} reason={outcome.reason}"
                )
            results[model.model_type] = outcome

        return results
