"""
api/app/ai/arena/monitoring.py — Phase 9, Parties C/D (LIVE performance
monitoring + Model Health).

Tout ce qui a été mesuré jusqu'à la Phase 8 (GET /models/performance,
GET /models/benchmark) mélange, par défaut, LIVE et BACKTEST — comportement
Phase 5-7 volontairement inchangé (voir service.py). Ce module ajoute une vue
STRICTEMENT LIVE (`source="live"` explicite), jamais mélangée par défaut,
avec :

  1. get_live_monitoring()  : métriques par marché, sur plusieurs fenêtres
     glissantes (ALL_TIME/LAST_100/LAST_50/LAST_30_DAYS), chacune
     individuellement honnête sur l'insuffisance de données (§13 du ticket
     Phase 9 : une fenêtre avec trop peu de lignes ne prétend jamais être
     significative).
  2. get_live_summary()     : compteurs bruts par model_type (predictions_
     total/resolved/pending, last_prediction_at/last_resolved_at) — §12.
  3. compute_model_health() : compare une fenêtre "baseline" à une fenêtre
     "récente" pour détecter une dégradation (§16-18), avec des seuils
     PARAMÈTRES (pas des vérités scientifiques, voir doc des variables
     d'environnement ci-dessous) et un MIN_MONITORING_SAMPLE pour ne jamais
     alerter sur une poignée de matchs.

RÉUTILISE, sans réimplémentation, le calcul de métriques déjà existant en
Phase 5/6 (service.py::_market_observation / _compute_market_metrics /
_model_prediction_payload) — même formule log_loss/brier/accuracy/
calibration que GET /models/benchmark, jamais une seconde version qui
pourrait diverger.

`role` (Phase 9, voir app/models/model_prediction.py) : filtré explicitement
à "active" par défaut partout dans ce module — une prédiction "shadow"
n'entre JAMAIS dans les agrégats par défaut (§25 du ticket : "ne pas les
mélanger avec les prédictions actives"). Passer role="shadow" explicitement
pour mesurer une version en observation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional

from sqlmodel import Session, select

from app.models.model_prediction import ModelPrediction

from .availability import compute_model_availability
from .ensemble import KNOWN_MODEL_TYPES
from .models_common import PredictionModel
from .service import (
    MARKETS,
    MIN_BENCHMARK_SAMPLE_SIZE,
    MarketMetrics,
    _compute_market_metrics,
    _market_observation,
    _model_prediction_payload,
)

LiveWindow = Literal["ALL_TIME", "LAST_100", "LAST_50", "LAST_30_DAYS"]
DEFAULT_WINDOWS: tuple[LiveWindow, ...] = ("ALL_TIME", "LAST_100", "LAST_50", "LAST_30_DAYS")

# Paramètres de détection de dégradation (§18 du ticket Phase 9) — VALEURS
# PARAMÈTRES choisies par convention opérationnelle, PAS des vérités
# scientifiques (§18 : "Ne pas prétendre qu'elles sont optimales"). Origine :
# MIN_MONITORING_SAMPLE=30 est un seuil plus permissif que
# MIN_BENCHMARK_SAMPLE_SIZE (100, Phase 5) car le health check est un signal
# d'ALERTE PRÉCOCE (WARNING avant DEGRADED), pas une comparaison publiée —
# WARNING_DELTA/CRITICAL_DELTA (en log_loss, plus petit = meilleur donc un
# delta positif = dégradation) sont des paliers ronds arbitraires, à ajuster
# empiriquement une fois des données LIVE réelles accumulées (voir rapport
# final Phase 9, section Limitations).
MIN_MONITORING_SAMPLE = int(os.environ.get("MIN_MONITORING_SAMPLE", "30"))
HEALTH_WARNING_DELTA = float(os.environ.get("HEALTH_WARNING_DELTA", "0.05"))
HEALTH_CRITICAL_DELTA = float(os.environ.get("HEALTH_CRITICAL_DELTA", "0.15"))

HealthStatus = Literal["HEALTHY", "WARNING", "DEGRADED", "INSUFFICIENT_DATA", "UNAVAILABLE"]

# Seuils de significativité pour la comparaison Ensemble vs meilleur modèle
# individuel (§35 du ticket Phase 9) — mêmes deux paliers que le ticket
# nomme explicitement (PRELIMINARY/STATISTICALLY_RELEVANT), valeurs
# PARAMÈTRES documentées comme telles (même esprit que MIN_MONITORING_SAMPLE
# ci-dessus) : ENSEMBLE_PRELIMINARY_MIN_SAMPLE=20 est un strict minimum pour
# qu'un delta ait un sens directionnel du tout ; ENSEMBLE_RELEVANT_MIN_SAMPLE
# reprend MIN_BENCHMARK_SAMPLE_SIZE (Phase 5, 100) — même seuil que le reste
# de l'Arena pour "assez de données pour publier une conclusion".
BenchmarkStatus = Literal["INSUFFICIENT_DATA", "PRELIMINARY", "STATISTICALLY_RELEVANT"]
ENSEMBLE_PRELIMINARY_MIN_SAMPLE = int(os.environ.get("ENSEMBLE_PRELIMINARY_MIN_SAMPLE", "20"))
ENSEMBLE_RELEVANT_MIN_SAMPLE = int(os.environ.get("ENSEMBLE_RELEVANT_MIN_SAMPLE", str(MIN_BENCHMARK_SAMPLE_SIZE)))


@dataclass
class WindowStats:
    window: str
    status: Literal["OK", "INSUFFICIENT_DATA"]
    sample_size: int
    metrics: Optional[MarketMetrics] = None


@dataclass
class LiveSummary:
    model_type: str
    source: str
    role: str
    predictions_total: int
    predictions_resolved: int
    predictions_pending: int
    last_prediction_at: Optional[datetime]
    last_resolved_at: Optional[datetime]


@dataclass
class ModelHealth:
    model_type: str
    market: str
    status: HealthStatus
    reason: str
    baseline_window: str
    recent_window: str
    baseline_log_loss: Optional[float] = None
    recent_log_loss: Optional[float] = None
    delta: Optional[float] = None
    min_monitoring_sample: int = MIN_MONITORING_SAMPLE
    warning_delta: float = HEALTH_WARNING_DELTA
    critical_delta: float = HEALTH_CRITICAL_DELTA


def _base_query(model_type: str, source: str, role: str, resolved_only: bool):
    stmt = select(ModelPrediction).where(
        ModelPrediction.model_type == model_type,
        ModelPrediction.source == source,
        ModelPrediction.role == role,
    )
    if resolved_only:
        stmt = stmt.where(ModelPrediction.status == "resolved")
    return stmt


def _rows_for_window(session: Session, model_type: str, window: LiveWindow, *, source: str, role: str) -> list[ModelPrediction]:
    """
    Sélectionne les lignes RÉSOLUES pertinentes pour une fenêtre donnée.
    LAST_100/LAST_50 : les N plus récentes PAR DATE DE MATCH (jamais par
    ordre d'insertion, qui pourrait diverger si des résolutions arrivent en
    désordre). LAST_30_DAYS : toutes les lignes dont le match a eu lieu dans
    les 30 derniers jours (calendaires, pas un compte). ALL_TIME : aucune
    restriction.
    """
    stmt = _base_query(model_type, source, role, resolved_only=True).order_by(ModelPrediction.match_date.desc())
    if window == "ALL_TIME":
        return session.exec(stmt).all()
    if window == "LAST_100":
        return session.exec(stmt.limit(100)).all()
    if window == "LAST_50":
        return session.exec(stmt.limit(50)).all()
    if window == "LAST_30_DAYS":
        cutoff = date.today() - timedelta(days=30)
        return session.exec(stmt.where(ModelPrediction.match_date >= cutoff)).all()
    raise ValueError(f"Fenêtre inconnue : {window}")


def _window_stats(rows: list[ModelPrediction], window: LiveWindow, market: str, min_sample_size: int) -> WindowStats:
    observations = []
    for row in rows:
        obs = _market_observation(row, _model_prediction_payload(row), market)
        if obs is not None:
            observations.append(obs)

    if len(observations) < min_sample_size:
        return WindowStats(window=window, status="INSUFFICIENT_DATA", sample_size=len(observations))

    metrics = _compute_market_metrics(observations)
    return WindowStats(window=window, status="OK", sample_size=len(observations), metrics=metrics)


def get_live_monitoring(
    session: Session,
    model_type: str,
    market: str,
    *,
    windows: tuple[LiveWindow, ...] = DEFAULT_WINDOWS,
    min_sample_size: int = MIN_BENCHMARK_SAMPLE_SIZE,
    source: str = "live",
    role: str = "active",
) -> dict[str, WindowStats]:
    """
    Métriques LIVE (par défaut) d'un model_type sur UN marché, pour chaque
    fenêtre demandée — chaque fenêtre est gated INDÉPENDAMMENT (§13 : une
    fenêtre LAST_100 avec 37 lignes reste INSUFFICIENT_DATA même si ALL_TIME,
    lui, a assez de données).
    """
    if market not in MARKETS:
        raise ValueError(f"Marché inconnu : {market}")

    result: dict[str, WindowStats] = {}
    for window in windows:
        rows = _rows_for_window(session, model_type, window, source=source, role=role)
        result[window] = _window_stats(rows, window, market, min_sample_size)
    return result


def get_live_summary(
    session: Session, model_type: str, *, source: str = "live", role: str = "active",
) -> LiveSummary:
    """Compteurs bruts (§12), indépendants du marché — une ModelPrediction
    "pending" n'a par définition pas encore de résultat, donc pas de marché à
    évaluer."""
    all_rows = session.exec(_base_query(model_type, source, role, resolved_only=False)).all()
    resolved = [r for r in all_rows if r.status == "resolved"]
    pending = [r for r in all_rows if r.status == "pending"]

    last_prediction_at = max((r.predicted_at for r in all_rows), default=None)
    last_resolved_at = max(
        (r.result_fetched_at for r in resolved if r.result_fetched_at is not None), default=None,
    )

    return LiveSummary(
        model_type=model_type,
        source=source,
        role=role,
        predictions_total=len(all_rows),
        predictions_resolved=len(resolved),
        predictions_pending=len(pending),
        last_prediction_at=last_prediction_at,
        last_resolved_at=last_resolved_at,
    )


def compute_model_health(
    session: Session,
    model_type: str,
    market: str,
    *,
    baseline_window: LiveWindow = "ALL_TIME",
    recent_window: LiveWindow = "LAST_30_DAYS",
    min_monitoring_sample: int = MIN_MONITORING_SAMPLE,
    warning_delta: float = HEALTH_WARNING_DELTA,
    critical_delta: float = HEALTH_CRITICAL_DELTA,
    source: str = "live",
    role: str = "active",
    models: Optional[list[PredictionModel]] = None,
) -> ModelHealth:
    """
    Compare le log_loss d'une fenêtre "récente" à une fenêtre "baseline"
    (§17). delta = recent.log_loss - baseline.log_loss (log_loss : plus
    petit = meilleur, donc delta > 0 = dégradation).

      - `models` fourni (liste des PredictionModel réellement enregistrés,
        ex. main.py::_PREDICTION_MODELS) ET check_availability() dit non
        disponible -> UNAVAILABLE, prioritaire sur tout calcul de delta
        (un modèle indisponible n'a pas de santé à mesurer). `models=None`
        (ex. tests unitaires isolés de l'orchestrateur) -> cette vérification
        est simplement sautée, JAMAIS supposée indisponible par défaut.
      - échantillon baseline OU récent < min_monitoring_sample ->
        INSUFFICIENT_DATA (§17 : "ne pas désactiver automatiquement le
        modèle sur quelques matchs").
      - delta <= warning_delta -> HEALTHY
      - warning_delta < delta <= critical_delta -> WARNING
      - delta > critical_delta -> DEGRADED
    """
    if models is not None:
        availability = compute_model_availability(session, models)
        avail = availability.get(model_type)
        if avail is not None and not avail.live_available:
            return ModelHealth(
                model_type=model_type, market=market, status="UNAVAILABLE",
                reason=avail.live_reason or "Modèle non disponible en direct actuellement.",
                baseline_window=baseline_window, recent_window=recent_window,
                min_monitoring_sample=min_monitoring_sample,
                warning_delta=warning_delta, critical_delta=critical_delta,
            )

    monitoring = get_live_monitoring(
        session, model_type, market, windows=(baseline_window, recent_window),
        min_sample_size=min_monitoring_sample, source=source, role=role,
    )
    baseline, recent = monitoring[baseline_window], monitoring[recent_window]

    if baseline.status != "OK" or recent.status != "OK" or baseline.metrics.log_loss is None or recent.metrics.log_loss is None:
        return ModelHealth(
            model_type=model_type, market=market, status="INSUFFICIENT_DATA",
            reason=(
                f"Échantillon insuffisant pour comparer {baseline_window} "
                f"({baseline.sample_size}) et {recent_window} ({recent.sample_size}) "
                f"contre le seuil min_monitoring_sample={min_monitoring_sample}."
            ),
            baseline_window=baseline_window, recent_window=recent_window,
            min_monitoring_sample=min_monitoring_sample,
            warning_delta=warning_delta, critical_delta=critical_delta,
        )

    delta = round(recent.metrics.log_loss - baseline.metrics.log_loss, 4)
    if delta <= warning_delta:
        status: HealthStatus = "HEALTHY"
        reason = f"Log loss récent ({recent.metrics.log_loss}) proche ou meilleur que la baseline ({baseline.metrics.log_loss})."
    elif delta <= critical_delta:
        status = "WARNING"
        reason = f"Dégradation modérée détectée : delta log_loss = {delta} (seuil warning={warning_delta})."
    else:
        status = "DEGRADED"
        reason = f"Dégradation importante détectée : delta log_loss = {delta} (seuil critical={critical_delta})."

    return ModelHealth(
        model_type=model_type, market=market, status=status, reason=reason,
        baseline_window=baseline_window, recent_window=recent_window,
        baseline_log_loss=baseline.metrics.log_loss, recent_log_loss=recent.metrics.log_loss,
        delta=delta, min_monitoring_sample=min_monitoring_sample,
        warning_delta=warning_delta, critical_delta=critical_delta,
    )


@dataclass
class EnsembleDelta:
    market: str
    benchmark_status: BenchmarkStatus
    sample_size: Optional[int] = None
    ensemble_log_loss: Optional[float] = None
    best_individual_model: Optional[str] = None
    best_individual_log_loss: Optional[float] = None
    delta_log_loss: Optional[float] = None      # ensemble - best individuel (négatif = Ensemble meilleur)
    delta_brier: Optional[float] = None         # idem, plus petit = meilleur
    delta_accuracy: Optional[float] = None      # ensemble - best individuel (positif = Ensemble meilleur)
    reason: Optional[str] = None


def compute_ensemble_delta(
    session: Session,
    market: str,
    *,
    window: LiveWindow = "ALL_TIME",
    source: str = "live",
    role: str = "active",
    preliminary_min_sample: int = ENSEMBLE_PRELIMINARY_MIN_SAMPLE,
    relevant_min_sample: int = ENSEMBLE_RELEVANT_MIN_SAMPLE,
) -> EnsembleDelta:
    """
    Compare l'Ensemble au MEILLEUR modèle individuel sur données LIVE
    UNIQUEMENT (§33-35) : `delta = Ensemble - meilleur individuel` — pour
    log_loss/brier (plus petit = meilleur), un delta NÉGATIF signifie
    "Ensemble meilleur" ; pour accuracy (plus grand = meilleur), un delta
    POSITIF signifie "Ensemble meilleur" (§34 : attention au sens inversé
    entre les deux familles de métriques).

    Ne conclut JAMAIS "Ensemble meilleur"/"Ensemble moins bon" sur un petit
    échantillon (§35) : `benchmark_status` reflète le sample_size le plus
    petit des deux séries comparées (Ensemble et meilleur individuel) —
    INSUFFICIENT_DATA sous `preliminary_min_sample`, PRELIMINARY sous
    `relevant_min_sample`, STATISTICALLY_RELEVANT au-delà seulement.
    """
    if market not in MARKETS:
        raise ValueError(f"Marché inconnu : {market}")

    ensemble_stats = get_live_monitoring(
        session, "ensemble", market, windows=(window,), min_sample_size=1, source=source, role=role,
    )[window]
    if ensemble_stats.status != "OK" or ensemble_stats.metrics is None or ensemble_stats.metrics.log_loss is None:
        return EnsembleDelta(
            market=market, benchmark_status="INSUFFICIENT_DATA",
            sample_size=ensemble_stats.sample_size,
            reason="Aucune prédiction Ensemble LIVE résolue avec log_loss disponible sur cette fenêtre.",
        )

    candidates: list[tuple[str, float, float, float, int]] = []  # (model_type, log_loss, brier, accuracy, n)
    for model_type in KNOWN_MODEL_TYPES:
        stats = get_live_monitoring(
            session, model_type, market, windows=(window,), min_sample_size=1, source=source, role=role,
        )[window]
        if stats.status == "OK" and stats.metrics is not None and stats.metrics.log_loss is not None:
            candidates.append((
                model_type, stats.metrics.log_loss, stats.metrics.brier_score,
                stats.metrics.accuracy, stats.sample_size,
            ))

    if not candidates:
        return EnsembleDelta(
            market=market, benchmark_status="INSUFFICIENT_DATA",
            sample_size=ensemble_stats.sample_size, ensemble_log_loss=ensemble_stats.metrics.log_loss,
            reason="Aucun modèle individuel avec des métriques LIVE résolues sur cette fenêtre — comparaison impossible.",
        )

    best_model_type, best_log_loss, best_brier, best_accuracy, best_n = min(candidates, key=lambda c: c[1])
    min_sample = min(ensemble_stats.sample_size, best_n)

    if min_sample < preliminary_min_sample:
        benchmark_status: BenchmarkStatus = "INSUFFICIENT_DATA"
    elif min_sample < relevant_min_sample:
        benchmark_status = "PRELIMINARY"
    else:
        benchmark_status = "STATISTICALLY_RELEVANT"

    return EnsembleDelta(
        market=market, benchmark_status=benchmark_status, sample_size=min_sample,
        ensemble_log_loss=ensemble_stats.metrics.log_loss,
        best_individual_model=best_model_type, best_individual_log_loss=best_log_loss,
        delta_log_loss=round(ensemble_stats.metrics.log_loss - best_log_loss, 4),
        delta_brier=round(ensemble_stats.metrics.brier_score - best_brier, 4),
        delta_accuracy=round(ensemble_stats.metrics.accuracy - best_accuracy, 4),
    )
