"""
Historique des matchs en base — fondation de données pour les phases
futures (Radar, Track Record), en complément du pipeline CSV existant.

IMPORTANT : purement additif. build_features.py, export_model_artifacts.py
et dixon_coles.py continuent de lire data/all_leagues_raw_with_stats.csv
exactement comme avant ; ces tables ne sont lues par aucun endpoint de
l'API pour l'instant. Chargées par scripts/load_historical_matches.py.

Match/MatchStats séparées (plutôt qu'une seule table large) : `match` reste
la table de référence légère (résultat, identité du match), `match_stats`
porte le détail (tirs, corners) qui pourrait s'enrichir plus tard avec
d'autres sources (xG, cartons) sans toucher à `match`.
"""

from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import UniqueConstraint
from sqlmodel import SQLModel, Field


class Match(SQLModel, table=True):
    __tablename__ = "match"
    __table_args__ = (
        UniqueConstraint("league", "date", "home_team", "away_team", name="uq_match_league_date_teams"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    league: str = Field(index=True)
    date: datetime = Field(index=True)
    home_team: str
    away_team: str
    home_goals: int
    away_goals: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MatchStats(SQLModel, table=True):
    __tablename__ = "match_stats"

    id: Optional[int] = Field(default=None, primary_key=True)
    match_id: int = Field(foreign_key="match.id", unique=True, index=True)
    # Nullable : au moins une ligne de l'historique source n'a pas ces
    # stats renseignées (vérifié sur data/all_leagues_raw_with_stats.csv,
    # 1 ligne sur 12 459 concernée) — jamais fabriquées à 0 par le loader.
    home_shots: Optional[int] = None
    away_shots: Optional[int] = None
    home_shots_target: Optional[int] = None
    away_shots_target: Optional[int] = None
    home_corners: Optional[int] = None
    away_corners: Optional[int] = None
