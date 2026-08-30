"""ajoute les tables model_selection_decisions et shadow_selection_predictions
pour la Phase 6 (Model Selection Engine V1 + Calibration Engine V1,
recherche + shadow uniquement — voir api/app/ai/arena/model_selection.py).

Purement additif : aucune table existante n'est modifiée. Ces deux tables
sont totalement isolées de model_predictions/model_versions/team_ratings et
de model_promotion_events (vocabulaire de promotion LIVE distinct, déjà
câblé aux crons Railway — jamais réutilisé ici).

Revision ID: a7c3e6f19b2d
Revises: f34a28d2538d
Create Date: 2026-08-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'a7c3e6f19b2d'
down_revision: Union[str, Sequence[str], None] = 'f34a28d2538d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'model_selection_decisions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('run_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('mode', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('market', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('league', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('as_of', sa.Date(), nullable=False),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('selected_model_type', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('runner_up_model_type', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('windows_evaluated', sa.Integer(), nullable=False),
        sa.Column('metrics', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('reason', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('calibration_choice', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('calibration_verdict', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_model_selection_decisions')),
    )
    op.create_index(op.f('ix_model_selection_decisions_run_id'), 'model_selection_decisions', ['run_id'], unique=False)
    op.create_index(op.f('ix_model_selection_decisions_mode'), 'model_selection_decisions', ['mode'], unique=False)
    op.create_index(op.f('ix_model_selection_decisions_market'), 'model_selection_decisions', ['market'], unique=False)
    op.create_index(op.f('ix_model_selection_decisions_as_of'), 'model_selection_decisions', ['as_of'], unique=False)
    op.create_index(op.f('ix_model_selection_decisions_status'), 'model_selection_decisions', ['status'], unique=False)
    op.create_index(op.f('ix_model_selection_decisions_selected_model_type'), 'model_selection_decisions',
                     ['selected_model_type'], unique=False)
    op.create_index(op.f('ix_model_selection_decisions_created_at'), 'model_selection_decisions', ['created_at'], unique=False)

    op.create_table(
        'shadow_selection_predictions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('selection_decision_id', sa.Integer(), nullable=False),
        sa.Column('league', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('match_date', sa.Date(), nullable=False),
        sa.Column('home_team', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('away_team', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('market', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('candidate_model_type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('calibration_applied', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('candidate_probs', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('production_model_type', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('production_model_version_id', sa.Integer(), nullable=True),
        sa.Column('production_probs', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('result_home_goals', sa.Integer(), nullable=True),
        sa.Column('result_away_goals', sa.Integer(), nullable=True),
        sa.Column('candidate_correct', sa.Boolean(), nullable=True),
        sa.Column('production_correct', sa.Boolean(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['selection_decision_id'], ['model_selection_decisions.id'],
                                 name=op.f('fk_shadow_selection_predictions_selection_decision_id_model_selection_decisions')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_shadow_selection_predictions')),
        sa.UniqueConstraint('league', 'match_date', 'home_team', 'away_team', 'market',
                             name='uq_shadow_selection_predictions_match_market'),
    )
    op.create_index(op.f('ix_shadow_selection_predictions_selection_decision_id'), 'shadow_selection_predictions',
                     ['selection_decision_id'], unique=False)
    op.create_index(op.f('ix_shadow_selection_predictions_league'), 'shadow_selection_predictions', ['league'], unique=False)
    op.create_index(op.f('ix_shadow_selection_predictions_match_date'), 'shadow_selection_predictions', ['match_date'], unique=False)
    op.create_index(op.f('ix_shadow_selection_predictions_market'), 'shadow_selection_predictions', ['market'], unique=False)
    op.create_index(op.f('ix_shadow_selection_predictions_candidate_model_type'), 'shadow_selection_predictions',
                     ['candidate_model_type'], unique=False)
    op.create_index(op.f('ix_shadow_selection_predictions_status'), 'shadow_selection_predictions', ['status'], unique=False)
    op.create_index(op.f('ix_shadow_selection_predictions_created_at'), 'shadow_selection_predictions', ['created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_shadow_selection_predictions_created_at'), table_name='shadow_selection_predictions')
    op.drop_index(op.f('ix_shadow_selection_predictions_status'), table_name='shadow_selection_predictions')
    op.drop_index(op.f('ix_shadow_selection_predictions_candidate_model_type'), table_name='shadow_selection_predictions')
    op.drop_index(op.f('ix_shadow_selection_predictions_market'), table_name='shadow_selection_predictions')
    op.drop_index(op.f('ix_shadow_selection_predictions_match_date'), table_name='shadow_selection_predictions')
    op.drop_index(op.f('ix_shadow_selection_predictions_league'), table_name='shadow_selection_predictions')
    op.drop_index(op.f('ix_shadow_selection_predictions_selection_decision_id'), table_name='shadow_selection_predictions')
    op.drop_table('shadow_selection_predictions')

    op.drop_index(op.f('ix_model_selection_decisions_created_at'), table_name='model_selection_decisions')
    op.drop_index(op.f('ix_model_selection_decisions_selected_model_type'), table_name='model_selection_decisions')
    op.drop_index(op.f('ix_model_selection_decisions_status'), table_name='model_selection_decisions')
    op.drop_index(op.f('ix_model_selection_decisions_as_of'), table_name='model_selection_decisions')
    op.drop_index(op.f('ix_model_selection_decisions_market'), table_name='model_selection_decisions')
    op.drop_index(op.f('ix_model_selection_decisions_mode'), table_name='model_selection_decisions')
    op.drop_index(op.f('ix_model_selection_decisions_run_id'), table_name='model_selection_decisions')
    op.drop_table('model_selection_decisions')
