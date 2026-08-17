"""
Endpoints de facturation Chariow (licences, remplace l'intégration Stripe —
audience ouest-africaine, support Mobile Money natif).

Flux complet :
  1. Utilisateur connecté -> POST /billing/checkout
     {"plan": "monthly", "first_name": ..., "last_name": ..., "phone_number": ...,
      "phone_country_code": "CI"}  # code pays ISO 3166-1 alpha-2, pas un indicatif
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
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlmodel import Session, select
from pydantic import BaseModel

from app.core.database import get_session
from app.core.rate_limit import limiter
from app.core.chariow_config import (
    CHARIOW_API_KEY,
    CHARIOW_API_BASE_URL,
    CHARIOW_PULSE_SECRET,
    PRODUCT_IDS,
    FRONTEND_URL,
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
    phone_country_code: str  # code pays ISO 3166-1 alpha-2 (ex. "CI"), PAS un indicatif ("+225")


class CheckoutResponse(BaseModel):
    checkout_url: str


class SubscriptionStatus(BaseModel):
    status: str
    plan: str | None
    is_active: bool
    current_period_end: datetime | None
    days_until_expiry: int | None


class ActivateLicenseRequest(BaseModel):
    license_key: str


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


def _require_chariow_api_key() -> None:
    """
    httpx refuse purement et simplement d'envoyer un en-tête
    "Authorization: Bearer " avec une clé vide (httpx.LocalProtocolError:
    Illegal header value) — sans cette garde, un CHARIOW_API_KEY vide/non
    configuré fait planter la requête avec une exception non gérée (500
    opaque, connexion coupée côté client) plutôt qu'une erreur exploitable.
    Découvert en testant _fetch_chariow_license en local sans avoir chargé
    api/.env dans l'environnement du process — un CHARIOW_API_KEY manquant
    reste possible en dehors de ce cas précis (mauvaise config), d'où une
    garde partagée plutôt qu'un correctif ponctuel.
    """
    if not CHARIOW_API_KEY:
        raise HTTPException(status_code=502, detail="Chariow: CHARIOW_API_KEY non configurée côté serveur.")


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

    Forme du payload confirmée empiriquement contre la vraie API (le premier
    essai avec un objet "customer" imbriqué a été rejeté en 422 listant les
    champs manquants) : email/first_name/last_name à la racine, téléphone en
    sous-objet "phone" avec "number"/"country_code" — pas de champ "customer".

    redirect_url (chariow.dev/en/guides/checkout) : sans ce champ, Chariow
    laisse le client sur SA page de post-achat par défaut au lieu de le
    ramener vers le frontend — c'est le bug rapporté ("après le paiement,
    la page reste sur Chariow"). FRONTEND_URL était déjà défini dans
    chariow_config.py mais jamais branché nulle part. Redirige vers
    billing.html, qui recharge déjà GET /billing/subscription au chargement
    (aucun code supplémentaire nécessaire pour afficher le nouveau statut).
    """
    _require_chariow_api_key()
    response = httpx.post(
        f"{CHARIOW_API_BASE_URL}/checkout",
        headers={"Authorization": f"Bearer {CHARIOW_API_KEY}"},
        json={
            "product_id": product_id,
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "phone": {
                "number": phone_number,
                "country_code": phone_country_code,
            },
            "custom_metadata": metadata,
            "redirect_url": f"{FRONTEND_URL}/billing.html",
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


def _fetch_chariow_license(license_key: str) -> dict:
    """
    Isolée dans sa propre fonction pour rester patchable dans les tests
    (même discipline que _create_chariow_checkout_link) — aucun appel
    réseau réel n'y est jamais fait en test.

    GET {CHARIOW_API_BASE_URL}/licenses/{license_key} (chariow.dev/api-
    reference/licenses/get-license) : renvoie status
    (active/expired/revoked/pending_activation), expires_at, product, et
    surtout metadata — le custom_metadata qu'on envoie déjà à POST
    /checkout ({"user_id": ..., "plan": ...}). Pas de champ email dans
    cette réponse, contrairement aux Pulses license.* — c'est le
    metadata.user_id qui sert à vérifier l'appartenance de la clé
    (voir activate_license ci-dessous), pas l'email.

    quote() : la clé de licence est fournie par l'utilisateur (collée
    depuis un email Chariow), jamais garanti sans caractères spéciaux au
    moment de la placer dans le chemin de l'URL.

    Gestion d'erreur alignée sur _create_chariow_checkout_link : 404 (clé
    inexistante) est une erreur normale côté appelant, remontée telle
    quelle ; tout autre code d'erreur (401 clé API invalide, 5xx Chariow,
    etc.) est une panne de configuration/service, jamais la faute de la
    clé collée par l'utilisateur -> 502 plutôt que de laisser
    raise_for_status() produire une 500 générique et opaque.
    """
    _require_chariow_api_key()
    response = httpx.get(
        f"{CHARIOW_API_BASE_URL}/licenses/{quote(license_key, safe='')}",
        headers={"Authorization": f"Bearer {CHARIOW_API_KEY}"},
        timeout=10.0,
    )
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Clé de licence introuvable.")
    if response.status_code >= 400:
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        message = body.get("message", f"Erreur Chariow ({response.status_code})")
        raise HTTPException(status_code=502, detail=f"Chariow: {message}")
    return response.json()["data"]


@router.post("/activate-license", response_model=SubscriptionStatus)
@limiter.limit("10/minute")
def activate_license(
    request: Request,
    body: ActivateLicenseRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """
    Filet de sécurité si la redirection post-paiement échoue/tarde et que
    le Pulse successful.sale/license.activated n'est pas encore arrivé (ou
    jamais, ex. email différent entre Chariow et le compte xfoot) :
    l'utilisateur colle la clé de licence reçue par email de Chariow.

    Vérification d'appartenance via metadata.user_id (fixé par NOUS à
    l'achat, voir create_checkout_session) plutôt que par email — jamais
    activer sur la seule confiance du texte collé par le client, sinon
    n'importe qui pourrait coller la clé de quelqu'un d'autre pour
    débloquer son propre compte.
    """
    license_data = _fetch_chariow_license(body.license_key)

    metadata = license_data.get("metadata") or {}
    license_user_id = metadata.get("user_id")
    if license_user_id is None or str(license_user_id) != str(current_user.id):
        raise HTTPException(
            status_code=403,
            detail="Cette clé de licence n'est pas associée à ton compte.",
        )

    license_status = license_data.get("status")
    if license_status != "active":
        messages = {
            "expired": "Cette licence a expiré — repasse par un nouvel achat pour la renouveler.",
            "revoked": "Cette licence a été révoquée.",
            "pending_activation": "Cette licence n'est pas encore activée côté Chariow — réessaie dans quelques instants.",
        }
        raise HTTPException(
            status_code=400,
            detail=messages.get(license_status, f"Licence non active (statut : {license_status})."),
        )

    sub = _get_or_create_subscription(session, current_user)
    sub.status = "active"
    sub.chariow_license_key = body.license_key
    sub.plan = metadata.get("plan")
    expires_at = _parse_datetime(license_data.get("expires_at"))
    if expires_at is not None:
        sub.current_period_end = expires_at
    sub.days_until_expiry = None  # confirmation d'achat, même convention que _handle_successful_sale
    sub.updated_at = datetime.now(timezone.utc)
    session.add(sub)
    session.commit()
    session.refresh(sub)

    return SubscriptionStatus(
        status=sub.status, plan=sub.plan, is_active=sub.is_active,
        current_period_end=sub.current_period_end, days_until_expiry=sub.days_until_expiry,
    )


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
    """
    Contrat confirmé via chariow.dev/en/guides/pulse-security : header
    x-chariow-signature, valeur "sha256=<hex hmac-sha256 du corps brut>".
    Seul le corps brut est signé (méthode HTTP, URL, headers exclus).
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    received_hex = signature_header[len("sha256="):]
    expected_hex = hmac.new(CHARIOW_PULSE_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_hex, received_hex)


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


def _find_subscription_by_email(session: Session, email: str | None) -> Subscription | None:
    """
    Les Pulses license.* (activated/expired/revoked/nearing_expiry) ne
    portent PAS notre custom_metadata (confirmé via chariow.dev/en/guides/pulses
    — seul l'objet "customer" y figure, avec son email) : on ne peut donc pas
    les relier à un utilisateur via chariow_license_key dès le premier
    league.activated (la licence n'est pas encore connue de notre côté à ce
    stade). On relie via l'email du client, identique à celui utilisé pour
    /billing/checkout (current_user.email).
    """
    if not email:
        return None
    user = session.exec(select(User).where(User.email == email)).first()
    if user is None:
        return None
    return session.exec(select(Subscription).where(Subscription.user_id == user.id)).first()


@router.post("/pulse", status_code=status.HTTP_200_OK)
@limiter.limit("60/minute")
async def chariow_pulse(request: Request, session: Session = Depends(get_session)):
    payload = await request.body()
    signature = request.headers.get("x-chariow-signature")

    if not _verify_pulse_signature(payload, signature):
        # Signature invalide ou absente -> on rejette, ne JAMAIS traiter un
        # événement dont on n'a pas pu vérifier l'authenticité (sinon
        # n'importe qui pourrait POST un faux "licence activée"). 401 :
        # recommandation explicite de la doc Chariow (Pulse Security).
        raise HTTPException(status_code=401, detail="Signature Pulse invalide")

    delivery_id = request.headers.get("x-pulse-delivery-id")
    if delivery_id and _already_processed(session, delivery_id):
        # Chariow peut renvoyer la même delivery plusieurs fois (retry
        # réseau) — on accuse réception sans retraiter.
        return {"received": True, "duplicate": True}

    # Pas de wrapper "data" : les champs sont à la racine, sous des clés qui
    # varient par type d'événement ("sale", "license", "customer", ...) —
    # confirmé via chariow.dev/en/guides/pulses.
    body = json.loads(payload)
    event_type = body.get("event")

    if event_type == "successful.sale":
        _handle_successful_sale(session, body)
    elif event_type == "license.activated":
        _handle_license_activated(session, body)
    elif event_type == "license.expired":
        _handle_license_status(session, body, new_status="expired")
    elif event_type == "license.revoked":
        _handle_license_status(session, body, new_status="revoked")
    elif event_type == "license.nearing_expiry":
        _handle_license_nearing_expiry(session, body)
    # Les autres types d'événements peuvent être ajoutés au besoin — ignorés
    # silencieusement pour l'instant plutôt que de lever une erreur
    # (Chariow réessaie sinon indéfiniment).

    if delivery_id:
        _mark_processed(session, delivery_id, event_type)

    return {"received": True}


def _handle_successful_sale(session: Session, body: dict):
    """
    Le sale ne porte PAS encore de clé de licence ni de date d'expiration
    (ces informations arrivent avec le Pulse license.activated qui suit) —
    on active l'accès dès maintenant sur la seule foi de custom_metadata
    (notre user_id, fixé par nous à la création du checkout), et
    license.activated affinera current_period_end juste après.
    """
    sale = body.get("sale") or {}
    custom_metadata = sale.get("custom_metadata") or {}
    user_id = custom_metadata.get("user_id")
    if user_id is None:
        return
    user_id = int(user_id)

    sub = session.exec(select(Subscription).where(Subscription.user_id == user_id)).first()
    if sub is None:
        return  # ne devrait pas arriver (/billing/checkout crée toujours l'enregistrement avant la vente)

    sub.plan = custom_metadata.get("plan")
    sub.status = "active"
    # Nouvel achat/renouvellement : le compte à rebours précédent n'a plus cours.
    sub.days_until_expiry = None
    sub.updated_at = datetime.now(timezone.utc)
    session.add(sub)
    session.commit()


def _handle_license_activated(session: Session, body: dict):
    """Complément de successful.sale : fournit la clé de licence et la date
    d'expiration, absentes du Pulse successful.sale. On relie via l'email du
    client (pas de custom_metadata sur cet événement, cf. _find_subscription_by_email)."""
    license_ = body.get("license") or {}
    customer = body.get("customer") or {}
    sub = _find_subscription_by_email(session, customer.get("email"))
    if sub is None:
        return
    sub.chariow_license_key = license_.get("key")
    sub.status = "active"
    expires_at = _parse_datetime(license_.get("expires_at"))
    if expires_at is not None:
        sub.current_period_end = expires_at
    sub.updated_at = datetime.now(timezone.utc)
    session.add(sub)
    session.commit()


def _handle_license_status(session: Session, body: dict, *, new_status: str):
    customer = body.get("customer") or {}
    sub = _find_subscription_by_email(session, customer.get("email"))
    if sub is None:
        return
    sub.status = new_status
    sub.updated_at = datetime.now(timezone.utc)
    session.add(sub)
    session.commit()


def _handle_license_nearing_expiry(session: Session, body: dict):
    customer = body.get("customer") or {}
    sub = _find_subscription_by_email(session, customer.get("email"))
    if sub is None:
        return
    sub.days_until_expiry = body.get("days_until_expiry")
    sub.updated_at = datetime.now(timezone.utc)
    session.add(sub)
    session.commit()
