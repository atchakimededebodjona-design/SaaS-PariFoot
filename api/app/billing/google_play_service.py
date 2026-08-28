"""
Google Play Billing (Phase 2) — 4 responsabilités séparées comme demandé :

  1. récupération  — _get_access_token / _fetch_google_subscription /
                      _acknowledge_google_purchase (seules fonctions qui
                      parlent réellement au réseau, patchées dans les tests)
  2. validation    — normalize_google_status / _extract_line_item /
                      _resolve_plan (vérification package/produit/statut,
                      jamais de confiance dans ce que le client déclare)
  3. mapping       — _apply_google_purchase / _refresh_provider_subscription_facade
                      (écriture GooglePlayPurchase + ProviderSubscription)
  4. recalcul      — appel direct à recompute_entitlement (Phase 1,
                      strictement INCHANGÉE — la règle OR déjà en place
                      couvre Google sans aucune modification)

Deux points d'entrée orchestrant ces 4 étapes :
  - verify_and_sync_google_purchase  : appelé par POST /billing/google/verify
    (utilisateur authentifié connu).
  - sync_known_google_purchase       : appelé par POST /billing/google/rtdn
    (pas d'utilisateur authentifié — résolution par purchase_token déjà
    connu ; ne fait JAMAIS confiance aux champs de la notification, sert
    uniquement de déclencheur pour re-vérifier la vérité fraîche via l'API).

Vérifié contre la documentation officielle Google (purchases.subscriptionsv2,
RTDN reference, Cloud Pub/Sub push authentication) le jour de l'écriture de
ce module :
  - SubscriptionPurchaseV2.subscriptionState n'a AUCUNE valeur "REVOKED" ni
    "REFUNDED" — un achat révoqué/remboursé revient identique à un achat
    naturellement expiré (SUBSCRIPTION_STATE_EXPIRED, expiryTime passée).
    Seul le TYPE de notification RTDN SUBSCRIPTION_REVOKED (12) permet de
    distinguer ce cas — d'où le paramètre `forced_status` ci-dessous.
  - SUBSCRIPTION_STATE_PENDING existe ("en attente de paiement à
    l'inscription") et ne doit JAMAIS accorder Premium — distinct de notre
    statut Chariow "none".
  - purchases.subscriptions.acknowledge reste l'endpoint courant pour
    l'acknowledgement (aucune dépréciation trouvée), scope
    androidpublisher.
"""

import base64
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlmodel import Session, select

from app.core.google_play_config import (
    GOOGLE_PLAY_API_BASE_URL,
    GOOGLE_PLAY_OAUTH_SCOPE,
    GOOGLE_PLAY_PACKAGE_NAME,
    GOOGLE_PLAY_PRODUCT_ID,
    GOOGLE_PLAY_BASE_PLAN_IDS,
    GOOGLE_PLAY_SERVICE_ACCOUNT_JSON,
    GOOGLE_PLAY_SERVICE_ACCOUNT_JSON_PATH,
    GOOGLE_RTDN_SHARED_SECRET,
)
from app.models.user import User
from app.models.provider_subscription import ProviderSubscription
from app.models.entitlement import Entitlement
from app.models.google_play_purchase import GooglePlayPurchase
from app.billing.entitlement_service import recompute_entitlement

logger = logging.getLogger(__name__)


class GoogleApiError(Exception):
    """Erreur de communication avec l'API Google Play Developer (réseau,
    timeout, erreur serveur Google, mauvaise config côté nous) — jamais la
    faute du purchaseToken fourni par le client. Mappée en 502 côté routeur."""


class GooglePurchaseInvalid(Exception):
    """L'achat lui-même est en cause (token introuvable, package/produit non
    reconnu) — faute de l'appelant. `status_code` porte le code HTTP à
    renvoyer (400 par défaut, 502 si la réponse Google est structurellement
    invalide)."""

    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class GoogleTokenOwnershipConflict(Exception):
    """purchaseToken déjà associé à un autre compte Xfoot — jamais de
    réassociation automatique (mappée en 409 côté routeur)."""


# ---------------------------------------------------------------------------
# 1. Récupération — authentification service account + appels API bruts
# ---------------------------------------------------------------------------

_credentials = None  # cache module-level, une seule fois par process


def _get_credentials():
    global _credentials
    if _credentials is not None:
        return _credentials

    from google.oauth2 import service_account

    if GOOGLE_PLAY_SERVICE_ACCOUNT_JSON:
        info = json.loads(GOOGLE_PLAY_SERVICE_ACCOUNT_JSON)
        _credentials = service_account.Credentials.from_service_account_info(
            info, scopes=[GOOGLE_PLAY_OAUTH_SCOPE]
        )
    elif GOOGLE_PLAY_SERVICE_ACCOUNT_JSON_PATH:
        _credentials = service_account.Credentials.from_service_account_file(
            GOOGLE_PLAY_SERVICE_ACCOUNT_JSON_PATH, scopes=[GOOGLE_PLAY_OAUTH_SCOPE]
        )
    else:
        raise GoogleApiError(
            "Google Play: aucun compte de service configuré "
            "(GOOGLE_PLAY_SERVICE_ACCOUNT_JSON[_PATH] absent)."
        )
    return _credentials


def _get_access_token() -> str:
    """Isolée dans sa propre fonction pour rester patchable en test (même
    discipline que _create_chariow_checkout_link côté Chariow) — jamais de
    vrai compte de service ni de vrai réseau dans les tests."""
    import google.auth.transport.requests

    creds = _get_credentials()
    if not creds.valid:
        creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def _fetch_google_subscription(package_name: str, purchase_token: str) -> dict:
    """GET purchases.subscriptionsv2.get — confirmé contre la doc officielle
    (developers.google.com/android-publisher/api-ref/rest/v3/purchases.subscriptionsv2/get).
    Ne fait jamais confiance au client : c'est TOUJOURS package_name côté
    serveur (jamais celui envoyé par le client) qui part dans cette requête."""
    token = _get_access_token()
    url = f"{GOOGLE_PLAY_API_BASE_URL}/applications/{package_name}/purchases/subscriptionsv2/tokens/{purchase_token}"
    try:
        response = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10.0)
    except httpx.TimeoutException as exc:
        raise GoogleApiError("Google Play: délai dépassé lors de la vérification de l'achat.") from exc
    except httpx.HTTPError as exc:
        raise GoogleApiError(f"Google Play: erreur réseau ({exc}).") from exc

    if response.status_code == 404:
        raise GooglePurchaseInvalid("purchaseToken introuvable chez Google Play.", status_code=400)
    if response.status_code in (401, 403):
        raise GoogleApiError(
            f"Google Play: authentification refusée ({response.status_code}) — "
            f"vérifier le compte de service et ses permissions Play Console."
        )
    if response.status_code >= 400:
        raise GoogleApiError(f"Google Play: erreur ({response.status_code}) {response.text[:300]}")
    return response.json()


def _acknowledge_google_purchase(package_name: str, product_id: str, purchase_token: str) -> None:
    """POST purchases.subscriptions.acknowledge — confirmé toujours courant
    (aucune dépréciation trouvée dans la doc officielle). `product_id` utilisé
    comme `subscriptionId` du chemin — c'est le même identifiant (SKU) dans
    le modèle Google ; la doc note que ce segment "n'est plus nécessaire ni
    recommandé pour les abonnements avec add-ons" depuis mai 2025, à
    reconfirmer contre un vrai compte de service avant mise en production
    réelle (non vérifiable sans accès à une vraie Play Console)."""
    token = _get_access_token()
    url = (
        f"{GOOGLE_PLAY_API_BASE_URL}/applications/{package_name}/purchases/subscriptions/"
        f"{product_id}/tokens/{purchase_token}:acknowledge"
    )
    try:
        response = httpx.post(url, headers={"Authorization": f"Bearer {token}"}, json={}, timeout=10.0)
    except httpx.TimeoutException as exc:
        raise GoogleApiError("Google Play: délai dépassé lors de l'acknowledgement.") from exc
    except httpx.HTTPError as exc:
        raise GoogleApiError(f"Google Play: erreur réseau lors de l'acknowledgement ({exc}).") from exc
    if response.status_code >= 400:
        raise GoogleApiError(f"Google Play: acknowledgement refusé ({response.status_code}) {response.text[:300]}")


# ---------------------------------------------------------------------------
# 2. Validation — statut normalisé, ligne de produit, plan
# ---------------------------------------------------------------------------

# Confirmé contre la doc officielle purchases.subscriptionsv2 : ce sont les
# 9 seules valeurs possibles de subscriptionState. AUCUNE ne signifie
# "révoqué" ou "remboursé" — voir docstring module.
_ACTIVE_STATES = {"SUBSCRIPTION_STATE_ACTIVE", "SUBSCRIPTION_STATE_IN_GRACE_PERIOD"}
_PENDING_STATES = {"SUBSCRIPTION_STATE_PENDING", "SUBSCRIPTION_STATE_PENDING_PURCHASE_CANCELED"}

# Notification RTDN dont la réception est le SEUL signal fiable pour
# distinguer une révocation/un remboursement d'une expiration naturelle
# (confirmé contre developer.android.com/google/play/billing/rtdn-reference :
# "A subscription can be revoked... including your backend revoking the
# subscription... or the purchase being charged back").
REVOKED_NOTIFICATION_TYPE = 12

RTDN_NOTIFICATION_TYPE_LABELS = {
    1: "SUBSCRIPTION_RECOVERED", 2: "SUBSCRIPTION_RENEWED", 3: "SUBSCRIPTION_CANCELED",
    4: "SUBSCRIPTION_PURCHASED", 5: "SUBSCRIPTION_ON_HOLD", 6: "SUBSCRIPTION_IN_GRACE_PERIOD",
    7: "SUBSCRIPTION_RESTARTED", 8: "SUBSCRIPTION_PRICE_CHANGE_CONFIRMED", 9: "SUBSCRIPTION_DEFERRED",
    10: "SUBSCRIPTION_PAUSED", 11: "SUBSCRIPTION_PAUSE_SCHEDULE_CHANGED", 12: "SUBSCRIPTION_REVOKED",
    13: "SUBSCRIPTION_EXPIRED", 17: "SUBSCRIPTION_ITEMS_CHANGED", 18: "SUBSCRIPTION_CANCELLATION_SCHEDULED",
    19: "SUBSCRIPTION_PRICE_CHANGE_UPDATED", 20: "SUBSCRIPTION_PENDING_PURCHASE_CANCELED",
    22: "SUBSCRIPTION_PRICE_STEP_UP_CONSENT_UPDATED",
}


def normalize_google_status(subscription_state: str, expiry_time: datetime, *, forced_status: Optional[str] = None) -> str:
    """Fonction pure, testable indépendamment de tout réseau/DB.

    `forced_status` court-circuite tout : utilisé uniquement quand
    l'appelant sait, par un canal hors subscriptionState (réception d'une
    notification RTDN SUBSCRIPTION_REVOKED), que l'achat a été révoqué ou
    remboursé — jamais déductible de la seule réponse subscriptionsv2.get
    (voir docstring module)."""
    if forced_status is not None:
        return forced_status
    if subscription_state in _PENDING_STATES:
        return "pending"
    if subscription_state in _ACTIVE_STATES:
        return "active"
    if subscription_state == "SUBSCRIPTION_STATE_CANCELED":
        now = datetime.now(timezone.utc)
        expiry = expiry_time if expiry_time.tzinfo else expiry_time.replace(tzinfo=timezone.utc)
        return "active" if now < expiry else "expired"
    # ON_HOLD, PAUSED, EXPIRED, UNSPECIFIED ou valeur inconnue -> pas d'accès.
    return "expired"


def _parse_google_datetime(value: Optional[str]) -> datetime:
    if not value:
        raise GooglePurchaseInvalid("Réponse Google Play sans expiryTime exploitable.", status_code=502)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _extract_line_item(raw: dict, expected_product_id: str) -> dict:
    """Ne fait jamais confiance à expected_product_id (déclaré par le
    client ou déjà connu de nous) pour choisir la ligne : cherche une
    correspondance réelle, sinon retombe sur la première ligne RÉELLEMENT
    renvoyée par Google (jamais une valeur inventée)."""
    line_items = raw.get("lineItems") or []
    if not line_items:
        raise GooglePurchaseInvalid("Réponse Google Play sans lineItems — achat invalide.", status_code=502)
    for item in line_items:
        if item.get("productId") == expected_product_id:
            return item
    logger.warning(
        "Google Play: productId attendu ('%s') absent des lineItems réels (%s) — "
        "utilisation du premier lineItem réellement renvoyé par Google.",
        expected_product_id, [i.get("productId") for i in line_items],
    )
    return line_items[0]


def _resolve_plan(product_id: str, base_plan_id: Optional[str]) -> Optional[str]:
    """Modèle Play Console 2022+ : un seul Product ID (xfoot_premium) porte
    plusieurs Base Plans — le plan Xfoot (monthly/yearly) se dérive du
    basePlanId, jamais du productId (qui est désormais constant). Rejette
    si le productId lui-même n'est pas le nôtre (protection contre un achat
    d'un tout autre produit/app) ou si le basePlanId est inconnu."""
    if not product_id or product_id != GOOGLE_PLAY_PRODUCT_ID:
        return None
    return next((p for p, bpid in GOOGLE_PLAY_BASE_PLAN_IDS.items() if bpid and bpid == base_plan_id), None)


def _strip_pii(raw: dict) -> str:
    """Retire subscribeWithGoogleInfo (peut contenir email/nom si l'achat a
    utilisé "Subscribe with Google") avant stockage — même discipline de
    minimisation que _strip_pii côté Chariow (app/billing/router.py)."""
    return json.dumps({k: v for k, v in raw.items() if k != "subscribeWithGoogleInfo"})


# ---------------------------------------------------------------------------
# 3. Mapping — écriture GooglePlayPurchase + ProviderSubscription
# ---------------------------------------------------------------------------

def _find_purchase_owner(session: Session, purchase_token: str) -> Optional[GooglePlayPurchase]:
    return session.exec(
        select(GooglePlayPurchase).where(GooglePlayPurchase.purchase_token == purchase_token)
    ).first()


def _get_or_create_google_provider_subscription(session: Session, user: User) -> ProviderSubscription:
    sub = session.exec(
        select(ProviderSubscription).where(
            ProviderSubscription.user_id == user.id,
            ProviderSubscription.provider == "google_play",
        )
    ).first()
    if sub is None:
        sub = ProviderSubscription(user_id=user.id, provider="google_play", status="none")
        session.add(sub)
        session.commit()
        session.refresh(sub)
    return sub


def _status_of_purchase(purchase: GooglePlayPurchase) -> str:
    forced = "revoked" if purchase.revoked_or_refunded_at is not None else None
    return normalize_google_status(purchase.subscription_state, purchase.expiry_time, forced_status=forced)


def _refresh_provider_subscription_facade(session: Session, provider_sub: ProviderSubscription) -> None:
    """ProviderSubscription(provider='google_play') reste UNE ligne par
    utilisateur (UNIQUE(user_id, provider) inchangée) même si plusieurs
    GooglePlayPurchase existent pour cet emplacement (réabonnement,
    changement de plan) — elle reflète toujours l'achat le plus pertinent :
    en priorité un achat actif (le plus tardif si plusieurs), sinon l'achat
    dont l'expiration est la plus récente."""
    purchases = session.exec(
        select(GooglePlayPurchase).where(GooglePlayPurchase.provider_subscription_id == provider_sub.id)
    ).all()
    if not purchases:
        return

    scored = [(p, _status_of_purchase(p)) for p in purchases]
    active = [p for p, s in scored if s == "active"]
    if active:
        chosen = max(active, key=lambda p: p.expiry_time)
        chosen_status = "active"
    else:
        chosen, chosen_status = max(scored, key=lambda ps: ps[0].expiry_time)

    provider_sub.status = chosen_status
    provider_sub.raw_status = chosen.subscription_state
    provider_sub.external_ref = chosen.purchase_token
    provider_sub.plan = _resolve_plan(chosen.product_id, chosen.base_plan_id)
    provider_sub.product_id = chosen.product_id
    provider_sub.current_period_end = chosen.expiry_time
    provider_sub.auto_renewing = chosen.auto_renewing
    provider_sub.canceled_at = chosen.canceled_at
    provider_sub.revoked_or_refunded_at = chosen.revoked_or_refunded_at
    provider_sub.updated_at = datetime.now(timezone.utc)
    session.add(provider_sub)
    session.commit()


def _apply_google_purchase(
    session: Session,
    *,
    user_id: int,
    purchase_token: str,
    product_id: str,
    base_plan_id: Optional[str],
    order_id: Optional[str],
    package_name: str,
    subscription_state: str,
    acknowledgement_state: str,
    expiry_time: datetime,
    auto_renewing: bool,
    linked_purchase_token: Optional[str],
    raw_response_dict: dict,
    forced_status: Optional[str] = None,
    notification_type: Optional[str] = None,
) -> tuple[ProviderSubscription, GooglePlayPurchase]:
    user = session.get(User, user_id)
    provider_sub = _get_or_create_google_provider_subscription(session, user)

    purchase = session.exec(
        select(GooglePlayPurchase).where(GooglePlayPurchase.purchase_token == purchase_token)
    ).first()
    if purchase is None:
        purchase = GooglePlayPurchase(
            provider_subscription_id=provider_sub.id,
            user_id=user_id,
            purchase_token=purchase_token,
            product_id=product_id,
            package_name=package_name,
            subscription_state=subscription_state,
            expiry_time=expiry_time,
        )

    status = normalize_google_status(subscription_state, expiry_time, forced_status=forced_status)

    purchase.product_id = product_id
    purchase.base_plan_id = base_plan_id
    purchase.order_id = order_id
    purchase.package_name = package_name
    purchase.subscription_state = subscription_state
    purchase.acknowledgement_state = acknowledgement_state
    purchase.expiry_time = expiry_time
    purchase.auto_renewing = auto_renewing
    purchase.linked_purchase_token = linked_purchase_token
    if subscription_state == "SUBSCRIPTION_STATE_CANCELED" and purchase.canceled_at is None:
        purchase.canceled_at = datetime.now(timezone.utc)
    if status in ("revoked", "refunded") and purchase.revoked_or_refunded_at is None:
        purchase.revoked_or_refunded_at = datetime.now(timezone.utc)
    if notification_type is not None:
        purchase.latest_notification_type = notification_type
    purchase.raw_response = _strip_pii(raw_response_dict)
    purchase.updated_at = datetime.now(timezone.utc)
    session.add(purchase)
    session.commit()
    session.refresh(purchase)

    _refresh_provider_subscription_facade(session, provider_sub)
    session.refresh(provider_sub)
    recompute_entitlement(
        session, user_id, provider_subscription_id=provider_sub.id,
        reason=f"google_play: {notification_type or ('forced:' + forced_status if forced_status else 'verify')}",
    )
    return provider_sub, purchase


# ---------------------------------------------------------------------------
# Orchestration — les deux points d'entrée appelés par le routeur
# ---------------------------------------------------------------------------

def verify_and_sync_google_purchase(
    session: Session,
    user: User,
    *,
    product_id: str,
    purchase_token: str,
    package_name: Optional[str] = None,
) -> tuple[ProviderSubscription, GooglePlayPurchase, Entitlement]:
    """Orchestration complète pour POST /billing/google/verify. Idempotente :
    upsert par purchase_token (contrainte UNIQUE), un second appel identique
    resynchronise sans dupliquer ni recréer d'EntitlementEvent si `premium`
    ne change pas (recompute_entitlement, Phase 1, inchangé)."""
    if package_name is not None and package_name != GOOGLE_PLAY_PACKAGE_NAME:
        raise GooglePurchaseInvalid(f"package_name inattendu : '{package_name}'.", status_code=400)

    existing = _find_purchase_owner(session, purchase_token)
    if existing is not None and existing.user_id != user.id:
        raise GoogleTokenOwnershipConflict("purchaseToken déjà associé à un autre compte Xfoot.")

    raw = _fetch_google_subscription(GOOGLE_PLAY_PACKAGE_NAME, purchase_token)
    line_item = _extract_line_item(raw, product_id)
    real_product_id = line_item.get("productId")
    base_plan_id = (line_item.get("offerDetails") or {}).get("basePlanId")
    plan = _resolve_plan(real_product_id, base_plan_id)
    if plan is None:
        raise GooglePurchaseInvalid(
            f"Produit/Base Plan Google Play non reconnu : productId='{real_product_id}', "
            f"basePlanId='{base_plan_id}'.", status_code=400,
        )

    expiry_time = _parse_google_datetime(line_item.get("expiryTime"))
    auto_renewing_plan = line_item.get("autoRenewingPlan") or {}
    auto_renewing = bool(auto_renewing_plan.get("autoRenewEnabled", False))
    order_id = line_item.get("latestSuccessfulOrderId")
    subscription_state = raw.get("subscriptionState", "SUBSCRIPTION_STATE_UNSPECIFIED")
    acknowledgement_state = raw.get("acknowledgementState", "ACKNOWLEDGEMENT_STATE_PENDING")

    provider_sub, purchase = _apply_google_purchase(
        session, user_id=user.id, purchase_token=purchase_token, product_id=real_product_id,
        base_plan_id=base_plan_id, order_id=order_id, package_name=GOOGLE_PLAY_PACKAGE_NAME,
        subscription_state=subscription_state, acknowledgement_state=acknowledgement_state,
        expiry_time=expiry_time, auto_renewing=auto_renewing,
        linked_purchase_token=raw.get("linkedPurchaseToken"), raw_response_dict=raw,
    )

    if purchase.acknowledgement_state == "ACKNOWLEDGEMENT_STATE_PENDING":
        try:
            _acknowledge_google_purchase(GOOGLE_PLAY_PACKAGE_NAME, real_product_id, purchase_token)
            purchase.acknowledgement_state = "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED"
            purchase.updated_at = datetime.now(timezone.utc)
            session.add(purchase)
            session.commit()
            session.refresh(purchase)
        except GoogleApiError as exc:
            # Un échec d'acknowledgement ne retire JAMAIS le Premium déjà
            # accordé — l'utilisateur a payé, l'accès reste. L'achat reste
            # marqué PENDING pour reprise ultérieure.
            logger.error(
                "Google Play: acknowledgement de %s a échoué (%s) — achat conservé "
                "PENDING, Premium non affecté.", purchase_token, exc,
            )

    entitlement = session.get(Entitlement, user.id)
    return provider_sub, purchase, entitlement


def sync_known_google_purchase(
    session: Session,
    purchase_token: str,
    *,
    notification_type: Optional[str] = None,
    forced_status: Optional[str] = None,
) -> Optional[Entitlement]:
    """Orchestration pour POST /billing/google/rtdn. Pas d'utilisateur
    authentifié ici : résolution uniquement via un purchase_token DÉJÀ connu
    (créé au préalable par verify_and_sync_google_purchase). Token inconnu
    -> no-op (accusé de réception sans action, même philosophie que le Pulse
    Chariow reçu sans /billing/checkout préalable).

    Ne fait JAMAIS confiance aux champs de la notification RTDN elle-même
    pour le statut/l'expiration — uniquement à ce que renvoie un nouvel
    appel à purchases.subscriptionsv2.get. Le TYPE de notification, lui, est
    exploité pour `forced_status` quand il s'agit de SUBSCRIPTION_REVOKED
    (seul signal fiable de révocation, voir docstring module)."""
    existing = _find_purchase_owner(session, purchase_token)
    if existing is None:
        return None

    raw = _fetch_google_subscription(existing.package_name, purchase_token)
    line_item = _extract_line_item(raw, existing.product_id)
    expiry_time = _parse_google_datetime(line_item.get("expiryTime"))
    auto_renewing_plan = line_item.get("autoRenewingPlan") or {}
    auto_renewing = bool(auto_renewing_plan.get("autoRenewEnabled", False))
    base_plan_id = (line_item.get("offerDetails") or {}).get("basePlanId")
    order_id = line_item.get("latestSuccessfulOrderId")
    subscription_state = raw.get("subscriptionState", "SUBSCRIPTION_STATE_UNSPECIFIED")
    acknowledgement_state = raw.get("acknowledgementState", existing.acknowledgement_state)

    _apply_google_purchase(
        session, user_id=existing.user_id, purchase_token=purchase_token,
        product_id=line_item.get("productId", existing.product_id),
        base_plan_id=base_plan_id, order_id=order_id, package_name=existing.package_name,
        subscription_state=subscription_state, acknowledgement_state=acknowledgement_state,
        expiry_time=expiry_time, auto_renewing=auto_renewing,
        linked_purchase_token=raw.get("linkedPurchaseToken"), raw_response_dict=raw,
        forced_status=forced_status, notification_type=notification_type,
    )
    return session.get(Entitlement, existing.user_id)


# ---------------------------------------------------------------------------
# RTDN — décodage de l'enveloppe Pub/Sub + vérification d'authenticité
# ---------------------------------------------------------------------------

def decode_rtdn_envelope(body: dict) -> dict:
    """Décode le message Pub/Sub RTDN — structure confirmée contre la doc
    officielle (developer.android.com/google/play/billing/rtdn-reference) :
    {"message": {"data": "<base64>", "messageId": ...}, "subscription": ...}."""
    message = body.get("message") or {}
    data_b64 = message.get("data")
    if not data_b64:
        raise GooglePurchaseInvalid("Message RTDN sans champ message.data.", status_code=400)
    try:
        decoded = base64.b64decode(data_b64)
        return json.loads(decoded)
    except (ValueError, json.JSONDecodeError) as exc:
        raise GooglePurchaseInvalid(f"Message RTDN illisible : {exc}", status_code=400) from exc


def verify_rtdn_shared_secret(provided: Optional[str]) -> bool:
    """Protection intérimaire de /billing/google/rtdn tant qu'aucun projet
    Google Cloud Pub/Sub réel n'est configuré (consigne explicite de cette
    phase). GOOGLE_RTDN_SHARED_SECRET vide -> toujours refusé (jamais
    d'endpoint ouvert par défaut)."""
    if not GOOGLE_RTDN_SHARED_SECRET:
        return False
    return provided is not None and hmac.compare_digest(provided, GOOGLE_RTDN_SHARED_SECRET)


def verify_rtdn_oidc_token(authorization_header: Optional[str], expected_audience: str) -> bool:
    """Vérification définitive une fois Pub/Sub réellement configuré avec
    authentification (google.oauth2.id_token.verify_oauth2_token, confirmé
    contre la doc officielle Cloud Pub/Sub "Authentication for push
    subscriptions"). Isolée dans sa propre fonction : non testable contre un
    vrai jeton sans projet Google Cloud réel (hors périmètre de cette
    phase) — testée uniquement par mock de cette fonction."""
    if not authorization_header or not authorization_header.startswith("Bearer "):
        return False
    token = authorization_header[len("Bearer "):]
    try:
        from google.oauth2 import id_token as google_id_token
        import google.auth.transport.requests

        claims = google_id_token.verify_oauth2_token(
            token, google.auth.transport.requests.Request(), audience=expected_audience
        )
        return bool(claims)
    except Exception:
        return False
