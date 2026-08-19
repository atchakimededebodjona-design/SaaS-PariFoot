"""
Historique append-only des décisions de promotion (Phase 10) — chaque appel à
GET/POST /models/promotion/evaluate ou /models/promotion/promote (manuel) et
chaque passage de scripts/evaluate_live_models.py (automatique) écrit
EXACTEMENT une ligne ici, y compris pour un rejet — §9/§18/règle 11 du ticket
Phase 10 : "aucune promotion ne doit être silencieuse", "toute décision de
promotion doit être traçable". Aucune ligne n'est jamais mise à jour ni
supprimée après écriture.

`model_version_id`/`previous_model_version_id` ne sont PAS des FK strictes
(nullable, pas de contrainte de suppression en cascade) : une ModelVersion
n'est jamais supprimée dans ce dépôt (voir team_rating.py), donc l'intégrité
référentielle est de fait toujours respectée, mais l'historique doit rester
lisible même dans un scénario où l'un des deux id référencerait une ligne
disparue (ex. reprise sur une base restaurée partiellement).
"""

from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field


class ModelPromotionEvent(SQLModel, table=True):
    __tablename__ = "model_promotion_events"

    id: Optional[int] = Field(default=None, primary_key=True)

    model_version_id: int = Field(foreign_key="model_versions.id", index=True)
    previous_model_version_id: Optional[int] = Field(default=None, foreign_key="model_versions.id")
    model_type: str = Field(index=True)
    market: str

    # "eligible" | "promoted" | "already_active" | "insufficient_data" |
    # "rejected" | "no_clear_gain" | "no_candidate" — mêmes valeurs que
    # LivePromotionDecision.status (voir app/ai/arena/promotion.py), plus
    # "promoted" qui n'existe QUE dans cet historique (appliqué avec succès,
    # distinct de "eligible" qui ne fait qu'évaluer).
    decision: str = Field(index=True)
    reason: str

    # JSON texte {"candidate": {...}, "baseline": {...}} — snapshot des
    # métriques LIVE au moment de la décision (même convention texte libre
    # non reparsé que ModelVersion.metrics/config).
    metrics: Optional[str] = None
    sample_size: Optional[int] = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)
    # Email de l'admin qui a déclenché l'action, ou "system/cron" pour
    # scripts/evaluate_live_models.py — jamais None (§9 : rien de silencieux).
    actor: str
    automatic: bool = Field(default=False)
