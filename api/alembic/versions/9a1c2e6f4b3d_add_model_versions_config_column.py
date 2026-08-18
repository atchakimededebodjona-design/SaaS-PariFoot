"""add model_versions.config column

Revision ID: 9a1c2e6f4b3d
Revises: 7ffc0de2ede4
Create Date: 2026-08-18 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '9a1c2e6f4b3d'
down_revision: Union[str, Sequence[str], None] = '7ffc0de2ede4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('model_versions', sa.Column('config', sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('model_versions', 'config')
