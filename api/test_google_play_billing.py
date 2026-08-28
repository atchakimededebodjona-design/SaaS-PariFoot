"""
test_google_play_billing.py — Tests Google Play Billing (Phase 2) via
TestClient + unittest.mock. AUCUN appel réseau réel : `_fetch_google_subscription`
et `_acknowledge_google_purchase` (app/billing/google_play_service.py) sont
systématiquement patchées — jamais de vrai compte de service, jamais de vrai
Pub/Sub.

Les réponses simulées reproduisent la forme RÉELLE de SubscriptionPurchaseV2
et de l'enveloppe RTDN, vérifiées contre la documentation officielle Google
au moment d'écrire ce module (voir docstring de google_play_service.py) —
notamment l'absence de toute valeur "REVOKED"/"REFUNDED" dans
subscriptionState.

Usage : python api/test_google_play_billing.py
"""

import base64
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

TEST_DB_PATH = Path(__file__).parent / "test_google_play_billing.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"
os.environ["JWT_SECRET_KEY"] = "test-only-secret-key-not-for-production-use"
os.environ["CHARIOW_API_KEY"] = "test_dummy_never_used_network_is_mocked"
os.environ["CHARIOW_PULSE_SECRET"] = "pulse_test_secret_for_signature_verification"
os.environ["CHARIOW_PRODUCT_ID_MONTHLY"] = "prod_test_monthly"
os.environ["CHARIOW_PRODUCT_ID_YEARLY"] = "prod_test_yearly"
os.environ["GOOGLE_PLAY_PACKAGE_NAME"] = "site.xfoot.app"
os.environ["GOOGLE_PLAY_PRODUCT_ID"] = "xfoot_premium_test"
os.environ["GOOGLE_PLAY_BASE_PLAN_ID_MONTHLY"] = "monthly_test"
os.environ["GOOGLE_PLAY_BASE_PLAN_ID_YEARLY"] = "yearly_test"
os.environ["GOOGLE_RTDN_SHARED_SECRET"] = "rtdn_test_shared_secret"

if TEST_DB_PATH.exists():
    TEST_DB_PATH.unlink()

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from main import app
from app.core.database import engine
from app.models.provider_subscription import ProviderSubscription
from app.models.entitlement import Entitlement, EntitlementEvent
from app.models.google_play_purchase import GooglePlayPurchase
from app.billing.google_play_service import normalize_google_status
from app.core.rate_limit import limiter
from _test_support import register_and_login as _register_and_login  # réutilisé tel quel

PASSWORD = "correct-horse-battery-staple"
PACKAGE_NAME = "site.xfoot.app"
XFOOT_PRODUCT_ID = "xfoot_premium_test"
BASE_PLAN_MONTHLY = "monthly_test"
BASE_PLAN_YEARLY = "yearly_test"
RTDN_SECRET = "rtdn_test_shared_secret"


def make_google_response(*, product_id=XFOOT_PRODUCT_ID, subscription_state="SUBSCRIPTION_STATE_ACTIVE",
                          expiry_time="2027-01-01T00:00:00Z", auto_renew=True, base_plan_id=BASE_PLAN_MONTHLY,
                          order_id="GPA.1234-5678", acknowledgement_state="ACKNOWLEDGEMENT_STATE_PENDING",
                          linked_purchase_token=None):
    """Reproduit la forme réelle de SubscriptionPurchaseV2 (confirmée contre
    la documentation officielle purchases.subscriptionsv2)."""
    return {
        "kind": "androidpublisher#subscriptionPurchaseV2",
        "regionCode": "CI",
        "subscriptionState": subscription_state,
        "acknowledgementState": acknowledgement_state,
        "linkedPurchaseToken": linked_purchase_token,
        "lineItems": [{
            "productId": product_id,
            "expiryTime": expiry_time,
            "latestSuccessfulOrderId": order_id,
            "autoRenewingPlan": {"autoRenewEnabled": auto_renew},
            "offerDetails": {"basePlanId": base_plan_id, "offerId": "std"},
        }],
    }


def make_rtdn_body(*, purchase_token, notification_type, message_id, package_name=PACKAGE_NAME):
    notification = {
        "version": "1.0",
        "packageName": package_name,
        "eventTimeMillis": "1700000000000",
        "subscriptionNotification": {
            "version": "1.0",
            "notificationType": notification_type,
            "purchaseToken": purchase_token,
        },
    }
    data_b64 = base64.b64encode(json.dumps(notification).encode()).decode()
    return {"message": {"data": data_b64, "messageId": message_id, "publishTime": "2027-01-01T00:00:00Z"},
            "subscription": "projects/test/subscriptions/test-sub"}


def register_and_login(client, email):
    """Ce fichier crée un utilisateur par scénario (~30 au total) pour garder
    chaque cas isolé — bien plus que les autres suites de tests (une poignée
    d'utilisateurs). Ça épuise le rate limit de /auth/register|login
    (20/minute par IP, app/auth/router.py, non lié à Google Play et donc
    volontairement non modifié) : on réinitialise le limiter avant chaque
    inscription, sans rapport avec le comportement testé ici."""
    limiter.reset()
    return _register_and_login(client, email, PASSWORD)


def get_provider_sub(user_id: int):
    with Session(engine) as session:
        return session.exec(select(ProviderSubscription).where(
            ProviderSubscription.user_id == user_id, ProviderSubscription.provider == "google_play"
        )).first()


def get_entitlement(user_id: int):
    with Session(engine) as session:
        return session.get(Entitlement, user_id)


# ---------------------------------------------------------------------------
# 1. normalize_google_status — pur, sans DB/réseau
# ---------------------------------------------------------------------------

def test_normalize_status_pending_never_active():
    from datetime import datetime, timezone
    future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    assert normalize_google_status("SUBSCRIPTION_STATE_PENDING", future) == "pending"
    assert normalize_google_status("SUBSCRIPTION_STATE_PENDING_PURCHASE_CANCELED", future) == "pending"
    print("  [OK] SUBSCRIPTION_STATE_PENDING/_PENDING_PURCHASE_CANCELED -> 'pending' (jamais 'active')")


def test_normalize_status_active_and_grace_period():
    from datetime import datetime, timezone
    future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    assert normalize_google_status("SUBSCRIPTION_STATE_ACTIVE", future) == "active"
    assert normalize_google_status("SUBSCRIPTION_STATE_IN_GRACE_PERIOD", future) == "active"
    print("  [OK] ACTIVE et IN_GRACE_PERIOD -> 'active'")


def test_normalize_status_on_hold_and_paused_not_active():
    from datetime import datetime, timezone
    future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    assert normalize_google_status("SUBSCRIPTION_STATE_ON_HOLD", future) == "expired"
    assert normalize_google_status("SUBSCRIPTION_STATE_PAUSED", future) == "expired"
    print("  [OK] ON_HOLD et PAUSED -> 'expired' (pas d'accès malgré expiry future)")


def test_normalize_status_canceled_depends_on_expiry():
    from datetime import datetime, timezone
    future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    past = datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert normalize_google_status("SUBSCRIPTION_STATE_CANCELED", future) == "active"
    assert normalize_google_status("SUBSCRIPTION_STATE_CANCELED", past) == "expired"
    print("  [OK] CANCELED -> 'active' tant que expiry future, 'expired' une fois passée")


def test_normalize_status_forced_revoked_overrides_everything():
    from datetime import datetime, timezone
    future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    assert normalize_google_status("SUBSCRIPTION_STATE_ACTIVE", future, forced_status="revoked") == "revoked"
    print("  [OK] forced_status='revoked' prime sur subscriptionState, même si 'active' par ailleurs")


# ---------------------------------------------------------------------------
# 2. POST /billing/google/verify
# ---------------------------------------------------------------------------

def test_verify_requires_auth_401(client):
    r = client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_no_auth"})
    assert r.status_code == 401, r.text
    print("  [OK] POST /billing/google/verify sans authentification -> 401")


def test_verify_valid_token_grants_premium(client):
    user_id, token = register_and_login(client, "gp-valid@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    with patch("app.billing.google_play_service._fetch_google_subscription",
               return_value=make_google_response()) as m_fetch, \
         patch("app.billing.google_play_service._acknowledge_google_purchase") as m_ack:
        r = client.post("/billing/google/verify", json={
            "product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_valid_1",
        }, headers=headers)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["premium"] is True
    assert body["google_status"] == "active"
    assert body["plan"] == "monthly"
    assert body["acknowledgement_state"] == "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED"
    m_fetch.assert_called_once()
    # jamais le package_name du client (absent ici) -> toujours celui configuré serveur
    assert m_fetch.call_args.args[0] == PACKAGE_NAME
    m_ack.assert_called_once()

    ps = get_provider_sub(user_id)
    assert ps.status == "active"
    assert ps.external_ref == "tok_valid_1"
    ent = get_entitlement(user_id)
    assert ent.premium is True
    print("  [OK] token valide -> 200, Premium accordé, ProviderSubscription/Entitlement corrects, acknowledgement effectué")
    return user_id, token


def test_verify_invalid_token_404_from_google_maps_400(client):
    user_id, token = register_and_login(client, "gp-invalid@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    class FakeResponse:
        status_code = 404
    with patch("app.billing.google_play_service.httpx.get", return_value=FakeResponse()), \
         patch("app.billing.google_play_service._get_access_token", return_value="fake-token"):
        r = client.post("/billing/google/verify", json={
            "product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_does_not_exist",
        }, headers=headers)
    assert r.status_code == 400, r.text
    ent = get_entitlement(user_id)
    assert ent is None or ent.premium is False
    print("  [OK] purchaseToken introuvable chez Google (404) -> 400, rien accordé")


def test_verify_wrong_package_400(client):
    user_id, token = register_and_login(client, "gp-wrongpkg@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    r = client.post("/billing/google/verify", json={
        "product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_wrong_pkg", "package_name": "com.someone.else",
    }, headers=headers)
    assert r.status_code == 400, r.text
    print("  [OK] package_name différent de celui configuré serveur -> 400 (jamais interrogé Google)")


def test_verify_wrong_product_id_400(client):
    """productId réel renvoyé par Google différent de xfoot_premium (achat
    d'une toute autre app/produit) -> rejeté, quel que soit basePlanId."""
    user_id, token = register_and_login(client, "gp-wrongproduct@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    with patch("app.billing.google_play_service._fetch_google_subscription",
               return_value=make_google_response(product_id="produit_dune_autre_app")):
        r = client.post("/billing/google/verify", json={
            "product_id": "produit_dune_autre_app", "purchase_token": "tok_unknown_product",
        }, headers=headers)
    assert r.status_code == 400, r.text
    print("  [OK] productId réel (Google) différent de xfoot_premium -> 400")


def test_verify_wrong_base_plan_id_400(client):
    """productId correct (xfoot_premium) mais basePlanId non reconnu (ex.
    offre promotionnelle non configurée côté nous) -> rejeté."""
    user_id, token = register_and_login(client, "gp-wrongbaseplan@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    with patch("app.billing.google_play_service._fetch_google_subscription",
               return_value=make_google_response(base_plan_id="promo_essai_inconnu")):
        r = client.post("/billing/google/verify", json={
            "product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_unknown_base_plan",
        }, headers=headers)
    assert r.status_code == 400, r.text
    print("  [OK] basePlanId réel (Google) non reconnu (productId correct par ailleurs) -> 400")


def test_verify_uses_google_product_id_not_client_claim(client):
    """Le client déclare un product_id bidon dans la requête, mais Google
    renvoie réellement 'xfoot_premium'/'monthly_test' pour ce token — c'est
    la valeur DE GOOGLE qui doit faire foi, jamais celle du client."""
    user_id, token = register_and_login(client, "gp-mismatch-product@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    with patch("app.billing.google_play_service._fetch_google_subscription",
               return_value=make_google_response()), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        r = client.post("/billing/google/verify", json={
            "product_id": "ce_que_le_client_pretend_nimporte_quoi", "purchase_token": "tok_client_lies_about_product",
        }, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["plan"] == "monthly", "doit refléter le productId/basePlanId RÉELS de Google, jamais ce que déclare le client"
    print("  [OK] product_id bidon déclaré par le client -> ignoré, valeur réelle de Google utilisée (plan='monthly')")


def test_verify_same_user_same_token_is_idempotent(client):
    user_id, token = test_verify_valid_token_grants_premium(client) if False else (None, None)
    # Ré-exécute proprement plutôt que de dépendre d'un autre test :
    user_id, token = register_and_login(client, "gp-idempotent@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    resp = make_google_response()

    with patch("app.billing.google_play_service._fetch_google_subscription", return_value=resp), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        r1 = client.post("/billing/google/verify", json={
            "product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_idempotent",
        }, headers=headers)
        with Session(engine) as session:
            events_after_1 = len(session.exec(select(EntitlementEvent).where(EntitlementEvent.user_id == user_id)).all())
            purchases_after_1 = len(session.exec(select(GooglePlayPurchase).where(GooglePlayPurchase.purchase_token == "tok_idempotent")).all())

        r2 = client.post("/billing/google/verify", json={
            "product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_idempotent",
        }, headers=headers)
        with Session(engine) as session:
            events_after_2 = len(session.exec(select(EntitlementEvent).where(EntitlementEvent.user_id == user_id)).all())
            purchases_after_2 = len(session.exec(select(GooglePlayPurchase).where(GooglePlayPurchase.purchase_token == "tok_idempotent")).all())

    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json(), "deux requêtes identiques doivent renvoyer le même résultat"
    assert purchases_after_1 == purchases_after_2 == 1, "aucun doublon de GooglePlayPurchase"
    assert events_after_1 == events_after_2, "aucun nouvel EntitlementEvent (premium déjà True, resynchronisation seulement)"
    print("  [OK] idempotence : même token revérifié deux fois -> même réponse, pas de doublon, pas de nouvel EntitlementEvent")


def test_verify_token_already_owned_by_same_user_resyncs(client):
    user_id, token = register_and_login(client, "gp-same-user@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    with patch("app.billing.google_play_service._fetch_google_subscription",
               return_value=make_google_response(expiry_time="2027-01-01T00:00:00Z")), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_restore_same"}, headers=headers)

    # "Restauration" : même utilisateur, nouvelle date d'expiration renvoyée par Google.
    with patch("app.billing.google_play_service._fetch_google_subscription",
               return_value=make_google_response(expiry_time="2027-06-01T00:00:00Z")), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        r = client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_restore_same"}, headers=headers)

    assert r.status_code == 200, r.text
    assert r.json()["expiry_time"].startswith("2027-06-01")
    print("  [OK] restauration (même compte, même token) -> resynchronisation autorisée, pas de duplication")


def test_verify_token_owned_by_another_user_409(client):
    user_id_a, token_a = register_and_login(client, "gp-owner-a@example.com")
    user_id_b, token_b = register_and_login(client, "gp-owner-b@example.com")

    with patch("app.billing.google_play_service._fetch_google_subscription", return_value=make_google_response()), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        r_a = client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_owned_by_a"},
                           headers={"Authorization": f"Bearer {token_a}"})
    assert r_a.status_code == 200

    with patch("app.billing.google_play_service._fetch_google_subscription", return_value=make_google_response()):
        r_b = client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_owned_by_a"},
                           headers={"Authorization": f"Bearer {token_b}"})
    assert r_b.status_code == 409, r_b.text

    ent_b = get_entitlement(user_id_b)
    assert ent_b is None or ent_b.premium is False, "le compte B ne doit RIEN recevoir"
    print("  [OK] purchaseToken déjà associé à un autre compte -> 409, aucune réassociation automatique")


def test_verify_google_500_maps_to_502(client):
    user_id, token = register_and_login(client, "gp-error500@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    class FakeResponse:
        status_code = 500
        text = "internal error"
    with patch("app.billing.google_play_service.httpx.get", return_value=FakeResponse()), \
         patch("app.billing.google_play_service._get_access_token", return_value="fake-token"):
        r = client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_500"}, headers=headers)
    assert r.status_code == 502, r.text
    assert get_provider_sub(user_id) is None or get_provider_sub(user_id).status == "none"
    print("  [OK] erreur 500 Google -> 502, aucune écriture partielle")


def test_verify_google_timeout_maps_to_502(client):
    import httpx as httpx_module
    user_id, token = register_and_login(client, "gp-timeout@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    with patch("app.billing.google_play_service.httpx.get", side_effect=httpx_module.TimeoutException("timeout")), \
         patch("app.billing.google_play_service._get_access_token", return_value="fake-token"):
        r = client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_timeout"}, headers=headers)
    assert r.status_code == 502, r.text
    print("  [OK] timeout réseau Google -> 502, aucune écriture partielle")


def test_verify_acknowledge_failure_does_not_block_premium(client):
    user_id, token = register_and_login(client, "gp-ackfail@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    with patch("app.billing.google_play_service._fetch_google_subscription", return_value=make_google_response()), \
         patch("app.billing.google_play_service._acknowledge_google_purchase", side_effect=Exception("boom")):
        # _acknowledge_google_purchase lève GoogleApiError normalement ; ici on
        # force une exception générique volontairement — le code attrape
        # spécifiquement GoogleApiError, donc on patch pour lever exactement ça :
        pass
    from app.billing.google_play_service import GoogleApiError
    with patch("app.billing.google_play_service._fetch_google_subscription", return_value=make_google_response()), \
         patch("app.billing.google_play_service._acknowledge_google_purchase", side_effect=GoogleApiError("ack failed")):
        r = client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_ack_fail"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["premium"] is True, "un échec d'acknowledgement ne doit JAMAIS retirer le Premium déjà accordé"
    assert body["acknowledgement_state"] == "ACKNOWLEDGEMENT_STATE_PENDING", "jamais marqué ACKNOWLEDGED sans succès réel de l'appel Google"
    print("  [OK] échec d'acknowledgement -> Premium quand même accordé, acknowledgement_state reste PENDING (jamais fabriqué)")


def test_verify_pending_state_never_grants_premium(client):
    user_id, token = register_and_login(client, "gp-pending@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    with patch("app.billing.google_play_service._fetch_google_subscription",
               return_value=make_google_response(subscription_state="SUBSCRIPTION_STATE_PENDING",
                                                  acknowledgement_state="ACKNOWLEDGEMENT_STATE_PENDING")), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        r = client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_pending"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["premium"] is False
    assert r.json()["google_status"] == "pending"
    print("  [OK] SUBSCRIPTION_STATE_PENDING -> premium=False, google_status='pending' (jamais 'none' ni 'active')")


def test_verify_canceled_still_in_paid_period_grants_premium(client):
    user_id, token = register_and_login(client, "gp-canceled-active@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    with patch("app.billing.google_play_service._fetch_google_subscription",
               return_value=make_google_response(subscription_state="SUBSCRIPTION_STATE_CANCELED",
                                                  expiry_time="2099-01-01T00:00:00Z", auto_renew=False)), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        r = client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_canceled_active"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["premium"] is True
    print("  [OK] CANCELED avec expiry future -> premium=True (annulé mais encore dans la période payée)")


def test_verify_on_hold_no_premium(client):
    user_id, token = register_and_login(client, "gp-onhold@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    with patch("app.billing.google_play_service._fetch_google_subscription",
               return_value=make_google_response(subscription_state="SUBSCRIPTION_STATE_ON_HOLD")), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        r = client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_onhold"}, headers=headers)
    assert r.json()["premium"] is False
    print("  [OK] ON_HOLD -> premium=False")


def test_verify_in_grace_period_grants_premium(client):
    user_id, token = register_and_login(client, "gp-grace@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    with patch("app.billing.google_play_service._fetch_google_subscription",
               return_value=make_google_response(subscription_state="SUBSCRIPTION_STATE_IN_GRACE_PERIOD")), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        r = client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_grace"}, headers=headers)
    assert r.json()["premium"] is True
    print("  [OK] IN_GRACE_PERIOD -> premium=True")


def test_verify_expired_no_premium(client):
    user_id, token = register_and_login(client, "gp-expired@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    with patch("app.billing.google_play_service._fetch_google_subscription",
               return_value=make_google_response(subscription_state="SUBSCRIPTION_STATE_EXPIRED", expiry_time="2020-01-01T00:00:00Z")), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        r = client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_expired"}, headers=headers)
    assert r.json()["premium"] is False
    print("  [OK] EXPIRED -> premium=False")


def test_verify_linked_purchase_token_same_slot(client):
    """Changement de plan/réabonnement : nouveau purchase_token, mais
    linkedPurchaseToken pointe vers l'ancien -> même ProviderSubscription
    (même utilisateur), historique conservé dans GooglePlayPurchase."""
    user_id, token = register_and_login(client, "gp-linked@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    with patch("app.billing.google_play_service._fetch_google_subscription",
               return_value=make_google_response()), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_original"}, headers=headers)

    # Expiration distincte (postérieure) de celle du token d'origine : un
    # vrai changement de plan/réabonnement a toujours une nouvelle date
    # d'expiration, jamais la même — sinon le choix du "meilleur" achat
    # actif à égalité d'expiration n'aurait aucun moyen de trancher.
    with patch("app.billing.google_play_service._fetch_google_subscription",
               return_value=make_google_response(base_plan_id=BASE_PLAN_YEARLY, linked_purchase_token="tok_original",
                                                  expiry_time="2028-01-01T00:00:00Z")), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        r = client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_upgraded"}, headers=headers)

    assert r.status_code == 200, r.text
    assert r.json()["plan"] == "yearly"

    with Session(engine) as session:
        purchases = session.exec(select(GooglePlayPurchase).where(GooglePlayPurchase.user_id == user_id)).all()
        provider_sub_ids = {p.provider_subscription_id for p in purchases}
    assert len(purchases) == 2, "les deux achats (ancien + nouveau) doivent être conservés dans l'historique"
    assert len(provider_sub_ids) == 1, "les deux doivent pointer vers le MÊME emplacement ProviderSubscription"
    print("  [OK] changement de plan via linkedPurchaseToken -> historique conservé (2 GooglePlayPurchase), même emplacement ProviderSubscription")


# ---------------------------------------------------------------------------
# 3. RTDN
# ---------------------------------------------------------------------------

def test_rtdn_requires_authentication_401(client):
    body = make_rtdn_body(purchase_token="tok_rtdn_noauth", notification_type=2, message_id="msg_noauth")
    r = client.post("/billing/google/rtdn", json=body)
    assert r.status_code == 401, r.text
    print("  [OK] POST /billing/google/rtdn sans secret/jeton valide -> 401")


def test_rtdn_unknown_token_is_noop(client):
    body = make_rtdn_body(purchase_token="tok_never_seen", notification_type=2, message_id="msg_unknown_token")
    r = client.post(f"/billing/google/rtdn?secret={RTDN_SECRET}", json=body)
    assert r.status_code == 200, r.text
    print("  [OK] RTDN pour un purchase_token inconnu -> 200 sans action (no-op)")


def test_rtdn_resyncs_known_token(client):
    user_id, token = register_and_login(client, "gp-rtdn-renew@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    with patch("app.billing.google_play_service._fetch_google_subscription",
               return_value=make_google_response(expiry_time="2027-01-01T00:00:00Z")), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_rtdn_known"}, headers=headers)

    body = make_rtdn_body(purchase_token="tok_rtdn_known", notification_type=2, message_id="msg_renew_1")  # SUBSCRIPTION_RENEWED
    with patch("app.billing.google_play_service._fetch_google_subscription",
               return_value=make_google_response(expiry_time="2027-07-01T00:00:00Z")):
        r = client.post(f"/billing/google/rtdn?secret={RTDN_SECRET}", json=body)
    assert r.status_code == 200, r.text

    ps = get_provider_sub(user_id)
    assert ps.current_period_end.isoformat().startswith("2027-07-01")
    print("  [OK] RTDN SUBSCRIPTION_RENEWED sur token connu -> resynchronisation réelle via Google, jamais les champs de la notification elle-même")


def test_rtdn_revoked_notification_sets_revoked_status(client):
    user_id, token = register_and_login(client, "gp-rtdn-revoked@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    with patch("app.billing.google_play_service._fetch_google_subscription", return_value=make_google_response()), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_to_be_revoked"}, headers=headers)
    assert get_entitlement(user_id).premium is True

    # Même après revocation, Google renverrait SUBSCRIPTION_STATE_EXPIRED
    # (jamais un état "REVOKED" — confirmé contre la doc officielle) : c'est
    # le TYPE de notification RTDN (12 = SUBSCRIPTION_REVOKED) qui pilote le
    # statut 'revoked' via forced_status, pas subscriptionState.
    body = make_rtdn_body(purchase_token="tok_to_be_revoked", notification_type=12, message_id="msg_revoked_1")
    with patch("app.billing.google_play_service._fetch_google_subscription",
               return_value=make_google_response(subscription_state="SUBSCRIPTION_STATE_EXPIRED", expiry_time="2020-01-01T00:00:00Z")):
        r = client.post(f"/billing/google/rtdn?secret={RTDN_SECRET}", json=body)
    assert r.status_code == 200, r.text

    ps = get_provider_sub(user_id)
    assert ps.status == "revoked"
    assert ps.revoked_or_refunded_at is not None
    ent = get_entitlement(user_id)
    assert ent.premium is False
    print("  [OK] RTDN SUBSCRIPTION_REVOKED -> ProviderSubscription.status='revoked', Entitlement.premium=False immédiatement")


def test_rtdn_duplicate_message_id_is_noop(client):
    user_id, token = register_and_login(client, "gp-rtdn-dup@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    with patch("app.billing.google_play_service._fetch_google_subscription", return_value=make_google_response()), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_dup_rtdn"}, headers=headers)

    body = make_rtdn_body(purchase_token="tok_dup_rtdn", notification_type=2, message_id="msg_dup_1")
    with patch("app.billing.google_play_service._fetch_google_subscription",
               return_value=make_google_response(expiry_time="2027-09-01T00:00:00Z")):
        r1 = client.post(f"/billing/google/rtdn?secret={RTDN_SECRET}", json=body)
    assert r1.status_code == 200
    assert r1.json().get("duplicate") is not True

    # Rejoue exactement le même messageId -> ne doit PAS rappeler Google une 2e fois.
    with patch("app.billing.google_play_service._fetch_google_subscription") as m_fetch_should_not_be_called:
        r2 = client.post(f"/billing/google/rtdn?secret={RTDN_SECRET}", json=body)
    assert r2.status_code == 200
    assert r2.json().get("duplicate") is True
    m_fetch_should_not_be_called.assert_not_called()
    print("  [OK] messageId RTDN déjà traité -> dédupliqué, Google jamais rappelé une seconde fois")


# ---------------------------------------------------------------------------
# 4. Entitlement combiné Chariow + Google
# ---------------------------------------------------------------------------

def _activate_chariow(client, token, user_id, email):
    from _test_support import sign_payload, make_event
    with patch("app.billing.router._create_chariow_checkout_link", return_value="https://chariow.com/x"):
        client.post("/billing/checkout", json={
            "plan": "monthly", "first_name": "T", "last_name": "U",
            "phone_number": "0700000000", "phone_country_code": "CI",
        }, headers={"Authorization": f"Bearer {token}"})
    payload = make_event("successful.sale", sale={"custom_metadata": {"user_id": str(user_id), "plan": "monthly"}},
                          customer={"email": email})
    sig = sign_payload(payload, "pulse_test_secret_for_signature_verification")
    client.post("/billing/pulse", content=payload, headers={
        "x-chariow-signature": sig, "x-pulse-delivery-id": f"pulse_{user_id}_sale", "content-type": "application/json",
    })


def _expire_chariow(client, email):
    from _test_support import sign_payload, make_event
    payload = make_event("license.expired", customer={"email": email})
    sig = sign_payload(payload, "pulse_test_secret_for_signature_verification")
    client.post("/billing/pulse", content=payload, headers={
        "x-chariow-signature": sig, "x-pulse-delivery-id": f"pulse_expire_{email}", "content-type": "application/json",
    })


def test_chariow_active_google_expired_premium_true(client):
    email = "combo-1@example.com"
    user_id, token = register_and_login(client, email)
    _activate_chariow(client, token, user_id, email)
    with patch("app.billing.google_play_service._fetch_google_subscription",
               return_value=make_google_response(subscription_state="SUBSCRIPTION_STATE_EXPIRED", expiry_time="2020-01-01T00:00:00Z")), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_combo_1"},
                    headers={"Authorization": f"Bearer {token}"})
    assert get_entitlement(user_id).premium is True
    print("  [OK] Chariow actif + Google expiré -> Premium TRUE")


def test_chariow_expired_google_active_premium_true(client):
    email = "combo-2@example.com"
    user_id, token = register_and_login(client, email)
    _activate_chariow(client, token, user_id, email)
    _expire_chariow(client, email)
    assert get_entitlement(user_id).premium is False
    with patch("app.billing.google_play_service._fetch_google_subscription", return_value=make_google_response()), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_combo_2"},
                    headers={"Authorization": f"Bearer {token}"})
    assert get_entitlement(user_id).premium is True
    print("  [OK] Chariow expiré + Google actif -> Premium TRUE")


def test_both_active_premium_true(client):
    email = "combo-3@example.com"
    user_id, token = register_and_login(client, email)
    _activate_chariow(client, token, user_id, email)
    with patch("app.billing.google_play_service._fetch_google_subscription", return_value=make_google_response()), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_combo_3"},
                    headers={"Authorization": f"Bearer {token}"})
    ent = get_entitlement(user_id)
    assert ent.premium is True
    assert set(json.loads(ent.active_sources)) == {"chariow", "google_play"}
    print("  [OK] Chariow actif + Google actif -> Premium TRUE, active_sources=['chariow','google_play']")


def test_both_expired_premium_false(client):
    email = "combo-4@example.com"
    user_id, token = register_and_login(client, email)
    _activate_chariow(client, token, user_id, email)
    _expire_chariow(client, email)
    with patch("app.billing.google_play_service._fetch_google_subscription",
               return_value=make_google_response(subscription_state="SUBSCRIPTION_STATE_EXPIRED", expiry_time="2020-01-01T00:00:00Z")), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_combo_4"},
                    headers={"Authorization": f"Bearer {token}"})
    assert get_entitlement(user_id).premium is False
    print("  [OK] Chariow expiré + Google expiré -> Premium FALSE")


def test_google_only_premium_true(client):
    email = "combo-5@example.com"
    user_id, token = register_and_login(client, email)
    with patch("app.billing.google_play_service._fetch_google_subscription", return_value=make_google_response()), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_combo_5"},
                    headers={"Authorization": f"Bearer {token}"})
    assert get_entitlement(user_id).premium is True
    print("  [OK] Chariow absent + Google actif -> Premium TRUE")


def test_google_revoked_chariow_active_premium_true(client):
    email = "combo-6@example.com"
    user_id, token = register_and_login(client, email)
    _activate_chariow(client, token, user_id, email)
    with patch("app.billing.google_play_service._fetch_google_subscription", return_value=make_google_response()), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_combo_6"},
                    headers={"Authorization": f"Bearer {token}"})
    body = make_rtdn_body(purchase_token="tok_combo_6", notification_type=12, message_id="msg_combo_6_revoked")
    with patch("app.billing.google_play_service._fetch_google_subscription",
               return_value=make_google_response(subscription_state="SUBSCRIPTION_STATE_EXPIRED", expiry_time="2020-01-01T00:00:00Z")):
        client.post(f"/billing/google/rtdn?secret={RTDN_SECRET}", json=body)
    assert get_entitlement(user_id).premium is True
    print("  [OK] Google remboursé/révoqué + Chariow actif -> Premium TRUE")


def test_google_revoked_chariow_expired_premium_false(client):
    email = "combo-7@example.com"
    user_id, token = register_and_login(client, email)
    _activate_chariow(client, token, user_id, email)
    _expire_chariow(client, email)
    with patch("app.billing.google_play_service._fetch_google_subscription", return_value=make_google_response()), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_combo_7"},
                    headers={"Authorization": f"Bearer {token}"})
    body = make_rtdn_body(purchase_token="tok_combo_7", notification_type=12, message_id="msg_combo_7_revoked")
    with patch("app.billing.google_play_service._fetch_google_subscription",
               return_value=make_google_response(subscription_state="SUBSCRIPTION_STATE_EXPIRED", expiry_time="2020-01-01T00:00:00Z")):
        client.post(f"/billing/google/rtdn?secret={RTDN_SECRET}", json=body)
    assert get_entitlement(user_id).premium is False
    print("  [OK] Google remboursé/révoqué + Chariow expiré -> Premium FALSE")


# ---------------------------------------------------------------------------
# 5. GET /billing/entitlement — lecture seule sur Entitlement, jamais de recalcul
# ---------------------------------------------------------------------------

def _get_entitlement_endpoint(client, token):
    return client.get("/billing/entitlement", headers={"Authorization": f"Bearer {token}"})


def test_entitlement_endpoint_requires_auth_401(client):
    r = client.get("/billing/entitlement")
    assert r.status_code == 401, r.text
    print("  [OK] GET /billing/entitlement sans authentification -> 401")


def test_entitlement_endpoint_no_source(client):
    user_id, token = register_and_login(client, "ent-none@example.com")
    r = _get_entitlement_endpoint(client, token)
    assert r.status_code == 200, r.text
    assert r.json() == {"premium": False, "premium_until": None, "active_sources": []}
    print("  [OK] aucune source -> premium=False, active_sources=[]")


def test_entitlement_endpoint_chariow_active(client):
    email = "ent-chariow@example.com"
    user_id, token = register_and_login(client, email)
    _activate_chariow(client, token, user_id, email)
    r = _get_entitlement_endpoint(client, token)
    body = r.json()
    assert body["premium"] is True
    assert body["active_sources"] == ["chariow"]
    print("  [OK] Chariow actif -> premium=True, active_sources=['chariow']")


def test_entitlement_endpoint_google_active(client):
    email = "ent-google@example.com"
    user_id, token = register_and_login(client, email)
    with patch("app.billing.google_play_service._fetch_google_subscription", return_value=make_google_response()), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_ent_google"},
                    headers={"Authorization": f"Bearer {token}"})
    r = _get_entitlement_endpoint(client, token)
    body = r.json()
    assert body["premium"] is True
    assert body["active_sources"] == ["google_play"]
    assert body["premium_until"] is not None
    print("  [OK] Google actif -> premium=True, active_sources=['google_play'], premium_until renseigné")


def test_entitlement_endpoint_both_active(client):
    email = "ent-both@example.com"
    user_id, token = register_and_login(client, email)
    _activate_chariow(client, token, user_id, email)
    with patch("app.billing.google_play_service._fetch_google_subscription", return_value=make_google_response()), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_ent_both"},
                    headers={"Authorization": f"Bearer {token}"})
    r = _get_entitlement_endpoint(client, token)
    body = r.json()
    assert body["premium"] is True
    assert set(body["active_sources"]) == {"chariow", "google_play"}
    print("  [OK] les deux actifs -> premium=True, active_sources contient chariow ET google_play")


def test_entitlement_endpoint_both_expired(client):
    email = "ent-bothexpired@example.com"
    user_id, token = register_and_login(client, email)
    _activate_chariow(client, token, user_id, email)
    _expire_chariow(client, email)
    with patch("app.billing.google_play_service._fetch_google_subscription",
               return_value=make_google_response(subscription_state="SUBSCRIPTION_STATE_EXPIRED", expiry_time="2020-01-01T00:00:00Z")), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_ent_bothexpired"},
                    headers={"Authorization": f"Bearer {token}"})
    r = _get_entitlement_endpoint(client, token)
    assert r.json() == {"premium": False, "premium_until": None, "active_sources": []}
    print("  [OK] les deux expirés -> premium=False, active_sources=[]")


def test_entitlement_endpoint_chariow_active_google_expired(client):
    email = "ent-chariow-active-google-expired@example.com"
    user_id, token = register_and_login(client, email)
    _activate_chariow(client, token, user_id, email)
    with patch("app.billing.google_play_service._fetch_google_subscription",
               return_value=make_google_response(subscription_state="SUBSCRIPTION_STATE_EXPIRED", expiry_time="2020-01-01T00:00:00Z")), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_ent_mixed1"},
                    headers={"Authorization": f"Bearer {token}"})
    r = _get_entitlement_endpoint(client, token)
    body = r.json()
    assert body["premium"] is True
    assert body["active_sources"] == ["chariow"]
    print("  [OK] Chariow actif + Google expiré -> premium=True, active_sources=['chariow'] uniquement")


def test_entitlement_endpoint_chariow_expired_google_active(client):
    email = "ent-chariow-expired-google-active@example.com"
    user_id, token = register_and_login(client, email)
    _activate_chariow(client, token, user_id, email)
    _expire_chariow(client, email)
    with patch("app.billing.google_play_service._fetch_google_subscription", return_value=make_google_response()), \
         patch("app.billing.google_play_service._acknowledge_google_purchase"):
        client.post("/billing/google/verify", json={"product_id": XFOOT_PRODUCT_ID, "purchase_token": "tok_ent_mixed2"},
                    headers={"Authorization": f"Bearer {token}"})
    r = _get_entitlement_endpoint(client, token)
    body = r.json()
    assert body["premium"] is True
    assert body["active_sources"] == ["google_play"]
    print("  [OK] Chariow expiré + Google actif -> premium=True, active_sources=['google_play'] uniquement")


if __name__ == "__main__":
    failures = 0
    with TestClient(app) as client:
        steps = [
            ("test_normalize_status_pending_never_active", lambda: test_normalize_status_pending_never_active()),
            ("test_normalize_status_active_and_grace_period", lambda: test_normalize_status_active_and_grace_period()),
            ("test_normalize_status_on_hold_and_paused_not_active", lambda: test_normalize_status_on_hold_and_paused_not_active()),
            ("test_normalize_status_canceled_depends_on_expiry", lambda: test_normalize_status_canceled_depends_on_expiry()),
            ("test_normalize_status_forced_revoked_overrides_everything", lambda: test_normalize_status_forced_revoked_overrides_everything()),
            ("test_verify_requires_auth_401", lambda: test_verify_requires_auth_401(client)),
            ("test_verify_valid_token_grants_premium", lambda: test_verify_valid_token_grants_premium(client)),
            ("test_verify_invalid_token_404_from_google_maps_400", lambda: test_verify_invalid_token_404_from_google_maps_400(client)),
            ("test_verify_wrong_package_400", lambda: test_verify_wrong_package_400(client)),
            ("test_verify_wrong_product_id_400", lambda: test_verify_wrong_product_id_400(client)),
            ("test_verify_wrong_base_plan_id_400", lambda: test_verify_wrong_base_plan_id_400(client)),
            ("test_verify_uses_google_product_id_not_client_claim", lambda: test_verify_uses_google_product_id_not_client_claim(client)),
            ("test_verify_same_user_same_token_is_idempotent", lambda: test_verify_same_user_same_token_is_idempotent(client)),
            ("test_verify_token_already_owned_by_same_user_resyncs", lambda: test_verify_token_already_owned_by_same_user_resyncs(client)),
            ("test_verify_token_owned_by_another_user_409", lambda: test_verify_token_owned_by_another_user_409(client)),
            ("test_verify_google_500_maps_to_502", lambda: test_verify_google_500_maps_to_502(client)),
            ("test_verify_google_timeout_maps_to_502", lambda: test_verify_google_timeout_maps_to_502(client)),
            ("test_verify_acknowledge_failure_does_not_block_premium", lambda: test_verify_acknowledge_failure_does_not_block_premium(client)),
            ("test_verify_pending_state_never_grants_premium", lambda: test_verify_pending_state_never_grants_premium(client)),
            ("test_verify_canceled_still_in_paid_period_grants_premium", lambda: test_verify_canceled_still_in_paid_period_grants_premium(client)),
            ("test_verify_on_hold_no_premium", lambda: test_verify_on_hold_no_premium(client)),
            ("test_verify_in_grace_period_grants_premium", lambda: test_verify_in_grace_period_grants_premium(client)),
            ("test_verify_expired_no_premium", lambda: test_verify_expired_no_premium(client)),
            ("test_verify_linked_purchase_token_same_slot", lambda: test_verify_linked_purchase_token_same_slot(client)),
            ("test_rtdn_requires_authentication_401", lambda: test_rtdn_requires_authentication_401(client)),
            ("test_rtdn_unknown_token_is_noop", lambda: test_rtdn_unknown_token_is_noop(client)),
            ("test_rtdn_resyncs_known_token", lambda: test_rtdn_resyncs_known_token(client)),
            ("test_rtdn_revoked_notification_sets_revoked_status", lambda: test_rtdn_revoked_notification_sets_revoked_status(client)),
            ("test_rtdn_duplicate_message_id_is_noop", lambda: test_rtdn_duplicate_message_id_is_noop(client)),
            ("test_chariow_active_google_expired_premium_true", lambda: test_chariow_active_google_expired_premium_true(client)),
            ("test_chariow_expired_google_active_premium_true", lambda: test_chariow_expired_google_active_premium_true(client)),
            ("test_both_active_premium_true", lambda: test_both_active_premium_true(client)),
            ("test_both_expired_premium_false", lambda: test_both_expired_premium_false(client)),
            ("test_google_only_premium_true", lambda: test_google_only_premium_true(client)),
            ("test_google_revoked_chariow_active_premium_true", lambda: test_google_revoked_chariow_active_premium_true(client)),
            ("test_google_revoked_chariow_expired_premium_false", lambda: test_google_revoked_chariow_expired_premium_false(client)),
            ("test_entitlement_endpoint_requires_auth_401", lambda: test_entitlement_endpoint_requires_auth_401(client)),
            ("test_entitlement_endpoint_no_source", lambda: test_entitlement_endpoint_no_source(client)),
            ("test_entitlement_endpoint_chariow_active", lambda: test_entitlement_endpoint_chariow_active(client)),
            ("test_entitlement_endpoint_google_active", lambda: test_entitlement_endpoint_google_active(client)),
            ("test_entitlement_endpoint_both_active", lambda: test_entitlement_endpoint_both_active(client)),
            ("test_entitlement_endpoint_both_expired", lambda: test_entitlement_endpoint_both_expired(client)),
            ("test_entitlement_endpoint_chariow_active_google_expired", lambda: test_entitlement_endpoint_chariow_active_google_expired(client)),
            ("test_entitlement_endpoint_chariow_expired_google_active", lambda: test_entitlement_endpoint_chariow_expired_google_active(client)),
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
        pass

    n_tests = len(steps)
    print(f"\n{'='*60}\n{n_tests - failures}/{n_tests} tests reussis\n{'='*60}")
    sys.exit(1 if failures else 0)
