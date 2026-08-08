import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# api/ doit être sur sys.path pour que `from app...` résolve, quel que soit
# le répertoire courant depuis lequel `alembic` est invoqué.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import SQLModel

# Importés uniquement pour que leurs classes table=True s'enregistrent
# dans SQLModel.metadata avant l'autogénération — jamais utilisés directement.
from app.models.user import User  # noqa: F401
from app.models.subscription import Subscription, ProcessedPulseDelivery  # noqa: F401
from app.models.model_artifact import ModelArtifact  # noqa: F401

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Même variable d'environnement que l'application (app/core/database.py) —
# une seule source de vérité pour l'URL de connexion, jamais dupliquée dans
# alembic.ini.
db_url = os.environ.get("DATABASE_URL", "sqlite:///./app.db")
config.set_main_option("sqlalchemy.url", db_url)

target_metadata = SQLModel.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
