"""
test_premium.py — Tests de la protection des endpoints de prédiction par
require_active_subscription (app/billing/dependencies.py).

Scénario, dans l'ordre (chaque étape dépend de l'état laissé par la
précédente) :
  1. Aucune authentification du tout               -> 401 (avant même le
     check d'abonnement — get_current_user échoue en premier)
  2. Authentifié, mais AUCUN abonnement             -> 402
  3. /ratings/{league} reste public, même sans abonnement -> 200
  4. POST /billing/checkout (mocké) PUIS Pulse successful.sale (dans CET
     ordre — un Pulse seul ne suffit pas : sans checkout préalable, aucun
     enregistrement Subscription n'existe encore pour que le Pulse puisse
     le faire passer à 'active', cf. _handle_successful_sale dans
     app/billing/router.py qui retourne silencieusement si `sub is None`)
  5. Après ces deux étapes -> prédiction accessible -> 200
  6. Pulse license.revoked                          -> retour à 402
  7. Le endpoint batch suit la même logique (401/402/200)

Note : require_active_subscription et sa logique (401/402, dépendance
inchangée entre Stripe et Chariow) ne sont PAS le sujet de la migration —
seule la façon de forger un événement brut ci-dessous (format Pulse,
header x-pulse-signature, endpoint /billing/pulse) a dû être adaptée.

Usage : python api/test_premium.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import (
    configure_test_env, cleanup_db, sign_payload, make_event,
    register_and_login, activate_subscription, cancel_subscription,
)

WEBHOOK_SECRET = "pulse_test_secret_for_signature_verification"
DB_PATH = configure_test_env("test_premium.db", webhook_secret=WEBHOOK_SECRET)

from fastapi.testclient import TestClient
from main import app

EMAIL = "carla@example.com"
PASSWORD = "correct-horse-battery-staple"
LICENSE_KEY = "lic_test_premium_001"

BATCH_PAYLOAD = [{"league": "Ligue1", "home_team": "PSG", "away_team": "Marseille"}]


def test_prediction_no_auth_at_all_401(client):
    r = client.get("/predictions/Ligue1/PSG/Marseille")
    assert r.status_code == 401, r.text
    print(f"  [OK] prédiction sans authentification du tout -> 401 (avant même le check d'abonnement)")

    r_batch = client.post("/predictions/batch", json=BATCH_PAYLOAD)
    assert r_batch.status_code == 401, r_batch.text
    print(f"  [OK] batch sans authentification du tout -> 401")


def test_prediction_authenticated_no_subscription_402(client, token):
    headers = {"Authorization": f"Bearer {token}"}
    r = client.get("/predictions/Ligue1/PSG/Marseille", headers=headers)
    assert r.status_code == 402, r.text
    print(f"  [OK] authentifié mais SANS abonnement -> 402, detail: {r.json()['detail']}")

    r_batch = client.post("/predictions/batch", json=BATCH_PAYLOAD, headers=headers)
    assert r_batch.status_code == 402, r_batch.text
    print(f"  [OK] batch authentifié mais SANS abonnement -> 402")


def test_ratings_public_even_without_subscription(client):
    r = client.get("/ratings/Ligue1")
    assert r.status_code == 200, r.text
    print(f"  [OK] /ratings/Ligue1 reste accessible sans abonnement -> 200 ({len(r.json())} équipes)")


def test_pulse_alone_without_prior_checkout_does_not_activate(client, user_id, token):
    """Vérifie explicitement la mise en garde du scénario : envoyer le
    Pulse successful.sale SANS avoir appelé /billing/checkout au préalable
    ne doit PAS activer l'abonnement (aucune Subscription à mettre à
    jour), donc la prédiction doit rester en 402."""
    data = {
        "license_key": "lic_test_premature",
        "metadata": {"user_id": str(user_id), "plan": "monthly"},
    }
    payload = make_event("successful.sale", data)
    sig = sign_payload(payload, WEBHOOK_SECRET)
    r = client.post("/billing/pulse", content=payload,
                     headers={"x-pulse-signature": sig, "x-pulse-delivery-id": "pulse_premature",
                              "content-type": "application/json"})
    assert r.status_code == 200, r.text  # le Pulse répond 200 (accusé de réception), mais n'active rien

    r_pred = client.get("/predictions/Ligue1/PSG/Marseille", headers={"Authorization": f"Bearer {token}"})
    assert r_pred.status_code == 402, (
        f"le Pulse seul n'aurait PAS dû activer l'abonnement (pas de /billing/checkout préalable) : {r_pred.text}"
    )
    print(f"  [OK] Pulse successful.sale SANS /billing/checkout préalable -> "
          f"n'active rien, prédiction toujours 402")


def test_checkout_then_pulse_activates_prediction_200(client, token, user_id):
    activate_subscription(client, token, user_id, webhook_secret=WEBHOOK_SECRET,
                           stripe_subscription_id=LICENSE_KEY)
    print(f"  [OK] /billing/checkout (mocké) PUIS Pulse successful.sale -> abonnement activé")

    headers = {"Authorization": f"Bearer {token}"}
    r = client.get("/predictions/Ligue1/PSG/Marseille", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["home_team"] == "Paris SG"
    print(f"  [OK] prédiction accessible après abonnement actif -> 200, home_win={body['home_win']}")

    r_batch = client.post("/predictions/batch", json=BATCH_PAYLOAD, headers=headers)
    assert r_batch.status_code == 200, r_batch.text
    assert r_batch.json()[0]["ok"] is True
    print(f"  [OK] batch accessible après abonnement actif -> 200")


def test_subscription_revoked_reverts_to_402(client, token):
    cancel_subscription(client, LICENSE_KEY, webhook_secret=WEBHOOK_SECRET)
    print(f"  [OK] Pulse license.revoked envoyé")

    headers = {"Authorization": f"Bearer {token}"}
    r = client.get("/predictions/Ligue1/PSG/Marseille", headers=headers)
    assert r.status_code == 402, r.text
    print(f"  [OK] après révocation -> retour à 402")

    r_batch = client.post("/predictions/batch", json=BATCH_PAYLOAD, headers=headers)
    assert r_batch.status_code == 402, r_batch.text
    print(f"  [OK] batch après révocation -> retour à 402")


if __name__ == "__main__":
    failures = 0
    with TestClient(app) as client:
        user_id, token = register_and_login(client, EMAIL, PASSWORD)

        steps = [
            ("test_prediction_no_auth_at_all_401", lambda: test_prediction_no_auth_at_all_401(client)),
            ("test_prediction_authenticated_no_subscription_402", lambda: test_prediction_authenticated_no_subscription_402(client, token)),
            ("test_ratings_public_even_without_subscription", lambda: test_ratings_public_even_without_subscription(client)),
            ("test_pulse_alone_without_prior_checkout_does_not_activate", lambda: test_pulse_alone_without_prior_checkout_does_not_activate(client, user_id, token)),
            ("test_checkout_then_pulse_activates_prediction_200", lambda: test_checkout_then_pulse_activates_prediction_200(client, token, user_id)),
            ("test_subscription_revoked_reverts_to_402", lambda: test_subscription_revoked_reverts_to_402(client, token)),
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

    cleanup_db(DB_PATH)
    n_tests = len(steps)
    print(f"\n{'='*60}\n{n_tests - failures}/{n_tests} tests reussis\n{'='*60}")
    sys.exit(1 if failures else 0)
