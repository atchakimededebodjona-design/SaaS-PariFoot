"""
api/app/ai/arena/availability.py — Phase 8, Partie B (§12-§13 du ticket).

Sépare explicitement quatre notions jusqu'ici confondues derrière le seul
champ ModelVersion.is_active (Phase 3-7) :

  - active             : cette version est-elle la version DÉPLOYÉE pour ce
                          model_type (ModelVersion.is_active) ? Un état
                          désormais purement OPÉRATIONNEL (Phase 8, §13) —
                          plus un jugement de performance : Dixon-Coles, Elo,
                          XGBoost, LightGBM s'activent tous INCONDITIONNELLEMENT
                          sur un entraînement réussi (voir
                          scripts/backtest_elo.py, scripts/train_ml_stacking_from_db.py).
  - live_available      : cette version PEUT-ELLE réellement produire une
                          prédiction en direct maintenant (artefact/config/
                          ratings chargeables) ? Calculé en réutilisant TEL
                          QUEL PredictionModel.check_availability() (voir
                          models_common.py) — jamais une seconde
                          implémentation qui pourrait diverger de celle
                          RÉELLEMENT utilisée par ModelOrchestrator.
  - benchmark_eligible  : PAR MARCHÉ — cette version a-t-elle assez de
                          données résolues (>= MIN_BENCHMARK_SAMPLE_SIZE,
                          seuil Phase 5 inchangé) pour que ses métriques
                          soient dignes de confiance ?
  - ensemble_eligible   : PAR MARCHÉ — live_available ET benchmark_eligible
                          sur CE marché précis. C'est EXACTEMENT la condition
                          que EnsembleEngine.compute_market_weights applique
                          déjà (voir ensemble.py) — réutilisée ici pour
                          affichage, jamais recalculée séparément.

Exemple concret (celui qui motive cette Phase, §13) : Elo peut être `active`
(déployé) et `live_available` (calibration exploitable) alors même qu'il n'a
que 5 prédictions résolues sur un marché — `benchmark_eligible=False` sur ce
marché reflète honnêtement cette insuffisance SANS avoir besoin de
désactiver le modèle entier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from sqlmodel import Session

from .ensemble import KNOWN_MODEL_TYPES, MARKET_OUTCOME_KEYS, MIN_BENCHMARK_SAMPLE_SIZE, _active_version, _market_metrics_for_model
from .models_common import PredictionModel


@dataclass
class MarketAvailability:
    benchmark_eligible: bool
    ensemble_eligible: bool
    sample_size: int
    reason: Optional[str] = None


@dataclass
class ModelAvailability:
    model_type: str
    active: bool
    model_version_id: Optional[int]
    version_name: Optional[str]
    live_available: bool
    live_reason: Optional[str]
    markets: dict[str, MarketAvailability] = field(default_factory=dict)


def compute_model_availability(
    session: Session,
    models: list[PredictionModel],
    as_of: Optional[date] = None,
    min_sample_size: int = MIN_BENCHMARK_SAMPLE_SIZE,
) -> dict[str, ModelAvailability]:
    """
    Calcule la matrice de disponibilité pour chaque model_type CONNU
    (dixon_coles/elo/xgboost/lightgbm — jamais "ensemble" lui-même, même
    exclusion que ensemble.py::KNOWN_MODEL_TYPES). `as_of` (défaut :
    aujourd'hui) est la même borne anti-fuite que compute_market_weights —
    un modèle inconnu de `models` (aucun PredictionModel enregistré pour ce
    type) est honnêtement live_available=False, jamais supposé disponible.
    """
    as_of = as_of or date.today()
    by_type = {m.model_type: m for m in models}
    result: dict[str, ModelAvailability] = {}

    for model_type in KNOWN_MODEL_TYPES:
        version = _active_version(session, model_type)
        pm = by_type.get(model_type)
        check = pm.check_availability(session) if pm is not None else None
        live_available = bool(check and check.live_available)
        live_reason = check.reason if check is not None else "Aucun PredictionModel enregistré pour ce type."

        markets: dict[str, MarketAvailability] = {}
        for market in MARKET_OUTCOME_KEYS:
            if version is None:
                markets[market] = MarketAvailability(
                    benchmark_eligible=False, ensemble_eligible=False, sample_size=0,
                    reason="Aucune version active pour ce type de modèle.",
                )
                continue

            metrics = _market_metrics_for_model(session, model_type, version.id, market, as_of)
            benchmark_eligible = metrics.sample_size >= min_sample_size and metrics.log_loss is not None
            ensemble_eligible = benchmark_eligible and live_available

            reason = None
            if not benchmark_eligible:
                reason = (
                    f"Échantillon résolu insuffisant sur {market} ({metrics.sample_size} < {min_sample_size}) "
                    "ou marché non modélisé par ce type de modèle."
                )
            elif not ensemble_eligible:
                reason = f"Historique suffisant sur {market}, mais modèle non live_available actuellement : {live_reason}"

            markets[market] = MarketAvailability(
                benchmark_eligible=benchmark_eligible, ensemble_eligible=ensemble_eligible,
                sample_size=metrics.sample_size, reason=reason,
            )

        result[model_type] = ModelAvailability(
            model_type=model_type,
            active=version.is_active if version is not None else False,
            model_version_id=version.id if version is not None else None,
            version_name=version.name if version is not None else None,
            live_available=live_available,
            live_reason=live_reason,
            markets=markets,
        )

    return result
