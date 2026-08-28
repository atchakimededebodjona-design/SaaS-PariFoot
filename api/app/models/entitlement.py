"""
Entitlement Premium unifié — cache calculé, jamais écrit directement par un
handler de paiement (Chariow aujourd'hui, Google Play plus tard).

Seul app/billing/entitlement_service.py::recompute_entitlement() écrit dans
Entitlement/EntitlementEvent, toujours en réaction à un changement sur
ProviderSubscription (app/models/provider_subscription.py) — jamais sur la
seule foi d'un webhook non encore traduit en ProviderSubscription.

Phase 1 : une seule source possible ("chariow"), donc `premium` est
strictement équivalent à l'ancien Subscription.is_active. La table existe
dès maintenant pour que le code applicatif (require_active_subscription
notamment) lise déjà la bonne abstraction, sans dépendre du nombre de
providers réellement branchés.
"""

from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field


class Entitlement(SQLModel, table=True):
    __tablename__ = "entitlement"

    # Une seule ligne par utilisateur — pas d'id auto-incrémenté séparé,
    # user_id EST la clé primaire (cache 1-1, pas un historique).
    user_id: int = Field(foreign_key="user.id", primary_key=True)

    premium: bool = Field(default=False)

    # Date max des sources actives au moment du calcul — purement
    # informatif (affichage), jamais utilisé pour décider `premium`.
    premium_until: Optional[datetime] = None

    # Liste des providers actuellement actifs, sérialisée en JSON texte
    # (même convention que ModelArtifact.payload), ex. '["chariow"]'.
    active_sources: str = Field(default="[]")

    computed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EntitlementEvent(SQLModel, table=True):
    """Journal d'audit append-only — jamais de UPDATE/DELETE applicatif sur
    cette table. Une ligne par recalcul d'entitlement qui change réellement
    quelque chose (voir recompute_entitlement)."""

    __tablename__ = "entitlement_event"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    provider_subscription_id: Optional[int] = Field(default=None, foreign_key="provider_subscription.id")

    # "granted" | "renewed" | "canceled" | "expired" | "revoked" | "restored" | "merged"
    event_type: str

    previous_premium: bool
    new_premium: bool

    reason: Optional[str] = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
