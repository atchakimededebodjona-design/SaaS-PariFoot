"""
Xfoot AI Arena — service (Phase 5, V1 + V2 Performance & Benchmarking).

Compare objectivement les modèles de prédiction connus de Xfoot en lisant
UNIQUEMENT des données déjà stockées : aucun réentraînement, aucun appel
réseau, aucun calcul ML dans ce module.

=== Deux sources de données, jamais confondues (déjà établi en V1) ===

  - Le modèle de PRODUCTION (Dixon-Coles) n'a PAS de ligne dans
    `model_versions` — il est chargé au démarrage du service web depuis
    `model_artifact`/les fichiers api/model_artifacts/*.json, un artefact
    PAR LIGUE (voir main.py::LEAGUE_MODELS).

  - Elo / XGBoost / LightGBM (et tout futur modèle backtesté) sont des
    lignes réelles de `model_versions`, avec un verdict honnête en texte
    libre (`notes`) et un `is_active` décidé par leur script de backtest.

=== Ce que la V2 ajoute : métriques par marché, calculées SANS FUITE ===

Audit préalable (Phase 5 V2, §1) : `prediction_log.payload` contient déjà,
pour CHAQUE prédiction Dixon-Coles, les probabilités COMPLÈTES produites au
moment de la requête (home_win/draw/away_win, btts_yes/no, over_2_5/
under_2_5) — vérifié en base, pas supposé. Ces probabilités sont écrites
AVANT que le résultat soit connu (`_log_prediction` dans main.py, appelé au
moment de la prédiction) ; les colonnes `result_*`/`correct_*` ne sont
remplies qu'APRÈS coup par fetch_daily_results.py. Aucune fuite possible :
la probabilité utilisée pour évaluer une prédiction est toujours celle
réellement produite avant le match, jamais recalculée avec le résultat en
main.

SEUL Dixon-Coles écrit dans `prediction_log` (`_log_prediction` n'est
appelé que depuis `_resolve_and_predict`, elle-même uniquement utilisée par
`/predictions/*`) — vérifié en lisant main.py, pas supposé. Elo/XGBoost/
LightGBM n'ont donc AUCUNE donnée par-prédiction stockée nulle part : leurs
scripts de backtest (scripts/backtest_elo.py,
scripts/train_ml_stacking_from_db.py) calculent déjà leurs propres métriques
une fois, sur leur propre split, et n'écrivent que le verdict en texte libre
dans `ModelVersion.notes` (voir V1). Ce module ne les reparse JAMAIS par
regex pour en extraire des nombres structurés — ce serait fragile et
trompeur. Pour ces modèles, `markets[...]` reste donc NOT_AVAILABLE
(sample_size=0) : c'est l'état réel des données, pas une limitation de ce
code.

Conséquence directe (Phase 5 V2) : la comparaison croisée (GET /models/
benchmark) ne pouvait alors jamais désigner de "meilleur modèle" par
métrique structurée — un seul modèle (Dixon-Coles) avait des données
comparables.

=== Phase 6 : model_predictions comme source pour Elo/XGBoost/LightGBM ===

Depuis la Phase 6 (voir app/ai/arena/prediction_logging.py), Elo/XGBoost/
LightGBM PEUVENT avoir des données par-prédiction réelles : leurs scripts de
backtest (scripts/backtest_elo.py, scripts/train_ml_stacking_from_db.py)
logguent désormais individuellement chaque prédiction de leur ensemble de
test (déjà walk-forward/split temporel, donc sans fuite) dans la table
`model_predictions`, résolues immédiatement puisque le résultat historique y
est déjà connu. Une `ModelVersion` sans lignes `model_predictions` associées
(ex. une ancienne version créée avant ce ticket) reste honnêtement
NOT_AVAILABLE — jamais reconstruite depuis le texte libre de `notes`.

Dixon-Coles continue d'être mesuré EXCLUSIVEMENT depuis `prediction_log`
(inchangé) — même s'il écrit AUSSI, depuis la Phase 6, dans
`model_predictions` (dual-write, pour passer par le même mécanisme commun
que les autres modèles) : ses métriques ne sont JAMAIS recalculées à partir
de cette seconde source, pour ne jamais compter une prédiction deux fois ni
risquer une divergence entre deux calculs (§7 du ticket Phase 6 : "conserver
une source unique de vérité").
"""

import json
import math
import os
from datetime import date

from sqlalchemy import func
from sqlmodel import Session, select

from app.models.model_prediction import ModelPrediction
from app.models.prediction_log import PredictionLog
from app.models.team_rating import ModelVersion, TeamRating

from .schemas import (
    AppliedFilters,
    ArenaBenchmarkResponse,
    ArenaPerformanceResponse,
    BestModelResult,
    LeagueArtifactInfo,
    MarketBenchmark,
    MarketMetrics,
    ModelMarketSummary,
    ModelMetrics,
    ModelPerformanceEntry,
    ModelPredictionRead,
)

MARKETS = ("1X2", "BTTS", "OVER_UNDER_2_5")

# Seuil en-dessous duquel une métrique n'est jamais utilisée pour désigner un
# "meilleur modèle" (§11 du ticket) — configurable via variable
# d'environnement, définie UNE SEULE FOIS ici (jamais hardcodée ailleurs).
# 100 est la valeur proposée par le ticket ; avec les volumes réels observés
# en local (3 prédictions loguées, voir rapport), ce seuil signifie que le
# benchmark renvoie honnêtement "insufficient_data" tant que l'usage réel
# n'a pas accumulé assez de prédictions résolues — comportement voulu, pas
# un bug.
MIN_BENCHMARK_SAMPLE_SIZE = int(os.environ.get("MIN_BENCHMARK_SAMPLE_SIZE", "100"))

# Protection numérique contre log(0) — convention standard (ex. sklearn
# log_loss), jamais une donnée inventée : borne la probabilité RÉELLEMENT
# stockée, ne la remplace jamais par une valeur arbitraire au milieu.
_LOG_EPS = 1e-15


def _clip(p: float) -> float:
    return min(max(p, _LOG_EPS), 1.0 - _LOG_EPS)


def _market_observation(log, payload: dict, market: str) -> dict | None:
    """
    Reconstruit, pour UNE prédiction déjà résolue, l'issue réellement
    survenue et les probabilités que le modèle avait RÉELLEMENT produites
    (jamais recalculées) pour le marché demandé. Retourne None si le
    marché ne s'applique pas ou si une donnée requise est absente
    (jamais fabriquée).

    `log` est duck-typé : soit un PredictionLog (Phase 5, Dixon-Coles),
    soit un ModelPrediction (Phase 6, tous modèles) — les deux exposent les
    mêmes attributs result_home_goals/result_away_goals/correct_1x2/
    correct_btts/correct_over_2_5, `payload` en normalise l'accès (voir
    _model_prediction_payload ci-dessous pour l'adaptateur ModelPrediction).
    """
    hg, ag = log.result_home_goals, log.result_away_goals
    if hg is None or ag is None:
        return None

    if market == "1X2":
        actual = "home_win" if hg > ag else ("draw" if hg == ag else "away_win")
        probs = {"home_win": payload["home_win"], "draw": payload["draw"], "away_win": payload["away_win"]}
        correct = log.correct_1x2
    elif market == "BTTS":
        actual = "yes" if (hg > 0 and ag > 0) else "no"
        probs = {"yes": payload["btts_yes"], "no": payload["btts_no"]}
        correct = log.correct_btts
    elif market == "OVER_UNDER_2_5":
        actual = "over" if (hg + ag) > 2.5 else "under"
        probs = {"over": payload["over_2_5"], "under": payload["under_2_5"]}
        correct = log.correct_over_2_5
    else:
        raise ValueError(f"Marché inconnu : {market}")

    if correct is None:
        return None  # pas encore rapproché, ou marché non modélisé par ce modèle (Phase 6)

    if any(p is None for p in probs.values()):
        return None  # modèle ne modélisant pas ce marché (ex. Elo/XGBoost/LightGBM sur BTTS/O-U) — jamais fabriqué

    return {"p_true": probs[actual], "probs": probs, "actual": actual, "correct": correct}


_CALIBRATION_BINS = 5  # granularité fixe (§26) — pas configurable, pour ne jamais choisir un binning après coup


def _compute_calibration(observations: list[dict]) -> list[dict] | None:
    """
    Diagramme de calibration (§26 Phase 8) : regroupe les observations par
    tranche de confiance dans le PICK du modèle (probabilité maximale parmi
    les issues du marché — ex. "domicile à 70%"), puis compare cette
    confiance moyenne à la fréquence RÉELLEMENT observée où ce pick était
    correct dans ce bin. Répond à « une prédiction à 70% se réalise-t-elle
    vraiment ~70% du temps ? » — jamais une transformation des métriques
    Phase 5/6 (accuracy/log_loss/brier_score restent calculées séparément,
    inchangées).

    None si l'échantillon total est sous MIN_BENCHMARK_SAMPLE_SIZE (même
    seuil que le reste de l'Arena) : un diagramme de calibration sur trop
    peu de points n'est pas digne de confiance, jamais affiché comme tel.
    """
    if len(observations) < MIN_BENCHMARK_SAMPLE_SIZE:
        return None

    bins: list[list[tuple[float, float]]] = [[] for _ in range(_CALIBRATION_BINS)]
    for o in observations:
        p_pick = max(o["probs"].values())
        idx = min(int(p_pick * _CALIBRATION_BINS), _CALIBRATION_BINS - 1)
        bins[idx].append((p_pick, 1.0 if o["correct"] else 0.0))

    result = []
    for i, b in enumerate(bins):
        if not b:
            continue
        preds = [x[0] for x in b]
        outcomes = [x[1] for x in b]
        result.append({
            "bin_range": [round(i / _CALIBRATION_BINS, 2), round((i + 1) / _CALIBRATION_BINS, 2)],
            "predicted_confidence_avg": round(sum(preds) / len(preds), 4),
            "observed_frequency": round(sum(outcomes) / len(outcomes), 4),
            "count": len(b),
        })
    return result


def _compute_market_metrics(observations: list[dict]) -> MarketMetrics:
    """Agrège une liste d'observations (voir _market_observation) en
    accuracy/log_loss/brier_score — MarketMetrics vide (NOT_AVAILABLE) si
    la liste est vide, jamais une valeur inventée."""
    if not observations:
        return MarketMetrics(sample_size=0)

    n = len(observations)
    correct_n = sum(1 for o in observations if o["correct"])
    log_loss = -sum(math.log(_clip(o["p_true"])) for o in observations) / n
    brier = sum(
        sum((p - (1.0 if cls == o["actual"] else 0.0)) ** 2 for cls, p in o["probs"].items())
        for o in observations
    ) / n

    return MarketMetrics(
        accuracy=round(correct_n / n, 4),
        log_loss=round(log_loss, 4),
        brier_score=round(brier, 4),
        sample_size=n,
        correct_predictions=correct_n,
        calibration=_compute_calibration(observations),
    )


def _fetch_resolved_predictions(
    session: Session, league: str | None, since: date | None, until: date | None,
    prediction_source: str | None = None,
) -> list[PredictionLog]:
    # prediction_log n'a AUCUNE colonne `source` (Dixon-Coles n'y écrit QUE
    # depuis /predictions/*, jamais depuis un backtest — voir docstring de
    # prediction_logging.py) : filtrer sur prediction_source="backtest" est
    # donc honnêtement TOUJOURS vide ici (jamais une table jointe/devinée
    # pour combler), "live"/None laisse le comportement Phase 5 inchangé
    # (§21 et §36 du ticket Phase 8 : jamais mélanger LIVE/BACKTEST en
    # silence, mais jamais casser le comportement par défaut existant non plus).
    # Nommé `prediction_source` (pas `source`) pour ne jamais se confondre
    # avec ModelPerformanceEntry.source (model_artifact/model_versions —
    # concept sans rapport : la PROVENANCE de stockage, pas live/backtest).
    if prediction_source == "backtest":
        return []
    stmt = select(PredictionLog).where(PredictionLog.result_fetched_at.is_not(None))
    if league is not None:
        stmt = stmt.where(PredictionLog.league == league)
    if since is not None:
        stmt = stmt.where(PredictionLog.match_date >= since)
    if until is not None:
        stmt = stmt.where(PredictionLog.match_date <= until)
    return session.exec(stmt).all()


def _dixon_coles_markets(
    session: Session, league: str | None, since: date | None, until: date | None,
    prediction_source: str | None = None,
) -> dict[str, MarketMetrics]:
    """
    Calcule dynamiquement, pour CHAQUE marché, les métriques Dixon-Coles à
    partir de prediction_log — une seule requête DB (filtrée), puis un seul
    passage Python par marché sur les lignes déjà récupérées (pas de requête
    répétée). Volume actuel : quelques milliers de lignes au plus — calcul
    dynamique volontairement conservé pour cette V2 (voir §16 du ticket ;
    recommandation de cache/job si le volume grossit, voir rapport final).

    `prediction_source` (Phase 8, §21) : None (défaut, comportement Phase
    5/6/7 INCHANGÉ) = toutes origines confondues ; "live"|"backtest"
    restreint explicitement — jamais mélangées SANS que l'appelant l'ait
    demandé.
    """
    logs = _fetch_resolved_predictions(session, league, since, until, prediction_source=prediction_source)

    parsed: list[tuple[PredictionLog, dict]] = []
    for log in logs:
        try:
            parsed.append((log, json.loads(log.payload)))
        except (json.JSONDecodeError, TypeError):
            continue  # payload corrompu — ignoré, jamais fabriqué (ne devrait pas arriver en pratique)

    result: dict[str, MarketMetrics] = {}
    for market in MARKETS:
        observations = []
        for log, payload in parsed:
            obs = _market_observation(log, payload, market)
            if obs is not None:
                observations.append(obs)
        result[market] = _compute_market_metrics(observations)
    return result


def _dixon_coles_production_entry(
    session: Session, league_models: dict, league: str | None, since: date | None, until: date | None,
    prediction_source: str | None = None,
) -> ModelPerformanceEntry:
    leagues = [
        LeagueArtifactInfo(
            league=lg,
            trained_at=league_models[lg].trained_at,
            data_up_to=league_models[lg].data_up_to,
        )
        for lg in sorted(league_models.keys())
    ]
    markets = _dixon_coles_markets(session, league, since, until, prediction_source=prediction_source)
    legacy_metrics = ModelMetrics(
        accuracy_1x2=markets["1X2"].accuracy,
        accuracy_btts=markets["BTTS"].accuracy,
        accuracy_over_under_2_5=markets["OVER_UNDER_2_5"].accuracy,
        sample_size=markets["1X2"].sample_size,
    )
    return ModelPerformanceEntry(
        model_type="dixon_coles",
        version="production",
        source="model_artifact",
        model_version_id=None,
        # Structurel, pas un champ stocké : c'est le seul modèle chargé dans
        # LEAGUE_MODELS et réellement servi par /predictions/* — jamais "à
        # parité" avec les versions backtestées inactives ci-dessous.
        is_active=True,
        trained_at=None,
        leagues=leagues,
        metrics=legacy_metrics,
        metrics_available=legacy_metrics.sample_size > 0,
        markets=markets,
        team_ratings_count=None,
        notes=(
            "Modèle de production, servi par /predictions/*. Entraîné séparément PAR "
            "LIGUE (voir champ 'leagues') — aucune ligne model_versions dédiée pour "
            "cette entrée. Métriques par marché calculées depuis prediction_log "
            "(probabilités pré-match réellement produites, jamais recalculées)."
        ),
    )


def _legacy_metrics_from_markets(markets: dict[str, MarketMetrics]) -> ModelMetrics:
    """Reconstruit le champ V1 `metrics` (Phase 5) à partir de `markets`
    (Phase 5 V2/6) — une seule implémentation, réutilisée pour dixon_coles
    ET les versions model_predictions, jamais deux calculs distincts."""
    return ModelMetrics(
        accuracy_1x2=markets["1X2"].accuracy,
        accuracy_btts=markets["BTTS"].accuracy,
        accuracy_over_under_2_5=markets["OVER_UNDER_2_5"].accuracy,
        sample_size=markets["1X2"].sample_size,
    )


def _model_prediction_payload(row: ModelPrediction) -> dict:
    """Adapte une ligne ModelPrediction (colonnes explicites) à la même
    forme de dict que prediction_log.payload (JSON) — permet à
    _market_observation de traiter les deux sources IDENTIQUEMENT, sans
    dupliquer la logique de calcul (§7 du ticket Phase 6)."""
    return {
        "home_win": row.prob_home, "draw": row.prob_draw, "away_win": row.prob_away,
        "btts_yes": row.prob_btts_yes, "btts_no": row.prob_btts_no,
        "over_2_5": row.prob_over_2_5, "under_2_5": row.prob_under_2_5,
    }


def _fetch_resolved_model_predictions(
    session: Session, model_type: str, model_version_id: int,
    league: str | None, since: date | None, until: date | None, prediction_source: str | None = None,
) -> list[ModelPrediction]:
    stmt = select(ModelPrediction).where(
        ModelPrediction.model_type == model_type,
        ModelPrediction.model_version_id == model_version_id,
        ModelPrediction.status == "resolved",
    )
    if league is not None:
        stmt = stmt.where(ModelPrediction.league == league)
    if since is not None:
        stmt = stmt.where(ModelPrediction.match_date >= since)
    if until is not None:
        stmt = stmt.where(ModelPrediction.match_date <= until)
    if prediction_source is not None:
        stmt = stmt.where(ModelPrediction.source == prediction_source)
    return session.exec(stmt).all()


def _model_predictions_markets(
    session: Session, model_type: str, model_version_id: int,
    league: str | None, since: date | None, until: date | None, prediction_source: str | None = None,
) -> dict[str, MarketMetrics]:
    """
    Équivalent Phase 6 de _dixon_coles_markets, mais sourcé depuis
    `model_predictions` pour UNE version de modèle donnée — vide
    (NOT_AVAILABLE) si cette version n'a aucune ligne résolue (ex. une
    ancienne ModelVersion créée avant ce ticket, jamais reconstruite depuis
    `notes`).

    `prediction_source` (Phase 8, §21) : None (défaut, comportement Phase
    5/6/7 INCHANGÉ) = "live" et "backtest" confondus ; sinon restreint
    explicitement — jamais mélangés sans que l'appelant l'ait demandé.
    """
    rows = _fetch_resolved_model_predictions(
        session, model_type, model_version_id, league, since, until, prediction_source=prediction_source,
    )

    result: dict[str, MarketMetrics] = {}
    for market in MARKETS:
        observations = []
        for row in rows:
            obs = _market_observation(row, _model_prediction_payload(row), market)
            if obs is not None:
                observations.append(obs)
        result[market] = _compute_market_metrics(observations)
    return result


def _entry_from_model_version(
    session: Session, version: ModelVersion, league: str | None, since: date | None, until: date | None,
    prediction_source: str | None = None,
) -> ModelPerformanceEntry:
    ratings_count = session.exec(
        select(func.count()).select_from(TeamRating).where(TeamRating.model_version_id == version.id)
    ).one()
    markets = _model_predictions_markets(session, version.model_type, version.id, league, since, until,
                                          prediction_source=prediction_source)
    return ModelPerformanceEntry(
        model_type=version.model_type,
        version=version.name,
        source="model_versions",
        model_version_id=version.id,
        is_active=version.is_active,
        trained_at=version.trained_at.isoformat(),
        leagues=None,
        metrics=_legacy_metrics_from_markets(markets),
        metrics_available=markets["1X2"].sample_size > 0,
        # NOT_AVAILABLE (sample_size=0) si cette version n'a aucune ligne
        # model_predictions résolue (voir docstring _model_predictions_markets)
        # — jamais reconstruit depuis le texte libre de `notes`.
        markets=markets,
        team_ratings_count=ratings_count,
        notes=version.notes,
    )


def get_models_performance(
    session: Session,
    league_models: dict,
    league: str | None = None,
    since: date | None = None,
    until: date | None = None,
    prediction_source: str | None = None,
) -> ArenaPerformanceResponse:
    """
    `prediction_source` (Phase 8, §21) : None (défaut, comportement Phase
    5/6/7 INCHANGÉ) = "live" et "backtest" confondus ; "live"|"backtest"
    restreint explicitement — jamais mélangés sans que l'appelant l'ait
    demandé (voir _model_predictions_markets/_dixon_coles_markets).
    """
    entries = [_dixon_coles_production_entry(session, league_models, league, since, until, prediction_source)]

    versions = session.exec(
        select(ModelVersion).order_by(ModelVersion.model_type, ModelVersion.id)
    ).all()
    entries += [_entry_from_model_version(session, v, league, since, until, prediction_source) for v in versions]

    active_types = [e.model_type for e in entries if e.is_active]
    if len(active_types) == 1:
        active_model_type = active_types[0]
    elif not active_types:
        active_model_type = "insufficient_data"  # ne devrait pas arriver (production toujours active)
    else:
        active_model_type = "ambiguous"  # plusieurs entrées actives à la fois — jamais tranché arbitrairement

    # Aucune métrique structurée n'est comparable entre types de modèles
    # aujourd'hui (seul dixon_coles en a, voir docstring module) : pas de
    # classement possible, jamais inventé — voir §9 du ticket Phase 5 V2 et
    # GET /models/benchmark pour la logique de comparaison détaillée.
    best_model_by_metric = "insufficient_data"

    filters = AppliedFilters(
        league=league,
        since=since.isoformat() if since else None,
        until=until.isoformat() if until else None,
        period="date_range" if (since or until) else "all_time",
        prediction_source=prediction_source,
    )

    return ArenaPerformanceResponse(
        models=entries,
        active_model_type=active_model_type,
        best_model_by_metric=best_model_by_metric,
        filters=filters,
        min_benchmark_sample_size=MIN_BENCHMARK_SAMPLE_SIZE,
    )


def get_model_version_detail(session: Session, model_version_id: int) -> ModelPerformanceEntry | None:
    """Détail d'une version BACKTESTÉE (ligne réelle de `model_versions`,
    id auto-incrémenté). Retourne None si l'id est inconnu — l'entrée
    production Dixon-Coles n'a jamais d'id ici, voir main.py."""
    version = session.get(ModelVersion, model_version_id)
    if version is None:
        return None
    return _entry_from_model_version(session, version, league=None, since=None, until=None)


def _pick_best_model(
    summaries: list[ModelMarketSummary], market: str, min_sample_size: int = MIN_BENCHMARK_SAMPLE_SIZE
) -> BestModelResult:
    """
    Désigne un "meilleur modèle" pour UN marché, UNIQUEMENT si les données
    le permettent réellement (§9-§10-§11 du ticket) :

      1. Ne considère que les modèles avec sample_size >= min_sample_size
         (prudence sur les petits échantillons — §11).
      2. Compare par métrique probabiliste en priorité — log_loss, puis
         brier_score (les deux : plus bas = meilleur), puis accuracy en
         dernier recours (plus élevé = meilleur) — §9.
      3. Nécessite AU MOINS 2 modèles avec la même métrique disponible pour
         qu'une comparaison ait un sens ; sinon "insufficient_data" avec la
         raison explicite, jamais un gagnant choisi par défaut.
    """
    eligible = [s for s in summaries if s.sample_size >= min_sample_size]

    for metric_name, lower_is_better in (("log_loss", True), ("brier_score", True), ("accuracy", False)):
        candidates = [s for s in eligible if getattr(s, metric_name) is not None]
        if len(candidates) < 2:
            continue
        best = (min if lower_is_better else max)(candidates, key=lambda s: getattr(s, metric_name))
        return BestModelResult(
            status="ok",
            metric=metric_name,
            market=market,
            best_model=best.model_type,
            version=best.version,
            value=getattr(best, metric_name),
            sample_size=best.sample_size,
        )

    if len(eligible) < 2:
        reason = (
            f"Moins de 2 modèles avec sample_size >= {min_sample_size} sur le marché {market} "
            f"({len(eligible)} éligible(s) sur {len(summaries)})."
        )
    else:
        reason = f"Aucune métrique commune (log_loss/brier_score/accuracy) disponible sur au moins 2 modèles pour {market}."
    return BestModelResult(status="insufficient_data", market=market, reason=reason)


def get_models_benchmark(
    session: Session,
    league_models: dict,
    market: str | None = None,
    league: str | None = None,
    since: date | None = None,
    until: date | None = None,
    prediction_source: str | None = None,
) -> ArenaBenchmarkResponse:
    performance = get_models_performance(session, league_models, league=league, since=since, until=until,
                                          prediction_source=prediction_source)

    markets_to_report = [market] if market else list(MARKETS)
    result_markets: dict[str, MarketBenchmark] = {}
    for m in markets_to_report:
        summaries = [
            ModelMarketSummary(
                model_type=e.model_type,
                version=e.version,
                source=e.source,
                is_active=e.is_active,
                accuracy=e.markets[m].accuracy,
                log_loss=e.markets[m].log_loss,
                brier_score=e.markets[m].brier_score,
                sample_size=e.markets[m].sample_size,
            )
            for e in performance.models
        ]
        result_markets[m] = MarketBenchmark(models=summaries, best_model=_pick_best_model(summaries, m))

    return ArenaBenchmarkResponse(
        status="ok",
        min_benchmark_sample_size=MIN_BENCHMARK_SAMPLE_SIZE,
        filters=performance.filters,
        markets=result_markets,
    )


# ---------------------------------------------------------------------------
# Consultation brute de model_predictions (Phase 6, §10 — observabilité/audit,
# distincte des vues agrégées ci-dessus).
# ---------------------------------------------------------------------------

def _to_prediction_read(row: ModelPrediction) -> ModelPredictionRead:
    return ModelPredictionRead(
        id=row.id,
        league=row.league,
        match_date=row.match_date.isoformat(),
        home_team=row.home_team,
        away_team=row.away_team,
        model_type=row.model_type,
        model_version_id=row.model_version_id,
        predicted_at=row.predicted_at.isoformat(),
        source=row.source,
        prob_home=row.prob_home, prob_draw=row.prob_draw, prob_away=row.prob_away,
        prob_btts_yes=row.prob_btts_yes, prob_btts_no=row.prob_btts_no,
        prob_over_2_5=row.prob_over_2_5, prob_under_2_5=row.prob_under_2_5,
        pick_1x2=row.pick_1x2, pick_btts=row.pick_btts, pick_over_2_5=row.pick_over_2_5,
        confidence=row.confidence, features_version=row.features_version,
        status=row.status,
        result_home_goals=row.result_home_goals, result_away_goals=row.result_away_goals,
        result_fetched_at=row.result_fetched_at.isoformat() if row.result_fetched_at else None,
        correct_1x2=row.correct_1x2, correct_btts=row.correct_btts, correct_over_2_5=row.correct_over_2_5,
    )


def list_model_predictions(
    session: Session,
    model_type: str | None = None,
    status: str | None = None,
    league: str | None = None,
    limit: int = 100,
) -> tuple[list[ModelPredictionRead], int]:
    """Liste brute, la plus récente d'abord — plafonnée à `limit` (défaut
    100) pour rester légère (§14 : pas de calcul lourd par requête).
    Retourne (lignes, total réellement matché avant troncature)."""
    base = select(ModelPrediction)
    if model_type is not None:
        base = base.where(ModelPrediction.model_type == model_type)
    if status is not None:
        base = base.where(ModelPrediction.status == status)
    if league is not None:
        base = base.where(ModelPrediction.league == league)

    total = session.exec(select(func.count()).select_from(base.subquery())).one()
    rows = session.exec(base.order_by(ModelPrediction.predicted_at.desc()).limit(limit)).all()
    return [_to_prediction_read(r) for r in rows], total


def get_model_prediction(session: Session, prediction_id: int) -> ModelPredictionRead | None:
    row = session.get(ModelPrediction, prediction_id)
    if row is None:
        return None
    return _to_prediction_read(row)
