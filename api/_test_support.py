"""
_test_support.py — Aides partagées entre les suites de tests API
(test_main.py, test_premium.py) pour l'authentification et l'activation
d'un abonnement de test, sans jamais appeler le vrai réseau Stripe.

Ne pas importer ce module APRÈS `main` — les variables d'environnement
(DATABASE_URL, JWT_SECRET_KEY, STRIPE_*) doivent être positionnées avant le
premier import de `app.core.database` (le moteur SQLAlchemy est créé une
fois pour toutes au chargement du module). Chaque script de test appelant
`configure_test_env()` doit le faire tout en haut, avant `from main import app`.
"""

import hashlib
import hmac
import json
import os
import time
from pathlib import Path


def configure_test_env(db_name: str, webhook_secret: str = "whsec_test_secret_for_signature_verification") -> Path:
    """
    Positionne les variables d'environnement pour une base SQLite ISOLÉE
    (jamais api/app.db) et des clés de test. À appeler avant tout import
    de `main`/`app.core.database`. Retourne le chemin du fichier DB (pour
    nettoyage en fin d'exécution).
    """
    db_path = Path(__file__).parent / db_name
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["JWT_SECRET_KEY"] = "test-only-secret-key-not-for-production-use"
    os.environ["STRIPE_SECRET_KEY"] = "sk_test_dummy_never_used_network_is_mocked"
    os.environ["STRIPE_WEBHOOK_SECRET"] = webhook_secret
    os.environ["STRIPE_PRICE_ID_MONTHLY"] = "price_test_monthly"
    os.environ["STRIPE_PRICE_ID_YEARLY"] = "price_test_yearly"

    if db_path.exists():
        db_path.unlink()
    return db_path


def cleanup_db(db_path: Path) -> None:
    try:
        if db_path.exists():
            db_path.unlink()
    except PermissionError:
        pass  # verrou Windows bref sur le fichier SQLite — sans conséquence


def sign_payload(payload_bytes: bytes, secret: str, timestamp: int | None = None) -> str:
    """Signature webhook Stripe réelle (schéma documenté t=...,v1=... —
    https://stripe.com/docs/webhooks/signatures#verify-manually), jamais mockée."""
    timestamp = timestamp or int(time.time())
    signed_payload = f"{timestamp}.".encode() + payload_bytes
    signature = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={signature}"


def make_event(event_type: str, obj: dict) -> bytes:
    event = {"id": "evt_test_123", "object": "event", "type": event_type, "data": {"object": obj}}
    return json.dumps(event).encode()


def register_and_login(client, email: str, password: str) -> tuple[int, str]:
    r = client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]

    r = client.post("/auth/login", data={"username": email, "password": password})
    assert r.status_code == 200, r.text
    return user_id, r.json()["access_token"]


def activate_subscription(client, token: str, user_id: int, webhook_secret: str,
                           stripe_subscription_id: str = "sub_test_premium_001") -> None:
    """
    Reproduit le VRAI flux d'activation, dans l'ordre : POST /billing/checkout
    (Customer.create + checkout.Session.create mockés — aucun réseau) crée
    d'abord l'enregistrement Subscription (status='none') lié au customer,
    PUIS le webhook checkout.session.completed le fait passer à 'active'.
    Le webhook seul, sans checkout préalable, ne suffit pas : il n'y aurait
    aucun enregistrement Subscription où écrire (cf. _handle_checkout_completed
    dans app/billing/router.py, qui retourne silencieusement si `sub is None`).
    """
    from unittest.mock import patch, MagicMock

    fake_customer = MagicMock(id=f"cus_test_{user_id}")
    fake_checkout_session = MagicMock(url="https://checkout.stripe.com/test-session")
    with patch("app.billing.router.stripe.Customer.create", return_value=fake_customer), \
         patch("app.billing.router.stripe.checkout.Session.create", return_value=fake_checkout_session):
        r = client.post("/billing/checkout", json={"plan": "monthly"},
                         headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, f"checkout mocké échoué : {r.text}"

    obj = {
        "id": "cs_test_activate",
        "customer": fake_customer.id,
        "subscription": stripe_subscription_id,
        "metadata": {"user_id": str(user_id), "plan": "monthly"},
    }
    payload = make_event("checkout.session.completed", obj)
    sig = sign_payload(payload, webhook_secret)
    r = client.post("/billing/webhook", content=payload,
                     headers={"stripe-signature": sig, "content-type": "application/json"})
    assert r.status_code == 200, f"webhook checkout.session.completed échoué : {r.text}"


def cancel_subscription(client, stripe_subscription_id: str, webhook_secret: str) -> None:
    """Envoie un webhook customer.subscription.deleted réel (signature valide)."""
    obj = {"id": stripe_subscription_id, "status": "canceled"}
    payload = make_event("customer.subscription.deleted", obj)
    sig = sign_payload(payload, webhook_secret)
    r = client.post("/billing/webhook", content=payload,
                     headers={"stripe-signature": sig, "content-type": "application/json"})
    assert r.status_code == 200, f"webhook customer.subscription.deleted échoué : {r.text}"
