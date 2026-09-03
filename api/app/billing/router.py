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
from app.models.subscription import ProcessedPulseDelivery
from app.models.provider_subscription import ProviderSubscription
from app.models.entitlement import Entitlement
from app.billing.entitlement_service import recompute_entitlement
from app.referral.commission_service import create_commission_for_confirmed_payment, reverse_commissions_for_subscription
from app.models.google_play_purchase import ProcessedGoogleNotification
from app.billing.google_play_service import (
    GoogleApiError,
    GooglePurchaseInvalid,
    GoogleTokenOwnershipConflict,
    RTDN_NOTIFICATION_TYPE_LABELS,
    REVOKED_NOTIFICATION_TYPE,
    decode_rtdn_envelope,
    sync_known_google_purchase,
    verify_and_sync_google_purchase,
    verify_rtdn_oidc_token,
    verify_rtdn_shared_secret,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])


class CheckoutRequest(BaseModel):
    plan: str  # "biweekly", "monthly" ou "yearly" — validé dynamiquement contre PRODUCT_IDS (chariow_config.py), jamais une liste figée ici
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


def _get_or_create_provider_subscription(session: Session, user: User) -> ProviderSubscription:
    """Récupère la ligne ProviderSubscription(provider='chariow') de
    l'utilisateur, ou en crée une vide (status='none') s'il n'en existe pas
    encore. Contrairement à Stripe, rien à créer côté Chariow à l'avance :
    les informations client sont envoyées directement à chaque appel de
    checkout.

    Remplace l'ancien _get_or_create_subscription (table Subscription,
    conservée en lecture seule pour l'historique pré-migration, plus jamais
    écrite — voir app/models/provider_subscription.py et
    app/billing/backfill.py)."""
    sub = session.exec(
        select(ProviderSubscription).where(
            ProviderSubscription.user_id == user.id,
            ProviderSubscription.provider == "chariow",
        )
    ).first()
    if sub is None:
        sub = ProviderSubscription(user_id=user.id, provider="chariow", status="none")
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

    _get_or_create_provider_subscription(session, current_user)

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
    (active/expired/revoked/pending_activation), expires_at, product,
    metadata (le custom_metadata envoyé à POST /checkout — {"user_id":
    ..., "plan": ...}) ET customer.email (vérifié empiriquement contre une
    vraie clé — présent malgré la doc Chariow qui prétendait ce champ
    absent). Les deux servent de vérification d'appartenance dans
    activate_license ci-dessous : metadata.user_id en priorité, email en
    secours pour les achats antérieurs à la correction du bug
    metadata/custom_metadata (voir test_create_checkout_link_sends_custom_metadata_key)
    — ces licences-là n'ont AUCUNE metadata (null), pas juste un user_id
    manquant, donc aucune correction rétroactive possible côté Chariow.

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


def _activate_chariow_license(license_key: str) -> dict:
    """
    POST {CHARIOW_API_BASE_URL}/licenses/{license_key}/activate
    (chariow.dev/api-reference/licenses/activate-license).

    Découvert en diagnostiquant une vraie clé de licence en prod : un
    achat NE rend PAS automatiquement une licence "active" côté Chariow,
    sauf si le produit est configuré avec requires_activation=false dans
    le dashboard Chariow (mode recommandé pour un SaaS, pas activé sur nos
    produits au moment d'écrire ceci) — sans ce réglage, une licence reste
    "pending_activation" tant que rien n'appelle cette route. La doc
    Chariow désigne "l'app/l'appareil du client" comme appelant normal ;
    pour un SaaS web sans notion d'appareil, c'est notre backend qui joue
    ce rôle au nom du client, une fois son appartenance déjà vérifiée
    (jamais appelée sur une licence dont on n'a pas confirmé le
    propriétaire, voir activate_license).

    Pas de device_identifier envoyé (champ optionnel, pensé pour du
    logiciel desktop avec empreinte machine — aucun sens pour ce service).
    """
    _require_chariow_api_key()
    response = httpx.post(
        f"{CHARIOW_API_BASE_URL}/licenses/{quote(license_key, safe='')}/activate",
        headers={"Authorization": f"Bearer {CHARIOW_API_KEY}"},
        json={},
        timeout=10.0,
    )
    if response.status_code >= 400:
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        message = body.get("message", f"Erreur Chariow ({response.status_code})")
        raise HTTPException(status_code=502, detail=f"Chariow: activation impossible ({message})")
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

    Vérification d'appartenance en deux temps, jamais sur la seule
    confiance du texte collé par le client :
      1. metadata.user_id (fixé par NOUS à l'achat, voir
         create_checkout_session) — le cas normal.
      2. Repli sur customer.email si aucune metadata n'existe DU TOUT
         (achats antérieurs à la correction du bug metadata/custom_metadata,
         voir _fetch_chariow_license — pas juste user_id manquant, la
         licence entière n'a jamais reçu de metadata, rien à corriger
         rétroactivement côté Chariow) — même modèle de confiance que
         _find_subscription_by_email pour les Pulses license.*.

    Active elle-même la licence côté Chariow si nécessaire (voir
    _activate_chariow_license) : un achat ne suffit pas toujours à rendre
    une licence "active" chez Chariow (découvert en diagnostiquant une
    vraie clé bloquée en pending_activation malgré un paiement réel) —
    seulement APRÈS avoir confirmé l'appartenance ci-dessus, jamais avant.
    """
    license_data = _fetch_chariow_license(body.license_key)

    metadata = license_data.get("metadata") or {}
    license_user_id = metadata.get("user_id")
    if license_user_id is not None:
        owns_license = str(license_user_id) == str(current_user.id)
    else:
        customer_email = (license_data.get("customer") or {}).get("email")
        owns_license = customer_email is not None and customer_email.lower() == current_user.email.lower()

    if not owns_license:
        raise HTTPException(
            status_code=403,
            detail="Cette clé de licence n'est pas associée à ton compte.",
        )

    license_status = license_data.get("status")
    if license_status == "pending_activation":
        license_data = _activate_chariow_license(body.license_key)
        license_status = license_data.get("status")
        metadata = license_data.get("metadata") or metadata  # inchangée par l'activation, mais robuste si Chariow la renvoie

    if license_status != "active":
        messages = {
            "expired": "Cette licence a expiré — repasse par un nouvel achat pour la renouveler.",
            "revoked": "Cette licence a été révoquée.",
        }
        raise HTTPException(
            status_code=400,
            detail=messages.get(license_status, f"Licence non active (statut : {license_status})."),
        )

    plan = metadata.get("plan")
    if plan is None:
        # Metadata absente (achat pré-correctif, cf. ci-dessus) -> déduit du
        # product_id Chariow plutôt que laissé vide, en comparant aux
        # Product ID monthly/yearly déjà configurés (PRODUCT_IDS).
        product_id = (license_data.get("product") or {}).get("id")
        plan = next((p for p, pid in PRODUCT_IDS.items() if pid == product_id), None)
        # Phase 15.10 : delibérément PAS de rejet si plan reste None ici — comportement historique
        # déjà testé (test_activate_license_email_fallback_when_no_metadata) : une licence legacy
        # SANS metadata NI product_id reconnu est tout de même activée (Chariow confirme un achat
        # réel, refuser l'accès sur la seule absence d'un libellé serait pire que l'accepter). Le
        # plan=None qui en résulte est inoffensif pour la commission ci-dessous : le calcul ne
        # dépend jamais de `plan`, uniquement du montant réel (voir money.py).

    sub = _get_or_create_provider_subscription(session, current_user)
    sub.status = "active"
    sub.raw_status = "active"
    sub.external_ref = body.license_key
    sub.plan = plan
    expires_at = _parse_datetime(license_data.get("expires_at"))
    if expires_at is not None:
        sub.current_period_end = expires_at
    sub.days_until_expiry = None  # confirmation d'achat, même convention que _handle_successful_sale
    sub.updated_at = datetime.now(timezone.utc)
    session.add(sub)
    session.commit()
    session.refresh(sub)
    recompute_entitlement(session, current_user.id, provider_subscription_id=sub.id,
                           reason="chariow: activate-license (filet de secours)")

    # Phase 15.10 : ce endpoint est le filet de sécurité quand le Pulse successful.sale n'a jamais
    # finalisé le traitement — la vente/commission de parrainage doit donc pouvoir se produire ICI
    # AUSSI, en RÉUTILISANT exactement le même point d'entrée unique que le webhook
    # (create_commission_for_confirmed_payment, jamais une seconde logique de calcul). sale_body=
    # license_data (pas un objet "sale" Pulse) : extract_actual_paid_amount() y cherche les mêmes clés
    # candidates de montant (amount/amount_paid/price/...) à la racine, et retombe sur son repli déjà
    # existant (prix configuré fixe) ou sur "unavailable" si rien n'est trouvé — JAMAIS un montant
    # inventé ici (§33/§43, money.py, inchangé). Idempotence : source_event_id dérivé de la clé de
    # licence elle-même (stable, unique par achat Chariow, jamais transmise par le client comme
    # identifiant de session) — un second appel avec la MÊME licence retombe sur la contrainte UNIQUE
    # déjà en place (commission_service.py), jamais une deuxième commission, y compris en cas d'appels
    # concurrents (IntegrityError déjà gérée). promoter_id n'est JAMAIS lu depuis le corps de la requête
    # client (ActivateLicenseRequest n'a même pas ce champ) : il provient exclusivement de la
    # ReferralAttribution déjà en base pour CET utilisateur authentifié. Best-effort : ne doit jamais
    # faire échouer l'activation de l'abonnement déjà committée ci-dessus.
    try:
        create_commission_for_confirmed_payment(
            session, provider_subscription=sub, sale_body=license_data,
            delivery_id=f"activate-license:{body.license_key}",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Création de commission de parrainage impossible via activate-license (non bloquant) "
            "pour provider_subscription_id=%s : %s", sub.id, e,
        )

    return SubscriptionStatus(
        status=sub.status, plan=sub.plan, is_active=sub.is_effectively_active,
        current_period_end=sub.current_period_end, days_until_expiry=sub.days_until_expiry,
    )


@router.get("/subscription", response_model=SubscriptionStatus)
def get_subscription_status(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    sub = session.exec(
        select(ProviderSubscription).where(
            ProviderSubscription.user_id == current_user.id,
            ProviderSubscription.provider == "chariow",
        )
    ).first()
    if sub is None:
        return SubscriptionStatus(
            status="none", plan=None, is_active=False,
            current_period_end=None, days_until_expiry=None,
        )
    return SubscriptionStatus(
        status=sub.status, plan=sub.plan, is_active=sub.is_effectively_active,
        current_period_end=sub.current_period_end, days_until_expiry=sub.days_until_expiry,
    )


class EntitlementResponse(BaseModel):
    premium: bool
    premium_until: datetime | None
    active_sources: list[str]


@router.get("/entitlement", response_model=EntitlementResponse)
def get_entitlement(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """
    Endpoint générique multi-provider (Chariow + Google Play), destiné à
    l'app Android en premier lieu mais réutilisable plus tard par le
    frontend web. Lit UNIQUEMENT la table Entitlement (cache déjà tenu à
    jour par recompute_entitlement à chaque changement de
    ProviderSubscription, voir app/billing/entitlement_service.py) —
    aucun recalcul ici, pour ne jamais faire diverger ce que cet endpoint
    répond de ce que require_active_subscription vérifie réellement.

    Ne renvoie aucune information sensible (pas de purchaseToken, pas de
    clé de licence, pas de détail par provider au-delà de son nom).
    """
    entitlement = session.get(Entitlement, current_user.id)
    if entitlement is None:
        return EntitlementResponse(premium=False, premium_until=None, active_sources=[])
    return EntitlementResponse(
        premium=entitlement.premium,
        premium_until=entitlement.premium_until,
        active_sources=json.loads(entitlement.active_sources),
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


def _find_provider_subscription_by_email(session: Session, email: str | None) -> ProviderSubscription | None:
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
    return session.exec(
        select(ProviderSubscription).where(
            ProviderSubscription.user_id == user.id,
            ProviderSubscription.provider == "chariow",
        )
    ).first()


def _strip_pii(body: dict) -> str:
    """Sérialise le payload Pulse pour audit (ProviderSubscription.raw_payload)
    en retirant l'objet "customer" (email/nom/téléphone) — déjà rattaché à
    l'utilisateur via user_id (FK), inutile de le dupliquer dans une colonne
    d'audit. Ne contient jamais de donnée bancaire (Chariow n'en envoie
    jamais dans ses Pulses)."""
    return json.dumps({k: v for k, v in body.items() if k != "customer"})


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
        # réseau) — on accuse réception sans retraiter, quel que soit le
        # contenu de CE second envoi (jamais reparsé/recomparé — la
        # déduplication porte sur l'identité de la delivery, pas sur son
        # contenu, §12 : un même payment/transaction ID ne doit jamais
        # produire un second traitement, même avec des données divergentes).
        return {"received": True, "duplicate": True}

    # Pas de wrapper "data" : les champs sont à la racine, sous des clés qui
    # varient par type d'événement ("sale", "license", "customer", ...) —
    # confirmé via chariow.dev/en/guides/pulses.
    #
    # Phase 15.9 : la signature a déjà été vérifiée ci-dessus (payload
    # authentiquement issu de Chariow) — mais "signé" ne garantit pas "JSON
    # bien formé", ni "porteur d'un champ 'event'". Un corps illisible ou
    # sans 'event' n'est PAS un type d'événement qu'on choisit d'ignorer
    # (voir plus bas) : c'est un Pulse structurellement invalide, jamais vu
    # dans la doc Chariow — 400 explicite plutôt qu'une 500 générique
    # (JSONDecodeError non attrapée) ou un 200 silencieux masquant un vrai
    # problème de parsing.
    try:
        body = json.loads(payload)
    except json.JSONDecodeError:
        logger.error("Pulse Chariow : corps JSON invalide (delivery_id=%s, signature pourtant valide).", delivery_id)
        raise HTTPException(status_code=400, detail="Corps de Pulse JSON invalide")

    event_type = body.get("event")
    if event_type is None:
        logger.error("Pulse Chariow : champ 'event' absent du corps (delivery_id=%s).", delivery_id)
        raise HTTPException(status_code=400, detail="Champ 'event' manquant")

    if event_type == "successful.sale":
        _handle_successful_sale(session, body, delivery_id)
    elif event_type == "license.activated":
        _handle_license_activated(session, body)
    elif event_type == "license.expired":
        _handle_license_status(session, body, new_status="expired")
    elif event_type == "license.revoked":
        _handle_license_status(session, body, new_status="revoked")
    elif event_type == "license.nearing_expiry":
        _handle_license_nearing_expiry(session, body)
    else:
        # Type d'événement RECONNU COMME PRÉSENT mais non géré par ce code —
        # rester en 200 (Chariow réessaie sinon indéfiniment un type qu'on
        # ne traitera jamais, voir doc Pulse) MAIS journalisé explicitement
        # (Phase 15.9 : "réponse explicite et loggable", jamais un silence
        # total comme avant — c'est ce même silence qui a rendu le diagnostic
        # du paiement pi_ih0s0eixhgm1 impossible à confirmer depuis les logs).
        logger.info("Pulse Chariow : event_type '%s' non géré, accusé réception sans traitement (delivery_id=%s).", event_type, delivery_id)

    if delivery_id:
        _mark_processed(session, delivery_id, event_type)

    return {"received": True}


def _handle_successful_sale(session: Session, body: dict, delivery_id: str | None = None):
    """
    Le sale ne porte PAS encore de clé de licence ni de date d'expiration
    (ces informations arrivent avec le Pulse license.activated qui suit) —
    on active l'accès dès maintenant sur la seule foi de custom_metadata
    (notre user_id, fixé par nous à la création du checkout), et
    license.activated affinera current_period_end juste après.

    Phase 14 : c'est ICI, et UNIQUEMENT ici, que le paiement devient
    PAID/CONFIRMED pour ce projet (§11 du prompt Phase 14 — jamais au clic,
    à l'inscription, à la création du checkout, ni au choix d'un plan) — le
    point d'accroche naturel pour la création d'une commission de
    parrainage, réutilisant `delivery_id` comme clé d'idempotence (déjà
    dédupliqué en amont par ProcessedPulseDelivery, voir chariow_pulse
    ci-dessus ; la commission ajoute sa propre contrainte UNIQUE en défense
    en profondeur, voir app/referral/commission_service.py).
    """
    sale = body.get("sale") or {}
    custom_metadata = sale.get("custom_metadata") or {}
    user_id_raw = custom_metadata.get("user_id")

    sub = None
    if user_id_raw is not None:
        try:
            user_id = int(user_id_raw)
        except (TypeError, ValueError):
            logger.warning(
                "successful.sale : custom_metadata.user_id non numérique (%r) — delivery_id=%s, "
                "tentative de repli par email.", user_id_raw, delivery_id,
            )
        else:
            sub = session.exec(
                select(ProviderSubscription).where(
                    ProviderSubscription.user_id == user_id,
                    ProviderSubscription.provider == "chariow",
                )
            ).first()
            if sub is None:
                logger.warning(
                    "successful.sale : aucun ProviderSubscription pour user_id=%s (custom_metadata) — "
                    "delivery_id=%s, tentative de repli par email.", user_id, delivery_id,
                )

    if sub is None:
        # Repli EMAIL — RÉUTILISE _find_provider_subscription_by_email (déjà en place et testé pour
        # les Pulses license.*, jamais un second mécanisme inventé ici). Nécessaire car ce dépôt a
        # DÉJÀ rencontré, une fois, un cas réel où Chariow ne renvoie pas metadata sous la structure
        # attendue (voir _fetch_chariow_license, "correction du bug metadata/custom_metadata")  — la
        # même prudence s'applique ici tant que le Pulse successful.sale réel n'a jamais été
        # confirmé conforme à la doc contre un paiement réel avant celui-ci (Phase 15.8).
        customer_email = (sale.get("customer") or {}).get("email")
        sub = _find_provider_subscription_by_email(session, customer_email)
        if sub is None:
            logger.warning(
                "successful.sale IGNORÉ : ni custom_metadata.user_id ni repli email n'ont permis de "
                "retrouver un ProviderSubscription Xfoot — delivery_id=%s. Paiement Chariow "
                "potentiellement confirmé mais NON rattaché, nécessite une investigation manuelle "
                "(voir raw_payload si une ligne ProviderSubscription existe déjà pour ce client).",
                delivery_id,
            )
            return
        logger.info(
            "successful.sale rattaché par repli email (customer.email) pour provider_subscription_id=%s, "
            "delivery_id=%s — custom_metadata absente ou incomplète.", sub.id, delivery_id,
        )

    # custom_metadata.get("plan") reste vide en repli email (aucune metadata) — ne jamais écraser un
    # plan déjà connu par une valeur absente dans ce cas précis.
    sub.plan = custom_metadata.get("plan") or sub.plan
    sub.status = "active"
    sub.raw_status = "active"
    # Nouvel achat/renouvellement : le compte à rebours précédent n'a plus cours.
    sub.days_until_expiry = None
    sub.raw_payload = _strip_pii(body)
    sub.updated_at = datetime.now(timezone.utc)
    session.add(sub)
    session.commit()
    session.refresh(sub)
    # sub.user_id (jamais la variable locale user_id, qui n'existe pas en repli email) : source
    # fiable dans les DEUX chemins (custom_metadata ou repli email), toujours celui de la ligne
    # ProviderSubscription réellement mise à jour ci-dessus.
    recompute_entitlement(session, sub.user_id, provider_subscription_id=sub.id, reason="chariow: successful.sale")

    # Phase 14 : paiement RÉELLEMENT confirmé ci-dessus (sub.status="active", déjà committé) — seul
    # endroit où une commission de parrainage peut être créée (§11/§52). Best-effort : ne doit jamais faire
    # échouer le traitement du paiement/entitlement déjà appliqué au-dessus (même discipline que
    # api/main.py::_log_prediction) — aucune vente/aucun abonné n'est affecté si la commission échoue.
    try:
        create_commission_for_confirmed_payment(session, provider_subscription=sub, sale_body=sale, delivery_id=delivery_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("Création de commission de parrainage impossible (non bloquant) pour provider_subscription_id=%s : %s", sub.id, e)


def _handle_license_activated(session: Session, body: dict):
    """Complément de successful.sale : fournit la clé de licence et la date
    d'expiration, absentes du Pulse successful.sale. On relie via l'email du
    client (pas de custom_metadata sur cet événement, cf.
    _find_provider_subscription_by_email)."""
    license_ = body.get("license") or {}
    customer = body.get("customer") or {}
    sub = _find_provider_subscription_by_email(session, customer.get("email"))
    if sub is None:
        return
    sub.external_ref = license_.get("key")
    sub.status = "active"
    sub.raw_status = "active"
    expires_at = _parse_datetime(license_.get("expires_at"))
    if expires_at is not None:
        sub.current_period_end = expires_at
    sub.raw_payload = _strip_pii(body)
    sub.updated_at = datetime.now(timezone.utc)
    session.add(sub)
    session.commit()
    session.refresh(sub)
    recompute_entitlement(session, sub.user_id, provider_subscription_id=sub.id, reason="chariow: license.activated")


def _handle_license_status(session: Session, body: dict, *, new_status: str):
    customer = body.get("customer") or {}
    sub = _find_provider_subscription_by_email(session, customer.get("email"))
    if sub is None:
        return
    sub.status = new_status
    sub.raw_status = new_status
    if new_status == "revoked":
        sub.revoked_or_refunded_at = datetime.now(timezone.utc)
    sub.raw_payload = _strip_pii(body)
    sub.updated_at = datetime.now(timezone.utc)
    session.add(sub)
    session.commit()
    session.refresh(sub)
    recompute_entitlement(session, sub.user_id, provider_subscription_id=sub.id, reason=f"chariow: license.{new_status}")

    # Phase 14 : §13/§35 — "revoked" est déjà le vocabulaire de ce dépôt pour révocation/remboursement
    # (voir revoked_or_refunded_at ci-dessus, nom de champ déjà existant, jamais un second concept
    # introduit). Toute commission ACCRUED liée à cet abonnement passe à REVERSED — jamais supprimée.
    if new_status == "revoked":
        try:
            reverse_commissions_for_subscription(session, sub.id, reason="chariow: license.revoked")
        except Exception as e:  # noqa: BLE001
            logger.warning("Réversion de commission de parrainage impossible (non bloquant) pour provider_subscription_id=%s : %s", sub.id, e)


def _handle_license_nearing_expiry(session: Session, body: dict):
    customer = body.get("customer") or {}
    sub = _find_provider_subscription_by_email(session, customer.get("email"))
    if sub is None:
        return
    sub.days_until_expiry = body.get("days_until_expiry")
    sub.raw_payload = _strip_pii(body)
    sub.updated_at = datetime.now(timezone.utc)
    session.add(sub)
    session.commit()
    # Purement informatif (compte à rebours affiché côté frontend) — n'affecte
    # jamais `status`/`is_effectively_active`, donc aucun recalcul d'entitlement.


# ---------------------------------------------------------------------------
# Google Play Billing (Phase 2) — provider indépendant, voir
# app/billing/google_play_service.py pour l'implémentation complète
# (validation serveur, anti-fraude, mapping ProviderSubscription/
# GooglePlayPurchase, recalcul Entitlement). Chariow ci-dessus reste
# entièrement inchangé.
# ---------------------------------------------------------------------------

class GoogleVerifyRequest(BaseModel):
    product_id: str
    purchase_token: str
    # Optionnel : comparé à GOOGLE_PLAY_PACKAGE_NAME configuré côté serveur,
    # mais JAMAIS utilisé comme valeur réelle envoyée à l'API Google (voir
    # verify_and_sync_google_purchase) — un client ne choisit jamais quel
    # package notre backend interroge.
    package_name: str | None = None


class GoogleVerifyResponse(BaseModel):
    premium: bool
    google_status: str
    plan: str | None
    expiry_time: datetime | None
    acknowledgement_state: str


@router.post("/google/verify", response_model=GoogleVerifyResponse)
@limiter.limit("60/minute")
def google_verify_purchase(
    request: Request,
    body: GoogleVerifyRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """
    Point d'entrée unique pour un achat Google Play côté Android (pas encore
    intégré cette phase — backend uniquement) : sert aussi bien un premier
    achat qu'une restauration (queryPurchasesAsync côté client renverrait le
    même purchase_token, cet endpoint est idempotent par construction) —
    aucun endpoint /restore séparé n'est nécessaire (décision validée).

    Ne fait JAMAIS confiance au statut envoyé par le client : tout repart
    d'un appel serveur->Google (purchases.subscriptionsv2.get).
    """
    try:
        provider_sub, purchase, entitlement = verify_and_sync_google_purchase(
            session, current_user,
            product_id=body.product_id, purchase_token=body.purchase_token,
            package_name=body.package_name,
        )
    except GoogleTokenOwnershipConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except GooglePurchaseInvalid as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    except GoogleApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    return GoogleVerifyResponse(
        premium=entitlement.premium if entitlement else False,
        google_status=provider_sub.status,
        plan=provider_sub.plan,
        expiry_time=provider_sub.current_period_end,
        acknowledgement_state=purchase.acknowledgement_state,
    )


def _already_processed_google_notification(session: Session, message_id: str) -> bool:
    return session.exec(
        select(ProcessedGoogleNotification).where(ProcessedGoogleNotification.pubsub_message_id == message_id)
    ).first() is not None


def _mark_google_notification_processed(session: Session, message_id: str, notification_type: str | None) -> None:
    session.add(ProcessedGoogleNotification(pubsub_message_id=message_id, notification_type=notification_type))
    session.commit()


@router.post("/google/rtdn", status_code=status.HTTP_200_OK)
async def google_rtdn(request: Request, session: Session = Depends(get_session)):
    """
    Réception des Real-time Developer Notifications Google Play — structure
    backend uniquement cette phase, AUCUNE configuration Google Cloud
    Pub/Sub réelle (consigne explicite). Authentification prévue en deux
    couches, contrôlées par la configuration présente :

      1. Vérification OIDC du jeton signé par Pub/Sub (chemin définitif une
         fois une vraie souscription Push authentifiée configurée) — voir
         verify_rtdn_oidc_token, non testable contre un vrai jeton sans
         projet Google Cloud réel.
      2. Secret partagé en query string (`?secret=...`, comparé à
         GOOGLE_RTDN_SHARED_SECRET) — filet intérimaire pour les tests
         locaux avant configuration Pub/Sub réelle.

    Si aucune des deux n'est configurée/valide -> 401 (jamais d'endpoint
    ouvert par défaut).

    Ne fait JAMAIS confiance au statut contenu dans la notification elle-
    même : sert uniquement de déclencheur pour rappeler l'API Google et
    resynchroniser l'état réel (voir sync_known_google_purchase).
    """
    audience = str(request.url).split("?")[0]
    authenticated = verify_rtdn_oidc_token(request.headers.get("authorization"), audience) or \
        verify_rtdn_shared_secret(request.query_params.get("secret"))
    if not authenticated:
        raise HTTPException(status_code=401, detail="Authentification RTDN invalide.")

    body = await request.json()
    message = body.get("message") or {}
    message_id = message.get("messageId")
    if message_id and _already_processed_google_notification(session, message_id):
        return {"received": True, "duplicate": True}

    try:
        notification = decode_rtdn_envelope(body)
    except GooglePurchaseInvalid as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))

    subscription_notification = notification.get("subscriptionNotification")
    notification_type_label = None
    if subscription_notification:
        purchase_token = subscription_notification.get("purchaseToken")
        notification_type_int = subscription_notification.get("notificationType")
        notification_type_label = RTDN_NOTIFICATION_TYPE_LABELS.get(notification_type_int, str(notification_type_int))
        forced_status = "revoked" if notification_type_int == REVOKED_NOTIFICATION_TYPE else None

        try:
            sync_known_google_purchase(
                session, purchase_token,
                notification_type=notification_type_label, forced_status=forced_status,
            )
        except GoogleApiError as exc:
            logger.error("Google RTDN: resynchronisation impossible pour %s (%s) — message non marqué traité.",
                         purchase_token, exc)
            raise HTTPException(status_code=502, detail=str(exc))
        except GooglePurchaseInvalid as exc:
            logger.warning("Google RTDN: %s", exc)
    # oneTimeProductNotification/voidedPurchaseNotification/testNotification :
    # hors périmètre (Xfoot ne vend que des abonnements) — accusés de
    # réception sans action, jamais d'erreur (Pub/Sub réessaierait sinon).

    if message_id:
        _mark_google_notification_processed(session, message_id, notification_type_label)

    return {"received": True}
