"""
research.py — Phase 5.7 : Ensemble Research & Backtest V2.

Bibliothèque de RECHERCHE, séparée de la production (ensemble.py,
orchestrator.py, models_common.py, prediction_logging.py, service.py — AUCUN
de ces modules n'est importé en écriture, AUCUN n'est modifié par ce
ticket). Aucune fonction de ce fichier n'écrit dans model_predictions,
model_versions, team_ratings ou api/model_artifacts/*.json : uniquement des
lectures (session.exec(select(...))) et des ré-entraînements Dixon-Coles
EN MÉMOIRE sur un historique tronqué à une date, jamais persistés.

=== Pourquoi Dixon-Coles peut être walk-forward ici alors qu'il est absent
    de scripts/walk_forward_ensemble.py ===

Ce dernier lit `model_predictions` (source="backtest") pour elo/xgboost/
lightgbm — Dixon-Coles n'y a AUCUNE ligne (il n'écrit que du "live", voir
prediction_logging.py). Mais son moteur d'entraînement EST rejouable
librement à partir de la table `match` (app/ai/engine/dixon_coles.py::
load_matches_from_db + export_model_artifacts.export_league, LE moteur de
production _FastDixonColesL2 — mêmes maths, pas une réimplémentation) : en
tronquant l'historique d'entraînement à une date `until`, on obtient des
prédictions Dixon-Coles walk-forward, sans fuite, pour n'importe quel match
postérieur à `until` — voir build_dixon_coles_walk_forward.

=== Pourquoi la pondération réutilise ensemble.py au lieu de le dupliquer ===

`compute_market_weights_including_dc` reproduit EXACTEMENT la boucle de
`ensemble.compute_market_weights` (mêmes conditions d'exclusion, mêmes
strategy.compute_scores()/_normalize_scores() importés tels quels) pour
pouvoir inclure une 4e source de métriques (Dixon-Coles, walk-forward en
mémoire, jamais model_predictions) aux côtés des 3 sources DB existantes.
Le résultat est un WeightResult standard (simple dataclass) : le combine()
de ensemble.py, NON MODIFIÉ, l'accepte tel quel — aucune divergence de
logique de combinaison entre recherche et production.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import numpy as np
from sqlmodel import Session

from .ensemble import KNOWN_MODEL_TYPES, ModelWeight, WeightResult, WeightStrategy, _normalize_scores
from .schemas import MarketMetrics
from .service import MARKETS, _compute_market_metrics, _model_predictions_markets

# Importer app.ai.engine.dixon_coles EN PREMIER : il insère la racine du
# dépôt dans sys.path en effet de bord (voir son propre docstring), ce qui
# rend ensuite `dixon_coles` (fichier racine, la classe de référence) et
# `export_model_artifacts` importables — même ordre que scripts/backtest_elo.py.
from app.ai.engine.dixon_coles import export_league, load_matches_from_db  # noqa: E402
from dixon_coles import DixonColesModel  # noqa: E402  (racine du dépôt, déjà sur sys.path ci-dessus)

MIN_DC_TRAIN_MATCHES = 200  # seuil documenté : en-dessous, un Dixon-Coles walk-forward par ligue n'est pas jugé fiable (ordres de grandeur très inférieurs à ce que voit le pipeline de production, plusieurs saisons/ligue) — jamais entraîné quand même.


# ---------------------------------------------------------------------------
# 1. Simple Average — stratégie de RECHERCHE UNIQUEMENT.
#
# Jamais ajoutée à ensemble.WEIGHT_STRATEGIES (§24/§29 du prompt Phase 5.7 :
# ne jamais changer xfoot-ensemble-v3 ni GET /models/ensemble/strategies).
# ---------------------------------------------------------------------------

class SimpleAverageStrategy(WeightStrategy):
    """Score=1 pour tout modèle éligible (historique suffisant) -> poids
    égaux après normalisation (_normalize_scores, réutilisée telle quelle,
    jamais recalculée). ensemble.combine() (non modifié) renormalise déjà
    sur les seuls modèles ayant RÉELLEMENT produit une probabilité pour un
    match donné -> un modèle historiquement éligible mais absent sur CE
    match est honnêtement exclu et les poids restants renormalisés — jamais
    un poids fabriqué pour compenser (§12 du prompt)."""

    name = "simple_average"

    def compute_scores(self, metrics: dict[str, MarketMetrics]) -> dict[str, float]:
        return {mt: 1.0 for mt in metrics}


# ---------------------------------------------------------------------------
# 2. Dixon-Coles walk-forward (en mémoire, jamais persisté) — §4 du prompt.
# ---------------------------------------------------------------------------

def _dixon_coles_model_from_artifact(artifact: dict) -> DixonColesModel:
    """Reconstruit un DixonColesModel déjà entraîné à partir d'un artefact
    export_league() — même glue que scripts/backtest_elo.py::
    dc_model_from_artifact, dupliquée ici volontairement (api/app/ ne doit
    jamais dépendre de scripts/, qui dépend de api/app/ — jamais l'inverse) ;
    aucun calcul, uniquement une affectation de champs déjà validés par
    export_league (le moteur de production)."""
    m = DixonColesModel(xi=artifact["xi"], l2=artifact["l2_reg"])
    m.teams_ = artifact["teams"]
    m.attack_ = artifact["attack"]
    m.defense_ = artifact["defense"]
    m.home_advantage_ = artifact["home_advantage"]
    m.rho_ = artifact["rho"]
    return m


def actual_outcome(market: str, home_goals: int, away_goals: int) -> str:
    """Dérive l'issue RÉELLEMENT survenue pour un marché à partir du score
    — même définition que service.py::_market_observation/
    prediction_logging.compute_correctness (ligne 2.5 fixe pour O/U), mais
    exposée ici comme fonction publique réutilisable (au lieu d'être
    ré-écrite inline à chaque nouveau module — voir Phase 7
    track_record.py/scripts/model_selection_shadow.py)."""
    if market == "1X2":
        return "home_win" if home_goals > away_goals else ("draw" if home_goals == away_goals else "away_win")
    if market == "BTTS":
        return "yes" if (home_goals > 0 and away_goals > 0) else "no"
    if market == "OVER_UNDER_2_5":
        return "over" if (home_goals + away_goals) > 2.5 else "under"
    raise ValueError(f"Marché inconnu : {market}")


def dc_predict_market_probs(model: DixonColesModel, home_team: str, away_team: str) -> Optional[dict[str, dict[str, float]]]:
    """Prédit les 3 marchés (1X2/BTTS/OVER_UNDER_2_5) pour un match, à partir
    d'un DixonColesModel déjà entraîné. BTTS n'existe pas nativement sur
    DixonColesModel (racine) — dérivé de la même matrice de score que
    predict_1x2/predict_over_under, EXACTEMENT comme LeagueModel.predict_btts
    en production (api/main.py) : yes = P(buts dom >= 1 ET buts ext >= 1),
    pas un nouveau paramètre appris. None si une équipe est inconnue du
    train tronqué à `until` (promotion en cours de fenêtre) — jamais fabriqué."""
    try:
        p1x2 = model.predict_1x2(home_team, away_team)
        pou = model.predict_over_under(home_team, away_team, line=2.5)
        matrix = model.predict_score_matrix(home_team, away_team)
    except ValueError:
        return None
    yes = float(matrix[1:, 1:].sum())
    return {
        "1X2": {"home_win": float(p1x2["home_win"]), "draw": float(p1x2["draw"]), "away_win": float(p1x2["away_win"])},
        "BTTS": {"yes": round(yes, 6), "no": round(1.0 - yes, 6)},
        "OVER_UNDER_2_5": {"over": float(pou["over"]), "under": float(pou["under"])},
    }


def _find_match_row(all_matches, match_date: date, home_team: str, away_team: str):
    mask = (
        (all_matches["date"].dt.date == match_date)
        & (all_matches["home_team"] == home_team)
        & (all_matches["away_team"] == away_team)
    )
    matched = all_matches[mask]
    if matched.empty:
        return None
    return matched.iloc[0]


@dataclass
class DCPrediction:
    key: tuple
    fold_index: int
    probs: dict[str, dict[str, float]]
    actual: dict[str, str]


@dataclass
class DCCoverageEntry:
    fold_index: int
    league: str
    until: str
    train_matches: int
    evaluable_matches: int
    skipped_matches: int
    available: bool
    reason: Optional[str] = None


@dataclass
class DCWalkForwardResult:
    predictions: dict[tuple, DCPrediction] = field(default_factory=dict)
    coverage: list[DCCoverageEntry] = field(default_factory=list)
    skip_reasons: dict[str, int] = field(default_factory=dict)


def build_dixon_coles_walk_forward(
    folds: list[list[tuple]],
    min_train_matches: int = MIN_DC_TRAIN_MATCHES,
) -> DCWalkForwardResult:
    """
    Pour chaque fold (dans l'ordre chronologique déjà utilisé pour
    elo/xgboost/lightgbm, voir scripts/walk_forward_ensemble.py::_make_folds),
    pour chaque ligue présente dans ce fold : entraîne Dixon-Coles
    (export_league, le moteur de production) sur l'historique de `match`
    STRICTEMENT jusqu'à `until = premier_match_du_fold - 1 jour` (borne
    incluse), puis prédit chaque match du fold pour cette ligue.

    Si l'historique d'entraînement disponible est trop court
    (< min_train_matches), la ligue entière est marquée indisponible pour ce
    fold avec la raison explicite "Dixon-Coles non disponible pour ce fold /
    cette période." (§4 du prompt) — JAMAIS simulé. Une équipe inconnue du
    train tronqué (promotion en cours de fenêtre) fait échouer UNIQUEMENT ce
    match (skip compté, jamais un match entier de fold perdu pour ça).
    """
    result = DCWalkForwardResult()
    league_match_cache: dict[str, "object"] = {}

    for fold_idx, fold_keys in enumerate(folds):
        if not fold_keys:
            continue
        until = fold_keys[0][1] - timedelta(days=1)
        by_league: dict[str, list[tuple]] = {}
        for key in fold_keys:
            by_league.setdefault(key[0], []).append(key)

        for league, keys in by_league.items():
            if league not in league_match_cache:
                raw = load_matches_from_db(league)
                # load_matches_from_db renvoie un DataFrame SANS colonnes (pd.DataFrame([]))
                # si la ligue n'a aucune ligne dans `match` -- jamais supposé non vide.
                league_match_cache[league] = (
                    raw.sort_values("date").reset_index(drop=True) if not raw.empty else raw
                )
            all_matches = league_match_cache[league]
            if all_matches.empty:
                train_df = all_matches
            else:
                train_df = all_matches[all_matches["date"].dt.date <= until].reset_index(drop=True)

            if len(train_df) < min_train_matches:
                result.coverage.append(DCCoverageEntry(
                    fold_index=fold_idx, league=league, until=until.isoformat(),
                    train_matches=len(train_df), evaluable_matches=0, skipped_matches=len(keys),
                    available=False,
                    reason=(
                        f"Dixon-Coles non disponible pour ce fold / cette période "
                        f"(historique d'entraînement={len(train_df)} < {min_train_matches} matchs requis avant "
                        f"{until.isoformat()})."
                    ),
                ))
                result.skip_reasons["insufficient_training_history"] = (
                    result.skip_reasons.get("insufficient_training_history", 0) + len(keys)
                )
                continue

            artifact = export_league(league, train_df)
            model = _dixon_coles_model_from_artifact(artifact)

            evaluable, skipped = 0, 0
            for key in keys:
                _, match_date, home_team, away_team = key
                match_row = _find_match_row(all_matches, match_date, home_team, away_team)
                if match_row is None:
                    skipped += 1
                    result.skip_reasons["result_not_found"] = result.skip_reasons.get("result_not_found", 0) + 1
                    continue
                probs = dc_predict_market_probs(model, home_team, away_team)
                if probs is None:
                    skipped += 1
                    result.skip_reasons["unknown_team"] = result.skip_reasons.get("unknown_team", 0) + 1
                    continue
                hg, ag = int(match_row["home_goals"]), int(match_row["away_goals"])
                actual = {m: actual_outcome(m, hg, ag) for m in ("1X2", "BTTS", "OVER_UNDER_2_5")}
                result.predictions[key] = DCPrediction(key=key, fold_index=fold_idx, probs=probs, actual=actual)
                evaluable += 1

            result.coverage.append(DCCoverageEntry(
                fold_index=fold_idx, league=league, until=until.isoformat(),
                train_matches=len(train_df), evaluable_matches=evaluable, skipped_matches=skipped,
                available=evaluable > 0,
                reason=None if evaluable > 0 else "Aucun match évaluable dans ce fold pour cette ligue (toutes les équipes étaient inconnues du train tronqué).",
            ))

    return result


def dc_observation(pred: DCPrediction, market: str) -> Optional[dict]:
    """Même forme que service.py::_market_observation — {p_true, probs,
    actual, correct} — pour que _compute_market_metrics (réutilisée telle
    quelle) traite les prédictions Dixon-Coles walk-forward identiquement
    aux observations sourcées en base."""
    probs = pred.probs.get(market)
    actual = pred.actual.get(market)
    if probs is None or actual is None:
        return None
    pick = max(probs, key=probs.get)
    return {"p_true": probs[actual], "probs": probs, "actual": actual, "correct": pick == actual}


def dc_observations_for_fold(dcwf: DCWalkForwardResult, market: str, fold_index: int) -> list[dict]:
    return [
        obs for pred in dcwf.predictions.values() if pred.fold_index == fold_index
        for obs in [dc_observation(pred, market)] if obs is not None
    ]


def dc_market_metrics_before_fold(dcwf: DCWalkForwardResult, market: str, fold_index: int) -> MarketMetrics:
    """Métriques Dixon-Coles walk-forward calculées EXCLUSIVEMENT à partir
    des folds STRICTEMENT antérieurs à `fold_index` (anti-fuite — même
    principe que `until` dans ensemble.compute_market_weights)."""
    observations = [
        obs for pred in dcwf.predictions.values() if pred.fold_index < fold_index
        for obs in [dc_observation(pred, market)] if obs is not None
    ]
    return _compute_market_metrics(observations)


def dc_market_metrics_in_range(dcwf: DCWalkForwardResult, market: str, since: Optional[date], until: Optional[date]) -> MarketMetrics:
    """Variante par PLAGE DE DATES de dc_market_metrics_before_fold — utile
    quand l'appelant raisonne en fenêtres temporelles explicites (Phase 6,
    voir model_selection.py) plutôt qu'en index de fold (Phase 5.7). Bornes
    incluses, mêmes conventions que service.py (`since`/`until` optionnels)."""
    observations = []
    for pred in dcwf.predictions.values():
        match_date = pred.key[1]
        if since is not None and match_date < since:
            continue
        if until is not None and match_date > until:
            continue
        obs = dc_observation(pred, market)
        if obs is not None:
            observations.append(obs)
    return _compute_market_metrics(observations)


# ---------------------------------------------------------------------------
# 3. Pondération incluant Dixon-Coles — parallèle à ensemble.compute_market_weights.
# ---------------------------------------------------------------------------

def compute_market_weights_including_dc(
    session: Session,
    market: str,
    until: date,
    fold_index: int,
    dcwf: DCWalkForwardResult,
    min_sample_size: int,
    strategy: WeightStrategy,
    model_versions: dict[str, object],
) -> WeightResult:
    """
    Reproduit EXACTEMENT la boucle de ensemble.compute_market_weights pour
    elo/xgboost/lightgbm (mêmes sources DB via _model_predictions_markets,
    mêmes conditions d'exclusion), et ajoute dixon_coles depuis l'historique
    walk-forward EN MÉMOIRE (dc_market_metrics_before_fold) au lieu de
    model_predictions (qui n'a aucune ligne backtest pour ce modèle — voir
    docstring de module). Le score brut et la normalisation finale
    réutilisent `strategy.compute_scores`/`_normalize_scores` de
    ensemble.py, jamais une deuxième implémentation.
    """
    weights: dict[str, ModelWeight] = {}
    excluded: dict[str, str] = {}
    metrics_by_model: dict[str, MarketMetrics] = {}

    for model_type in ("elo", "xgboost", "lightgbm"):
        version = model_versions.get(model_type)
        if version is None:
            excluded[model_type] = "Aucune version fournie pour ce type de modèle."
            continue
        metrics = _model_predictions_markets(session, model_type, version.id, league=None, since=None, until=until)[market]
        if metrics.sample_size < min_sample_size:
            excluded[model_type] = (
                f"Échantillon insuffisant sur {market} (toutes ligues) avant {until.isoformat()} "
                f"({metrics.sample_size} < {min_sample_size})."
            )
            continue
        if metrics.log_loss is None:
            excluded[model_type] = f"Marché {market} non modélisé par '{model_type}' (aucune probabilité disponible)."
            continue
        metrics_by_model[model_type] = metrics
        weights[model_type] = ModelWeight(
            model_type=model_type, version=version.name, model_version_id=version.id,
            weight=0.0, log_loss=metrics.log_loss, sample_size=metrics.sample_size, brier_score=metrics.brier_score,
        )

    dc_metrics = dc_market_metrics_before_fold(dcwf, market, fold_index)
    if dc_metrics.sample_size < min_sample_size:
        excluded["dixon_coles"] = (
            f"Échantillon insuffisant sur {market} avant {until.isoformat()} "
            f"({dc_metrics.sample_size} < {min_sample_size}) — historique Dixon-Coles walk-forward encore insuffisant."
        )
    elif dc_metrics.log_loss is None:
        excluded["dixon_coles"] = f"Marché {market} non modélisé par 'dixon_coles' sur cet historique walk-forward."
    else:
        metrics_by_model["dixon_coles"] = dc_metrics
        weights["dixon_coles"] = ModelWeight(
            model_type="dixon_coles", version="research-walk-forward", model_version_id=None,
            weight=0.0, log_loss=dc_metrics.log_loss, sample_size=dc_metrics.sample_size, brier_score=dc_metrics.brier_score,
        )

    scores = strategy.compute_scores(metrics_by_model)
    for model_type in list(weights.keys()):
        if model_type not in scores:
            excluded[model_type] = f"Stratégie '{strategy.name}' sans score calculable pour '{model_type}' sur {market}."
            del weights[model_type]

    if weights:
        normalized = _normalize_scores(scores)
        for model_type, w in weights.items():
            w.weight = round(normalized[model_type], 6)

    return WeightResult(market=market, until=until, strategy=strategy.name, league=None, weights=weights, excluded=excluded)


def market_model_coverage(session: Session, model_versions: dict[str, object]) -> dict[str, list[str]]:
    """Vérifie EMPIRIQUEMENT (jamais supposé, §14/§15 du prompt) quels
    model_types parmi elo/xgboost/lightgbm ont au moins une ligne
    model_predictions avec une probabilité non nulle sur BTTS/OVER_UNDER_2_5,
    pour les versions données. dixon_coles n'est pas inclus ici (son
    historique BTTS/O-U est walk-forward en mémoire, jamais dans
    model_predictions en source='backtest' — voir dcwf.predictions,
    combiné séparément par l'appelant)."""
    coverage: dict[str, list[str]] = {}
    for market in MARKETS:
        models_with_data = []
        for model_type in ("elo", "xgboost", "lightgbm"):
            version = model_versions.get(model_type)
            if version is None:
                continue
            metrics = _model_predictions_markets(session, model_type, version.id, league=None, since=None, until=None)[market]
            if metrics.sample_size > 0:
                models_with_data.append(model_type)
        coverage[market] = models_with_data
    return coverage


# ---------------------------------------------------------------------------
# 4. Diagnostic des poids — §28 du prompt (quantification du "67%").
# ---------------------------------------------------------------------------

def weight_diagnostics(weight_results: list[WeightResult]) -> dict:
    if not weight_results:
        return {"total_weight_computations": 0}

    by_model: dict[str, list[float]] = {}
    single_model_count = 0
    multi_model_count = 0
    zero_model_count = 0
    for wr in weight_results:
        n = len(wr.weights)
        if n == 0:
            zero_model_count += 1
        elif n == 1:
            single_model_count += 1
        else:
            multi_model_count += 1
        for model_type, w in wr.weights.items():
            by_model.setdefault(model_type, []).append(w.weight)

    per_model = {}
    for model_type, ws in by_model.items():
        per_model[model_type] = {
            "mean": round(statistics.mean(ws), 4),
            "median": round(statistics.median(ws), 4),
            "min": round(min(ws), 4),
            "max": round(max(ws), 4),
            "count": len(ws),
            "frequency_weight_over_90pct": round(sum(1 for w in ws if w > 0.9) / len(ws), 4),
        }

    total = len(weight_results)
    return {
        "total_weight_computations": total,
        "zero_model_fold_count": zero_model_count,
        "single_model_fold_count": single_model_count,
        "single_model_fraction": round(single_model_count / total, 4) if total else None,
        "multi_model_fold_count": multi_model_count,
        "multi_model_fraction": round(multi_model_count / total, 4) if total else None,
        "per_model": per_model,
    }


# ---------------------------------------------------------------------------
# 5. Statistiques — §19/§20/§21 du prompt (échantillon, incertitude).
# ---------------------------------------------------------------------------

def obs_log_loss(obs: dict, eps: float = 1e-15) -> float:
    """Log_loss d'UNE observation {p_true, probs, actual, correct} — même
    définition que service.py::_compute_market_metrics, exposée ici en
    version PAR OBSERVATION (nécessaire pour les tests appariés bootstrap/
    McNemar, qui comparent deux séries de valeurs INDIVIDUELLES sur les
    mêmes matchs, jamais des moyennes déjà agrégées)."""
    return -math.log(min(max(obs["p_true"], eps), 1 - eps))


def obs_brier(obs: dict) -> float:
    """Brier d'UNE observation — même définition que service.py, version
    par observation (voir obs_log_loss)."""
    return sum((p - (1.0 if k == obs["actual"] else 0.0)) ** 2 for k, p in obs["probs"].items())


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[Optional[float], Optional[float]]:
    """Intervalle de confiance de Wilson pour une proportion (accuracy) —
    plus fiable qu'un intervalle normal standard sur des échantillons
    modestes (~100-300 matchs, cas typique des folds de ce backtest)."""
    if n == 0:
        return None, None
    p = k / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))) / denom
    return round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4)


def bootstrap_paired_diff(pairs: list[tuple[float, float]], n_boot: int = 2000, seed: int = 20260828) -> dict:
    """IC bootstrap à 95% de (moyenne(a) - moyenne(b)) sur des paires
    PAR MATCH (log_loss ou brier individuel, jamais des moyennes déjà
    agrégées) évaluées par deux modèles/stratégies sur EXACTEMENT les mêmes
    matchs — un bootstrap PAIRÉ, pas deux échantillons indépendants.
    `significant`=True si l'IC exclut 0. Seed fixe -> reproductible (§22)."""
    n = len(pairs)
    if n == 0:
        return {"sample_size": 0, "mean_diff": None, "ci_low": None, "ci_high": None, "significant": False}
    rng = np.random.default_rng(seed)
    diffs = np.array([a - b for a, b in pairs])
    mean_diff = float(diffs.mean())
    boot_means = np.empty(n_boot)
    for i in range(n_boot):
        idx = rng.integers(0, n, size=n)
        boot_means[i] = diffs[idx].mean()
    ci_low, ci_high = (float(x) for x in np.percentile(boot_means, [2.5, 97.5]))
    return {
        "sample_size": n, "mean_diff": round(mean_diff, 5),
        "ci_low": round(ci_low, 5), "ci_high": round(ci_high, 5),
        "significant": bool(ci_low > 0 or ci_high < 0),
    }


def mcnemar_test(b: int, c: int) -> dict:
    """Test de McNemar (avec correction de continuité) sur des paires
    discordantes (b = A correct/B faux, c = l'inverse) — approprié car A et
    B sont évalués sur les MÊMES matchs (comparaison PAIRÉE), jamais un test
    à deux échantillons indépendants."""
    n = b + c
    if n == 0:
        return {"b": 0, "c": 0, "statistic": None, "p_value": None, "significant": False}
    from scipy.stats import chi2
    statistic = (abs(b - c) - 1) ** 2 / n
    p_value = float(1 - chi2.cdf(statistic, df=1))
    return {"b": b, "c": c, "statistic": round(float(statistic), 4), "p_value": round(p_value, 4), "significant": p_value < 0.05}


# ---------------------------------------------------------------------------
# 6. Calibration — RECHERCHE UNIQUEMENT (§16/§17/§18 du prompt), jamais activée
#    en production, jamais entraînée sur le fold de test.
# ---------------------------------------------------------------------------

def expected_calibration_error(bins: Optional[list[dict]]) -> Optional[float]:
    """ECE = moyenne pondérée de |confiance prédite - fréquence observée|
    sur le diagramme de calibration déjà produit par
    service.py::_compute_calibration (réutilisé tel quel, jamais recalculé)."""
    if not bins:
        return None
    total = sum(b["count"] for b in bins)
    if total == 0:
        return None
    return round(sum(b["count"] * abs(b["predicted_confidence_avg"] - b["observed_frequency"]) for b in bins) / total, 4)


def platt_calibrate(train_conf: list[float], train_correct: list[bool], test_conf: list[float]) -> list[float]:
    """Platt Scaling appliqué à la confiance du pick (probabilité maximale
    parmi les issues du marché) — pas les 3/2 probabilités indépendamment,
    voir apply_pick_calibration ci-dessous et la section Limites du rapport.
    Entraîné UNIQUEMENT sur (train_conf, train_correct) — jamais sur le
    fold de test. Si train+validation n'a qu'une seule classe observée
    (échantillon trop homogène), aucune calibration n'est forcée (§16) :
    les probabilités de test sont retournées inchangées."""
    from sklearn.linear_model import LogisticRegression

    y_train = [1 if c else 0 for c in train_correct]
    if len(set(y_train)) < 2:
        return list(test_conf)
    clf = LogisticRegression()
    clf.fit(np.array(train_conf).reshape(-1, 1), np.array(y_train))
    return clf.predict_proba(np.array(test_conf).reshape(-1, 1))[:, 1].tolist()


def isotonic_calibrate(train_conf: list[float], train_correct: list[bool], test_conf: list[float]) -> list[float]:
    """Isotonic Regression, même discipline train/validation-only que
    platt_calibrate ci-dessus."""
    from sklearn.isotonic import IsotonicRegression

    if len(set(train_correct)) < 2:
        return list(test_conf)
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(np.array(train_conf), np.array([1.0 if c else 0.0 for c in train_correct]))
    return iso.predict(np.array(test_conf)).tolist()


def redistribute_pick_probability(probs: dict[str, float], calibrated_pick_prob: float) -> dict[str, float]:
    """Remplace la probabilité du PICK (probabilité maximale parmi les
    issues) par `calibrated_pick_prob` et redistribue la masse restante
    proportionnellement aux probabilités D'ORIGINE des autres issues — la
    seule pièce mathématique de la recalibration "confiance du pick" (voir
    apply_pick_calibration, qui l'applique à une liste d'observations
    RÉSOLUES, et Phase 6 calibration_engine.py::produce_candidate_probability,
    qui l'applique à UNE prédiction non résolue). Extrait ici pour ne jamais
    dupliquer cette arithmétique entre les deux usages."""
    pick = max(probs, key=probs.get)
    old_pick_p = probs[pick]
    remaining_old = 1.0 - old_pick_p
    remaining_new = max(0.0, 1.0 - calibrated_pick_prob)
    other_keys = [k for k in probs if k != pick]
    new_probs = {pick: calibrated_pick_prob}
    if remaining_old > 1e-9:
        for k in other_keys:
            new_probs[k] = probs[k] / remaining_old * remaining_new
    else:
        for k in other_keys:
            new_probs[k] = remaining_new / max(len(other_keys), 1)
    return new_probs


def apply_pick_calibration(observations: list[dict], calibrated_pick_probs: list[float]) -> list[dict]:
    """Recalibre la probabilité du PICK (confiance) et redistribue la masse
    restante proportionnellement aux probabilités d'origine des AUTRES
    issues — hypothèse documentée (voir Limites du rapport) : ceci
    recalibre la confiance du pick, pas les 3/2 issues indépendamment (un
    Platt/Isotonic multi-classe complet serait un chantier séparé). Le pick
    lui-même ne change jamais ici (Platt/Isotonic sont monotones dans la
    confiance d'origine -> le classement des issues d'un même match est
    préservé)."""
    out = []
    for obs, p_cal in zip(observations, calibrated_pick_probs):
        new_probs = redistribute_pick_probability(obs["probs"], p_cal)
        actual = obs["actual"]
        out.append({"p_true": new_probs[actual], "probs": new_probs, "actual": actual, "correct": obs["correct"]})
    return out


def derive_calibration_verdict(raw_metrics: MarketMetrics, calibrated_metrics: Optional[MarketMetrics], min_sample_size: int) -> str:
    """HELPFUL/NEUTRAL/HARMFUL/INSUFFICIENT_DATA à partir de l'amélioration
    RELATIVE de log_loss (calibré vs brut) — extrait de la logique déjà
    validée dans scripts/research_ensemble.py (Phase 5.7, ré-exécutée sans
    changement ici) pour être réutilisable par calibration_engine.py (Phase
    6) sans dupliquer le seuil de décision (1% relatif, documenté ici une
    seule fois). Jamais calculé si l'échantillon brut est sous le seuil."""
    if (raw_metrics is None or calibrated_metrics is None
            or raw_metrics.sample_size < min_sample_size or not raw_metrics.log_loss):
        return "INSUFFICIENT_DATA"
    rel = (raw_metrics.log_loss - calibrated_metrics.log_loss) / raw_metrics.log_loss
    if rel > 0.01:
        return "HELPFUL"
    if rel < -0.01:
        return "HARMFUL"
    return "NEUTRAL"


# ---------------------------------------------------------------------------
# 7. Rapport persistant — §23/§35 du prompt. Écriture fichier UNIQUEMENT,
#    jamais dans la base (Aucun accès session ici).
# ---------------------------------------------------------------------------

def write_reports(result: dict, outdir, run_id: str) -> tuple:
    import json
    from pathlib import Path

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    json_path = outdir / f"ensemble_backtest_{run_id}.json"
    md_path = outdir / f"ensemble_backtest_{run_id}.md"
    json_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown_report(result), encoding="utf-8")
    return json_path, md_path


def _fmt(value, digits: int = 4) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _metrics_table(rows: list[dict]) -> str:
    header = "| Modèle/Stratégie | Accuracy | Log Loss | Brier | N | CI Accuracy (95%) |\n"
    header += "|---|---|---|---|---|---|\n"
    lines = [header]
    for r in rows:
        ci = r.get("accuracy_ci")
        ci_str = f"[{_fmt(ci[0])}, {_fmt(ci[1])}]" if ci and ci[0] is not None else "N/A"
        lines.append(
            f"| {r['name']} | {_fmt(r.get('accuracy'))} | {_fmt(r.get('log_loss'))} | "
            f"{_fmt(r.get('brier_score'))} | {r.get('sample_size', 0)} | {ci_str} |\n"
        )
    return "".join(lines)


def render_markdown_report(result: dict) -> str:
    """Formate `result` (assemblé par scripts/research_ensemble.py) selon
    les 24 sections du §35 du prompt Phase 5.7. Pure fonction de mise en
    forme — aucune valeur n'est recalculée ici, uniquement affichée."""
    md = ["# XFOOT ENSEMBLE RESEARCH & BACKTEST V2\n"]

    md.append("\n## 1. Résumé exécutif\n")
    md.append(f"\nRun id : `{result.get('run_id')}` — généré le {result.get('generated_at')}.\n")
    verdicts = result.get("verdicts", {})
    for k, v in verdicts.items():
        md.append(f"\n- **{k}** : {v}\n")

    md.append("\n## 2. Dataset\n")
    ds = result.get("dataset", {})
    md.append(f"\n- Ligues : {', '.join(ds.get('leagues', []))}\n")
    md.append(f"- Période : {ds.get('period_start')} → {ds.get('period_end')}\n")
    md.append(f"- Matchs communs (elo/xgboost/lightgbm) : {ds.get('common_match_count')}\n")
    md.append(f"- Échantillons par modèle (avant intersection) : {ds.get('per_model_sample_sizes')}\n")

    md.append("\n## 3. Méthodologie\n")
    md.append(f"\n{result.get('methodology', '')}\n")

    md.append("\n## 4. Walk-forward\n")
    md.append(f"\n{result.get('walk_forward_description', '')}\n")

    md.append("\n## 5. Folds\n")
    for f in result.get("folds", []):
        md.append(
            f"\n- Fold {f['index']} ({f['role']}) : {f['n_matches']} matchs, "
            f"{f['start_date']} → {f['end_date']}, until={f['until']}\n"
        )

    md.append("\n## 6. Baselines (1X2, folds de test uniquement)\n\n")
    md.append(_metrics_table(result.get("baselines_test", [])))

    md.append("\n## 7. Simple Average\n\n")
    md.append(_metrics_table(result.get("strategies_test", {}).get("simple_average", [])))

    md.append("\n## 8. Inverse Log Loss\n\n")
    md.append(_metrics_table(result.get("strategies_test", {}).get("inverse_log_loss", [])))
    md.append(f"\nDiagnostic des poids : {result.get('weight_diagnostics', {}).get('inverse_log_loss', {})}\n")

    md.append("\n## 9. Softmax\n\n")
    md.append(f"\nTempérature sélectionnée (validation) : {result.get('softmax_temperature_selected')}\n\n")
    md.append(_metrics_table(result.get("strategies_test", {}).get("softmax_log_loss", [])))

    md.append("\n## 10. Brier\n\n")
    md.append(_metrics_table(result.get("strategies_test", {}).get("brier", [])))

    md.append("\n## 11. Hybrid\n\n")
    md.append("\nalpha=0.5, fixe (non optimisé — même politique que la production).\n\n")
    md.append(_metrics_table(result.get("strategies_test", {}).get("hybrid", [])))

    md.append("\n## 12. Dixon-Coles (walk-forward, en mémoire)\n\n")
    md.append(f"\nCouverture par fold/ligue :\n\n{result.get('dc_coverage_summary', '')}\n")

    md.append("\n## 13. BTTS\n\n")
    md.append(f"\nVerdict couverture : {result.get('btts_verdict')}\n\n")
    md.append(_metrics_table(result.get("btts_results", [])))

    md.append("\n## 14. Over/Under 2.5\n\n")
    md.append(f"\nVerdict couverture : {result.get('over_under_verdict')}\n\n")
    md.append(_metrics_table(result.get("over_under_results", [])))

    md.append("\n## 15. Calibration\n\n")
    md.append(_metrics_table(result.get("calibration_results", [])))

    md.append("\n## 16. Analyse des poids\n\n")
    md.append(f"\n```\n{result.get('weight_diagnostics', {})}\n```\n")

    md.append("\n## 17. Leakage audit\n\n")
    for entry in result.get("leakage_audit", []):
        md.append(f"\n- {entry['experiment']} : **{entry['status']}** — {entry['note']}\n")

    md.append("\n## 18. Résultats par fold\n\n")
    for fold_result in result.get("per_fold_results", []):
        md.append(f"\n### Fold {fold_result['fold_index']}\n\n")
        md.append(_metrics_table(fold_result.get("rows", [])))

    md.append("\n## 19. Résultats agrégés\n\n")
    md.append(_metrics_table(result.get("aggregated_results", [])))

    md.append("\n## 20. Comparaison aux modèles individuels\n\n")
    md.append(f"\n{result.get('comparison_vs_individuals', '')}\n")

    md.append("\n## 21. Significativité / incertitude\n\n")
    md.append(f"\n```\n{result.get('significance', {})}\n```\n")

    md.append("\n## 22. Reproductibilité\n\n")
    repro = result.get("reproducibility", {})
    for k, v in repro.items():
        md.append(f"\n- {k} : {v}\n")

    md.append("\n## 23. Limites\n\n")
    for limit in result.get("limitations", []):
        md.append(f"\n- {limit}\n")

    md.append("\n## 24. Conclusion\n\n")
    md.append(f"\n{result.get('conclusion', '')}\n")

    md.append("\n---\n\nPHASE 5.7 — XFOOT ENSEMBLE RESEARCH & BACKTEST V2 TERMINÉE. "
               "AUCUNE PROMOTION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.\n")

    return "".join(md)


# ---------------------------------------------------------------------------
# 8. Rapport Model Selection Engine V1 / Calibration Engine V1 — Phase 6.
#    Même convention que write_reports/render_markdown_report (§7 ci-dessus)
#    mais nom de fichier et sections distincts — jamais mélangé aux rapports
#    Ensemble Phase 5.7 (répertoire de sortie différent, voir
#    scripts/model_selection_research.py).
# ---------------------------------------------------------------------------

def write_model_selection_reports(result: dict, outdir, run_id: str) -> tuple:
    import json
    from pathlib import Path

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    json_path = outdir / f"model_selection_{run_id}.json"
    md_path = outdir / f"model_selection_{run_id}.md"
    json_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_model_selection_markdown_report(result), encoding="utf-8")
    return json_path, md_path


def render_model_selection_markdown_report(result: dict) -> str:
    """Formate `result` (assemblé par scripts/model_selection_research.py) —
    pure fonction de mise en forme, réutilise _fmt/_metrics_table (§7)."""
    md = ["# XFOOT MODEL SELECTION ENGINE V1 + CALIBRATION ENGINE V1\n"]

    md.append("\n## 1. Résumé exécutif\n")
    md.append(f"\nRun id : `{result.get('run_id')}` — généré le {result.get('generated_at')} — mode : {result.get('mode')}.\n")

    md.append("\n## 2. Dataset\n")
    ds = result.get("dataset", {})
    md.append(f"\n- Ligues : {', '.join(ds.get('leagues', []))}\n")
    md.append(f"- Période : {ds.get('period_start')} → {ds.get('period_end')}\n")
    md.append(f"- Matchs communs (elo/xgboost/lightgbm) : {ds.get('common_match_count')}\n")

    md.append("\n## 3. Méthodologie\n")
    md.append(f"\n{result.get('methodology', '')}\n")

    md.append("\n## 4. Fenêtres\n")
    for w in result.get("windows", []):
        md.append(f"\n- Fenêtre {w['index']} ({w['role']}) : {w['n_matches']} matchs, {w['start_date']} → {w['end_date']}\n")

    md.append("\n## 5. Décisions de sélection par marché\n\n")
    for market, decision in result.get("selection_by_market", {}).items():
        md.append(f"\n### {market}\n\n")
        md.append(f"- **Statut** : {decision.get('status')}\n")
        md.append(f"- Modèle sélectionné : {decision.get('selected_model_type')}\n")
        md.append(f"- Dauphin : {decision.get('runner_up_model_type')}\n")
        md.append(f"- Fenêtres évaluées : {decision.get('windows_evaluated')}\n")
        md.append(f"- Modèles éligibles (données suffisantes) : {decision.get('eligible_models')}\n")
        md.append(f"- Rang #1 par fenêtre : {decision.get('top_rank_counts')}\n")
        md.append(f"- Coefficient de variation log_loss : {decision.get('log_loss_cv')}\n")
        md.append(f"- Crédibilité statistique (bootstrap) : {decision.get('credibility')}\n")
        md.append(f"- Raison : {decision.get('reason')}\n")

    md.append("\n## 6. Calibration par marché\n\n")
    md.append(_metrics_table(result.get("calibration_rows", [])))
    for market, calib in result.get("calibration_by_market", {}).items():
        md.append(f"\n- **{market}** : choix={calib.get('choice')}, verdict={calib.get('verdict')}, "
                   f"ECE brut={calib.get('raw_ece')}, ECE Platt={calib.get('platt_ece')}, ECE Isotonic={calib.get('isotonic_ece')}\n")

    md.append("\n## 7. Résultats agrégés par marché\n\n")
    md.append(_metrics_table(result.get("aggregated_results", [])))

    md.append("\n## 8. Leakage audit\n\n")
    for entry in result.get("leakage_audit", []):
        md.append(f"\n- {entry['experiment']} : **{entry['status']}** — {entry['note']}\n")

    md.append("\n## 9. Reproductibilité\n\n")
    repro = result.get("reproducibility", {})
    for k, v in repro.items():
        md.append(f"\n- {k} : {v}\n")

    md.append("\n## 10. Limites\n\n")
    for limit in result.get("limitations", []):
        md.append(f"\n- {limit}\n")

    md.append("\n## 11. Conclusion\n\n")
    md.append(f"\n{result.get('conclusion', '')}\n")

    md.append("\n---\n\nPHASE 6 — XFOOT MODEL SELECTION ENGINE V1 + CALIBRATION ENGINE V1 TERMINÉE (RECHERCHE + SHADOW). "
               "AUCUNE PROMOTION PRODUCTION EFFECTUÉE. AUCUN MODÈLE DE PRODUCTION REMPLACÉ.\n")

    return "".join(md)
