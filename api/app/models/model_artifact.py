"""
Stockage en base des artefacts de modèle Dixon-Coles (attack/defense/
home_advantage/rho par ligue), en complément des fichiers
api/model_artifacts/<league>.json.

Nécessaire depuis que le ré-entraînement (refresh_and_retrain.py) tourne
sur un Cron Job Railway séparé du service web (api/main.py) : les deux
services ne partagent PAS de système de fichiers (un Volume Railway est
attaché à un seul service, jamais partagé entre deux — voir
RAILWAY_CRON_SETUP.md), donc un fichier écrit par le Cron Job n'est jamais
vu par le service web. La base de données Postgres, elle, est accessible
aux deux.

`payload` reproduit tel quel le contenu JSON de <league>.json (attack,
defense, home_advantage, rho, teams, xi, l2_reg, trained_at, data_up_to)
plutôt que d'éclater ces champs en colonnes — évite de dupliquer/faire
diverger le schéma entre le fichier et la base pour un artefact qui reste,
fonctionnellement, le même objet.
"""

from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field


class ModelArtifact(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    league: str = Field(unique=True, index=True)
    payload: str  # JSON complet — voir docstring module
    trained_at: str
    data_up_to: str
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
