"""
_test_support.py — Aides partagées entre les suites de tests API
(test_main.py, test_premium.py) pour l'authentification et l'activation
d'une licence Chariow de test, sans jamais appeler le vrai réseau Chariow.

Ne pas importer ce module APRÈS `main` — les variables d'environnement
(DATABASE_URL, JWT_SECRET_KEY, CHARIOW_*) doivent être positionnées avant le
premier import de `app.core.database` (le moteur SQLAlchemy est créé une
fois pour toutes au chargement du module). Chaque script de test appelant
`configure_test_env()` doit le faire tout en haut, avant `from main import app`.

Note sur les noms de paramètres : `webhook_secret` et `stripe_subscription_id`
sont conservés tels quels (plutôt que renommés en `pulse_secret`/`license_key`)
pour que test_main.py et test_premium.py, qui les passent en kwargs, n'aient
pas besoin d'être modifiés au-delà de ce qui est strictement nécessaire —
ils désignent maintenant respectivement le secret de signature des Pulses
et la clé de licence Chariow.
"""

import hashlib
import hmac
import json
import os
from pathlib import Path
from unittest.mock import patch


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
    os.environ["CHARIOW_API_KEY"] = "test_dummy_never_used_network_is_mocked"
    os.environ["CHARIOW_PULSE_SECRET"] = webhook_secret
    os.environ["CHARIOW_PRODUCT_ID_MONTHLY"] = "prod_test_monthly"
    os.environ["CHARIOW_PRODUCT_ID_YEARLY"] = "prod_test_yearly"

    if db_path.exists():
        db_path.unlink()
    return db_path


def cleanup_db(db_path: Path) -> None:
    try:
        if db_path.exists():
            db_path.unlink()
    except PermissionError:
        pass  # verrou Windows bref sur le fichier SQLite — sans conséquence


def sign_payload(payload_bytes: bytes, secret: str) -> str:
    """Signature Pulse Chariow réelle : HMAC-SHA256 hex du corps brut,
    envoyée dans le header x-pulse-signature — jamais mockée."""
    return hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()


def make_event(event_type: str, data: dict) -> bytes:
    event = {"event": event_type, "data": data}
    return json.dumps(event).encode()


def register_and_login(client, email: str, password: str) -> tuple[int, str]:
    r = client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]

    r = client.post("/auth/login", data={"username": email, "password": password})
    assert r.status_code == 200, r.text
    return user_id, r.json()["access_token"]


def activate_subscription(client, token: str, user_id: int, webhook_secret: str,
                           stripe_subscription_id: str = "lic_test_premium_001",
                           delivery_id: str = "pulse_test_activate") -> None:
    """
    Reproduit le VRAI flux d'activation, dans l'ordre : POST /billing/checkout
    (lien de checkout Chariow mocké — aucun réseau) crée d'abord
    l'enregistrement Subscription (status='none'), PUIS le Pulse
    successful.sale le fait passer à 'active'. Le Pulse seul, sans checkout
    préalable, ne suffit pas : aucun enregistrement Subscription où écrire
    (cf. _handle_successful_sale dans app/billing/router.py, qui retourne
    silencieusement si `sub is None`).
    """
    with patch("app.billing.router._create_chariow_checkout_link",
               return_value="https://chariow.com/checkout/test-link"):
        r = client.post("/billing/checkout", json={
            "plan": "monthly",
            "first_name": "Test", "last_name": "User",
            "phone_number": "0100000000", "phone_country_code": "+225",
        }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, f"checkout mocké échoué : {r.text}"

    data = {
        "license_key": stripe_subscription_id,
        "metadata": {"user_id": str(user_id), "plan": "monthly"},
    }
    payload = make_event("successful.sale", data)
    sig = sign_payload(payload, webhook_secret)
    r = client.post("/billing/pulse", content=payload,
                     headers={"x-pulse-signature": sig, "x-pulse-delivery-id": delivery_id,
                              "content-type": "application/json"})
    assert r.status_code == 200, f"Pulse successful.sale échoué : {r.text}"


def cancel_subscription(client, stripe_subscription_id: str, webhook_secret: str,
                         delivery_id: str = "pulse_test_revoke") -> None:
    """Envoie un Pulse license.revoked réel (signature valide)."""
    data = {"license_key": stripe_subscription_id}
    payload = make_event("license.revoked", data)
    sig = sign_payload(payload, webhook_secret)
    r = client.post("/billing/pulse", content=payload,
                     headers={"x-pulse-signature": sig, "x-pulse-delivery-id": delivery_id,
                              "content-type": "application/json"})
    assert r.status_code == 200, f"Pulse license.revoked échoué : {r.text}"
