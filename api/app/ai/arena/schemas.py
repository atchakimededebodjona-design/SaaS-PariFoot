"""
Formes de réponse de Xfoot AI Arena (Phase 5).

Aucune classe SQLModel `table=True` ici : l'Arena ne crée AUCUNE nouvelle
table, elle ne fait que lire/recomposer des données déjà stockées dans
`model_artifact`, `model_versions`, `team_ratings` et `prediction_log` —
voir service.py pour le détail de provenance de chaque champ.
"""

from pydantic import BaseModel


class LeagueArtifactInfo(BaseModel):
    league: str
    trained_at: str
    data_up_to: str


class ModelMetrics(BaseModel):
    """Champ V1, CONSERVÉ TEL QUEL pour compatibilité (voir service.py) —
    équivalent à markets["1X2"/"BTTS"/"OVER_UNDER_2_5"].accuracy. Un champ à
    None = NOT AVAILABLE, jamais deviné/recalculé à partir d'un texte libre."""
    accuracy_1x2: float | None = None
    accuracy_btts: float | None = None
    accuracy_over_under_2_5: float | None = None
    sample_size: int = 0


class MarketMetrics(BaseModel):
    """
    Métriques structurées d'UN marché (1X2 | BTTS | OVER_UNDER_2_5), pour UN
    modèle, calculées dynamiquement depuis prediction_log (voir service.py).
    Chaque champ à None = NOT_AVAILABLE, jamais deviné.

    - accuracy : correct_predictions / sample_size.
    - log_loss : -mean(log(p_true)), p_true = probabilité RÉELLEMENT
      attribuée par le modèle à l'issue réellement survenue (jamais
      recalculée après coup), clippée à [1e-15, 1-1e-15] contre log(0).
    - brier_score : somme des carrés d'écart (probabilité prédite -
      indicateur 0/1) sur TOUTES les issues du marché (2 pour BTTS/O-U, 3
      pour 1X2 — même définition que scripts/backtest_elo.py::brier_score,
      généralisée à N issues), moyennée sur l'échantillon. Plus bas = mieux.
    - calibration (Phase 8, §26) : diagramme de calibration par tranche de
      confiance dans le pick (predicted_confidence_avg vs observed_frequency,
      voir service.py::_compute_calibration) — None si sample_size est sous
      le seuil MIN_BENCHMARK_SAMPLE_SIZE, jamais un binning sur trop peu de
      points. Ne remplace ni ne modifie accuracy/log_loss/brier_score.
    """
    accuracy: float | None = None
    log_loss: float | None = None
    brier_score: float | None = None
    sample_size: int = 0
    correct_predictions: int | None = None
    calibration: list[dict] | None = None


class AppliedFilters(BaseModel):
    """Filtres réellement appliqués à la requête — toujours renvoyés pour
    que l'appelant sache exactement sur quel sous-ensemble portent les
    métriques (transparence, voir §18 du ticket Phase 5 V2)."""
    market: str | None = None
    league: str | None = None
    since: str | None = None
    until: str | None = None
    period: str = "all_time"  # "all_time" | "date_range" — SEASON non disponible (aucun identifiant de saison en base)
    # None (défaut) = LIVE et BACKTEST confondus (comportement Phase 5/6/7
    # inchangé) ; "live"|"backtest" = restreint explicitement (§21 Phase 8) —
    # jamais mélangés SANS que l'appelant l'ait demandé.
    prediction_source: str | None = None


class ModelPerformanceEntry(BaseModel):
    model_type: str
    version: str
    # "model_artifact" : modèle de production Dixon-Coles (api/model_artifacts/*.json
    #   ou table model_artifact, voir main.py::LEAGUE_MODELS) — pas de ligne model_versions.
    # "model_versions" : version backtestée réelle (table model_versions).
    source: str
    model_version_id: int | None = None  # id réel de `model_versions` — jamais fabriqué
    is_active: bool
    trained_at: str | None = None  # None pour l'entrée production (une date PAR LIGUE, voir `leagues`)
    leagues: list[LeagueArtifactInfo] | None = None
    metrics: ModelMetrics  # V1, conservé — voir markets pour le détail V2
    metrics_available: bool
    markets: dict[str, MarketMetrics]  # V2 — toujours les 3 clés (1X2/BTTS/OVER_UNDER_2_5), NOT_AVAILABLE si pas de donnée
    team_ratings_count: int | None = None
    notes: str | None = None


class ArenaPerformanceResponse(BaseModel):
    models: list[ModelPerformanceEntry]
    # model_type dont une entrée est is_active=True. "ambiguous" si plusieurs
    # (ex. une version model_versions activée manuellement en plus de la
    # production), jamais deviné.
    active_model_type: str
    # "insufficient_data" tant qu'aucune métrique structurée n'est comparable
    # entre types de modèles (voir service.py) — jamais un classement inventé.
    best_model_by_metric: str
    filters: AppliedFilters
    min_benchmark_sample_size: int


class ModelMarketSummary(BaseModel):
    """Résumé compact d'un modèle sur UN marché — utilisé par
    GET /models/benchmark pour la comparaison croisée."""
    model_type: str
    version: str
    source: str
    is_active: bool
    accuracy: float | None = None
    log_loss: float | None = None
    brier_score: float | None = None
    sample_size: int = 0


class BestModelResult(BaseModel):
    """
    status="ok" UNIQUEMENT si au moins 2 modèles ont, sur le MÊME marché,
    la même métrique disponible avec sample_size >= min_benchmark_sample_size
    (voir service.py::_pick_best_model). Priorité de métrique (§9 du
    ticket) : log_loss, puis brier_score, puis accuracy — jamais un
    classement arbitraire, jamais un "gagnant" choisi sur une métrique
    différente entre deux modèles.
    """
    status: str  # "ok" | "insufficient_data"
    metric: str | None = None  # "log_loss" | "brier_score" | "accuracy"
    market: str | None = None
    best_model: str | None = None
    version: str | None = None
    value: float | None = None
    sample_size: int | None = None
    reason: str | None = None  # explique pourquoi insufficient_data, jamais laissé vide dans ce cas


class MarketBenchmark(BaseModel):
    models: list[ModelMarketSummary]
    best_model: BestModelResult


class ArenaBenchmarkResponse(BaseModel):
    status: str  # toujours "ok" : l'ABSENCE de gagnant (best_model.status="insufficient_data") n'est pas une erreur HTTP
    min_benchmark_sample_size: int
    filters: AppliedFilters
    markets: dict[str, MarketBenchmark]


class ModelPredictionRead(BaseModel):
    """
    Vue publique d'UNE ligne model_predictions (Phase 6) — reflète
    exactement les colonnes de app/models/model_prediction.py, jamais de
    secret/PII (aucun champ utilisateur sur cette table)."""
    id: int
    league: str
    match_date: str
    home_team: str
    away_team: str
    model_type: str
    model_version_id: int
    predicted_at: str
    source: str
    prob_home: float
    prob_draw: float
    prob_away: float
    prob_btts_yes: float | None = None
    prob_btts_no: float | None = None
    prob_over_2_5: float | None = None
    prob_under_2_5: float | None = None
    pick_1x2: str
    pick_btts: str | None = None
    pick_over_2_5: str | None = None
    confidence: float | None = None
    features_version: str | None = None
    status: str
    result_home_goals: int | None = None
    result_away_goals: int | None = None
    result_fetched_at: str | None = None
    correct_1x2: bool | None = None
    correct_btts: bool | None = None
    correct_over_2_5: bool | None = None


class ModelPredictionListResponse(BaseModel):
    predictions: list[ModelPredictionRead]
    count: int
    limit: int
