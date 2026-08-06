"""
Endpoints de facturation Chariow (licences, remplace l'intégration Stripe —
audience ouest-africaine, support Mobile Money natif).

Flux complet :
  1. Utilisateur connecté -> POST /billing/checkout
     {"plan": "monthly", "first_name": ..., "last_name": ..., "phone_number": ...,
      "phone_country_code": "+225"}
     -> on crée un lien de checkout Chariow (hébergé, aucune donnée de
     paiement ne transite par notre backend) et on le renvoie
  2. Paiement réussi -> Chariow envoie le Pulse successful.sale, puis en
     complément license.activated -> on active l'abonnement en base
  3. GET /billing/subscription -> l'utilisateur voit son statut actuel
  4. Le Pulse license.nearing_expiry informe du nombre de jours restants
     avant expiration (purement informatif, pour afficher un compte à
     rebours côté frontend)
  5. Les Pulses license.expired / license.revoked font repasser
     l'abonnement à un état non actif

IMPORTANT — PAS de renouvellement automatique : contrairement à Stripe,
les produits Licence Chariow ne sont PAS des abonnements récurrents
prélevés automatiquement (confirmé via la documentation/interface
Chariow — produits créés en "Paiement unique", prix fixe). Renouveler
= repasser par POST /billing/checkout avec le même plan, avant ou après
expiration ; c'est exactement le même endpoint que pour un premier achat,
aucune branche de code séparée n'est nécessaire.

Principe de sécurité central : la SEULE source de vérité sur l'état réel
d'une licence, ce sont les Pulses Chariow (signature HMAC vérifiée). On ne
fait JAMAIS confiance à ce qu'un client dirait de son propre statut.

Pas d'équivalent du Portail Client Stripe ici : Chariow n'expose pas ce
concept pour les produits Licence — gérer/renouveler se fait entièrement
via /billing/checkout.
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlmodel import Session, select
from pydantic import BaseModel

from app.core.database import get_session
from app.core.chariow_config import (
    CHARIOW_API_KEY,
    CHARIOW_API_BASE_URL,
    CHARIOW_PULSE_SECRET,
    PRODUCT_IDS,
)
from app.auth.security import get_current_user
from app.models.user import User
from app.models.subscription import Subscription, ProcessedPulseDelivery

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])


class CheckoutRequest(BaseModel):
    plan: str  # "monthly" ou "yearly"
    # Chariow a besoin de ces informations pour générer le lien de checkout
    # (contrairement à Stripe Checkout, qui ne demandait rien de plus que
    # le plan) — le frontend doit les collecter avant d'appeler cet endpoint.
    first_name: str
    last_name: str
    phone_number: str
    phone_country_code: str


class CheckoutResponse(BaseModel):
    checkout_url: str


class SubscriptionStatus(BaseModel):
    status: str
    plan: str | None
    is_active: bool
    current_period_end: datetime | None
    days_until_expiry: int | None


def _get_or_create_subscription(session: Session, user: User) -> Subscription:
    """Récupère l'enregistrement Subscription de l'utilisateur, ou en crée
    un vide (status='none') s'il n'existe pas encore. Contrairement à
    Stripe, rien à créer côté Chariow à l'avance : les informations client
    sont envoyées directement à chaque appel de checkout."""
    sub = session.exec(select(Subscription).where(Subscription.user_id == user.id)).first()
    if sub is None:
        sub = Subscription(user_id=user.id, status="none")
        session.add(sub)
        session.commit()
        session.refresh(sub)
    return sub


def _create_chariow_checkout_link(
    *,
    product_id: str,
    email: str,
    first_name: str,
    last_name: str,
    phone_number: str,
    phone_country_code: str,
    metadata: dict,
) -> str:
    """
    Isolé dans sa propre fonction pour rester facile à patcher dans les
    tests (aucun appel réseau réel n'y est jamais fait).

    Endpoint et structure de réponse confirmés via la doc officielle
    (chariow.dev/en/guides/checkout) :
      - POST {CHARIOW_API_BASE_URL}/checkout
      - response["data"]["step"] vaut :
          "payment"            -> cas normal, l'URL est dans
                                   response["data"]["payment"]["checkout_url"]
          "completed"          -> vente déjà finalisée ; ne devrait pas
                                   arriver avec nos produits à prix fixe
          "already_purchased"  -> ne devrait JAMAIS arriver pour un produit
                                   Licence (qui autorise toujours le rachat
                                   selon la doc) ; loggé en warning si ça
                                   arrive quand même
    """
    response = httpx.post(
        f"{CHARIOW_API_BASE_URL}/checkout",
        headers={"Authorization": f"Bearer {CHARIOW_API_KEY}"},
        json={
            "product_id": product_id,
            "customer": {
                "email": email,
                "first_name": first_name,
                "last_name": last_name,
                "phone_number": phone_number,
                "phone_country_code": phone_country_code,
            },
            "metadata": metadata,
        },
        timeout=10.0,
    )

    if response.status_code in (401, 404, 422):
        body = response.json()
        message = body.get("message", f"Erreur Chariow ({response.status_code})")
        if response.status_code == 422 and body.get("errors"):
            message = f"{message} : {body['errors']}"
        # 422 = données client invalides (probablement corrigeable côté
        # frontend) -> 400 ; 401/404 = mauvaise config de notre côté (clé ou
        # Product ID) -> 502, ce n'est pas la faute de l'appelant.
        status_code = 400 if response.status_code == 422 else 502
        raise HTTPException(status_code=status_code, detail=f"Chariow: {message}")

    response.raise_for_status()
    data = response.json()["data"]
    step = data.get("step")

    if step == "already_purchased":
        logger.warning(
            "Chariow a renvoyé step='already_purchased' pour le produit %s "
            "(metadata=%s) — inattendu pour un produit Licence, qui autorise "
            "toujours le rachat selon la doc Chariow.",
            product_id, metadata,
        )
    elif step not in ("payment", "completed"):
        raise HTTPException(status_code=502, detail=f"Chariow: étape de checkout inconnue '{step}'")

    return data["payment"]["checkout_url"]


@router.post("/checkout", response_model=CheckoutResponse)
def create_checkout_session(
    body: CheckoutRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if body.plan not in PRODUCT_IDS or not PRODUCT_IDS[body.plan]:
        raise HTTPException(status_code=400, detail=f"Plan inconnu ou non configuré : '{body.plan}'")

    _get_or_create_subscription(session, current_user)

    checkout_url = _create_chariow_checkout_link(
        product_id=PRODUCT_IDS[body.plan],
        email=current_user.email,
        first_name=body.first_name,
        last_name=body.last_name,
        phone_number=body.phone_number,
        phone_country_code=body.phone_country_code,
        metadata={"user_id": str(current_user.id), "plan": body.plan},
    )
    return CheckoutResponse(checkout_url=checkout_url)


@router.get("/subscription", response_model=SubscriptionStatus)
def get_subscription_status(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    sub = session.exec(select(Subscription).where(Subscription.user_id == current_user.id)).first()
    if sub is None:
        return SubscriptionStatus(
            status="none", plan=None, is_active=False,
            current_period_end=None, days_until_expiry=None,
        )
    return SubscriptionStatus(
        status=sub.status, plan=sub.plan, is_active=sub.is_active,
        current_period_end=sub.current_period_end, days_until_expiry=sub.days_until_expiry,
    )


# ---------------------------------------------------------------------------
# Pulse (webhook Chariow) — SEULE source de vérité sur l'état réel d'une licence
# ---------------------------------------------------------------------------

def _verify_pulse_signature(payload: bytes, signature_header: str | None) -> bool:
    if not signature_header:
        return False
    expected = hmac.new(CHARIOW_PULSE_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _already_processed(session: Session, delivery_id: str) -> bool:
    return session.exec(
        select(ProcessedPulseDelivery).where(ProcessedPulseDelivery.pulse_delivery_id == delivery_id)
    ).first() is not None


def _mark_processed(session: Session, delivery_id: str, event_type: str) -> None:
    session.add(ProcessedPulseDelivery(pulse_delivery_id=delivery_id, event_type=event_type))
    session.commit()


def _parse_datetime(value) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _find_by_license_key(session: Session, license_key: str | None) -> Subscription | None:
    if not license_key:
        return None
    return session.exec(
        select(Subscription).where(Subscription.chariow_license_key == license_key)
    ).first()


@router.post("/pulse", status_code=status.HTTP_200_OK)
async def chariow_pulse(request: Request, session: Session = Depends(get_session)):
    payload = await request.body()
    signature = request.headers.get("x-pulse-signature")

    if not _verify_pulse_signature(payload, signature):
        # Signature invalide ou absente -> on rejette, ne JAMAIS traiter un
        # événement dont on n'a pas pu vérifier l'authenticité (sinon
        # n'importe qui pourrait POST un faux "licence activée").
        raise HTTPException(status_code=400, detail="Signature Pulse invalide")

    delivery_id = request.headers.get("x-pulse-delivery-id")
    if delivery_id and _already_processed(session, delivery_id):
        # Chariow peut renvoyer la même delivery plusieurs fois (retry
        # réseau) — on accuse réception sans retraiter.
        return {"received": True, "duplicate": True}

    event = json.loads(payload)
    event_type = event.get("event")
    data = event.get("data", {})

    if event_type == "successful.sale":
        _handle_successful_sale(session, data)
    elif event_type == "license.activated":
        _handle_license_activated(session, data)
    elif event_type == "license.expired":
        _handle_license_status(session, data, new_status="expired")
    elif event_type == "license.revoked":
        _handle_license_status(session, data, new_status="revoked")
    elif event_type == "license.nearing_expiry":
        _handle_license_nearing_expiry(session, data)
    # Les autres types d'événements peuvent être ajoutés au besoin — ignorés
    # silencieusement pour l'instant plutôt que de lever une erreur
    # (Chariow réessaie sinon indéfiniment).

    if delivery_id:
        _mark_processed(session, delivery_id, event_type)

    return {"received": True}


def _handle_successful_sale(session: Session, data: dict):
    metadata = data.get("metadata") or {}
    user_id = metadata.get("user_id")
    if user_id is None:
        return
    user_id = int(user_id)

    sub = session.exec(select(Subscription).where(Subscription.user_id == user_id)).first()
    if sub is None:
        return  # ne devrait pas arriver (/billing/checkout crée toujours l'enregistrement avant la vente)

    sub.chariow_license_key = data.get("license_key")
    sub.plan = metadata.get("plan")
    sub.status = "active"
    sub.current_period_end = _parse_datetime(data.get("expires_at"))
    # Nouvel achat/renouvellement : le compte à rebours précédent n'a plus cours.
    sub.days_until_expiry = None
    sub.updated_at = datetime.now(timezone.utc)
    session.add(sub)
    session.commit()


def _handle_license_activated(session: Session, data: dict):
    """Complément de successful.sale : confirme l'activation de la licence,
    éventuellement avec une date d'expiration plus à jour."""
    sub = _find_by_license_key(session, data.get("license_key"))
    if sub is None:
        return
    sub.status = "active"
    expires_at = _parse_datetime(data.get("expires_at"))
    if expires_at is not None:
        sub.current_period_end = expires_at
    sub.updated_at = datetime.now(timezone.utc)
    session.add(sub)
    session.commit()


def _handle_license_status(session: Session, data: dict, *, new_status: str):
    sub = _find_by_license_key(session, data.get("license_key"))
    if sub is None:
        return
    sub.status = new_status
    sub.updated_at = datetime.now(timezone.utc)
    session.add(sub)
    session.commit()


def _handle_license_nearing_expiry(session: Session, data: dict):
    sub = _find_by_license_key(session, data.get("license_key"))
    if sub is None:
        return
    sub.days_until_expiry = data.get("days_until_expiry")
    sub.updated_at = datetime.now(timezone.utc)
    session.add(sub)
    session.commit()
