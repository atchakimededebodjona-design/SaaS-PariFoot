"""ajoute la table d'audit model_promotion_events pour la Phase 10

Revision ID: d3f6a1b8c452
Revises: b4d1e7f92a6c
Create Date: 2026-08-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'd3f6a1b8c452'
down_revision: Union[str, Sequence[str], None] = 'b4d1e7f92a6c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'model_promotion_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('model_version_id', sa.Integer(), nullable=False),
        sa.Column('previous_model_version_id', sa.Integer(), nullable=True),
        sa.Column('model_type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('market', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('decision', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('reason', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('metrics', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('sample_size', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('actor', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('automatic', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['model_version_id'], ['model_versions.id'],
                                 name=op.f('fk_model_promotion_events_model_version_id_model_versions')),
        sa.ForeignKeyConstraint(['previous_model_version_id'], ['model_versions.id'],
                                 name=op.f('fk_model_promotion_events_previous_model_version_id_model_versions')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_model_promotion_events')),
    )
    op.create_index(op.f('ix_model_promotion_events_model_version_id'), 'model_promotion_events',
                     ['model_version_id'], unique=False)
    op.create_index(op.f('ix_model_promotion_events_model_type'), 'model_promotion_events',
                     ['model_type'], unique=False)
    op.create_index(op.f('ix_model_promotion_events_decision'), 'model_promotion_events',
                     ['decision'], unique=False)
    op.create_index(op.f('ix_model_promotion_events_created_at'), 'model_promotion_events',
                     ['created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_model_promotion_events_created_at'), table_name='model_promotion_events')
    op.drop_index(op.f('ix_model_promotion_events_decision'), table_name='model_promotion_events')
    op.drop_index(op.f('ix_model_promotion_events_model_type'), table_name='model_promotion_events')
    op.drop_index(op.f('ix_model_promotion_events_model_version_id'), table_name='model_promotion_events')
    op.drop_table('model_promotion_events')
