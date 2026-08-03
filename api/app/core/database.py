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

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./app.db")

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
