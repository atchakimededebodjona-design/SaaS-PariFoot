"""
Ratings d'équipes calculés par un modèle (Dixon-Coles, Elo, ...) et
métadonnées de version de modèle — Phase 3 du plan Xfoot AI.

Noms de tables au pluriel (team_ratings, model_versions), tels que
spécifiés dans le ticket Phase 3 et dans le schéma cible de l'audit
Phase 1 (§9) — contrairement à `match`/`match_stats` (Phase 2), qui
suivent la convention singulière déjà en place pour user/subscription.
Incohérence mineure entre les deux phases, gardée telle quelle plutôt que
de renommer `match`/`match_stats` sans qu'on me l'ait demandé.
"""

from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import UniqueConstraint
from sqlmodel import SQLModel, Field


class ModelVersion(SQLModel, table=True):
    __tablename__ = "model_versions"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)  # ex. "xfoot-dixon-coles-v1"
    model_type: str = Field(index=True)  # "dixon_coles" | "elo"
    trained_at: datetime
    is_active: bool = Field(default=False)
    notes: Optional[str] = None


class TeamRating(SQLModel, table=True):
    __tablename__ = "team_ratings"
    __table_args__ = (
        # Même principe que la contrainte UNIQUE sur `match` en Phase 2 : un
        # (team, league) ne doit jamais avoir deux lignes de rating pour la
        # MÊME version de modèle — protège contre une double exécution
        # accidentelle du script de persistance, qui dupliquerait sinon
        # silencieusement les ratings plutôt que de les mettre à jour.
        UniqueConstraint("team", "league", "model_version_id", name="uq_team_ratings_team_league_model_version"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    team: str = Field(index=True)
    league: str = Field(index=True)
    # Dixon-Coles : attack/defense = les deux paramètres appris.
    # Elo : une seule dimension de force -> stockée dans `attack`, `defense`
    # fixé à 0.0 par convention (documentée ici plutôt qu'ajouter une
    # colonne hors du schéma spécifié dans le ticket).
    attack: float
    defense: float
    model_version_id: int = Field(foreign_key="model_versions.id", index=True)
    computed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
