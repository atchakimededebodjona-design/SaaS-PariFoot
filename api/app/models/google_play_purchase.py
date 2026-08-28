"""
Détails Google Play Billing — délibérément séparés de ProviderSubscription
(voir app/models/provider_subscription.py) : ces champs n'ont de sens que
pour ce provider, les y ajouter directement aurait reproduit l'anti-pattern
"une colonne par provider" déjà écarté à la conception (voir document
d'architecture "Coexistence Chariow + Google Play").

Cardinalité (analysée avant implémentation, voir échanges de conception) :
ProviderSubscription(user_id, provider='google_play') reste EXACTEMENT une
ligne par utilisateur (contrainte UNIQUE(user_id, provider) inchangée,
partagée avec Chariow) — elle représente "l'achat Google actuellement
pertinent" pour cet utilisateur. GooglePlayPurchase, elle, est un historique
APPEND-ONLY : plusieurs lignes (un purchaseToken différent à chaque
réabonnement/changement de plan sans linkedPurchaseToken, ou une mise à jour
en place du même purchaseToken lors d'un simple renouvellement) peuvent
pointer vers le même ProviderSubscription.id via une FK NON unique. C'est
`purchase_token`, UNIQUE ici (globalement, tous utilisateurs confondus), qui
porte la garantie anti-duplication — jamais la relation vers
ProviderSubscription.

subscription_state porte la valeur BRUTE de l'API Google
(SUBSCRIPTION_STATE_ACTIVE, _CANCELED, _IN_GRACE_PERIOD, _ON_HOLD, _PAUSED,
_EXPIRED, _PENDING, _PENDING_PURCHASE_CANCELED — confirmé contre la
documentation officielle purchases.subscriptionsv2). Cette valeur ne permet
PAS à elle seule de distinguer un abonnement révoqué/remboursé d'un
abonnement simplement expiré (Google ne renvoie aucune valeur "REVOKED" —
confirmé contre la doc officielle) : c'est le type de notification RTDN
SUBSCRIPTION_REVOKED, traité par app/billing/google_play_service.py, qui
seul permet de journaliser ce cas distinctement côté ProviderSubscription.
"""

from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field


class GooglePlayPurchase(SQLModel, table=True):
    __tablename__ = "google_play_purchase"

    id: Optional[int] = Field(default=None, primary_key=True)

    provider_subscription_id: int = Field(foreign_key="provider_subscription.id", index=True)
    # Dénormalisé délibérément : la vérification anti-fraude ("ce token
    # appartient-il à un autre compte ?") doit rester une requête à une
    # seule colonne, sans jointure via provider_subscription_id.
    user_id: int = Field(foreign_key="user.id", index=True)

    purchase_token: str = Field(unique=True, index=True)
    product_id: str
    base_plan_id: Optional[str] = None
    order_id: Optional[str] = None  # latestOrderId — change à chaque renouvellement, token inchangé
    package_name: str

    subscription_state: str  # valeur brute Google, voir docstring module
    acknowledgement_state: str = Field(default="ACKNOWLEDGEMENT_STATE_PENDING")

    expiry_time: datetime
    auto_renewing: bool = Field(default=False)

    # Chaînage réabonnement/changement de plan (linkedPurchaseToken Google) —
    # permet de rattacher un nouveau token au même "emplacement"
    # ProviderSubscription que l'ancien plutôt que de le traiter comme un
    # achat totalement indépendant.
    linked_purchase_token: Optional[str] = None

    canceled_at: Optional[datetime] = None
    revoked_or_refunded_at: Optional[datetime] = None

    # Dernier type de notification RTDN observé (libellé, ex.
    # "SUBSCRIPTION_RENEWED") — purement informatif/audit.
    latest_notification_type: Optional[str] = None

    # Dernière réponse purchases.subscriptionsv2.get, sérialisée en JSON
    # texte, "subscribeWithGoogleInfo" retiré avant stockage (peut contenir
    # email/nom si l'achat a utilisé "Subscribe with Google" — même
    # discipline de minimisation que _strip_pii côté Chariow). Ne contient
    # jamais de donnée bancaire (Google ne l'expose pas dans cette réponse).
    raw_response: Optional[str] = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ProcessedGoogleNotification(SQLModel, table=True):
    """Déduplication des messages RTDN (Pub/Sub) — même principe que
    ProcessedPulseDelivery côté Chariow : chaque messageId Pub/Sub n'est
    traité qu'une seule fois (livraison "at least once", jamais garantie
    unique par Pub/Sub lui-même — confirmé via la doc officielle)."""

    __tablename__ = "processed_google_notification"

    id: Optional[int] = Field(default=None, primary_key=True)
    pubsub_message_id: str = Field(unique=True, index=True)
    notification_type: Optional[str] = None
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
