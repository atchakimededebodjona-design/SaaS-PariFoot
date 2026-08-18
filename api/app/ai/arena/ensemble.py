"""
Ensemble Engine — Phase 7 (§11-§19 du ticket).

Combine les prédictions individuelles DÉJÀ produites (par ModelOrchestrator,
voir orchestrator.py) en une prédiction finale pondérée, par marché,
uniquement à partir de poids dérivés de métriques historiques réellement
mesurées — jamais un poids arbitraire (§13).

=== Anti-fuite (§19) — le point le plus important de ce module ===

Les poids d'un marché, pour une prédiction dont le match a lieu à
`match_date`, sont calculés à partir des SEULES prédictions résolues dont
le match a eu lieu STRICTEMENT AVANT `match_date` (voir `compute_market_weights`,
paramètre `until`, réutilisant _dixon_coles_markets/_model_predictions_markets
de service.py avec une borne supérieure explicite). Pour une prédiction
LIVE (aujourd'hui), cette contrainte est automatiquement respectée : aucune
prédiction future n'existe encore en base. Pour une évaluation walk-forward
historique (scripts/walk_forward_ensemble.py), c'est ce même paramètre qui
garantit qu'aucun résultat du fold évalué n'entre dans le calcul de ses
propres poids.

=== Sélection des modèles éligibles (§11) ===

Un model_type est éligible à peser dans l'Ensemble pour UN marché si, à la
date `until` :
  1. il a une ModelVersion active (sinon : NOT_AVAILABLE en LIVE de toute
     façon, voir models_common.py) ;
  2. ce marché a au moins `min_sample_size` (défaut MIN_BENCHMARK_SAMPLE_SIZE,
     même seuil que la Phase 5 — §31) prédictions résolues pour ce marché ;
  3. sa log_loss sur cet échantillon est disponible (jamais None).

=== Ce qui se passe si un modèle éligible n'a, pour CE match précis, pas de
    prédiction valide (indisponible/erreur) ===

Les poids sont recalculés (renormalisés) sur le sous-ensemble des modèles
qui ont RÉELLEMENT produit une probabilité valide pour ce marché sur ce
match (voir `combine`) — jamais une probabilité manquante remplacée par une
valeur par défaut. Si un seul modèle reste, l'Ensemble de ce marché est ce
modèle seul (`degraded=True`, `models_used=1`) : un résultat honnête plutôt
qu'un NOT_AVAILABLE qui masquerait une prédiction pourtant disponible — mais
jamais présenté comme une vraie combinaison multi-modèles (voir le champ
`degraded` dans la réponse API et le tableau de bord Arena).
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from sqlmodel import Session, select

from app.models.team_rating import ModelVersion

from .prediction_logging import PredictionRecord, get_or_create_active_model_version, log_prediction
from .schemas import MarketMetrics
from .service import MIN_BENCHMARK_SAMPLE_SIZE, _dixon_coles_markets, _model_predictions_markets

logger = logging.getLogger("xfoot.arena.ensemble")

KNOWN_MODEL_TYPES = ("dixon_coles", "elo", "xgboost", "lightgbm")

# Marché -> clés d'issue, dans l'ordre stable utilisé pour la réponse API et
# la persistance dans model_predictions (voir _combined_to_record).
MARKET_OUTCOME_KEYS: dict[str, tuple[str, ...]] = {
    "1X2": ("home_win", "draw", "away_win"),
    "BTTS": ("yes", "no"),
    "OVER_UNDER_2_5": ("over", "under"),
}

_WEIGHT_EPS = 1e-4  # protection contre une division par un log_loss/brier quasi nul (§14)


@dataclass
class ModelWeight:
    model_type: str
    version: str
    model_version_id: Optional[int]
    weight: float
    log_loss: float
    sample_size: int
    brier_score: Optional[float] = None
    league_fallback: bool = False  # True si la version ligue n'avait pas assez de données -> repli sur le global (§24)


@dataclass
class WeightResult:
    market: str
    until: date
    strategy: str = "inverse_log_loss"
    league: Optional[str] = None
    weights: dict[str, ModelWeight] = field(default_factory=dict)
    excluded: dict[str, str] = field(default_factory=dict)
    metric: str = "log_loss"


# ---------------------------------------------------------------------------
# Weight strategies — Phase 8, Partie C (§14-§18 du ticket).
#
# Toutes reçoivent le MÊME dict {model_type: MarketMetrics} (déjà calculé par
# compute_market_weights à partir de service.py, jamais recalculé deux fois)
# et retournent un score BRUT positif par model_type — jamais déjà normalisé
# à somme 1 : la normalisation finale (score/somme) reste TOUJOURS faite au
# même endroit (compute_market_weights), pour ne jamais dupliquer cette
# arithmétique entre stratégies (source unique de vérité, même principe que
# partout ailleurs dans l'Arena). Un model_type absent du dict retourné est
# simplement exclu par l'appelant (ex. BrierStrategy sur un modèle sans
# brier_score) — jamais une exception, jamais un score fabriqué à 0.
# ---------------------------------------------------------------------------

class WeightStrategy(ABC):
    name: str

    @abstractmethod
    def compute_scores(self, metrics: dict[str, MarketMetrics]) -> dict[str, float]:
        ...


class InverseLogLossStrategy(WeightStrategy):
    """Stratégie de référence (Phase 7, conservée telle quelle — §15) :
    score = 1 / max(log_loss, epsilon)."""

    name = "inverse_log_loss"

    def __init__(self, epsilon: float = _WEIGHT_EPS):
        self.epsilon = epsilon

    def compute_scores(self, metrics: dict[str, MarketMetrics]) -> dict[str, float]:
        return {mt: 1.0 / max(m.log_loss, self.epsilon) for mt, m in metrics.items() if m.log_loss is not None}


class SoftmaxLogLossStrategy(WeightStrategy):
    """
    §16 : score = exp(-temperature * log_loss). `temperature` contrôle le
    contraste : élevée -> le meilleur modèle rafle presque tout le poids ;
    faible -> poids presque uniformes entre modèles éligibles. Valeur par
    défaut modérée (5.0) choisie arbitrairement comme point de départ, JAMAIS
    optimisée contre le test final ici — voir
    scripts/walk_forward_ensemble.py::select_softmax_temperature (§20) pour
    la sélection par validation temporelle, seul endroit où `temperature`
    doit être réellement choisie.
    """

    name = "softmax_log_loss"

    def __init__(self, temperature: float = 5.0):
        self.temperature = temperature

    def compute_scores(self, metrics: dict[str, MarketMetrics]) -> dict[str, float]:
        return {mt: math.exp(-self.temperature * m.log_loss) for mt, m in metrics.items() if m.log_loss is not None}


class BrierStrategy(WeightStrategy):
    """§17 : score = 1 / max(brier_score, epsilon) — plus bas = meilleur,
    même convention que log_loss. Un modèle sans brier_score disponible
    (ne devrait pas arriver, brier_score est calculé en même temps que
    log_loss dans service.py, mais jamais supposé) est naturellement exclu,
    jamais fabriqué."""

    name = "brier"

    def __init__(self, epsilon: float = _WEIGHT_EPS):
        self.epsilon = epsilon

    def compute_scores(self, metrics: dict[str, MarketMetrics]) -> dict[str, float]:
        return {mt: 1.0 / max(m.brier_score, self.epsilon) for mt, m in metrics.items() if m.brier_score is not None}


def _normalize_scores(scores: dict[str, float]) -> dict[str, float]:
    total = sum(scores.values())
    if total <= 0:
        return {}
    return {mt: s / total for mt, s in scores.items()}


class HybridStrategy(WeightStrategy):
    """
    §18 : combined_score = alpha * normalized(log_loss scores) +
    (1-alpha) * normalized(brier scores) — COMPOSÉE des deux stratégies
    simples ci-dessus (jamais une 3e formule indépendante à maintenir).
    Un model_type absent d'une des deux sous-stratégies contribue 0 pour
    CETTE part uniquement (jamais un score fabriqué pour compenser) ; en
    pratique log_loss et brier_score sont toujours calculés ensemble
    (service.py::_compute_market_metrics), donc cette situation ne devrait
    pas se produire sur des données réelles.
    """

    name = "hybrid"

    def __init__(self, alpha: float = 0.5):
        if not 0.0 <= alpha <= 1.0:
            raise ValueError(f"alpha doit être dans [0,1], reçu {alpha}")
        self.alpha = alpha
        self._log_loss = InverseLogLossStrategy()
        self._brier = BrierStrategy()

    def compute_scores(self, metrics: dict[str, MarketMetrics]) -> dict[str, float]:
        ll_norm = _normalize_scores(self._log_loss.compute_scores(metrics))
        br_norm = _normalize_scores(self._brier.compute_scores(metrics))
        model_types = set(ll_norm) | set(br_norm)
        return {
            mt: self.alpha * ll_norm.get(mt, 0.0) + (1 - self.alpha) * br_norm.get(mt, 0.0)
            for mt in model_types
        }


DEFAULT_STRATEGY = InverseLogLossStrategy()

# Registre nommé (§34 : GET /models/ensemble/strategies) — un futur type de
# stratégie s'ajoute UNIQUEMENT ici, jamais en dupliquant compute_market_weights.
WEIGHT_STRATEGIES: dict[str, WeightStrategy] = {
    "inverse_log_loss": InverseLogLossStrategy(),
    "softmax_log_loss": SoftmaxLogLossStrategy(),
    "brier": BrierStrategy(),
    "hybrid": HybridStrategy(),
}


@dataclass
class CombinedMarket:
    probs: dict[str, float]
    weights_used: dict[str, float]
    models_used: int
    degraded: bool


@dataclass
class EnsembleMarketResult:
    status: str  # "ok" | "not_available"
    combined: Optional[CombinedMarket] = None
    reason: Optional[str] = None


def _active_version(session: Session, model_type: str) -> Optional[ModelVersion]:
    return session.exec(
        select(ModelVersion).where(ModelVersion.model_type == model_type, ModelVersion.is_active == True)  # noqa: E712
    ).first()


def latest_version(session: Session, model_type: str) -> Optional[ModelVersion]:
    """Dernière ModelVersion entraînée pour ce type (max id), active ou non
    — utilisé par scripts/walk_forward_ensemble.py (voir docstring de
    compute_market_weights) pour évaluer XGBoost/LightGBM, jamais activés
    par politique mais dont on veut honnêtement mesurer l'apport potentiel
    à l'Ensemble sur leur dernier entraînement connu."""
    return session.exec(
        select(ModelVersion).where(ModelVersion.model_type == model_type).order_by(ModelVersion.id.desc())
    ).first()


def _market_metrics_for_model(
    session: Session, model_type: str, model_version_id: int, market: str, until: date, league: Optional[str] = None,
) -> MarketMetrics:
    """
    Réutilise TEL QUEL le calcul de métriques par marché déjà validé en
    Phase 5/6 (app/ai/arena/service.py) — jamais un second calcul de
    log_loss ici. dixon_coles reste sourcé depuis prediction_log
    exclusivement (voir docstring de service.py), les autres depuis
    model_predictions pour LEUR version active — même règle qu'ailleurs
    dans l'Arena, jamais deux implémentations divergentes (§7 Phase 6).
    `league` restreint l'échantillon à une seule ligue (§24) — None (défaut)
    = toutes ligues confondues, comportement Phase 7 inchangé.
    """
    if model_type == "dixon_coles":
        markets = _dixon_coles_markets(session, league=league, since=None, until=until)
    else:
        markets = _model_predictions_markets(session, model_type, model_version_id, league=league, since=None, until=until)
    return markets[market]


def compute_market_weights(
    session: Session, market: str, until: date, min_sample_size: int = MIN_BENCHMARK_SAMPLE_SIZE,
    model_versions: Optional[dict[str, ModelVersion]] = None,
    strategy: WeightStrategy = DEFAULT_STRATEGY,
    league: Optional[str] = None,
) -> WeightResult:
    """
    Calcule les poids d'UN marché à partir des seules données résolues AVANT
    `until` (borne incluse, voir _dixon_coles_markets/_model_predictions_markets
    — `until` doit donc déjà être `match_date - 1 jour` côté appelant pour
    exclure strictement le jour du match, voir build_live_ensemble). Score
    brut fourni par `strategy` (défaut : InverseLogLossStrategy, la baseline
    Phase 7 — §14), normalisé à somme 1 ICI, au même endroit pour toutes les
    stratégies (jamais dupliqué dans chacune).

    `model_versions` : par défaut (None), la version considérée pour CHAQUE
    model_type est sa ModelVersion ACTIVE (cohérent avec le service LIVE —
    on ne veut jamais pondérer une prédiction en cours avec les métriques
    d'une version différente de celle qui vote réellement, voir
    build_live_ensemble). Un appelant peut fournir explicitement
    `{model_type: ModelVersion}` pour évaluer une AUTRE version — c'est le
    cas de scripts/walk_forward_ensemble.py, qui veut comparer des versions
    précises indépendamment de `is_active` (voir Phase 8 §12-§13 : `is_active`
    ne reflète plus qu'un déploiement live, jamais un critère d'éligibilité
    Ensemble — voir app/ai/arena/availability.py).

    `league` (§24) : si fourni, tente d'abord les métriques restreintes à
    CETTE ligue pour chaque modèle ; si l'échantillon ligue est insuffisant
    (< min_sample_size) pour un modèle donné, retombe sur ses métriques
    GLOBALES (toutes ligues) pour CE modèle seulement (`league_fallback=True`
    sur son ModelWeight) — jamais un poids calculé sur 5 ou 10 matchs d'une
    ligue, jamais un repli en bloc qui pénaliserait un modèle ayant, lui,
    assez de données ligue.
    """
    weights: dict[str, ModelWeight] = {}
    excluded: dict[str, str] = {}
    metrics_by_model: dict[str, MarketMetrics] = {}
    fallback_used: dict[str, bool] = {}

    for model_type in KNOWN_MODEL_TYPES:
        version = model_versions.get(model_type) if model_versions is not None else _active_version(session, model_type)
        if version is None:
            excluded[model_type] = "Aucune version active pour ce type de modèle." if model_versions is None else "Aucune version fournie pour ce type de modèle."
            continue

        metrics = _market_metrics_for_model(session, model_type, version.id, market, until, league=league)
        used_fallback = False
        if league is not None and metrics.sample_size < min_sample_size:
            metrics = _market_metrics_for_model(session, model_type, version.id, market, until, league=None)
            used_fallback = True

        if metrics.sample_size < min_sample_size:
            scope = "toutes ligues" if league is None or used_fallback else f"ligue '{league}'"
            excluded[model_type] = (
                f"Échantillon insuffisant sur {market} ({scope}) avant {until.isoformat()} "
                f"({metrics.sample_size} < {min_sample_size})."
            )
            continue
        if metrics.log_loss is None:
            excluded[model_type] = f"Marché {market} non modélisé par '{model_type}' (aucune probabilité disponible)."
            continue

        metrics_by_model[model_type] = metrics
        fallback_used[model_type] = used_fallback
        weights[model_type] = ModelWeight(
            model_type=model_type, version=version.name, model_version_id=version.id,
            weight=0.0, log_loss=metrics.log_loss, sample_size=metrics.sample_size,
            brier_score=metrics.brier_score, league_fallback=used_fallback,
        )

    scores = strategy.compute_scores(metrics_by_model)
    # Un modèle éligible sur le seuil d'échantillon mais exclu par LA
    # STRATÉGIE elle-même (ex. BrierStrategy sans brier_score disponible,
    # cas théorique — voir docstring BrierStrategy) doit être retiré ici,
    # jamais laissé avec un poids fabriqué à 0.
    for model_type in list(weights.keys()):
        if model_type not in scores:
            excluded[model_type] = f"Stratégie '{strategy.name}' sans score calculable pour '{model_type}' sur {market}."
            del weights[model_type]

    if weights:
        normalized = _normalize_scores(scores)
        for model_type, w in weights.items():
            w.weight = round(normalized[model_type], 6)
        logger.info(
            f"weights_calculated market={market} until={until.isoformat()} strategy={strategy.name} league={league} "
            f"weights={{ {', '.join(f'{k}: {v.weight}' for k, v in weights.items())} }} "
            f"excluded={list(excluded.keys())}"
        )
    else:
        logger.info(f"weights_calculated market={market} until={until.isoformat()} strategy={strategy.name} "
                    f"league={league} weights={{}} (aucun modèle éligible)")

    return WeightResult(market=market, until=until, strategy=strategy.name, league=league, weights=weights, excluded=excluded)


def combine(model_probs: dict[str, dict[str, float]], weight_result: WeightResult) -> Optional[CombinedMarket]:
    """
    Combine les probabilités RÉELLEMENT disponibles pour ce match (§12) —
    ne considère que les model_type à la fois pondérés (weight_result) ET
    présents dans `model_probs` (le modèle a produit une prédiction valide
    pour CE match précis) ; renormalise les poids sur ce sous-ensemble
    (jamais sur les poids "historiques" bruts, qui incluent des modèles
    absents ici). None si aucun modèle éligible n'a de probabilité
    disponible pour ce marché (§15 : jamais fabriqué).
    """
    eligible = {mt: probs for mt, probs in model_probs.items() if mt in weight_result.weights and probs is not None}
    if not eligible:
        return None

    raw_weights = {mt: weight_result.weights[mt].weight for mt in eligible}
    total = sum(raw_weights.values())
    sub_weights = {mt: w / total for mt, w in raw_weights.items()}

    outcome_keys = MARKET_OUTCOME_KEYS[weight_result.market]
    combined_probs = {
        outcome: round(sum(sub_weights[mt] * eligible[mt].get(outcome, 0.0) for mt in eligible), 6)
        for outcome in outcome_keys
    }

    return CombinedMarket(
        probs=combined_probs, weights_used=sub_weights,
        models_used=len(eligible), degraded=len(eligible) < 2,
    )


def compute_confidence(probs_1x2: dict[str, float]) -> Optional[float]:
    """
    Confiance = écart entre la meilleure probabilité 1X2 et la deuxième
    (§17) — PAS la probabilité de victoire elle-même : un match "48% / 27%
    / 25%" a une confiance faible (0.21) même si "domicile" reste le pick,
    parce que les 3 issues restent proches. Documentée ici pour être
    reprise telle quelle par le frontend (jamais réinterprétée)."""
    if len(probs_1x2) < 2:
        return None
    sorted_probs = sorted(probs_1x2.values(), reverse=True)
    return round(sorted_probs[0] - sorted_probs[1], 4)


def _record_market_probs(record: PredictionRecord, market: str) -> Optional[dict[str, float]]:
    if market == "1X2":
        return {"home_win": record.prob_home, "draw": record.prob_draw, "away_win": record.prob_away}
    if market == "BTTS":
        if record.prob_btts_yes is None:
            return None
        return {"yes": record.prob_btts_yes, "no": record.prob_btts_no}
    if market == "OVER_UNDER_2_5":
        if record.prob_over_2_5 is None:
            return None
        return {"over": record.prob_over_2_5, "under": record.prob_under_2_5}
    raise ValueError(f"Marché inconnu : {market}")


@dataclass
class LiveEnsembleResult:
    status: str  # toujours "ok" — l'absence de marché combinable n'est pas une erreur HTTP (même convention que /models/benchmark)
    model_version_id: Optional[int]
    markets: dict[str, EnsembleMarketResult]
    weight_results: dict[str, WeightResult]
    confidence: Optional[float]


def build_live_ensemble(
    session: Session,
    model_records: dict[str, PredictionRecord],
    league: str, home_team: str, away_team: str, match_date: date,
    min_sample_size: int = MIN_BENCHMARK_SAMPLE_SIZE,
    persist: bool = True,
    strategy: WeightStrategy = DEFAULT_STRATEGY,
    use_league_weights: bool = True,
) -> LiveEnsembleResult:
    """
    Point d'entrée LIVE (§7-§18) : `model_records` ne contient QUE les
    PredictionRecord des modèles ayant réussi (status="ok") pour ce match —
    voir main.py pour l'appel depuis ModelOrchestrator.predict_all(). Calcule
    les poids par marché (anti-fuite : until = match_date - 1 jour, jamais le
    jour même), combine, puis enregistre la prédiction Ensemble elle-même
    (model_type="ensemble") via le même logger commun que les autres
    modèles — jamais mélangée aux prédictions individuelles (§18).

    `use_league_weights=True` (défaut, §24) : tente d'abord les poids
    SPÉCIFIQUES à `league` (repli automatique par modèle sur le global si
    l'échantillon ligue est insuffisant — voir compute_market_weights,
    paramètre `league`) ; `False` force un calcul toutes ligues confondues
    (comportement Phase 7, utilisé par les tests de non-régression).
    """
    until = match_date - timedelta(days=1)
    markets: dict[str, EnsembleMarketResult] = {}
    weight_results: dict[str, WeightResult] = {}
    weight_league = league if use_league_weights else None

    for market in MARKET_OUTCOME_KEYS:
        weight_result = compute_market_weights(session, market, until, min_sample_size, strategy=strategy, league=weight_league)
        weight_results[market] = weight_result

        model_probs = {
            mt: _record_market_probs(record, market)
            for mt, record in model_records.items()
        }
        model_probs = {mt: p for mt, p in model_probs.items() if p is not None}

        combined = combine(model_probs, weight_result)
        if combined is None:
            reason = (
                "Aucun modèle éligible (historique suffisant) n'a produit de prédiction valide pour ce marché "
                "sur ce match." if weight_result.weights else
                "Aucun modèle n'a assez d'historique résolu sur ce marché pour être pondéré "
                f"(seuil={min_sample_size})."
            )
            markets[market] = EnsembleMarketResult(status="not_available", reason=reason)
        else:
            markets[market] = EnsembleMarketResult(status="ok", combined=combined)

    confidence = None
    ensemble_version_id = None
    market_1x2 = markets["1X2"]
    if market_1x2.status == "ok":
        confidence = compute_confidence(market_1x2.combined.probs)

        if persist:
            try:
                version = get_or_create_active_model_version(
                    session, "ensemble", "xfoot-ensemble",
                    notes="Prédiction combinée (log_loss-weighted) des modèles individuels éligibles — "
                          "voir app/ai/arena/ensemble.py pour la méthode. Poids recalculés à chaque prédiction, "
                          "jamais figés dans cette version.",
                )
                ensemble_version_id = version.id
                market_ou = markets.get("OVER_UNDER_2_5")
                market_btts = markets.get("BTTS")
                record = PredictionRecord(
                    league=league, match_date=match_date, home_team=home_team, away_team=away_team,
                    model_type="ensemble",
                    prob_home=market_1x2.combined.probs["home_win"],
                    prob_draw=market_1x2.combined.probs["draw"],
                    prob_away=market_1x2.combined.probs["away_win"],
                    prob_btts_yes=market_btts.combined.probs["yes"] if market_btts and market_btts.status == "ok" else None,
                    prob_btts_no=market_btts.combined.probs["no"] if market_btts and market_btts.status == "ok" else None,
                    prob_over_2_5=market_ou.combined.probs["over"] if market_ou and market_ou.status == "ok" else None,
                    prob_under_2_5=market_ou.combined.probs["under"] if market_ou and market_ou.status == "ok" else None,
                    confidence=confidence,
                    source="live",
                )
                log_prediction(session, record, ensemble_version_id)
                session.commit()
                logger.info(
                    f"ensemble_prediction_created model_version_id={ensemble_version_id} "
                    f"match={league}:{home_team} vs {away_team} ({match_date.isoformat()}) "
                    f"models_used={market_1x2.combined.models_used}"
                )
            except Exception as e:
                logger.warning(f"ensemble_prediction_failed match={league}:{home_team} vs {away_team} error={e!r}")
    else:
        logger.info(
            f"ensemble_prediction_failed match={league}:{home_team} vs {away_team} "
            f"reason={market_1x2.reason}"
        )

    return LiveEnsembleResult(
        status="ok", model_version_id=ensemble_version_id, markets=markets,
        weight_results=weight_results, confidence=confidence,
    )
