"""
test_billing.py — Tests du système de facturation Stripe via TestClient +
unittest.mock (aucun appel réseau réel vers Stripe, pas de vraies clés
nécessaires).

La vérification de signature webhook n'est PAS mockée : les payloads de
test sont signés à la main avec le schéma HMAC documenté par Stripe
(https://stripe.com/docs/webhooks/signatures#verify-manually) et le même
STRIPE_WEBHOOK_SECRET de test que l'application utilise — si le code de
vérification dans router.py était cassé, ces tests échoueraient pour de
vraies raisons cryptographiques, pas parce qu'on aurait mocké la vérité.

Usage : python api/test_billing.py
"""

import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

TEST_DB_PATH = Path(__file__).parent / "test_billing.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"
os.environ["JWT_SECRET_KEY"] = "test-only-secret-key-not-for-production-use"
os.environ["STRIPE_SECRET_KEY"] = "sk_test_dummy_never_used_network_is_mocked"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test_secret_for_signature_verification"
os.environ["STRIPE_PRICE_ID_MONTHLY"] = "price_test_monthly"
os.environ["STRIPE_PRICE_ID_YEARLY"] = "price_test_yearly"

if TEST_DB_PATH.exists():
    TEST_DB_PATH.unlink()

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from main import app
from app.core.database import engine
from app.models.subscription import Subscription

WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
EMAIL = "bob@example.com"
PASSWORD = "correct-horse-battery-staple"


def sign_payload(payload_bytes: bytes, secret: str, timestamp: int | None = None) -> str:
    """Construit une signature webhook Stripe réelle, selon le schéma
    documenté (t=<timestamp>,v1=<hmac_sha256_hex>) — pas d'API privée du
    SDK, pas de mock : c'est exactement ce que fait un vrai serveur Stripe."""
    timestamp = timestamp or int(time.time())
    signed_payload = f"{timestamp}.".encode() + payload_bytes
    signature = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={signature}"


def make_event(event_type: str, obj: dict) -> bytes:
    event = {
        "id": "evt_test_123",
        "object": "event",
        "type": event_type,
        "data": {"object": obj},
    }
    return json.dumps(event).encode()


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
    print(f"  [OK] statut avant paiement -> status='none', is_active=False")


def test_checkout_session_creation(client, token):
    fake_customer = MagicMock(id="cus_test_123")
    fake_checkout_session = MagicMock(url="https://checkout.stripe.com/test-session-xyz")

    with patch("app.billing.router.stripe.Customer.create", return_value=fake_customer) as m_customer, \
         patch("app.billing.router.stripe.checkout.Session.create", return_value=fake_checkout_session) as m_checkout:
        r = client.post("/billing/checkout", json={"plan": "monthly"},
                         headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["checkout_url"] == "https://checkout.stripe.com/test-session-xyz"
    m_customer.assert_called_once()
    m_checkout.assert_called_once()
    print(f"  [OK] création session checkout (Customer.create + checkout.Session.create mockés) "
          f"-> checkout_url={body['checkout_url']}")


def test_checkout_unknown_plan_400(client, token):
    r = client.post("/billing/checkout", json={"plan": "biannual"},
                     headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400, r.text
    print(f"  [OK] plan inconnu -> 400, detail: {r.json()['detail']}")


def test_portal_session_creation(client, token):
    fake_portal_session = MagicMock(url="https://billing.stripe.com/test-portal-abc")
    with patch("app.billing.router.stripe.billing_portal.Session.create", return_value=fake_portal_session) as m_portal:
        r = client.get("/billing/subscription", headers={"Authorization": f"Bearer {token}"})  # s'assurer que le customer existe déjà (créé au test précédent)
        r = client.post("/billing/portal", headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 200, r.text
    assert r.json()["portal_url"] == "https://billing.stripe.com/test-portal-abc"
    m_portal.assert_called_once()
    print(f"  [OK] portail client (mocké) -> portal_url={r.json()['portal_url']}")


def test_webhook_checkout_completed(client, user_id):
    obj = {
        "id": "cs_test_123",
        "customer": "cus_test_123",
        "subscription": "sub_test_456",
        "metadata": {"user_id": str(user_id), "plan": "monthly"},
    }
    payload = make_event("checkout.session.completed", obj)
    sig = sign_payload(payload, WEBHOOK_SECRET)

    r = client.post("/billing/webhook", content=payload,
                     headers={"stripe-signature": sig, "content-type": "application/json"})
    assert r.status_code == 200, r.text

    sub = get_subscription_row(user_id)
    assert sub is not None
    assert sub.status == "active"
    assert sub.stripe_subscription_id == "sub_test_456"
    assert sub.plan == "monthly"
    print(f"  [OK] webhook checkout.session.completed (signature réelle) -> status='active', "
          f"stripe_subscription_id={sub.stripe_subscription_id}")


def test_webhook_subscription_updated(client, user_id):
    new_period_end = int(time.time()) + 30 * 24 * 3600
    obj = {
        "id": "sub_test_456",
        "status": "active",
        "current_period_end": new_period_end,
    }
    payload = make_event("customer.subscription.updated", obj)
    sig = sign_payload(payload, WEBHOOK_SECRET)

    r = client.post("/billing/webhook", content=payload,
                     headers={"stripe-signature": sig, "content-type": "application/json"})
    assert r.status_code == 200, r.text

    sub = get_subscription_row(user_id)
    assert sub.current_period_end is not None
    assert int(sub.current_period_end.timestamp()) == new_period_end
    print(f"  [OK] webhook customer.subscription.updated -> current_period_end mis à jour "
          f"({sub.current_period_end.isoformat()})")


def test_webhook_subscription_deleted(client, user_id):
    obj = {"id": "sub_test_456", "status": "canceled"}
    payload = make_event("customer.subscription.deleted", obj)
    sig = sign_payload(payload, WEBHOOK_SECRET)

    r = client.post("/billing/webhook", content=payload,
                     headers={"stripe-signature": sig, "content-type": "application/json"})
    assert r.status_code == 200, r.text

    sub = get_subscription_row(user_id)
    assert sub.status == "canceled"
    assert sub.is_active is False
    print(f"  [OK] webhook customer.subscription.deleted -> status='canceled', is_active=False")


def test_webhook_invalid_signature_400(client, user_id):
    obj = {"id": "sub_test_456", "status": "active"}
    payload = make_event("customer.subscription.updated", obj)

    r = client.post("/billing/webhook", content=payload,
                     headers={"stripe-signature": "t=1,v1=deadbeef" * 4, "content-type": "application/json"})
    assert r.status_code == 400, r.text

    sub = get_subscription_row(user_id)
    assert sub.status == "canceled", "l'événement à signature invalide n'aurait JAMAIS dû être traité"
    print(f"  [OK] signature invalide -> 400, statut resté inchangé ('canceled', événement non traité)")


if __name__ == "__main__":
    failures = 0
    with TestClient(app) as client:
        user_id, token = get_auth_token(client)

        steps = [
            ("test_subscription_status_before_any_payment", lambda: test_subscription_status_before_any_payment(client, token)),
            ("test_checkout_session_creation", lambda: test_checkout_session_creation(client, token)),
            ("test_checkout_unknown_plan_400", lambda: test_checkout_unknown_plan_400(client, token)),
            ("test_portal_session_creation", lambda: test_portal_session_creation(client, token)),
            ("test_webhook_checkout_completed", lambda: test_webhook_checkout_completed(client, user_id)),
            ("test_webhook_subscription_updated", lambda: test_webhook_subscription_updated(client, user_id)),
            ("test_webhook_subscription_deleted", lambda: test_webhook_subscription_deleted(client, user_id)),
            ("test_webhook_invalid_signature_400", lambda: test_webhook_invalid_signature_400(client, user_id)),
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
