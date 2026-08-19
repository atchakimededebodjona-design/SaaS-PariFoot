"""
api/app/ai/arena/live_validation.py — Phase 10 : métriques LIVE d'UNE
ModelVersion précise (jamais agrégées par `role` seul, voir docstring
promotion.py::evaluate_live_promotion pour la raison).

RÉUTILISE, sans réimplémentation, `service.py::_model_predictions_markets`
(donc `_market_observation`/`_compute_market_metrics`, déjà utilisés par
GET /models/performance|benchmark ET par monitoring.py) — même formule
log_loss/brier/accuracy/calibration partout, jamais une seconde version qui
pourrait diverger.

`prediction_source="live"` toujours fixé ici (jamais "backtest" : un backtest
n'est pas une preuve de performance EN PRODUCTION, précisément ce que la
Phase 10 doit mesurer) — distinct du filtre par défaut (`None`, live+backtest
confondus) utilisé par service.py pour les vues Phase 5/6/7 inchangées.

N'estime jamais rien sur les lignes `status="pending"` (§13 du ticket
Phase 10) : `predictions_pending` est un simple compteur, jamais inclus dans
`sample_size`/les métriques.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from sqlmodel import Session, select

from app.models.model_prediction import ModelPrediction

from .service import MARKETS, _model_predictions_markets


@dataclass
class LiveModelMetrics:
    model_type: str
    model_version_id: int
    market: str
    sample_size: int
    predictions_resolved: int
    predictions_pending: int
    accuracy: Optional[float] = None
    log_loss: Optional[float] = None
    brier_score: Optional[float] = None
    calibration: Optional[list] = None
    period_start: Optional[date] = None
    period_end: Optional[date] = None


def compute_live_model_metrics(
    session: Session, model_type: str, model_version_id: int, market: str = "1X2",
) -> LiveModelMetrics:
    """
    Métriques LIVE (`source="live"`) d'UNE `model_version_id` précise, sur UN
    marché — jamais mélangée avec une autre version du même `model_type`
    (contrairement à monitoring.py, qui agrège par `role` sur tout
    l'historique d'un `model_type` : suffisant pour un tableau de bord de
    tendance, mais PAS pour décider si CE candidat précis mérite d'être
    promu face à LA version active précise).
    """
    if market not in MARKETS:
        raise ValueError(f"Marché inconnu : {market}")

    markets = _model_predictions_markets(
        session, model_type, model_version_id, league=None, since=None, until=None, prediction_source="live",
    )
    metrics = markets[market]

    all_rows = session.exec(
        select(ModelPrediction).where(
            ModelPrediction.model_type == model_type,
            ModelPrediction.model_version_id == model_version_id,
            ModelPrediction.source == "live",
        )
    ).all()
    resolved = [r for r in all_rows if r.status == "resolved"]
    pending = [r for r in all_rows if r.status == "pending"]
    match_dates = [r.match_date for r in all_rows]

    return LiveModelMetrics(
        model_type=model_type,
        model_version_id=model_version_id,
        market=market,
        sample_size=metrics.sample_size,
        predictions_resolved=len(resolved),
        predictions_pending=len(pending),
        accuracy=metrics.accuracy,
        log_loss=metrics.log_loss,
        brier_score=metrics.brier_score,
        calibration=metrics.calibration,
        period_start=min(match_dates) if match_dates else None,
        period_end=max(match_dates) if match_dates else None,
    )
