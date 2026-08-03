"""
Modèle d'abonnement.

Ne stocke JAMAIS de données de carte bancaire (Stripe Checkout/Portal
héberge tout ça — notre backend ne voit que des identifiants Stripe et un
statut). C'est délibéré : ça évite toute obligation de conformité PCI-DSS
côté produit, Stripe s'en charge entièrement.
"""

from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field


class Subscription(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", unique=True, index=True)

    stripe_customer_id: str = Field(index=True)
    stripe_subscription_id: Optional[str] = Field(default=None, index=True)

    # Statuts Stripe standards : active, past_due, canceled, incomplete,
    # incomplete_expired, trialing, unpaid. "none" = jamais souscrit.
    status: str = Field(default="none")

    plan: Optional[str] = None  # ex. "monthly", "yearly" — libre, dépend des Price IDs Stripe créés
    current_period_end: Optional[datetime] = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_active(self) -> bool:
        """Un abonnement est considéré actif s'il est 'active' ou 'trialing'
        ET que la période en cours n'est pas expirée (garde-fou en cas de
        webhook manqué — évite de continuer à servir un accès premium
        indéfiniment si Stripe n'a pas pu notifier l'expiration)."""
        if self.status not in ("active", "trialing"):
            return False
        if self.current_period_end is None:
            return True  # pas encore de date connue (ex. juste après création) — on fait confiance au statut
        return datetime.now(timezone.utc) < self.current_period_end.replace(tzinfo=timezone.utc)
