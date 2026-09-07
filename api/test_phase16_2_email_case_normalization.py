"""
test_phase16_2_email_case_normalization.py — Phase 16.2 : non-régression de la
normalisation email (casse + espaces) sur les chemins billing/Chariow qui
recherchent un utilisateur par email plutôt que par ID :

  - _find_provider_subscription_by_email (app/billing/router.py), utilisée par
    TOUS les Pulses license.* et le repli email de successful.sale ;
  - le repli customer.email de /billing/activate-license (metadata absente).

N'appelle JAMAIS un vrai service Chariow (aucun appel réseau réel) : les
Pulses sont signés à la main avec le même schéma que test_chariow_billing.py,
et _fetch_chariow_license est mockée pour activate-license.

IMPORTANT (Phase 16.2) : User.email n'est JAMAIS modifié en base par ce
correctif — ces tests vérifient uniquement que la RECHERCHE/COMPARAISON
tolère une différence de casse/espaces, jamais qu'une valeur stockée change.

Usage : python api/test_phase16_2_email_case_normalization.py
"""

import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

TEST_DB_PATH = Path(__file__).parent / "test_phase16_2_email_case_normalization.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"
os.environ["JWT_SECRET_KEY"] = "test-only-secret-key-not-for-production-use"
os.environ["CHARIOW_API_KEY"] = "test_dummy_never_used_network_is_mocked"
os.environ["CHARIOW_PULSE_SECRET"] = "pulse_test_secret_for_signature_verification"
os.environ["CHARIOW_PRODUCT_ID_MONTHLY"] = "prod_test_monthly"
os.environ["CHARIOW_PRODUCT_ID_YEARLY"] = "prod_test_yearly"

if TEST_DB_PATH.exists():
    TEST_DB_PATH.unlink()

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from main import app
from app.core.database import engine
from app.models.user import User
from app.models.provider_subscription import ProviderSubscription
from app.billing.router import _normalize_email, _find_provider_subscription_by_email

PULSE_SECRET = os.environ["CHARIOW_PULSE_SECRET"]
PASSWORD = "correct-horse-battery-staple"
CHECKOUT_BODY = {
    "plan": "monthly", "first_name": "T", "last_name": "U",
    "phone_number": "0700000000", "phone_country_code": "CI",
}


def sign_payload(payload_bytes: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()


def post_pulse(client, event_type: str, delivery_id: str, **fields):
    payload = json.dumps({"event": event_type, **fields}).encode()
    sig = sign_payload(payload, PULSE_SECRET)
    return client.post("/billing/pulse", content=payload, headers={
        "x-chariow-signature": sig, "x-pulse-delivery-id": delivery_id,
        "content-type": "application/json",
    })


def register_and_checkout(client, email: str) -> tuple[int, str]:
    """Inscrit un utilisateur avec l'email fourni TEL QUEL (casse préservée,
    jamais normalisée à l'écriture — Phase 16.2 ne touche pas User.email),
    puis crée sa ligne ProviderSubscription(status='none') via /billing/checkout
    (appel réseau Chariow mocké) — pré-requis pour que
    _find_provider_subscription_by_email ait quelque chose à trouver."""
    r = client.post("/auth/register", json={"name": "Test User", "email": email, "password": PASSWORD})
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]
    # EmailStr (pydantic/email-validator) normalise le DOMAINE en minuscules à la validation
    # (les domaines DNS sont insensibles à la casse) mais préserve la casse de la partie locale
    # — donc l'email RÉELLEMENT stocké peut différer de la chaîne envoyée si le domaine était en
    # majuscule (ex. "Erik@Example.COM" -> stocké "Erik@example.com"). On relit celui renvoyé par
    # /auth/register (jamais une supposition) pour se connecter — le login lui-même reste hors
    # périmètre de la Phase 16.2 (comparaison exacte inchangée, non touchée par ce correctif).
    stored_email = r.json()["email"]
    r = client.post("/auth/login", data={"username": stored_email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]

    with patch("app.billing.router._create_chariow_checkout_link", return_value="https://chariow.com/checkout/x"):
        r = client.post("/billing/checkout", json=CHECKOUT_BODY, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return user_id, token


def get_subscription_row(user_id: int) -> ProviderSubscription | None:
    with Session(engine) as session:
        return session.exec(
            select(ProviderSubscription).where(
                ProviderSubscription.user_id == user_id,
                ProviderSubscription.provider == "chariow",
            )
        ).first()


def get_stored_email(user_id: int) -> str:
    """Preuve de compatibilité historique : lit la valeur RÉELLEMENT stockée
    en base, pour vérifier qu'elle n'a jamais été altérée par ce correctif."""
    with Session(engine) as session:
        return session.get(User, user_id).email


# ---------------------------------------------------------------------------
# 1-6 : _find_provider_subscription_by_email — comparaisons directes,
# sans passer par le webhook (isole la logique de recherche elle-même).
# ---------------------------------------------------------------------------

def test_1_same_email_same_case_matches(user_diana):
    user_id, _ = user_diana
    with Session(engine) as session:
        sub = _find_provider_subscription_by_email(session, "diana@example.com")
    assert sub is not None and sub.user_id == user_id
    print("  [OK] même email, même casse -> match")


def test_2_user_lowercase_chariow_uppercase_matches(user_diana):
    user_id, _ = user_diana
    with Session(engine) as session:
        sub = _find_provider_subscription_by_email(session, "DIANA@EXAMPLE.COM")
    assert sub is not None and sub.user_id == user_id
    print("  [OK] compte enregistré en minuscule + email Chariow en majuscule -> match")


def test_3_user_uppercase_chariow_lowercase_matches(user_Erik_mixed):
    """Le compte est enregistré avec une casse mixte (Erik@Example.COM) —
    Chariow renvoie une version entièrement minuscule."""
    user_id, _ = user_Erik_mixed
    with Session(engine) as session:
        sub = _find_provider_subscription_by_email(session, "erik@example.com")
    assert sub is not None and sub.user_id == user_id
    print("  [OK] compte enregistré casse mixte + email Chariow tout en minuscule -> match")


def test_4_mixed_case_matches(user_Erik_mixed):
    user_id, _ = user_Erik_mixed
    with Session(engine) as session:
        sub = _find_provider_subscription_by_email(session, "eRiK@eXamPle.CoM")
    assert sub is not None and sub.user_id == user_id
    print("  [OK] casse mixte des deux côtés -> match")


def test_5_surrounding_whitespace_normalized(user_diana):
    user_id, _ = user_diana
    with Session(engine) as session:
        sub = _find_provider_subscription_by_email(session, "  diana@example.com  ")
    assert sub is not None and sub.user_id == user_id
    print("  [OK] espaces autour de l'email Chariow -> normalisés, match quand même")


def test_6_genuinely_different_email_does_not_match(user_diana):
    with Session(engine) as session:
        sub = _find_provider_subscription_by_email(session, "quelquun.dautre@example.com")
    assert sub is None
    print("  [OK] email réellement différent -> aucun match (None), jamais un mauvais compte renvoyé")


def test_6b_normalize_email_helper_strip_and_lower():
    assert _normalize_email("  Diana@Example.COM  ") == "diana@example.com"
    print("  [OK] _normalize_email : strip() + lower() appliqués ensemble")


def test_6c_historical_email_never_mutated(user_diana):
    """Preuve de compatibilité historique explicitement demandée (Phase 16.2,
    Partie H item 4) : la valeur stockée dans User.email reste EXACTEMENT
    celle saisie à l'inscription, jamais réécrite par ce correctif — seule la
    RECHERCHE est tolérante à la casse, pas le stockage."""
    user_id, _ = user_diana
    assert get_stored_email(user_id) == "diana@example.com"
    print("  [OK] User.email en base reste inchangé (aucune migration, aucune réécriture silencieuse)")


# ---------------------------------------------------------------------------
# 7 : activate-license — repli customer.email (licence sans metadata)
# ---------------------------------------------------------------------------

def _fake_license_no_metadata(*, customer_email, status="active", expires_at="2027-01-01T00:00:00+00:00"):
    return {
        "license_key": "lic_case_test", "status": status, "expires_at": expires_at,
        "metadata": None, "customer": {"email": customer_email}, "product": {},
    }


def test_7_activate_license_case_mismatch_still_resolves_correct_user(client, user_Erik_mixed):
    """Licence Chariow sans metadata, customer.email renvoyé TOUT EN
    MINUSCULE + espaces alors que le compte est enregistré en casse mixte —
    doit tout de même activer sur le BON compte, jamais 403 à tort."""
    user_id, token = user_Erik_mixed
    with patch("app.billing.router._fetch_chariow_license",
               return_value=_fake_license_no_metadata(customer_email="  erik@example.com  ")):
        r = client.post("/billing/activate-license", json={"license_key": "lic_case_test"},
                         headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    sub = get_subscription_row(user_id)
    assert sub.external_ref == "lic_case_test"
    print("  [OK] activate-license : casse différente + espaces sur customer.email -> bon utilisateur activé (200)")


def test_7b_activate_license_case_mismatch_never_leaks_to_other_account(client, user_diana):
    """Le repli email ne doit JAMAIS matcher un compte différent, casse ou
    pas — ici l'email Chariow ne correspond ni de près ni de loin au compte connecté."""
    user_id, token = user_diana
    with patch("app.billing.router._fetch_chariow_license",
               return_value=_fake_license_no_metadata(customer_email="PERSONNE.DAUTRE@EXAMPLE.COM")):
        r = client.post("/billing/activate-license", json={"license_key": "lic_case_test_2"},
                         headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403, r.text
    print("  [OK] activate-license : email Chariow différent -> 403, jamais rattaché par erreur")


# ---------------------------------------------------------------------------
# 8-9 : webhook Chariow (Pulse) — activation et révocation avec casse différente
# ---------------------------------------------------------------------------

def test_8_webhook_license_activated_case_mismatch_updates_correct_subscription(client, user_fatou):
    user_id, _ = user_fatou
    r = post_pulse(client, "license.activated", "pulse_case_activated_1",
                    license={"key": "lic_fatou_case", "expires_at": "2027-03-03T00:00:00+00:00"},
                    customer={"email": "FATOU@EXAMPLE.COM"})
    assert r.status_code == 200, r.text
    sub = get_subscription_row(user_id)
    assert sub.status == "active"
    assert sub.external_ref == "lic_fatou_case"
    print("  [OK] Pulse license.activated : email en MAJUSCULE -> souscription du bon compte mise à jour")


def test_9_webhook_license_revoked_case_mismatch_updates_correct_subscription(client, user_fatou):
    user_id, _ = user_fatou
    r = post_pulse(client, "license.revoked", "pulse_case_revoked_1", customer={"email": "  Fatou@Example.com  "})
    assert r.status_code == 200, r.text
    sub = get_subscription_row(user_id)
    assert sub.status == "revoked"
    assert sub.revoked_or_refunded_at is not None
    print("  [OK] Pulse license.revoked : casse mixte + espaces -> révocation appliquée au bon compte")


# ---------------------------------------------------------------------------
# 10 : isolation — deux comptes à emails proches, la casse ne doit jamais
# faire glisser un événement vers le mauvais utilisateur.
# ---------------------------------------------------------------------------

def test_10_isolation_similar_emails_never_cross_accounts(client, user_carla, user_carlita):
    """carla@example.com et carlita@example.com sont deux comptes DISTINCTS
    (préfixes différents, pas juste une casse différente) — un Pulse visant
    l'un ne doit jamais toucher l'autre, casse ou pas."""
    carla_id, _ = user_carla
    carlita_id, _ = user_carlita

    r = post_pulse(client, "license.revoked", "pulse_isolation_1", customer={"email": "CARLITA@EXAMPLE.COM"})
    assert r.status_code == 200, r.text

    sub_carlita = get_subscription_row(carlita_id)
    sub_carla = get_subscription_row(carla_id)
    assert sub_carlita.status == "revoked"
    assert sub_carla.status == "none", "carla ne doit JAMAIS être affectée par un Pulse visant carlita"
    print("  [OK] isolation : Pulse visant carlita (casse différente) -> carla reste intacte ('none')")


if __name__ == "__main__":
    failures = 0
    with TestClient(app) as client:
        fixtures = {
            "user_diana": register_and_checkout(client, "diana@example.com"),
            "user_Erik_mixed": register_and_checkout(client, "Erik@Example.COM"),
            "user_fatou": register_and_checkout(client, "fatou@example.com"),
            "user_carla": register_and_checkout(client, "carla@example.com"),
            "user_carlita": register_and_checkout(client, "carlita@example.com"),
        }

        def _resolve(fn):
            import inspect
            kwargs = {}
            for name in inspect.signature(fn).parameters:
                if name == "client":
                    kwargs["client"] = client
                elif name in fixtures:
                    kwargs[name] = fixtures[name]
            return fn(**kwargs)

        steps = [
            test_1_same_email_same_case_matches,
            test_2_user_lowercase_chariow_uppercase_matches,
            test_3_user_uppercase_chariow_lowercase_matches,
            test_4_mixed_case_matches,
            test_5_surrounding_whitespace_normalized,
            test_6_genuinely_different_email_does_not_match,
            test_6b_normalize_email_helper_strip_and_lower,
            test_6c_historical_email_never_mutated,
            test_7_activate_license_case_mismatch_still_resolves_correct_user,
            test_7b_activate_license_case_mismatch_never_leaks_to_other_account,
            test_8_webhook_license_activated_case_mismatch_updates_correct_subscription,
            test_9_webhook_license_revoked_case_mismatch_updates_correct_subscription,
            test_10_isolation_similar_emails_never_cross_accounts,
        ]
        for fn in steps:
            print(f"\n=== {fn.__name__} ===")
            try:
                _resolve(fn)
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
