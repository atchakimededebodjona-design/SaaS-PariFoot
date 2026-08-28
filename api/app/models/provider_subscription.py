"""
Modèle d'abonnement générique par provider de paiement (Phase 1 : Chariow
uniquement — voir docstring de app/billing/entitlement_service.py pour le
principe général de l'architecture Provider/Entitlement).

Généralise l'ancien modèle unique `Subscription` (app/models/subscription.py,
conservé en lecture, plus jamais écrit après la bascule — voir
app/billing/backfill.py) pour permettre à un même utilisateur d'avoir
plusieurs abonnements actifs simultanément, un par provider (Chariow web
aujourd'hui, Google Play plus tard) — impossible avec l'ancienne contrainte
`Subscription.user_id UNIQUE`.

Une seule ligne "courante" par (user_id, provider), mise à jour en place à
chaque événement (même discipline que l'ancienne table `Subscription`)
plutôt qu'un historique append-only ligne par ligne — l'historique des
changements de statut vit dans EntitlementEvent (app/models/entitlement.py),
pas ici.
"""

from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import UniqueConstraint
from sqlmodel import SQLModel, Field


class ProviderSubscription(SQLModel, table=True):
    __tablename__ = "provider_subscription"
    __table_args__ = (
        # Empêche qu'une même référence externe (licence Chariow, futur
        # purchaseToken Google) ne soit rattachée à deux lignes différentes.
        UniqueConstraint("provider", "external_ref", name="uq_provider_subscription_provider_external_ref"),
        # Une seule ligne "courante" par utilisateur et par provider — un
        # renouvellement/changement de plan met à jour cette même ligne,
        # il ne la duplique jamais.
        UniqueConstraint("user_id", "provider", name="uq_provider_subscription_user_provider"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)

    # "chariow" pour l'instant — "google_play" ajouté dans une phase ultérieure.
    provider: str = Field(index=True)

    # Référence externe : chariow_license_key pour Chariow, purchaseToken
    # pour Google Play (phase future). Nullable : comme l'ancien
    # Subscription.chariow_license_key, connue seulement après le premier
    # Pulse license.activated, pas dès le premier successful.sale.
    external_ref: Optional[str] = Field(default=None, index=True)

    product_id: Optional[str] = None
    plan: Optional[str] = None  # "monthly" ou "yearly"

    # Vocabulaire inchangé par rapport à l'ancien Subscription.status pour
    # cette phase : "none" | "active" | "expired" | "revoked".
    status: str = Field(default="none")

    current_period_end: Optional[datetime] = None

    # Reporté tel quel depuis l'ancien Subscription.days_until_expiry (Pulse
    # license.nearing_expiry) — champ non listé dans l'architecture cible
    # d'origine (pensée pour Google Play, où ce compte à rebours n'existe
    # pas sous cette forme) mais nécessaire ici pour que GET /billing/
    # subscription conserve exactement son contrat actuel. Purement
    # informatif, n'affecte jamais `status`/is_effectively_active.
    days_until_expiry: Optional[int] = None

    # false pour Chariow (jamais de renouvellement automatique prélevé —
    # voir app/core/chariow_config.py) ; reflètera l'API Google Play en
    # phase future.
    auto_renewing: bool = Field(default=False)

    canceled_at: Optional[datetime] = None
    revoked_or_refunded_at: Optional[datetime] = None

    # Statut brut tel que reçu du provider, pour audit/debug uniquement —
    # jamais utilisé par la logique métier (qui se base sur `status`, notre
    # propre vocabulaire normalisé). Pour Chariow, identique à `status`
    # aujourd'hui (pas de traduction nécessaire), mais un provider futur
    # peut avoir un vocabulaire plus riche que le nôtre.
    raw_status: Optional[str] = None

    # Dernier payload webhook brut reçu, sérialisé en JSON texte (même
    # convention que ModelArtifact.payload) — pour audit/rejouabilité en cas
    # de litige. Ne doit jamais contenir de donnée bancaire : Chariow comme
    # Google Play n'exposent jamais ce type de donnée dans leurs webhooks/API.
    raw_payload: Optional[str] = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_effectively_active(self) -> bool:
        """Identique à l'ancienne Subscription.is_active — même comportement
        exact pour Chariow (aucun changement fonctionnel voulu en Phase 1)."""
        if self.status != "active":
            return False
        if self.current_period_end is None:
            return True
        return datetime.now(timezone.utc) < self.current_period_end.replace(tzinfo=timezone.utc)
