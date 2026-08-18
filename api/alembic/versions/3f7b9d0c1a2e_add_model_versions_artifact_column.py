"""add model_versions.artifact column

Revision ID: 3f7b9d0c1a2e
Revises: 9a1c2e6f4b3d
Create Date: 2026-08-18 22:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '3f7b9d0c1a2e'
down_revision: Union[str, Sequence[str], None] = '9a1c2e6f4b3d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('model_versions', sa.Column('artifact', sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('model_versions', 'artifact')
