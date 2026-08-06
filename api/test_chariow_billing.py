"""
test_chariow_billing.py — Tests du système de facturation Chariow (licences)
via TestClient + unittest.mock (aucun appel réseau réel vers Chariow, pas de
vraies clés nécessaires).

La vérification de signature des Pulses n'est PAS mockée : les payloads de
test sont signés à la main avec un HMAC-SHA256 réel du corps brut (header
x-pulse-signature) et le même CHARIOW_PULSE_SECRET de test que l'application
utilise — si le code de vérification dans router.py était cassé, ces tests
échoueraient pour de vraies raisons cryptographiques, pas parce qu'on aurait
mocké la vérité.

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
from app.billing.router import _create_chariow_checkout_link

PULSE_SECRET = os.environ["CHARIOW_PULSE_SECRET"]
EMAIL = "bob@example.com"
PASSWORD = "correct-horse-battery-staple"
CHECKOUT_BODY = {
    "plan": "monthly",
    "first_name": "Bob",
    "last_name": "Kouassi",
    "phone_number": "0700000000",
    "phone_country_code": "+225",
}


def sign_payload(payload_bytes: bytes, secret: str) -> str:
    """Signature Pulse réelle : HMAC-SHA256 hex du corps brut — pas de mock,
    c'est exactement ce qu'un vrai serveur Chariow calculerait."""
    return hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()


def make_event(event_type: str, data: dict) -> bytes:
    return json.dumps({"event": event_type, "data": data}).encode()


def post_pulse(client, event_type: str, data: dict, delivery_id: str, secret: str = PULSE_SECRET):
    payload = make_event(event_type, data)
    sig = sign_payload(payload, secret)
    return client.post("/billing/pulse", content=payload, headers={
        "x-pulse-signature": sig, "x-pulse-delivery-id": delivery_id,
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
    assert call_kwargs["phone_country_code"] == "+225"
    print(f"  [OK] création lien de checkout (_create_chariow_checkout_link mocké) "
          f"-> checkout_url={body['checkout_url']}")


# ---------------------------------------------------------------------------
# _create_chariow_checkout_link elle-même — test_checkout_session_creation
# ci-dessus mocke cette fonction en entier (au niveau de la route), donc
# n'exerce jamais son analyse de la réponse Chariow. Les tests suivants
# mockent httpx.post directement pour couvrir la vraie logique de parsing
# (les 3 valeurs de "step", et la remontée d'erreur 401/404/422).
# ---------------------------------------------------------------------------

CHECKOUT_LINK_KWARGS = dict(
    product_id="prod_test_monthly", email="a@b.com",
    first_name="A", last_name="B",
    phone_number="0700000000", phone_country_code="+225",
    metadata={"user_id": "1", "plan": "monthly"},
)


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
    data = {
        "license_key": "lic_test_456",
        "expires_at": "2026-09-02T20:23:24+00:00",
        "metadata": {"user_id": str(user_id), "plan": "monthly"},
    }
    r = post_pulse(client, "successful.sale", data, delivery_id="pulse_sale_1")
    assert r.status_code == 200, r.text

    sub = get_subscription_row(user_id)
    assert sub is not None
    assert sub.status == "active"
    assert sub.chariow_license_key == "lic_test_456"
    assert sub.plan == "monthly"
    assert sub.current_period_end is not None
    print(f"  [OK] Pulse successful.sale (signature réelle) -> status='active', "
          f"chariow_license_key={sub.chariow_license_key}, expire le {sub.current_period_end.isoformat()}")


def test_pulse_license_activated_updates_expiry(client, user_id):
    data = {"license_key": "lic_test_456", "expires_at": "2026-10-02T20:23:24+00:00"}
    r = post_pulse(client, "license.activated", data, delivery_id="pulse_activated_1")
    assert r.status_code == 200, r.text

    sub = get_subscription_row(user_id)
    assert sub.status == "active"
    assert sub.current_period_end.isoformat().startswith("2026-10-02")
    print(f"  [OK] Pulse license.activated (complément) -> current_period_end mis à jour "
          f"({sub.current_period_end.isoformat()})")


def test_pulse_nearing_expiry_sets_days_until_expiry(client, user_id, token):
    data = {"license_key": "lic_test_456", "days_until_expiry": 7}
    r = post_pulse(client, "license.nearing_expiry", data, delivery_id="pulse_nearing_1")
    assert r.status_code == 200, r.text

    r_status = client.get("/billing/subscription", headers={"Authorization": f"Bearer {token}"})
    assert r_status.json()["days_until_expiry"] == 7
    print(f"  [OK] Pulse license.nearing_expiry (days_until_expiry=7) -> "
          f"GET /billing/subscription reflète days_until_expiry=7")


def test_manual_renewal_resets_days_until_expiry(client, user_id, token):
    """Simule un renouvellement manuel (re-achat via /billing/checkout, puis
    nouveau Pulse successful.sale) : le compte à rebours précédent doit être
    effacé, il n'a plus cours après un nouvel achat."""
    with patch("app.billing.router._create_chariow_checkout_link",
               return_value="https://chariow.com/checkout/renewal-link"):
        r = client.post("/billing/checkout", json=CHECKOUT_BODY,
                         headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text

    data = {
        "license_key": "lic_test_456",
        "expires_at": "2026-11-02T20:23:24+00:00",
        "metadata": {"user_id": str(user_id), "plan": "monthly"},
    }
    r = post_pulse(client, "successful.sale", data, delivery_id="pulse_sale_renewal")
    assert r.status_code == 200, r.text

    r_status = client.get("/billing/subscription", headers={"Authorization": f"Bearer {token}"})
    body = r_status.json()
    assert body["days_until_expiry"] is None
    assert body["current_period_end"].startswith("2026-11-02")
    print(f"  [OK] renouvellement manuel (re-checkout + successful.sale) -> "
          f"days_until_expiry effacé, nouvelle expiration 2026-11-02")


def test_pulse_license_expired(client, user_id):
    data = {"license_key": "lic_test_456"}
    r = post_pulse(client, "license.expired", data, delivery_id="pulse_expired_1")
    assert r.status_code == 200, r.text

    sub = get_subscription_row(user_id)
    assert sub.status == "expired"
    assert sub.is_active is False
    print(f"  [OK] Pulse license.expired -> status='expired', is_active=False")


def test_pulse_license_revoked(client, user_id):
    data = {"license_key": "lic_test_456"}
    r = post_pulse(client, "license.revoked", data, delivery_id="pulse_revoked_1")
    assert r.status_code == 200, r.text

    sub = get_subscription_row(user_id)
    assert sub.status == "revoked"
    assert sub.is_active is False
    print(f"  [OK] Pulse license.revoked -> status='revoked', is_active=False")


def test_pulse_invalid_signature_400(client, user_id):
    data = {"license_key": "lic_test_456"}
    payload = make_event("license.activated", data)
    r = client.post("/billing/pulse", content=payload, headers={
        "x-pulse-signature": "deadbeef" * 8, "x-pulse-delivery-id": "pulse_bad_sig",
        "content-type": "application/json",
    })
    assert r.status_code == 400, r.text

    sub = get_subscription_row(user_id)
    assert sub.status == "revoked", "l'événement à signature invalide n'aurait JAMAIS dû être traité"
    print(f"  [OK] signature invalide -> 400, statut resté inchangé ('revoked', événement non traité)")


def test_pulse_dedup_on_delivery_id(client, user_id):
    """Rejoue la delivery de l'étape successful.sale (même x-pulse-delivery-id
    que test_pulse_successful_sale_activates) après la révocation : la
    déduplication doit empêcher tout retraitement, le statut doit rester
    'revoked' et non repasser à 'active'."""
    data = {
        "license_key": "lic_test_456",
        "expires_at": "2026-09-02T20:23:24+00:00",
        "metadata": {"user_id": str(user_id), "plan": "monthly"},
    }
    r = post_pulse(client, "successful.sale", data, delivery_id="pulse_sale_1")  # même delivery_id que la 1ère fois
    assert r.status_code == 200, r.text
    assert r.json().get("duplicate") is True

    sub = get_subscription_row(user_id)
    assert sub.status == "revoked", "la delivery dupliquée n'aurait JAMAIS dû être retraitée"
    print(f"  [OK] delivery rejouée (x-pulse-delivery-id déjà vu) -> ignorée, statut resté 'revoked'")


if __name__ == "__main__":
    failures = 0
    with TestClient(app) as client:
        user_id, token = get_auth_token(client)

        steps = [
            ("test_subscription_status_before_any_payment", lambda: test_subscription_status_before_any_payment(client, token)),
            ("test_checkout_missing_customer_fields_422", lambda: test_checkout_missing_customer_fields_422(client, token)),
            ("test_checkout_unknown_plan_400", lambda: test_checkout_unknown_plan_400(client, token)),
            ("test_checkout_session_creation", lambda: test_checkout_session_creation(client, token)),
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
            ("test_pulse_invalid_signature_400", lambda: test_pulse_invalid_signature_400(client, user_id)),
            ("test_pulse_dedup_on_delivery_id", lambda: test_pulse_dedup_on_delivery_id(client, user_id)),
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
