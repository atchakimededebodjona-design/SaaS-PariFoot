"""Phase 7 — Shadow Evaluation & Track Record V1 : ajoute
shadow_selection_predictions.candidate_probs_raw (probabilité AVANT
calibration, nécessaire car candidate_probs stocke la version FINALE après
calibration éventuelle — voir api/app/models/shadow_selection_prediction.py
et api/app/ai/arena/track_record.py). Purement additif (une seule colonne
nullable) : aucune table existante n'est modifiée ou supprimée, aucune
donnée n'est réécrite.

Revision ID: c4f8a1d75e93
Revises: a7c3e6f19b2d
Create Date: 2026-08-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'c4f8a1d75e93'
down_revision: Union[str, Sequence[str], None] = 'a7c3e6f19b2d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('shadow_selection_predictions',
                   sa.Column('candidate_probs_raw', sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('shadow_selection_predictions', 'candidate_probs_raw')
