"""
api/app/ai/arena/shadow_comparison.py — Phase 11 : comparaison "matched"
ACTIVE vs SHADOW — UNIQUEMENT sur les matchs que les DEUX ont réellement
prédits et résolus.

Distinct de app/ai/arena/live_validation.py (Phase 10) : celui-ci compare
deux échantillons LIVE INDÉPENDANTS (tout l'historique résolu de chaque
version), utile pour une décision de promotion globale. Ce module-ci répond
à une question plus stricte : "sur EXACTEMENT les mêmes matchs, qui a
raison le plus souvent ?" — une différence mesurée sur des fenêtres
différentes n'est jamais une comparaison directe valable (voir §9 du ticket
Phase 11).

RÉUTILISE, sans réimplémentation, `service.py::_market_observation`/
`_compute_market_metrics` (même formule que tout le reste de l'Arena) et
`promotion.py::get_active_version`/`LIVE_MIN_SAMPLE_SIZE` (Phase 10, aucun
nouveau seuil créé — §12 du ticket Phase 11 : "ne pas changer les seuils de
promotion").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from sqlmodel import Session, select

from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion

from . import promotion as arena_promotion
from .service import MARKETS, _market_observation, _compute_market_metrics, _model_prediction_payload

MatchedComparisonStatus = str  # "no_active" | "no_shadow" | "insufficient_matched_sample" | "ok"


@dataclass
class MatchedComparison:
    model_type: str
    market: str
    status: MatchedComparisonStatus
    reason: str
    active_version_id: Optional[int] = None
    shadow_version_id: Optional[int] = None
    matched_sample_size: int = 0
    active_accuracy: Optional[float] = None
    active_log_loss: Optional[float] = None
    active_brier: Optional[float] = None
    shadow_accuracy: Optional[float] = None
    shadow_log_loss: Optional[float] = None
    shadow_brier: Optional[float] = None
    # shadow - active : négatif (log_loss/brier) = shadow meilleur ; positif (accuracy) = shadow meilleur.
    delta_log_loss: Optional[float] = None
    delta_brier: Optional[float] = None
    delta_accuracy: Optional[float] = None
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    min_matched_sample_size: int = arena_promotion.LIVE_MIN_SAMPLE_SIZE


def _resolved_rows(session: Session, model_type: str, model_version_id: int, role: str) -> list[ModelPrediction]:
    return session.exec(
        select(ModelPrediction).where(
            ModelPrediction.model_type == model_type,
            ModelPrediction.model_version_id == model_version_id,
            ModelPrediction.role == role,
            ModelPrediction.source == "live",
            ModelPrediction.status == "resolved",
        )
    ).all()


def _natural_key(row: ModelPrediction) -> tuple:
    return (row.league, row.match_date, row.home_team, row.away_team)


def _latest_shadow_version(session: Session, model_type: str) -> Optional[ModelVersion]:
    """La version shadow la plus récente pour ce model_type (par id, donc
    par ordre de création) — s'il en existe plusieurs simultanément, seule
    la plus récente est comparée par défaut (voir get_shadow_version_id
    pour comparer une version précise)."""
    return session.exec(
        select(ModelVersion)
        .where(ModelVersion.model_type == model_type, ModelVersion.status == "shadow")
        .order_by(ModelVersion.id.desc())
    ).first()


def compute_matched_comparison(
    session: Session, model_type: str, market: str = "1X2", *, shadow_version_id: Optional[int] = None,
) -> MatchedComparison:
    """
    Compare la version ACTIVE et une version SHADOW du même `model_type`,
    UNIQUEMENT sur l'intersection des matchs que les deux ont effectivement
    prédits ET résolus — jamais deux échantillons indépendants présentés
    comme si c'était la même comparaison.

    `shadow_version_id` optionnel : sinon, la version shadow la plus
    récente de ce `model_type` est utilisée (voir _latest_shadow_version).
    """
    if market not in MARKETS:
        raise ValueError(f"Marché inconnu : {market}")

    active = arena_promotion.get_active_version(session, model_type)
    if active is None:
        return MatchedComparison(
            model_type=model_type, market=market, status="no_active",
            reason=f"Aucune version active pour model_type={model_type}.",
        )

    shadow = (
        session.get(ModelVersion, shadow_version_id) if shadow_version_id is not None
        else _latest_shadow_version(session, model_type)
    )
    if shadow is None or shadow.model_type != model_type or shadow.status != "shadow":
        return MatchedComparison(
            model_type=model_type, market=market, status="no_shadow",
            reason=f"Aucune version shadow active pour model_type={model_type}.",
            active_version_id=active.id,
        )

    active_rows = {_natural_key(r): r for r in _resolved_rows(session, model_type, active.id, "active")}
    shadow_rows = {_natural_key(r): r for r in _resolved_rows(session, model_type, shadow.id, "shadow")}
    matched_keys = set(active_rows) & set(shadow_rows)

    min_sample = arena_promotion.LIVE_MIN_SAMPLE_SIZE
    if len(matched_keys) < min_sample:
        return MatchedComparison(
            model_type=model_type, market=market, status="insufficient_matched_sample",
            reason=(
                f"Échantillon matched insuffisant : {len(matched_keys)} match(s) prédit(s) ET résolu(s) par "
                f"les DEUX versions < min_matched_sample_size={min_sample}."
            ),
            active_version_id=active.id, shadow_version_id=shadow.id,
            matched_sample_size=len(matched_keys),
        )

    active_obs, shadow_obs, dates = [], [], []
    for key in matched_keys:
        a_row, s_row = active_rows[key], shadow_rows[key]
        a_obs = _market_observation(a_row, _model_prediction_payload(a_row), market)
        s_obs = _market_observation(s_row, _model_prediction_payload(s_row), market)
        # Un marché non modélisé par ce model_type (ex. BTTS pour xgboost) renvoie None des
        # deux côtés de façon cohérente — jamais un côté rempli et l'autre vide sur le même
        # match, puisque active et shadow sont TOUJOURS la même implémentation de modèle.
        if a_obs is None or s_obs is None:
            continue
        active_obs.append(a_obs)
        shadow_obs.append(s_obs)
        dates.append(key[1])

    if len(active_obs) < min_sample:
        return MatchedComparison(
            model_type=model_type, market=market, status="insufficient_matched_sample",
            reason=(
                f"Marché {market} non modélisé (ou insuffisant) sur l'échantillon matched : "
                f"{len(active_obs)} observation(s) exploitable(s) < min_matched_sample_size={min_sample}."
            ),
            active_version_id=active.id, shadow_version_id=shadow.id,
            matched_sample_size=len(matched_keys),
        )

    active_metrics = _compute_market_metrics(active_obs)
    shadow_metrics = _compute_market_metrics(shadow_obs)

    return MatchedComparison(
        model_type=model_type, market=market, status="ok",
        reason=f"{len(active_obs)} match(s) comparés directement (mêmes matchs, actif vs shadow).",
        active_version_id=active.id, shadow_version_id=shadow.id,
        matched_sample_size=len(active_obs),
        active_accuracy=active_metrics.accuracy, active_log_loss=active_metrics.log_loss, active_brier=active_metrics.brier_score,
        shadow_accuracy=shadow_metrics.accuracy, shadow_log_loss=shadow_metrics.log_loss, shadow_brier=shadow_metrics.brier_score,
        delta_log_loss=(round(shadow_metrics.log_loss - active_metrics.log_loss, 4)
                         if shadow_metrics.log_loss is not None and active_metrics.log_loss is not None else None),
        delta_brier=(round(shadow_metrics.brier_score - active_metrics.brier_score, 4)
                     if shadow_metrics.brier_score is not None and active_metrics.brier_score is not None else None),
        delta_accuracy=(round(shadow_metrics.accuracy - active_metrics.accuracy, 4)
                         if shadow_metrics.accuracy is not None and active_metrics.accuracy is not None else None),
        period_start=min(dates) if dates else None,
        period_end=max(dates) if dates else None,
    )
