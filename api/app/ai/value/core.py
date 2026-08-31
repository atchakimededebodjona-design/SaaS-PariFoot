"""
api/app/ai/value/core.py — Phase 8H : XFOOT VALUE ENGINE & MARKET
INTELLIGENCE FOUNDATION V1.

Fonctions PURES uniquement (aucun accès DB, réseau, ni fournisseur d'odds).
RESEARCH + SHADOW ONLY — aucune fonction de ce module n'est appelée par la
production (main.py, scheduler.py, orchestrator.py, service.py, ensemble.py,
models_common.py, promotion.py) et aucune ne doit jamais l'être sans une
décision explicite d'une phase future (§37/§43 du prompt Phase 8H).

Réutilise TELLES QUELLES (jamais réimplémentées) :
  - api/app/ai/odds_research/core.py :: is_valid_decimal_odds,
    implied_probability, normalize_margin, overround (Phase 8D).
  - api/app/ai/value/quality.py :: classify_temporal_status,
    evaluate_quality_gates, compute_odds_age_hours (§10 du prompt).
  - api/app/ai/arena/research.py :: bootstrap_paired_diff, mcnemar_test,
    wilson_interval (Phase 5.7, §29 du prompt — jamais une deuxième
    implémentation statistique).

Convention de signe et d'unité (documentée une seule fois ici, jamais
implicite ailleurs) :
  - edge = p_model - p_market -> POSITIF si Xfoot voit une probabilité PLUS
    ÉLEVÉE que le marché pour cette issue (le marché la sous-évaluerait selon
    Xfoot) ; NÉGATIF si l'inverse.
  - expected_value = p_model * decimal_odds - 1 -> "retour NET attendu par
    unité misée" (+0.20 = +20% de gain net attendu par unité misée, PAS un
    multiplicateur de bankroll, PAS une garantie — §8/§30 : jamais présenté
    comme un gain certain).
"""

from __future__ import annotations

import statistics
from datetime import datetime
from typing import Optional

from app.ai.odds_research.core import (  # noqa: F401 — réutilisées telles quelles (Phase 8D)
    is_valid_decimal_odds, implied_probability, normalize_margin, overround as _overround,
)
from app.ai.value.quality import (
    classify_temporal_status, compute_odds_age_hours, evaluate_quality_gates,
)
from app.ai.value.schemas import (
    ModelProbability, OddsSnapshot, PredictionQuality, ValueSignal, ValueThresholds, RESEARCH_DEFAULT_THRESHOLDS,
)

# ---------------------------------------------------------------------------
# §13/§19 : vocabulaire de sortie, FIXE — jamais une chaîne libre inventée
# à la volée ailleurs dans ce module.
# ---------------------------------------------------------------------------

VALUE_TYPES = (
    "POSITIVE_VALUE", "NEUTRAL", "NEGATIVE_VALUE",
    "INSUFFICIENT_DATA", "TEMPORALLY_UNSAFE", "INVALID_ODDS",
)

REJECTION_REASONS = (
    "NO_ODDS", "INVALID_ODDS", "NO_MODEL_PROBABILITY", "INSUFFICIENT_DATA",
    "TEMPORAL_UNVERIFIED", "FUTURE_INFORMATION", "LOW_EDGE", "LOW_EV",
    "LOW_CONFIDENCE", "STALE_ODDS",
)


def _valid_probability(p: Optional[float]) -> bool:
    return p is not None and isinstance(p, (int, float)) and 0.0 <= p <= 1.0 and p == p  # p==p exclut NaN


# ---------------------------------------------------------------------------
# §5/§6/§12 : probabilité implicite, normalisation, overround — GÉNÉRIQUE sur
# le nombre d'issues (2 pour BTTS/O-U, 3 pour 1X2), construit à partir des
# primitives Phase 8D (jamais une réimplémentation des formules).
# ---------------------------------------------------------------------------

def compute_market_probabilities(odds_by_selection: dict[str, float]) -> Optional[dict]:
    """
    §5 : p_raw = 1/odds (implied_probability, réutilisée). §6 : normalisation
    proportionnelle (normalize_margin, réutilisée) — raw et normalized
    gardées SÉPARÉMENT dans le résultat, jamais mélangées. §12 : overround =
    sum(raw) - 1 (réutilisée). None si le marché a moins de 2 issues ou si
    UNE SEULE cote est invalide (§24 Phase 8D : jamais une cote manquante
    imputée) — correspond à NOT_AVAILABLE pour l'appelant (§22/§23).
    """
    if len(odds_by_selection) < 2 or not all(is_valid_decimal_odds(v) for v in odds_by_selection.values()):
        return None
    selections = list(odds_by_selection.keys())
    raw = {s: implied_probability(odds_by_selection[s]) for s in selections}
    normalized = dict(zip(selections, normalize_margin([raw[s] for s in selections])))
    return {"raw": raw, "normalized": normalized, "overround": _overround(list(raw.values()))}


# ---------------------------------------------------------------------------
# §7/§8 : edge, expected value — voir convention de signe/unité en tête de module.
# ---------------------------------------------------------------------------

def edge(p_model: float, p_market: float) -> float:
    return p_model - p_market


def expected_value(p_model: float, decimal_odds: float) -> float:
    return p_model * decimal_odds - 1.0


def classify_value_type(edge_value: float, ev_value: float) -> str:
    """§13 : POSITIVE_VALUE si edge ET ev sont POSITIFS ; NEGATIVE_VALUE si les deux sont NÉGATIFS ; NEUTRAL
    sinon (signes discordants ou nuls). Description, jamais une promesse de gain (§30)."""
    if edge_value > 0 and ev_value > 0:
        return "POSITIVE_VALUE"
    if edge_value < 0 and ev_value < 0:
        return "NEGATIVE_VALUE"
    return "NEUTRAL"


# ---------------------------------------------------------------------------
# §26 : MARKET_DOMINANT — jamais déduit automatiquement d'un edge, seulement
# d'une comparaison de qualité EXPLICITEMENT fournie par l'appelant (ex. un
# futur historique de calibration réel) — jamais fabriquée ici.
# ---------------------------------------------------------------------------

def classify_market_dominance(model_quality_score: Optional[float], market_quality_score: Optional[float]) -> str:
    """
    §26 : retourne "MARKET_DOMINANT" si market_quality_score > model_quality_score (les deux fournis), sinon
    "MODEL_COMPETITIVE" ; "UNKNOWN" si l'un des deux scores manque — CE MODULE NE FABRIQUE JAMAIS de score de
    qualité lui-même (aucune donnée de calibration réellement vérifiée n'est câblée dans cette V1, §40). Ne
    JAMAIS en déduire qu'un pari est automatiquement mauvais (§26) — c'est une classification descriptive, pas
    une décision.
    """
    if model_quality_score is None or market_quality_score is None:
        return "UNKNOWN"
    return "MARKET_DOMINANT" if market_quality_score > model_quality_score else "MODEL_COMPETITIVE"


# ---------------------------------------------------------------------------
# §14 : consensus de marché. §15 : dispersion bookmaker.
# ---------------------------------------------------------------------------

def build_market_consensus(
    odds_snapshots: list[OddsSnapshot], selection: str, cutoff_timestamp: datetime, min_bookmakers: int = 2,
) -> Optional[dict]:
    """
    §14 : consensus pour UNE sélection, à partir de PLUSIEURS snapshots bookmaker. Même méthode que
    odds_research.integrity.safe_consensus (Phase 8E) — inclut UNIQUEMENT les observations dont le timestamp
    est MESURÉ (has_measured_timestamp=True) ET <= cutoff_timestamp — généralisée ici à une sélection unique
    (safe_consensus a une forme de retour figée à home/draw/away, incompatible avec BTTS/O-U). §15 :
    INSUFFICIENT_DATA (None) si moins de `min_bookmakers` observations qualifient — ne comble jamais un
    bookmaker absent, retourne explicitement les bookmakers inclus/exclus (§20 Phase 8E).
    """
    same_selection = [o for o in odds_snapshots if o.selection == selection]
    included = [
        o for o in same_selection
        if o.has_measured_timestamp and o.odds_timestamp is not None and o.odds_timestamp <= cutoff_timestamp
        and is_valid_decimal_odds(o.decimal_odds)
    ]
    if len(included) < min_bookmakers:
        return None
    implied = [implied_probability(o.decimal_odds) for o in included]
    included_names = {o.bookmaker for o in included}
    return {
        "selection": selection, "bookmaker_count": len(included),
        "bookmakers_included": sorted(n for n in included_names if n is not None),
        "bookmakers_excluded": sorted({o.bookmaker for o in same_selection if o.bookmaker is not None} - included_names),
        "mean_implied_probability": statistics.mean(implied),
        "median_implied_probability": statistics.median(implied),
    }


def bookmaker_dispersion(odds_snapshots: list[OddsSnapshot], selection: str, min_bookmakers: int = 2):
    """§15 : min/max/mean/median/écart-type des cotes DÉCIMALES (pas des probabilités) pour une sélection.
    "INSUFFICIENT_DATA" (chaîne, §15) si moins de `min_bookmakers` cotes valides."""
    values = [o.decimal_odds for o in odds_snapshots if o.selection == selection and is_valid_decimal_odds(o.decimal_odds)]
    if len(values) < min_bookmakers:
        return "INSUFFICIENT_DATA"
    return {
        "n": len(values), "min_odds": min(values), "max_odds": max(values),
        "mean_odds": statistics.mean(values), "median_odds": statistics.median(values),
        "stdev_odds": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


# ---------------------------------------------------------------------------
# §18/§19/§20/§21/§22/§23 : assemblage complet d'UN ValueSignal, avec Quality
# Gate et raison de rejet explicite — vérifications dans un ordre FIXE (§35).
# ---------------------------------------------------------------------------

def build_value_signal(
    *, match_id: Optional[int], market: str, selection: str,
    model_probability: Optional[ModelProbability],
    market_odds: dict[str, OddsSnapshot],
    cutoff_timestamp: Optional[datetime],
    match_kickoff: Optional[datetime] = None,
    confidence: Optional[float] = None,
    thresholds: ValueThresholds = RESEARCH_DEFAULT_THRESHOLDS,
) -> ValueSignal:
    """
    `market_odds` : TOUTES les issues du marché pour LE MÊME bookmaker/snapshot (ex. {"home_win": OddsSnapshot,
    "draw": OddsSnapshot, "away_win": OddsSnapshot} pour 1X2) — nécessaire à la normalisation (§6). §22/§23 :
    pour BTTS/O-U fournis avec moins de 2 issues valides, le marché est traité comme non disponible
    (INSUFFICIENT_DATA), jamais supposé disponible.

    Ordre de vérification FIXE (§35, reproductibilité) :
    1. NO_ODDS (aucun OddsSnapshot pour `selection`)
    2. INVALID_ODDS (cote présente mais <= 1 / non finie)
    3. NO_MODEL_PROBABILITY (model_probability absente ou hors [0,1])
    4. marché insuffisant (moins de 2 issues valides dans market_odds) -> INSUFFICIENT_DATA
    5. statut temporel FUTURE_INFORMATION -> rejet FUTURE_INFORMATION ; UNKNOWN -> rejet TEMPORAL_UNVERIFIED
    6. seuils (thresholds) : LOW_CONFIDENCE / LOW_EDGE / LOW_EV / STALE_ODDS
    7. sinon : classify_value_type (POSITIVE_VALUE / NEUTRAL / NEGATIVE_VALUE)
    """
    odds_snapshot = market_odds.get(selection)
    odds_timestamp = odds_snapshot.odds_timestamp if odds_snapshot is not None else None
    has_measured = odds_snapshot.has_measured_timestamp if odds_snapshot is not None else False

    temporal_status = classify_temporal_status(odds_timestamp, cutoff_timestamp, match_kickoff, has_measured)
    odds_age = compute_odds_age_hours(cutoff_timestamp, odds_timestamp)

    def _reject(status: str, reason: str) -> ValueSignal:
        market_probs = compute_market_probabilities({s: o.decimal_odds for s, o in market_odds.items()})
        return ValueSignal(
            match_id=match_id, market=market, selection=selection,
            model_probability=model_probability.effective_probability() if model_probability is not None else None,
            market_probability_raw=market_probs["raw"].get(selection) if market_probs else None,
            market_probability_normalized=market_probs["normalized"].get(selection) if market_probs else None,
            odds=odds_snapshot.decimal_odds if odds_snapshot is not None else None,
            edge=None, expected_value=None, temporal_status=temporal_status,
            odds_timestamp=odds_timestamp, cutoff_timestamp=cutoff_timestamp, confidence=confidence,
            status=status, reason=reason,
        )

    # 1-2 : NO_ODDS / INVALID_ODDS
    if odds_snapshot is None:
        return _reject("INSUFFICIENT_DATA", "NO_ODDS")
    if not is_valid_decimal_odds(odds_snapshot.decimal_odds):
        return _reject("INVALID_ODDS", "INVALID_ODDS")

    # 3 : NO_MODEL_PROBABILITY
    p_model = model_probability.effective_probability() if model_probability is not None else None
    if not _valid_probability(p_model):
        return _reject("INSUFFICIENT_DATA", "NO_MODEL_PROBABILITY")

    # 4 : marché
    market_probs = compute_market_probabilities({s: o.decimal_odds for s, o in market_odds.items()})
    if market_probs is None or selection not in market_probs["normalized"]:
        return _reject("INSUFFICIENT_DATA", "INSUFFICIENT_DATA")
    p_market = market_probs["normalized"][selection]

    # 5 : temporel
    if temporal_status == "FUTURE_INFORMATION":
        return _reject("TEMPORALLY_UNSAFE", "FUTURE_INFORMATION")
    if temporal_status == "UNKNOWN":
        return _reject("TEMPORALLY_UNSAFE", "TEMPORAL_UNVERIFIED")

    edge_value = edge(p_model, p_market)
    ev_value = expected_value(p_model, odds_snapshot.decimal_odds)

    # 6 : seuils (RESEARCH_DEFAULT = permissif — voir schemas.py)
    if confidence is not None and confidence < thresholds.min_confidence:
        reason, status = "LOW_CONFIDENCE", "NEGATIVE_VALUE" if ev_value < 0 else "NEUTRAL"
        return ValueSignal(
            match_id=match_id, market=market, selection=selection, model_probability=p_model,
            market_probability_raw=market_probs["raw"][selection], market_probability_normalized=p_market,
            odds=odds_snapshot.decimal_odds, edge=edge_value, expected_value=ev_value,
            temporal_status=temporal_status, odds_timestamp=odds_timestamp, cutoff_timestamp=cutoff_timestamp,
            confidence=confidence, status="INSUFFICIENT_DATA", reason=reason,
        )
    if edge_value < thresholds.min_edge:
        return ValueSignal(
            match_id=match_id, market=market, selection=selection, model_probability=p_model,
            market_probability_raw=market_probs["raw"][selection], market_probability_normalized=p_market,
            odds=odds_snapshot.decimal_odds, edge=edge_value, expected_value=ev_value,
            temporal_status=temporal_status, odds_timestamp=odds_timestamp, cutoff_timestamp=cutoff_timestamp,
            confidence=confidence, status="INSUFFICIENT_DATA", reason="LOW_EDGE",
        )
    if ev_value < thresholds.min_ev:
        return ValueSignal(
            match_id=match_id, market=market, selection=selection, model_probability=p_model,
            market_probability_raw=market_probs["raw"][selection], market_probability_normalized=p_market,
            odds=odds_snapshot.decimal_odds, edge=edge_value, expected_value=ev_value,
            temporal_status=temporal_status, odds_timestamp=odds_timestamp, cutoff_timestamp=cutoff_timestamp,
            confidence=confidence, status="INSUFFICIENT_DATA", reason="LOW_EV",
        )
    if odds_age is not None and odds_age > thresholds.max_odds_age_hours:
        return ValueSignal(
            match_id=match_id, market=market, selection=selection, model_probability=p_model,
            market_probability_raw=market_probs["raw"][selection], market_probability_normalized=p_market,
            odds=odds_snapshot.decimal_odds, edge=edge_value, expected_value=ev_value,
            temporal_status=temporal_status, odds_timestamp=odds_timestamp, cutoff_timestamp=cutoff_timestamp,
            confidence=confidence, status="INSUFFICIENT_DATA", reason="STALE_ODDS",
        )

    # 7 : classification finale
    status = classify_value_type(edge_value, ev_value)
    return ValueSignal(
        match_id=match_id, market=market, selection=selection, model_probability=p_model,
        market_probability_raw=market_probs["raw"][selection], market_probability_normalized=p_market,
        odds=odds_snapshot.decimal_odds, edge=edge_value, expected_value=ev_value,
        temporal_status=temporal_status, odds_timestamp=odds_timestamp, cutoff_timestamp=cutoff_timestamp,
        confidence=confidence, status=status, reason=None,
    )


# ---------------------------------------------------------------------------
# §24 : tri multi-critères EXPLICITE — AUCUN score composite inventé.
# ---------------------------------------------------------------------------

RANKING_CRITERIA = ("expected_value", "edge", "confidence", "temporal_quality")
_TEMPORAL_QUALITY_ORDER = {"TEMPORALLY_VERIFIED": 3, "HISTORICAL_UNVERIFIED": 2, "UNKNOWN": 1, "FUTURE_INFORMATION": 0}


def rank_value_signals(signals: list[ValueSignal], criteria: list[str]) -> list[ValueSignal]:
    """
    §24 : tri STABLE, multi-critères, dans l'ordre de priorité fourni par l'appelant (le premier critère
    départage en premier) — décroissant (meilleur en premier) pour chaque critère. AUCUN score composite
    global n'est calculé ici : si aucune formule statistiquement validée n'existe, ne pas en fabriquer une
    (§24). `criteria` doit être un sous-ensemble non vide de RANKING_CRITERIA.
    """
    invalid = [c for c in criteria if c not in RANKING_CRITERIA]
    if invalid:
        raise ValueError(f"Critères de tri inconnus : {invalid}")
    if not criteria:
        raise ValueError("criteria ne peut pas être vide (§24 : tri multi-critères EXPLICITE, jamais implicite)")

    def key_fn(signal: ValueSignal) -> tuple:
        parts = []
        for c in criteria:
            if c == "expected_value":
                parts.append(-(signal.expected_value if signal.expected_value is not None else float("-inf")))
            elif c == "edge":
                parts.append(-(signal.edge if signal.edge is not None else float("-inf")))
            elif c == "confidence":
                parts.append(-(signal.confidence if signal.confidence is not None else float("-inf")))
            else:  # "temporal_quality"
                parts.append(-_TEMPORAL_QUALITY_ORDER.get(signal.temporal_status, -1))
        return tuple(parts)

    return sorted(signals, key=key_fn)


# ---------------------------------------------------------------------------
# §28 : grille de recherche RESEARCH ONLY — aucune valeur ne devient un
# défaut de production. §27 : jamais choisi contre un test (ce module ne
# fait qu'appliquer chaque candidat et compter, jamais sélectionner).
# ---------------------------------------------------------------------------

EDGE_GRID = (0.01, 0.02, 0.03, 0.05, 0.07, 0.10)
EV_GRID = (0.01, 0.02, 0.03, 0.05, 0.10)


def evaluate_threshold_grid(signals: list[ValueSignal]) -> list[dict]:
    valid_signals = [s for s in signals if s.edge is not None and s.expected_value is not None]
    rows = []
    for min_edge in EDGE_GRID:
        for min_ev in EV_GRID:
            kept = [s for s in valid_signals if s.edge >= min_edge and s.expected_value >= min_ev]
            rows.append({"min_edge": min_edge, "min_ev": min_ev, "n_candidates": len(kept)})
    return rows


# ---------------------------------------------------------------------------
# §29/§31 : interface statistique/backtest — RÉUTILISE api/app/ai/arena/
# research.py (jamais une deuxième implémentation). Aucun benchmark réel
# n'est exécuté par ce module (§31 : jamais sans odds temporellement
# vérifiées — aucune ne l'est encore, §40).
# ---------------------------------------------------------------------------

def compare_paired_log_loss(pairs: list[tuple[float, float]]) -> dict:
    """§29 : réutilise TEL QUEL research.bootstrap_paired_diff (Phase 5.7)."""
    from app.ai.arena.research import bootstrap_paired_diff
    return bootstrap_paired_diff(pairs)


def compare_paired_correctness(b: int, c: int) -> dict:
    """§29 : réutilise TEL QUEL research.mcnemar_test (Phase 5.7)."""
    from app.ai.arena.research import mcnemar_test
    return mcnemar_test(b, c)


def accuracy_confidence_interval(k: int, n: int) -> tuple:
    """§29 : réutilise TEL QUEL research.wilson_interval (Phase 5.7)."""
    from app.ai.arena.research import wilson_interval
    return wilson_interval(k, n)


def prepare_backtest_comparison(
    *, baseline_observations: list, xfoot_odds_observations: list,
    market_implied_observations: list, value_selected_observations: list,
) -> dict:
    """
    §31 : interface de préparation pour un futur backtest comparant Baseline Xfoot / Xfoot+odds / probabilité
    implicite marché / sous-ensemble sélectionné par le Value Engine — regroupe les listes d'observations
    fournies SANS calculer aucune métrique ici (§31 : ne lance AUCUN benchmark réel sans odds temporellement
    vérifiées — voir §40, STATISTICAL_VALUE_VALIDATION = NOT_AVAILABLE tant qu'aucune donnée réelle n'existe).
    L'appelant d'une future phase branchera compare_paired_log_loss/compare_paired_correctness sur les paires
    qu'il construit à partir de ces listes, une fois des odds réellement temporellement vérifiées disponibles.
    """
    return {
        "baseline_n": len(baseline_observations), "xfoot_odds_n": len(xfoot_odds_observations),
        "market_implied_n": len(market_implied_observations), "value_selected_n": len(value_selected_observations),
        "benchmark_executed": False,
        "reason": "STATISTICAL_VALUE_VALIDATION = NOT_AVAILABLE — aucune odds temporellement vérifiée disponible (§40).",
    }
