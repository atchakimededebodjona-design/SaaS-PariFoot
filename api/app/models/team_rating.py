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
from sqlmodel import SQLModel, Field, Session, select


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


def next_version_name(session: Session, base_name: str) -> str:
    """
    Prochain nom de version disponible pour `base_name` (ex.
    "xfoot-dixon-coles") — "xfoot-dixon-coles-v1" si aucune version
    n'existe encore, "xfoot-dixon-coles-v2" si "v1" existe déjà, etc.

    Corrige un plantage réel : les scripts de persistance (Phase 3)
    utilisaient un nom fixe ("xfoot-dixon-coles-v1") — un run interrompu
    après avoir écrit la ligne model_versions (mais avant team_ratings, ex.
    crash sur un bug non lié) laissait cette version en place, et tout run
    suivant échouait sur la contrainte UNIQUE de `name`. Chaque run crée
    maintenant une NOUVELLE version au lieu d'entrer en conflit — cohérent
    avec l'objet même de model_versions : garder un historique des
    versions successives du modèle, jamais écraser une version existante.
    """
    existing_names = session.exec(
        select(ModelVersion.name).where(ModelVersion.name.like(f"{base_name}-v%"))
    ).all()
    max_n = 0
    prefix_len = len(base_name) + 2  # +2 pour "-v"
    for name in existing_names:
        suffix = name[prefix_len:]
        if suffix.isdigit():
            max_n = max(max_n, int(suffix))
    return f"{base_name}-v{max_n + 1}"


def deactivate_other_versions(session: Session, model_type: str) -> None:
    """
    Désactive toutes les ModelVersion existantes du même model_type, avant
    qu'une nouvelle version ne soit potentiellement marquée active — au
    plus UNE version active par type de modèle à la fois, sinon
    `is_active` ne veut plus rien dire dès qu'une deuxième version existe.
    Ne commit pas elle-même (laisse l'appelant grouper dans sa transaction).
    """
    others = session.exec(select(ModelVersion).where(ModelVersion.model_type == model_type)).all()
    for other in others:
        other.is_active = False
        session.add(other)
