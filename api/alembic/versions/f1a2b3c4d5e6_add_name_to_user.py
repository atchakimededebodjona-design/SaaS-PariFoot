"""ajoute la colonne name (nom/pseudo) à la table user

Revision ID: f1a2b3c4d5e6
Revises: d3f6a1b8c452
Create Date: 2026-08-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'd3f6a1b8c452'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable : les comptes existants n'ont pas de nom renseigné, seules
    # les nouvelles inscriptions le rendent obligatoire (côté API).
    op.add_column('user', sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('user', 'name')
