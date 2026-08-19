"""ajoute le versioning, le cycle de vie et le mode shadow pour la Phase 9

Revision ID: b4d1e7f92a6c
Revises: 3f7b9d0c1a2e
Create Date: 2026-08-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'b4d1e7f92a6c'
down_revision: Union[str, Sequence[str], None] = '3f7b9d0c1a2e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('model_versions', sa.Column(
        'status', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default='active'))
    op.create_index(op.f('ix_model_versions_status'), 'model_versions', ['status'], unique=False)
    op.add_column('model_versions', sa.Column('activated_at', sa.DateTime(), nullable=True))
    op.add_column('model_versions', sa.Column('deactivated_at', sa.DateTime(), nullable=True))
    op.add_column('model_versions', sa.Column('training_period_start', sa.Date(), nullable=True))
    op.add_column('model_versions', sa.Column('training_period_end', sa.Date(), nullable=True))
    op.add_column('model_versions', sa.Column('validation_period_start', sa.Date(), nullable=True))
    op.add_column('model_versions', sa.Column('validation_period_end', sa.Date(), nullable=True))
    op.add_column('model_versions', sa.Column('test_period_start', sa.Date(), nullable=True))
    op.add_column('model_versions', sa.Column('test_period_end', sa.Date(), nullable=True))
    op.add_column('model_versions', sa.Column('sample_size', sa.Integer(), nullable=True))
    op.add_column('model_versions', sa.Column('metrics', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('model_versions', sa.Column('feature_version', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('model_versions', sa.Column('baseline_version_id', sa.Integer(), nullable=True))
    # batch_alter_table : SQLite ne supporte pas ALTER TABLE ADD CONSTRAINT
    # (nécessite une reconstruction de table, que le mode batch gère) — sur
    # Postgres (production), batch mode émet un ALTER TABLE classique, donc
    # ce bloc est sans effet de bord sur ce backend.
    with op.batch_alter_table('model_versions') as batch_op:
        batch_op.create_foreign_key(
            op.f('fk_model_versions_baseline_version_id_model_versions'),
            'model_versions', ['baseline_version_id'], ['id'],
        )

    op.add_column('model_predictions', sa.Column(
        'role', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default='active'))
    op.create_index(op.f('ix_model_predictions_role'), 'model_predictions', ['role'], unique=False)

    # Backfill : les versions déjà désactivées (is_active=False) au moment de
    # cette migration deviennent "retired" plutôt que de garder le défaut
    # "active" du nouveau champ `status` — celui-ci ne veut dire "active" que
    # pour une ligne qui n'a jamais été explicitement gérée par le code
    # Phase 9. Toute ligne is_active=True reste "active" (défaut déjà correct).
    model_versions = sa.table(
        'model_versions', sa.column('is_active', sa.Boolean()), sa.column('status', sa.String()),
    )
    op.execute(
        model_versions.update().where(model_versions.c.is_active.is_(False)).values(status='retired')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_model_predictions_role'), table_name='model_predictions')
    op.drop_column('model_predictions', 'role')

    with op.batch_alter_table('model_versions') as batch_op:
        batch_op.drop_constraint(op.f('fk_model_versions_baseline_version_id_model_versions'),
                                  type_='foreignkey')
    op.drop_column('model_versions', 'baseline_version_id')
    op.drop_column('model_versions', 'feature_version')
    op.drop_column('model_versions', 'metrics')
    op.drop_column('model_versions', 'sample_size')
    op.drop_column('model_versions', 'test_period_end')
    op.drop_column('model_versions', 'test_period_start')
    op.drop_column('model_versions', 'validation_period_end')
    op.drop_column('model_versions', 'validation_period_start')
    op.drop_column('model_versions', 'training_period_end')
    op.drop_column('model_versions', 'training_period_start')
    op.drop_column('model_versions', 'deactivated_at')
    op.drop_column('model_versions', 'activated_at')
    op.drop_index(op.f('ix_model_versions_status'), table_name='model_versions')
    op.drop_column('model_versions', 'status')
