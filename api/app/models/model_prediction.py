"""
Prédictions individuelles multi-modèles (Phase 6) — source de vérité commune
pour TOUS les modèles de Xfoot AI Arena (Dixon-Coles, Elo, XGBoost, LightGBM,
futurs modèles), en complément de `prediction_log` (réservée à Dixon-Coles,
inchangée — voir docstring de app/ai/arena/prediction_logging.py pour la
répartition exacte et pourquoi les deux coexistent plutôt qu'une migration).

`match_id` n'est PAS une clé étrangère vers la table `match` (Phase 2) :
celle-ci ne contient que des matchs déjà JOUÉS (historique d'entraînement),
jamais un match futur — une prédiction EN DIRECT (Dixon-Coles) porte sur un
match qui n'existe pas encore dans `match` au moment de la requête, exactement
la même contrainte qui empêchait déjà `prediction_log` d'avoir un vrai FK
(voir son docstring). league/match_date/home_team/away_team restent donc des
colonnes explicites et indexées — une clé naturelle, pas une relation SQL.

Probabilités BTTS/OVER_UNDER_2_5 nullables : Elo (api/app/ai/engine/elo.py)
et les modèles XGBoost/LightGBM actuels (scripts/train_ml_stacking_from_db.py)
ne modélisent QUE le 1X2 — vérifié dans leur code, pas supposé. Aucune
probabilité de score/BTTS n'existe pour ces moteurs ; les laisser nulles est
la seule option honnête (jamais fabriquées à 0.5 ou déduites du 1X2).
"""

from datetime import date, datetime, timezone
from typing import Optional
from sqlalchemy import UniqueConstraint
from sqlmodel import SQLModel, Field


class ModelPrediction(SQLModel, table=True):
    __tablename__ = "model_predictions"
    __table_args__ = (
        # Un seul snapshot par (match, type de modèle, VERSION de modèle) —
        # une nouvelle version (ex. relance d'un backtest, qui crée toujours
        # une NOUVELLE ModelVersion via next_version_name) produit de
        # nouvelles lignes légitimes, jamais un doublon de la même version
        # relancée deux fois (idempotence, §13 du ticket Phase 6).
        UniqueConstraint(
            "league", "match_date", "home_team", "away_team", "model_type", "model_version_id",
            name="uq_model_predictions_match_model_version",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    # Identité du match — clé naturelle, PAS un FK (voir docstring module).
    league: str = Field(index=True)
    match_date: date = Field(index=True)
    home_team: str
    away_team: str

    # Identité du modèle — model_version_id est un VRAI FK vers model_versions
    # (table déjà existante depuis la Phase 3) : aucun système de
    # versionnage parallèle créé ici (§4 du ticket).
    model_type: str = Field(index=True)
    model_version_id: int = Field(foreign_key="model_versions.id", index=True)

    predicted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # "live" : produite par un appel réel à /predictions/* (Dixon-Coles
    #   aujourd'hui, seul modèle servi en direct).
    # "backtest" : produite pendant l'évaluation walk-forward/split temporel
    #   d'un script de backtest (Elo, XGBoost, LightGBM) — le résultat réel
    #   est alors déjà connu au moment de l'insertion (match historique),
    #   résolue immédiatement — jamais un cas de fuite, la probabilité
    #   elle-même reste calculée sans connaître ce résultat (walk-forward/
    #   split temporel déjà en place avant ce ticket).
    source: str = Field(index=True)

    # --- Probabilités RÉELLEMENT produites par le modèle avant le match ---
    prob_home: float
    prob_draw: float
    prob_away: float
    prob_btts_yes: Optional[float] = None
    prob_btts_no: Optional[float] = None
    prob_over_2_5: Optional[float] = None
    prob_under_2_5: Optional[float] = None

    # Picks dérivés au moment de la prédiction (même convention que
    # PredictionLog.pick_1x2/pick_btts/pick_over_2_5, voir prediction_logging.py)
    # — None si le marché n'est pas modélisé par ce modèle.
    pick_1x2: str
    pick_btts: Optional[str] = None
    pick_over_2_5: Optional[str] = None

    confidence: Optional[float] = None
    features_version: Optional[str] = None

    status: str = Field(default="pending", index=True)  # "pending" | "resolved" | "invalid"

    result_home_goals: Optional[int] = None
    result_away_goals: Optional[int] = None
    result_fetched_at: Optional[datetime] = None
    correct_1x2: Optional[bool] = None
    correct_btts: Optional[bool] = None
    correct_over_2_5: Optional[bool] = None
