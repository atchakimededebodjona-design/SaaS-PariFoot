"""
Modèle d'abonnement (licence Chariow).

Ne stocke JAMAIS de donnée de paiement (numéro Mobile Money, carte...) —
Chariow héberge tout le flux de paiement, notre backend ne voit qu'une clé
de licence et un statut, reçus via les Pulses (webhooks Chariow) sur
POST /billing/pulse.

IMPORTANT — modèle de renouvellement : un produit Licence Chariow n'a PAS
de renouvellement automatique (pas d'abonnement récurrent prélevé côté
Chariow pour ce type de produit). L'utilisateur doit repasser par
POST /billing/checkout avec le même plan pour renouveler, avant ou après
expiration — voir app/billing/router.py et README.md, section Facturation.
"""

from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field


class Subscription(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", unique=True, index=True)

    # Clé de licence Chariow — connue seulement après le premier Pulse
    # successful.sale (rien à créer côté Chariow avant le checkout,
    # contrairement au Customer Stripe qu'il fallait pré-créer).
    chariow_license_key: Optional[str] = Field(default=None, index=True)

    # "none" (jamais acheté), "active", "expired", "revoked".
    status: str = Field(default="none")

    plan: Optional[str] = None  # "monthly" ou "yearly" — dépend des Product ID Chariow créés
    current_period_end: Optional[datetime] = None  # date d'expiration de la licence en cours

    # Dernière valeur reçue via le Pulse license.nearing_expiry — purement
    # informatif (compte à rebours affiché côté frontend), n'affecte PAS
    # is_active. Remis à None à chaque nouvel achat/renouvellement
    # (successful.sale), le compte à rebours précédent n'a alors plus cours.
    days_until_expiry: Optional[int] = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_active(self) -> bool:
        """Actif si le dernier statut connu est 'active' ET que la date
        d'expiration (si connue) n'est pas dépassée — garde-fou en cas de
        Pulse license.expired manqué."""
        if self.status != "active":
            return False
        if self.current_period_end is None:
            return True
        return datetime.now(timezone.utc) < self.current_period_end.replace(tzinfo=timezone.utc)


class ProcessedPulseDelivery(SQLModel, table=True):
    """Déduplication des Pulses Chariow : chaque delivery (identifiée par le
    header x-pulse-delivery-id) n'est traitée qu'une seule fois, même si
    Chariow la renvoie plusieurs fois (retries réseau après un 5xx/timeout
    par exemple)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    pulse_delivery_id: str = Field(unique=True, index=True)
    event_type: str
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
