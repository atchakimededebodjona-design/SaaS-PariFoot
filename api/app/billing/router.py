"""
Endpoints de facturation Stripe.

Flux complet :
  1. Utilisateur connecté -> POST /billing/checkout {"plan": "monthly"}
     -> redirection vers Stripe Checkout (hébergé, aucune donnée bancaire
     ne transite par notre backend)
  2. Paiement réussi -> Stripe envoie un webhook checkout.session.completed
     -> on crée/active l'abonnement en base
  3. Chaque mois, Stripe envoie customer.subscription.updated (renouvellement,
     changement de statut) et customer.subscription.deleted (annulation)
     -> on garde notre table Subscription synchronisée
  4. GET /billing/subscription -> l'utilisateur voit son statut actuel
  5. POST /billing/portal -> lien vers le Portail Client Stripe (gérer/annuler
     l'abonnement, mettre à jour la carte — encore une fois, hébergé par
     Stripe, zéro donnée bancaire chez nous)

Principe de sécurité central : la SEULE source de vérité sur l'état réel
d'un abonnement, ce sont les webhooks Stripe (signature vérifiée). On ne
fait JAMAIS confiance à ce qu'un client dirait de son propre statut
d'abonnement.
"""

from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlmodel import Session, select
from pydantic import BaseModel

from app.core.database import get_session
from app.core.stripe_config import STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, PRICE_IDS, FRONTEND_URL
from app.auth.security import get_current_user
from app.models.user import User
from app.models.subscription import Subscription

stripe.api_key = STRIPE_SECRET_KEY

router = APIRouter(prefix="/billing", tags=["billing"])


class CheckoutRequest(BaseModel):
    plan: str  # "monthly" ou "yearly"


class CheckoutResponse(BaseModel):
    checkout_url: str


class PortalResponse(BaseModel):
    portal_url: str


class SubscriptionStatus(BaseModel):
    status: str
    plan: str | None
    is_active: bool
    current_period_end: datetime | None


def _get_or_create_subscription(session: Session, user: User) -> Subscription:
    """Récupère l'enregistrement Subscription de l'utilisateur, ou en crée
    un vide (status='none', pas encore de customer Stripe) s'il n'existe
    pas encore — chaque utilisateur en a exactement un, créé au premier
    besoin plutôt qu'à l'inscription (évite de créer un customer Stripe
    pour des comptes qui ne paieront peut-être jamais)."""
    sub = session.exec(select(Subscription).where(Subscription.user_id == user.id)).first()
    if sub is None:
        customer = stripe.Customer.create(email=user.email, metadata={"user_id": str(user.id)})
        sub = Subscription(user_id=user.id, stripe_customer_id=customer.id, status="none")
        session.add(sub)
        session.commit()
        session.refresh(sub)
    return sub


@router.post("/checkout", response_model=CheckoutResponse)
def create_checkout_session(
    body: CheckoutRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if body.plan not in PRICE_IDS or not PRICE_IDS[body.plan]:
        raise HTTPException(status_code=400, detail=f"Plan inconnu ou non configuré : '{body.plan}'")

    sub = _get_or_create_subscription(session, current_user)

    checkout_session = stripe.checkout.Session.create(
        customer=sub.stripe_customer_id,
        mode="subscription",
        line_items=[{"price": PRICE_IDS[body.plan], "quantity": 1}],
        success_url=f"{FRONTEND_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{FRONTEND_URL}/billing/cancel",
        metadata={"user_id": str(current_user.id), "plan": body.plan},
    )
    return CheckoutResponse(checkout_url=checkout_session.url)


@router.post("/portal", response_model=PortalResponse)
def create_portal_session(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    sub = _get_or_create_subscription(session, current_user)
    portal_session = stripe.billing_portal.Session.create(
        customer=sub.stripe_customer_id,
        return_url=f"{FRONTEND_URL}/account",
    )
    return PortalResponse(portal_url=portal_session.url)


@router.get("/subscription", response_model=SubscriptionStatus)
def get_subscription_status(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    sub = session.exec(select(Subscription).where(Subscription.user_id == current_user.id)).first()
    if sub is None:
        return SubscriptionStatus(status="none", plan=None, is_active=False, current_period_end=None)
    return SubscriptionStatus(
        status=sub.status, plan=sub.plan, is_active=sub.is_active,
        current_period_end=sub.current_period_end,
    )


# ---------------------------------------------------------------------------
# Webhook — SEULE source de vérité pour l'état réel d'un abonnement
# ---------------------------------------------------------------------------

@router.post("/webhook", status_code=status.HTTP_200_OK)
async def stripe_webhook(request: Request, session: Session = Depends(get_session)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        # Signature invalide ou payload corrompu -> on rejette, ne JAMAIS
        # traiter un événement dont on n'a pas pu vérifier l'authenticité
        # (sinon n'importe qui pourrait POST un faux "abonnement activé").
        raise HTTPException(status_code=400, detail="Signature webhook invalide")

    event_type = event["type"]
    # Conversion explicite en dict Python natif : selon la version du SDK
    # Stripe, l'objet StripeObject imbriqué ne supporte pas toujours .get()
    # de façon fiable sur ses champs — to_dict() élimine cette ambiguïté
    # une fois pour toutes plutôt que de contourner au cas par cas.
    data = event["data"]["object"].to_dict()

    if event_type == "checkout.session.completed":
        _handle_checkout_completed(session, data)
    elif event_type in ("customer.subscription.updated", "customer.subscription.created"):
        _handle_subscription_updated(session, data)
    elif event_type == "customer.subscription.deleted":
        _handle_subscription_deleted(session, data)
    # Les autres types d'événements (invoice.payment_failed, etc.) peuvent
    # être ajoutés au besoin — ignorés silencieusement pour l'instant
    # plutôt que de lever une erreur (Stripe réessaie sinon indéfiniment).

    return {"received": True}


def _handle_checkout_completed(session: Session, data: dict):
    user_id = int(data["metadata"]["user_id"])
    plan = data["metadata"].get("plan")
    stripe_subscription_id = data.get("subscription")

    sub = session.exec(select(Subscription).where(Subscription.user_id == user_id)).first()
    if sub is None:
        return  # ne devrait pas arriver (le customer est créé avant le checkout) — sécurité passive
    sub.stripe_subscription_id = stripe_subscription_id
    sub.plan = plan
    sub.status = "active"
    sub.updated_at = datetime.now(timezone.utc)
    session.add(sub)
    session.commit()


def _handle_subscription_updated(session: Session, data: dict):
    stripe_subscription_id = data["id"]
    sub = session.exec(
        select(Subscription).where(Subscription.stripe_subscription_id == stripe_subscription_id)
    ).first()
    if sub is None:
        return
    sub.status = data["status"]
    period_end = data.get("current_period_end")
    if period_end:
        sub.current_period_end = datetime.fromtimestamp(period_end, tz=timezone.utc)
    sub.updated_at = datetime.now(timezone.utc)
    session.add(sub)
    session.commit()


def _handle_subscription_deleted(session: Session, data: dict):
    stripe_subscription_id = data["id"]
    sub = session.exec(
        select(Subscription).where(Subscription.stripe_subscription_id == stripe_subscription_id)
    ).first()
    if sub is None:
        return
    sub.status = "canceled"
    sub.updated_at = datetime.now(timezone.utc)
    session.add(sub)
    session.commit()
