"""
test_chariow_billing.py — Tests du système de facturation Chariow (licences)
via TestClient + unittest.mock (aucun appel réseau réel vers Chariow, pas de
vraies clés nécessaires).

La vérification de signature des Pulses n'est PAS mockée : les payloads de
test sont signés à la main avec un HMAC-SHA256 réel du corps brut (header
x-chariow-signature, format "sha256=<hex>") et le même CHARIOW_PULSE_SECRET
de test que l'application utilise — si le code de vérification dans
router.py était cassé, ces tests échoueraient pour de vraies raisons
cryptographiques, pas parce qu'on aurait mocké la vérité.

Forme des payloads Pulse confirmée via chariow.dev/en/guides/pulses (pas de
wrapper "data" ; successful.sale porte sale.custom_metadata mais ni clé de
licence ni date d'expiration ; license.activated porte license.key/expires_at
mais pas de custom_metadata — la liaison à un utilisateur s'y fait via
customer.email, cf. app/billing/router.py::_find_subscription_by_email).

Remplace l'ancien test_billing.py (Stripe) — remplacé par Chariow (audience
ouest-africaine, Mobile Money natif). Pas de test de portail client : Chariow
n'a pas d'équivalent pour les produits Licence (gérer/renouveler se fait
entièrement via /billing/checkout, cf. app/billing/router.py).

Usage : python api/test_chariow_billing.py
"""

import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

TEST_DB_PATH = Path(__file__).parent / "test_chariow_billing.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"
os.environ["JWT_SECRET_KEY"] = "test-only-secret-key-not-for-production-use"
os.environ["CHARIOW_API_KEY"] = "test_dummy_never_used_network_is_mocked"
os.environ["CHARIOW_PULSE_SECRET"] = "pulse_test_secret_for_signature_verification"
os.environ["CHARIOW_PRODUCT_ID_MONTHLY"] = "prod_test_monthly"
os.environ["CHARIOW_PRODUCT_ID_YEARLY"] = "prod_test_yearly"

if TEST_DB_PATH.exists():
    TEST_DB_PATH.unlink()

sys.path.insert(0, str(Path(__file__).parent))

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from main import app
from app.core.database import engine
from app.models.subscription import Subscription
from app.billing.router import _create_chariow_checkout_link, _fetch_chariow_license

PULSE_SECRET = os.environ["CHARIOW_PULSE_SECRET"]
EMAIL = "bob@example.com"
PASSWORD = "correct-horse-battery-staple"
CHECKOUT_BODY = {
    "plan": "monthly",
    "first_name": "Bob",
    "last_name": "Kouassi",
    "phone_number": "0700000000",
    "phone_country_code": "CI",
}


def sign_payload(payload_bytes: bytes, secret: str) -> str:
    """Signature Pulse réelle : "sha256=" + HMAC-SHA256 hex du corps brut —
    pas de mock, c'est exactement ce qu'un vrai serveur Chariow calculerait."""
    return "sha256=" + hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()


def make_event(event_type: str, **fields) -> bytes:
    return json.dumps({"event": event_type, **fields}).encode()


def post_pulse(client, event_type: str, delivery_id: str, secret: str = PULSE_SECRET, **fields):
    payload = make_event(event_type, **fields)
    sig = sign_payload(payload, secret)
    return client.post("/billing/pulse", content=payload, headers={
        "x-chariow-signature": sig, "x-pulse-delivery-id": delivery_id,
        "content-type": "application/json",
    })


def get_auth_token(client) -> tuple[int, str]:
    r = client.post("/auth/register", json={"email": EMAIL, "password": PASSWORD})
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]

    r = client.post("/auth/login", data={"username": EMAIL, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return user_id, r.json()["access_token"]


def get_subscription_row(user_id: int) -> Subscription | None:
    with Session(engine) as session:
        return session.exec(select(Subscription).where(Subscription.user_id == user_id)).first()


def test_subscription_status_before_any_payment(client, token):
    r = client.get("/billing/subscription", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "none"
    assert body["is_active"] is False
    assert body["days_until_expiry"] is None
    print(f"  [OK] statut avant paiement -> status='none', is_active=False, days_until_expiry=None")


def test_checkout_missing_customer_fields_422(client, token):
    r = client.post("/billing/checkout", json={"plan": "monthly"},
                     headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 422, r.text
    print(f"  [OK] checkout sans first_name/last_name/phone_number/phone_country_code -> 422")


def test_checkout_unknown_plan_400(client, token):
    body = {**CHECKOUT_BODY, "plan": "biannual"}
    r = client.post("/billing/checkout", json=body, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400, r.text
    print(f"  [OK] plan inconnu -> 400, detail: {r.json()['detail']}")


def test_checkout_session_creation(client, token):
    with patch("app.billing.router._create_chariow_checkout_link",
               return_value="https://chariow.com/checkout/test-link-xyz") as m_checkout:
        r = client.post("/billing/checkout", json=CHECKOUT_BODY,
                         headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["checkout_url"] == "https://chariow.com/checkout/test-link-xyz"
    m_checkout.assert_called_once()
    call_kwargs = m_checkout.call_args.kwargs
    assert call_kwargs["product_id"] == "prod_test_monthly"
    assert call_kwargs["first_name"] == "Bob"
    assert call_kwargs["phone_country_code"] == "CI"
    print(f"  [OK] création lien de checkout (_create_chariow_checkout_link mocké) "
          f"-> checkout_url={body['checkout_url']}")


# ---------------------------------------------------------------------------
# _create_chariow_checkout_link elle-même — test_checkout_session_creation
# ci-dessus mocke cette fonction en entier (au niveau de la route), donc
# n'exerce jamais son analyse de la réponse Chariow ni la forme du payload
# envoyé. Les tests suivants mockent httpx.post directement pour couvrir la
# vraie logique de parsing (les 3 valeurs de "step", la remontée d'erreur
# 401/404/422) et la forme réelle du payload envoyé.
# ---------------------------------------------------------------------------

CHECKOUT_LINK_KWARGS = dict(
    product_id="prod_test_monthly", email="a@b.com",
    first_name="A", last_name="B",
    phone_number="0700000000", phone_country_code="CI",
    metadata={"user_id": "1", "plan": "monthly"},
)


def test_create_checkout_link_sends_custom_metadata_key(client, token):
    """Régression : le premier essai envoyait 'metadata' au lieu de
    'custom_metadata' dans le payload — accepté silencieusement par Chariow
    (200 OK) mais jamais rattaché à la vente, donc jamais retrouvable dans
    le Pulse successful.sale. Découvert en testant contre la vraie API, pas
    par ce test — ajouté après coup pour ne plus jamais régresser dessus."""
    fake_response = MagicMock(status_code=200)
    fake_response.json.return_value = {
        "data": {"step": "payment", "payment": {"checkout_url": "https://chariow.com/pay/abc"}}
    }
    with patch("app.billing.router.httpx.post", return_value=fake_response) as m_post:
        _create_chariow_checkout_link(**CHECKOUT_LINK_KWARGS)
    sent_json = m_post.call_args.kwargs["json"]
    assert "custom_metadata" in sent_json, "le payload doit utiliser 'custom_metadata', pas 'metadata'"
    assert "metadata" not in sent_json
    assert sent_json["custom_metadata"] == {"user_id": "1", "plan": "monthly"}
    print(f"  [OK] payload envoyé à Chariow utilise bien 'custom_metadata' (pas 'metadata')")


def test_create_checkout_link_step_payment_returns_url(client, token):
    fake_response = MagicMock(status_code=200)
    fake_response.json.return_value = {
        "data": {"step": "payment", "payment": {"checkout_url": "https://chariow.com/pay/abc"}}
    }
    with patch("app.billing.router.httpx.post", return_value=fake_response):
        url = _create_chariow_checkout_link(**CHECKOUT_LINK_KWARGS)
    assert url == "https://chariow.com/pay/abc"
    print(f"  [OK] step='payment' -> checkout_url lu dans data.payment.checkout_url")


def test_create_checkout_link_step_already_purchased_logs_warning(client, token):
    fake_response = MagicMock(status_code=200)
    fake_response.json.return_value = {
        "data": {"step": "already_purchased", "payment": {"checkout_url": "https://chariow.com/pay/xyz"}}
    }
    with patch("app.billing.router.httpx.post", return_value=fake_response), \
         patch("app.billing.router.logger.warning") as m_warn:
        url = _create_chariow_checkout_link(**CHECKOUT_LINK_KWARGS)
    assert url == "https://chariow.com/pay/xyz"
    m_warn.assert_called_once()
    print(f"  [OK] step='already_purchased' -> warning loggé (comportement Chariow inattendu pour une Licence)")


def test_create_checkout_link_step_completed_returns_url(client, token):
    fake_response = MagicMock(status_code=200)
    fake_response.json.return_value = {
        "data": {"step": "completed", "payment": {"checkout_url": "https://chariow.com/pay/done"}}
    }
    with patch("app.billing.router.httpx.post", return_value=fake_response), \
         patch("app.billing.router.logger.warning") as m_warn:
        url = _create_chariow_checkout_link(**CHECKOUT_LINK_KWARGS)
    assert url == "https://chariow.com/pay/done"
    m_warn.assert_not_called()
    print(f"  [OK] step='completed' -> pas d'erreur, pas de warning (cas documenté comme improbable)")


def test_create_checkout_link_422_includes_errors_detail(client, token):
    fake_response = MagicMock(status_code=422)
    fake_response.json.return_value = {
        "message": "Validation échouée",
        "errors": {"phone_number": ["format invalide"]},
    }
    with patch("app.billing.router.httpx.post", return_value=fake_response):
        try:
            _create_chariow_checkout_link(**CHECKOUT_LINK_KWARGS)
            assert False, "aurait dû lever HTTPException"
        except HTTPException as exc:
            assert exc.status_code == 400
            assert "Validation échouée" in exc.detail
            assert "format invalide" in exc.detail
    print(f"  [OK] 422 -> 400 avec message + détail 'errors' remontés")


def test_create_checkout_link_401_maps_to_502(client, token):
    fake_response = MagicMock(status_code=401)
    fake_response.json.return_value = {"message": "Clé API invalide"}
    with patch("app.billing.router.httpx.post", return_value=fake_response):
        try:
            _create_chariow_checkout_link(**CHECKOUT_LINK_KWARGS)
            assert False, "aurait dû lever HTTPException"
        except HTTPException as exc:
            assert exc.status_code == 502
            assert "Clé API invalide" in exc.detail
    print(f"  [OK] 401 -> 502 (mauvaise config côté nous) avec message Chariow remonté")


def test_create_checkout_link_404_maps_to_502(client, token):
    fake_response = MagicMock(status_code=404)
    fake_response.json.return_value = {"message": "Produit introuvable"}
    with patch("app.billing.router.httpx.post", return_value=fake_response):
        try:
            _create_chariow_checkout_link(**CHECKOUT_LINK_KWARGS)
            assert False, "aurait dû lever HTTPException"
        except HTTPException as exc:
            assert exc.status_code == 502
            assert "Produit introuvable" in exc.detail
    print(f"  [OK] 404 -> 502 avec message Chariow remonté")


def test_pulse_successful_sale_activates(client, user_id):
    r = post_pulse(client, "successful.sale", "pulse_sale_1",
                    sale={"custom_metadata": {"user_id": str(user_id), "plan": "monthly"}},
                    customer={"email": EMAIL})
    assert r.status_code == 200, r.text

    sub = get_subscription_row(user_id)
    assert sub is not None
    assert sub.status == "active"
    assert sub.plan == "monthly"
    # Le sale ne porte ni clé de licence ni date d'expiration (elles arrivent
    # avec license.activated juste après) — l'accès est déjà débloqué (is_active
    # est True dès que current_period_end est None) mais ces deux champs restent vides.
    assert sub.chariow_license_key is None
    assert sub.current_period_end is None
    assert sub.is_active is True
    print(f"  [OK] Pulse successful.sale (signature réelle) -> status='active', is_active=True "
          f"(clé de licence/expiration pas encore connues, en attente de license.activated)")


def test_pulse_license_activated_updates_expiry(client, user_id):
    r = post_pulse(client, "license.activated", "pulse_activated_1",
                    license={"key": "lic_test_456", "expires_at": "2026-10-02T20:23:24+00:00"},
                    customer={"email": EMAIL})
    assert r.status_code == 200, r.text

    sub = get_subscription_row(user_id)
    assert sub.status == "active"
    assert sub.chariow_license_key == "lic_test_456"
    assert sub.current_period_end.isoformat().startswith("2026-10-02")
    print(f"  [OK] Pulse license.activated (complément, relié via customer.email) -> "
          f"chariow_license_key={sub.chariow_license_key}, expire le {sub.current_period_end.isoformat()}")


def test_pulse_nearing_expiry_sets_days_until_expiry(client, user_id, token):
    r = post_pulse(client, "license.nearing_expiry", "pulse_nearing_1",
                    customer={"email": EMAIL}, days_until_expiry=7)
    assert r.status_code == 200, r.text

    r_status = client.get("/billing/subscription", headers={"Authorization": f"Bearer {token}"})
    assert r_status.json()["days_until_expiry"] == 7
    print(f"  [OK] Pulse license.nearing_expiry (days_until_expiry=7) -> "
          f"GET /billing/subscription reflète days_until_expiry=7")


def test_manual_renewal_resets_days_until_expiry(client, user_id, token):
    """Simule un renouvellement manuel (re-achat via /billing/checkout, puis
    nouveaux Pulses successful.sale + license.activated) : le compte à
    rebours précédent doit être effacé dès le sale, la nouvelle expiration
    n'arrive qu'avec le license.activated qui suit — exactement comme un
    premier achat."""
    with patch("app.billing.router._create_chariow_checkout_link",
               return_value="https://chariow.com/checkout/renewal-link"):
        r = client.post("/billing/checkout", json=CHECKOUT_BODY,
                         headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text

    r = post_pulse(client, "successful.sale", "pulse_sale_renewal",
                    sale={"custom_metadata": {"user_id": str(user_id), "plan": "monthly"}},
                    customer={"email": EMAIL})
    assert r.status_code == 200, r.text

    r_status = client.get("/billing/subscription", headers={"Authorization": f"Bearer {token}"})
    assert r_status.json()["days_until_expiry"] is None
    print(f"  [OK] renouvellement (re-checkout + successful.sale) -> days_until_expiry effacé")

    r = post_pulse(client, "license.activated", "pulse_activated_renewal",
                    license={"key": "lic_test_456", "expires_at": "2026-11-02T20:23:24+00:00"},
                    customer={"email": EMAIL})
    assert r.status_code == 200, r.text

    r_status = client.get("/billing/subscription", headers={"Authorization": f"Bearer {token}"})
    body = r_status.json()
    assert body["current_period_end"].startswith("2026-11-02")
    print(f"  [OK] license.activated du renouvellement -> nouvelle expiration 2026-11-02")


def test_pulse_license_expired(client, user_id):
    r = post_pulse(client, "license.expired", "pulse_expired_1", customer={"email": EMAIL})
    assert r.status_code == 200, r.text

    sub = get_subscription_row(user_id)
    assert sub.status == "expired"
    assert sub.is_active is False
    print(f"  [OK] Pulse license.expired -> status='expired', is_active=False")


def test_pulse_license_revoked(client, user_id):
    r = post_pulse(client, "license.revoked", "pulse_revoked_1", customer={"email": EMAIL})
    assert r.status_code == 200, r.text

    sub = get_subscription_row(user_id)
    assert sub.status == "revoked"
    assert sub.is_active is False
    print(f"  [OK] Pulse license.revoked -> status='revoked', is_active=False")


def test_pulse_invalid_signature_401(client, user_id):
    payload = make_event("license.activated", license={"key": "lic_test_456"}, customer={"email": EMAIL})
    r = client.post("/billing/pulse", content=payload, headers={
        "x-chariow-signature": "sha256=" + "deadbeef" * 8, "x-pulse-delivery-id": "pulse_bad_sig",
        "content-type": "application/json",
    })
    assert r.status_code == 401, r.text

    sub = get_subscription_row(user_id)
    assert sub.status == "revoked", "l'événement à signature invalide n'aurait JAMAIS dû être traité"
    print(f"  [OK] signature invalide -> 401, statut resté inchangé ('revoked', événement non traité)")


def test_pulse_dedup_on_delivery_id(client, user_id):
    """Rejoue la delivery de l'étape successful.sale (même x-pulse-delivery-id
    que test_pulse_successful_sale_activates) après la révocation : la
    déduplication doit empêcher tout retraitement, le statut doit rester
    'revoked' et non repasser à 'active'."""
    r = post_pulse(client, "successful.sale", "pulse_sale_1",  # même delivery_id que la 1ère fois
                    sale={"custom_metadata": {"user_id": str(user_id), "plan": "monthly"}},
                    customer={"email": EMAIL})
    assert r.status_code == 200, r.text
    assert r.json().get("duplicate") is True

    sub = get_subscription_row(user_id)
    assert sub.status == "revoked", "la delivery dupliquée n'aurait JAMAIS dû être retraitée"
    print(f"  [OK] delivery rejouée (x-pulse-delivery-id déjà vu) -> ignorée, statut resté 'revoked'")


# ---------------------------------------------------------------------------
# POST /billing/activate-license — filet de secours si la redirection
# post-paiement échoue/tarde ou qu'un Pulse est manqué (voir
# app/billing/router.py::activate_license). Insérés en fin de chaîne pour ne
# pas perturber l'état séquentiel des tests Pulse ci-dessus (qui se terminent
# sur status='revoked', vérifié par test_pulse_dedup_on_delivery_id).
# ---------------------------------------------------------------------------

def _fake_license(*, user_id, plan="monthly", status="active", expires_at="2027-01-01T00:00:00+00:00"):
    return {
        "license_key": "lic_manual_test",
        "status": status,
        "expires_at": expires_at,
        "metadata": {"user_id": str(user_id), "plan": plan},
    }


def _fake_license_no_metadata(*, customer_email, status="active",
                               expires_at="2027-01-01T00:00:00+00:00", product_id=None):
    """Simule une licence achetée AVANT la correction du bug metadata/
    custom_metadata (voir test_create_checkout_link_sends_custom_metadata_key)
    — metadata=None, pas juste user_id manquant. Découvert en diagnostiquant
    une vraie clé de licence en prod."""
    return {
        "license_key": "lic_legacy_test",
        "status": status,
        "expires_at": expires_at,
        "metadata": None,
        "customer": {"email": customer_email},
        "product": {"id": product_id} if product_id else {},
    }


def test_activate_license_active_matches_user(client, user_id, token):
    with patch("app.billing.router._fetch_chariow_license",
               return_value=_fake_license(user_id=user_id)) as m_fetch:
        r = client.post("/billing/activate-license", json={"license_key": "lic_manual_test"},
                         headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    m_fetch.assert_called_once_with("lic_manual_test")

    body = r.json()
    assert body["status"] == "active"
    assert body["is_active"] is True
    assert body["plan"] == "monthly"

    sub = get_subscription_row(user_id)
    assert sub.chariow_license_key == "lic_manual_test"
    assert sub.current_period_end.isoformat().startswith("2027-01-01")
    print(f"  [OK] clé active + metadata.user_id correspondant -> 200, abonnement activé "
          f"(chariow_license_key={sub.chariow_license_key})")


def test_activate_license_user_mismatch_403(client, token):
    """La clé appartient à un AUTRE utilisateur (metadata.user_id différent
    du current_user) — ne doit jamais activer sur la seule foi du texte
    collé par le client."""
    other_email = "eve@example.com"
    client.post("/auth/register", json={"email": other_email, "password": PASSWORD})
    r_login = client.post("/auth/login", data={"username": other_email, "password": PASSWORD})
    other_token = r_login.json()["access_token"]
    other_user_id = client.get("/auth/me", headers={"Authorization": f"Bearer {other_token}"}).json()["id"]

    with patch("app.billing.router._fetch_chariow_license",
               return_value=_fake_license(user_id=other_user_id)):
        r = client.post("/billing/activate-license", json={"license_key": "lic_someone_else"},
                         headers={"Authorization": f"Bearer {token}"})  # token du PREMIER utilisateur
    assert r.status_code == 403, r.text
    assert "associée à ton compte" in r.json()["detail"]
    print(f"  [OK] metadata.user_id d'un autre compte -> 403, rien activé sur simple confiance du texte collé")


def test_activate_license_expired_400(client, user_id, token):
    with patch("app.billing.router._fetch_chariow_license",
               return_value=_fake_license(user_id=user_id, status="expired")):
        r = client.post("/billing/activate-license", json={"license_key": "lic_manual_test"},
                         headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400, r.text
    assert "expiré" in r.json()["detail"]
    print(f"  [OK] licence status='expired' -> 400, message explicite (rien activé)")


def test_activate_license_revoked_400(client, user_id, token):
    with patch("app.billing.router._fetch_chariow_license",
               return_value=_fake_license(user_id=user_id, status="revoked")):
        r = client.post("/billing/activate-license", json={"license_key": "lic_manual_test"},
                         headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400, r.text
    assert "révoquée" in r.json()["detail"]
    print(f"  [OK] licence status='revoked' -> 400, message explicite (rien activé)")


def test_activate_license_not_found_404(client, token):
    fake_response = MagicMock(status_code=404)
    with patch("app.billing.router.httpx.get", return_value=fake_response):
        try:
            _fetch_chariow_license("cle_inexistante")
            assert False, "aurait dû lever HTTPException"
        except HTTPException as exc:
            assert exc.status_code == 404
    print(f"  [OK] _fetch_chariow_license : 404 Chariow -> HTTPException(404), pas une 500 générique")


def test_activate_license_chariow_401_maps_to_502(client, token):
    """Clé API Chariow invalide côté nous (401) — jamais la faute de la clé
    de licence collée par l'utilisateur, donc jamais une 500 opaque."""
    fake_response = MagicMock(status_code=401, headers={"content-type": "application/json"})
    fake_response.json.return_value = {"message": "Clé API invalide"}
    with patch("app.billing.router.httpx.get", return_value=fake_response):
        try:
            _fetch_chariow_license("lic_manual_test")
            assert False, "aurait dû lever HTTPException"
        except HTTPException as exc:
            assert exc.status_code == 502
            assert "Clé API invalide" in exc.detail
    print(f"  [OK] _fetch_chariow_license : 401 Chariow -> HTTPException(502), pas une 500 générique")


def test_activate_license_empty_api_key_returns_502_not_crash(client, token):
    """Régression : httpx refuse d'envoyer "Authorization: Bearer " avec une
    clé vide (httpx.LocalProtocolError, non catchée avant ce correctif) —
    découvert en testant manuellement sans CHARIOW_API_KEY chargée. Doit
    renvoyer une 502 exploitable, jamais planter la requête."""
    with patch("app.billing.router.CHARIOW_API_KEY", ""):
        try:
            _fetch_chariow_license("lic_manual_test")
            assert False, "aurait dû lever HTTPException"
        except HTTPException as exc:
            assert exc.status_code == 502
            assert "CHARIOW_API_KEY" in exc.detail
    print(f"  [OK] CHARIOW_API_KEY vide -> HTTPException(502) propre, jamais httpx.LocalProtocolError non gérée")


def test_activate_license_email_fallback_when_no_metadata(client, user_id, token):
    """Licence sans metadata DU TOUT (achat pré-correctif) — repli sur
    customer.email, qui correspond au compte connecté (EMAIL)."""
    with patch("app.billing.router._fetch_chariow_license",
               return_value=_fake_license_no_metadata(customer_email=EMAIL)):
        r = client.post("/billing/activate-license", json={"license_key": "lic_legacy_test"},
                         headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    sub = get_subscription_row(user_id)
    assert sub.chariow_license_key == "lic_legacy_test"
    print(f"  [OK] metadata absente + customer.email correspondant -> 200, repli email accepté")


def test_activate_license_email_fallback_mismatch_403(client, token):
    """Licence sans metadata ET dont l'email Chariow ne correspond à aucun
    des deux comptes connus — 403, jamais activé sur une simple absence de
    contre-preuve."""
    with patch("app.billing.router._fetch_chariow_license",
               return_value=_fake_license_no_metadata(customer_email="quelquun.dautre@example.com")):
        r = client.post("/billing/activate-license", json={"license_key": "lic_legacy_test"},
                         headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403, r.text
    print(f"  [OK] metadata absente + customer.email différent -> 403, pas activé par défaut")


def test_activate_license_pending_activation_gets_activated(client, user_id, token):
    """Licence achetée mais jamais activée côté Chariow (produit sans
    requires_activation=false) — le backend l'active lui-même après avoir
    confirmé l'appartenance, plutôt que de bloquer un client qui a
    réellement payé."""
    pending = _fake_license(user_id=user_id, status="pending_activation", expires_at=None)
    activated = _fake_license(user_id=user_id, status="active", expires_at="2027-02-02T00:00:00+00:00")
    with patch("app.billing.router._fetch_chariow_license", return_value=pending), \
         patch("app.billing.router._activate_chariow_license", return_value=activated) as m_activate:
        r = client.post("/billing/activate-license", json={"license_key": "lic_manual_test"},
                         headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    m_activate.assert_called_once_with("lic_manual_test")
    sub = get_subscription_row(user_id)
    assert sub.status == "active"
    assert sub.current_period_end.isoformat().startswith("2027-02-02")
    print(f"  [OK] pending_activation -> le backend active lui-même la licence, abonnement activé "
          f"(expire le {sub.current_period_end.isoformat()})")


def test_activate_license_plan_inferred_from_product_id_when_metadata_missing(client, token):
    """Metadata absente -> le plan doit être déduit du product_id Chariow
    (comparé à PRODUCT_IDS) plutôt que laissé vide."""
    license_data = _fake_license_no_metadata(customer_email=EMAIL, product_id="prod_test_yearly")
    with patch("app.billing.router._fetch_chariow_license", return_value=license_data):
        r = client.post("/billing/activate-license", json={"license_key": "lic_legacy_test"},
                         headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json()["plan"] == "yearly", r.json()
    print(f"  [OK] metadata absente -> plan déduit du product_id ('yearly')")


if __name__ == "__main__":
    failures = 0
    with TestClient(app) as client:
        user_id, token = get_auth_token(client)

        steps = [
            ("test_subscription_status_before_any_payment", lambda: test_subscription_status_before_any_payment(client, token)),
            ("test_checkout_missing_customer_fields_422", lambda: test_checkout_missing_customer_fields_422(client, token)),
            ("test_checkout_unknown_plan_400", lambda: test_checkout_unknown_plan_400(client, token)),
            ("test_checkout_session_creation", lambda: test_checkout_session_creation(client, token)),
            ("test_create_checkout_link_sends_custom_metadata_key", lambda: test_create_checkout_link_sends_custom_metadata_key(client, token)),
            ("test_create_checkout_link_step_payment_returns_url", lambda: test_create_checkout_link_step_payment_returns_url(client, token)),
            ("test_create_checkout_link_step_already_purchased_logs_warning", lambda: test_create_checkout_link_step_already_purchased_logs_warning(client, token)),
            ("test_create_checkout_link_step_completed_returns_url", lambda: test_create_checkout_link_step_completed_returns_url(client, token)),
            ("test_create_checkout_link_422_includes_errors_detail", lambda: test_create_checkout_link_422_includes_errors_detail(client, token)),
            ("test_create_checkout_link_401_maps_to_502", lambda: test_create_checkout_link_401_maps_to_502(client, token)),
            ("test_create_checkout_link_404_maps_to_502", lambda: test_create_checkout_link_404_maps_to_502(client, token)),
            ("test_pulse_successful_sale_activates", lambda: test_pulse_successful_sale_activates(client, user_id)),
            ("test_pulse_license_activated_updates_expiry", lambda: test_pulse_license_activated_updates_expiry(client, user_id)),
            ("test_pulse_nearing_expiry_sets_days_until_expiry", lambda: test_pulse_nearing_expiry_sets_days_until_expiry(client, user_id, token)),
            ("test_manual_renewal_resets_days_until_expiry", lambda: test_manual_renewal_resets_days_until_expiry(client, user_id, token)),
            ("test_pulse_license_expired", lambda: test_pulse_license_expired(client, user_id)),
            ("test_pulse_license_revoked", lambda: test_pulse_license_revoked(client, user_id)),
            ("test_pulse_invalid_signature_401", lambda: test_pulse_invalid_signature_401(client, user_id)),
            ("test_pulse_dedup_on_delivery_id", lambda: test_pulse_dedup_on_delivery_id(client, user_id)),
            ("test_activate_license_active_matches_user", lambda: test_activate_license_active_matches_user(client, user_id, token)),
            ("test_activate_license_user_mismatch_403", lambda: test_activate_license_user_mismatch_403(client, token)),
            ("test_activate_license_expired_400", lambda: test_activate_license_expired_400(client, user_id, token)),
            ("test_activate_license_revoked_400", lambda: test_activate_license_revoked_400(client, user_id, token)),
            ("test_activate_license_not_found_404", lambda: test_activate_license_not_found_404(client, token)),
            ("test_activate_license_chariow_401_maps_to_502", lambda: test_activate_license_chariow_401_maps_to_502(client, token)),
            ("test_activate_license_empty_api_key_returns_502_not_crash", lambda: test_activate_license_empty_api_key_returns_502_not_crash(client, token)),
            ("test_activate_license_email_fallback_when_no_metadata", lambda: test_activate_license_email_fallback_when_no_metadata(client, user_id, token)),
            ("test_activate_license_email_fallback_mismatch_403", lambda: test_activate_license_email_fallback_mismatch_403(client, token)),
            ("test_activate_license_pending_activation_gets_activated", lambda: test_activate_license_pending_activation_gets_activated(client, user_id, token)),
            ("test_activate_license_plan_inferred_from_product_id_when_metadata_missing", lambda: test_activate_license_plan_inferred_from_product_id_when_metadata_missing(client, token)),
        ]
        for name, fn in steps:
            print(f"\n=== {name} ===")
            try:
                fn()
            except AssertionError as e:
                failures += 1
                print(f"  [ECHEC] {e}")
            except Exception as e:
                failures += 1
                print(f"  [ERREUR INATTENDUE] {type(e).__name__}: {e}")

    try:
        if TEST_DB_PATH.exists():
            TEST_DB_PATH.unlink()
    except PermissionError:
        pass  # verrou Windows bref sur le fichier SQLite — sans conséquence

    n_tests = len(steps)
    print(f"\n{'='*60}\n{n_tests - failures}/{n_tests} tests reussis\n{'='*60}")
    sys.exit(1 if failures else 0)
