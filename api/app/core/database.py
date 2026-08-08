"""
Configuration de la base de données.

DATABASE_URL par défaut : SQLite local (fichier app.db), pour développer
sans dépendance externe. En production, définir la variable
d'environnement DATABASE_URL vers PostgreSQL/Supabase, ex. :

    postgresql://user:password@host:5432/dbname

Le code applicatif (modèles, requêtes) ne change pas entre les deux —
SQLModel/SQLAlchemy abstrait la différence.
"""

import os
from sqlmodel import SQLModel, create_engine, Session


def resolve_database_url() -> str:
    """
    Résout DATABASE_URL depuis l'environnement — SEULE fonction du projet à
    faire cette résolution (alembic/env.py l'importe plutôt que de la
    dupliquer, pour rester la « seule source de vérité » que son propre
    commentaire revendiquait déjà sans que ce soit vraiment le cas jusqu'ici).

    Deux corrections apportées ici, absentes jusqu'à présent :

    1. Railway (et Heroku avant lui) peut fournir l'URL Postgres préfixée
       `postgres://` (forme historique) plutôt que `postgresql://`.
       SQLAlchemy 1.4+ ne reconnaît plus `postgres` comme nom de dialecte
       enregistré — create_engine() échoue alors sèchement. Normalisé ici.
    2. `os.environ.get("DATABASE_URL", DEFAULT)` ne retombe sur DEFAULT que
       si la clé est ABSENTE de l'environnement — pas si elle vaut ""
       (chaîne vide). Une variable Railway présente mais vide (ex. valeur
       de référence pas encore résolue au moment du déploiement) doit
       retomber sur le même défaut qu'une variable absente plutôt que de
       faire échouer create_engine("") avec une erreur peu explicite.
    """
    url = os.environ.get("DATABASE_URL") or "sqlite:///./app.db"
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


DATABASE_URL = resolve_database_url()

# check_same_thread=False nécessaire uniquement pour SQLite (FastAPI gère
# les requêtes dans des threads différents) — sans effet sur PostgreSQL.
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)


def init_db():
    """Crée les tables si elles n'existent pas encore. À appeler au démarrage."""
    SQLModel.metadata.create_all(engine)


def get_session():
    """Dépendance FastAPI : une session DB par requête, fermée proprement à la fin."""
    with Session(engine) as session:
        yield session
