"""
test_entitlement.py — Tests du service centralisé recompute_entitlement/
is_premium (app/billing/entitlement_service.py) et de son branchement dans
require_active_subscription (app/billing/dependencies.py) et dans
GET /billing/subscription (app/billing/router.py).

Deux familles de tests :
  1. Unitaires sur recompute_entitlement/is_premium directement (rapides,
     sans passer par l'API HTTP) — Chariow actif/expiré/révoqué, changement
     de plan sans bascule de `premium`, absence de ligne.
  2. Intégration via TestClient — le flux Pulse réel active/désactive bien
     l'Entitlement, un webhook rejoué ne journalise pas un second
     EntitlementEvent, et le contrat JSON de GET /billing/subscription est
     exactement celui attendu par billing.html/vip.html (mêmes clés,
     mêmes types).

Usage : python api/test_entitlement.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import (
    configure_test_env, cleanup_db, sign_payload, make_event,
    register_and_login, activate_subscription, cancel_subscription,
)

WEBHOOK_SECRET = "pulse_test_secret_for_signature_verification"
DB_PATH = configure_test_env("test_entitlement.db", webhook_secret=WEBHOOK_SECRET)

from datetime import datetime, timezone
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from main import app
from app.core.database import engine, init_db
from app.models.user import User
from app.models.provider_subscription import ProviderSubscription
from app.models.entitlement import Entitlement, EntitlementEvent
from app.billing.entitlement_service import recompute_entitlement, is_premium

init_db()

EMAIL = "entitlement-tests@example.com"
PASSWORD = "correct-horse-battery-staple"


# ---------------------------------------------------------------------------
# 1. Unitaires
# ---------------------------------------------------------------------------

def _make_user(session: Session, email: str) -> User:
    user = User(email=email, name="Test", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def test_is_premium_false_when_no_row_at_all():
    with Session(engine) as session:
        user = _make_user(session, "unit-no-row@example.com")
        assert is_premium(session, user.id) is False
    print("  [OK] is_premium() -> False pour un utilisateur sans aucune ligne ProviderSubscription/Entitlement")


def test_recompute_entitlement_chariow_active_grants_and_logs():
    with Session(engine) as session:
        user = _make_user(session, "unit-active@example.com")
        ps = ProviderSubscription(user_id=user.id, provider="chariow", status="active", plan="monthly")
        session.add(ps)
        session.commit()
        session.refresh(ps)

        ent = recompute_entitlement(session, user.id, provider_subscription_id=ps.id, reason="test: active")
        assert ent.premium is True
        assert "chariow" in ent.active_sources

        events = session.exec(select(EntitlementEvent).where(EntitlementEvent.user_id == user.id)).all()
        assert len(events) == 1
        assert events[0].event_type == "granted"
        assert events[0].previous_premium is False
        assert events[0].new_premium is True
    print("  [OK] Chariow actif -> Entitlement.premium=True, 1 EntitlementEvent 'granted'")


def test_recompute_entitlement_chariow_expired_revokes():
    with Session(engine) as session:
        user = _make_user(session, "unit-expired@example.com")
        ps = ProviderSubscription(user_id=user.id, provider="chariow", status="active", plan="monthly")
        session.add(ps)
        session.commit()
        session.refresh(ps)
        recompute_entitlement(session, user.id, provider_subscription_id=ps.id)

        ps.status = "expired"
        session.add(ps)
        session.commit()
        ent = recompute_entitlement(session, user.id, provider_subscription_id=ps.id, reason="test: expired")

        assert ent.premium is False
        events = session.exec(
            select(EntitlementEvent).where(EntitlementEvent.user_id == user.id).order_by(EntitlementEvent.id)
        ).all()
        assert len(events) == 2
        assert events[-1].event_type == "revoked"
        assert events[-1].previous_premium is True
        assert events[-1].new_premium is False
    print("  [OK] Chariow expiré -> Entitlement.premium=False, EntitlementEvent 'revoked'")


def test_recompute_entitlement_chariow_revoked():
    with Session(engine) as session:
        user = _make_user(session, "unit-revoked@example.com")
        ps = ProviderSubscription(user_id=user.id, provider="chariow", status="active", plan="yearly")
        session.add(ps)
        session.commit()
        session.refresh(ps)
        recompute_entitlement(session, user.id, provider_subscription_id=ps.id)

        ps.status = "revoked"
        ps.revoked_or_refunded_at = datetime.now(timezone.utc)
        session.add(ps)
        session.commit()
        ent = recompute_entitlement(session, user.id, provider_subscription_id=ps.id, reason="test: revoked")
        assert ent.premium is False
    print("  [OK] Chariow révoqué -> Entitlement.premium=False")


def test_plan_change_while_active_does_not_duplicate_event():
    """monthly -> yearly en restant actif tout du long : `premium` ne
    bascule jamais (True avant, True après) -> aucun EntitlementEvent
    supplémentaire ne doit être créé, seul ProviderSubscription.plan change."""
    with Session(engine) as session:
        user = _make_user(session, "unit-plan-change@example.com")
        ps = ProviderSubscription(user_id=user.id, provider="chariow", status="active", plan="monthly")
        session.add(ps)
        session.commit()
        session.refresh(ps)
        recompute_entitlement(session, user.id, provider_subscription_id=ps.id)

        events_before = len(session.exec(select(EntitlementEvent).where(EntitlementEvent.user_id == user.id)).all())

        ps.plan = "yearly"
        session.add(ps)
        session.commit()
        ent = recompute_entitlement(session, user.id, provider_subscription_id=ps.id, reason="test: renewal plan change")

        events_after = len(session.exec(select(EntitlementEvent).where(EntitlementEvent.user_id == user.id)).all())

        assert ent.premium is True
        assert ps.plan == "yearly"
        assert events_after == events_before, "aucun nouvel EntitlementEvent attendu (premium reste True)"
    print("  [OK] changement de plan monthly->yearly en restant actif -> premium inchangé, pas de nouvel EntitlementEvent")


# ---------------------------------------------------------------------------
# 2. Intégration (TestClient, flux Pulse réel)
# ---------------------------------------------------------------------------

def test_duplicate_webhook_does_not_duplicate_entitlement_event(client):
    user_id, token = register_and_login(client, EMAIL, PASSWORD)
    activate_subscription(client, token, user_id, email=EMAIL, webhook_secret=WEBHOOK_SECRET,
                           delivery_id_sale="ent_pulse_sale", delivery_id_license="ent_pulse_activate")

    with Session(engine) as session:
        events_after_first = len(session.exec(select(EntitlementEvent).where(EntitlementEvent.user_id == user_id)).all())

    # Rejoue exactement la même delivery successful.sale (même x-pulse-delivery-id)
    payload = make_event("successful.sale",
                          sale={"custom_metadata": {"user_id": str(user_id), "plan": "monthly"}},
                          customer={"email": EMAIL})
    sig = sign_payload(payload, WEBHOOK_SECRET)
    r = client.post("/billing/pulse", content=payload, headers={
        "x-chariow-signature": sig, "x-pulse-delivery-id": "ent_pulse_sale",  # même delivery_id
        "content-type": "application/json",
    })
    assert r.status_code == 200
    assert r.json().get("duplicate") is True

    with Session(engine) as session:
        events_after_replay = len(session.exec(select(EntitlementEvent).where(EntitlementEvent.user_id == user_id)).all())

    assert events_after_replay == events_after_first, "un webhook rejoué (dedup delivery_id) ne doit jamais retraiter, donc jamais recalculer l'entitlement"
    print(f"  [OK] webhook dupliqué (x-pulse-delivery-id déjà vu) -> aucun nouvel EntitlementEvent ({events_after_first} inchangé)")
    return token


def test_require_active_subscription_reflects_entitlement(client, token):
    headers = {"Authorization": f"Bearer {token}"}
    r = client.get("/predictions/Ligue1/PSG/Marseille", headers=headers)
    assert r.status_code == 200, r.text
    print("  [OK] Entitlement actif (via flux Pulse réel) -> endpoint premium accessible (200)")

    cancel_subscription(client, EMAIL, webhook_secret=WEBHOOK_SECRET, delivery_id="ent_pulse_revoke")
    r = client.get("/predictions/Ligue1/PSG/Marseille", headers=headers)
    assert r.status_code == 402, r.text
    print("  [OK] après license.revoked -> Entitlement.premium=False -> 402")


def test_get_billing_subscription_contract_unchanged(client):
    user_id, token = register_and_login(client, "contract-check@example.com", PASSWORD)
    r = client.get("/billing/subscription", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    body = r.json()
    expected_keys = {"status", "plan", "is_active", "current_period_end", "days_until_expiry"}
    assert set(body.keys()) == expected_keys, f"clés inattendues : {set(body.keys())}"
    assert body == {"status": "none", "plan": None, "is_active": False,
                     "current_period_end": None, "days_until_expiry": None}
    print(f"  [OK] GET /billing/subscription -> exactement les 5 clés attendues, valeurs par défaut inchangées")


if __name__ == "__main__":
    failures = 0

    unit_steps = [
        ("test_is_premium_false_when_no_row_at_all", test_is_premium_false_when_no_row_at_all),
        ("test_recompute_entitlement_chariow_active_grants_and_logs", test_recompute_entitlement_chariow_active_grants_and_logs),
        ("test_recompute_entitlement_chariow_expired_revokes", test_recompute_entitlement_chariow_expired_revokes),
        ("test_recompute_entitlement_chariow_revoked", test_recompute_entitlement_chariow_revoked),
        ("test_plan_change_while_active_does_not_duplicate_event", test_plan_change_while_active_does_not_duplicate_event),
    ]
    for name, fn in unit_steps:
        print(f"\n=== {name} ===")
        try:
            fn()
        except AssertionError as e:
            failures += 1
            print(f"  [ECHEC] {e}")
        except Exception as e:
            failures += 1
            print(f"  [ERREUR INATTENDUE] {type(e).__name__}: {e}")

    with TestClient(app) as client:
        integration_steps = [
            ("test_get_billing_subscription_contract_unchanged", lambda: test_get_billing_subscription_contract_unchanged(client)),
        ]
        token_holder = {}

        def _dup():
            token_holder["token"] = test_duplicate_webhook_does_not_duplicate_entitlement_event(client)

        integration_steps.append(("test_duplicate_webhook_does_not_duplicate_entitlement_event", _dup))
        integration_steps.append((
            "test_require_active_subscription_reflects_entitlement",
            lambda: test_require_active_subscription_reflects_entitlement(client, token_holder["token"]),
        ))

        for name, fn in integration_steps:
            print(f"\n=== {name} ===")
            try:
                fn()
            except AssertionError as e:
                failures += 1
                print(f"  [ECHEC] {e}")
            except Exception as e:
                failures += 1
                print(f"  [ERREUR INATTENDUE] {type(e).__name__}: {e}")

    n_tests = len(unit_steps) + len(integration_steps)
    cleanup_db(DB_PATH)
    print(f"\n{'='*60}\n{n_tests - failures}/{n_tests} tests reussis\n{'='*60}")
    sys.exit(1 if failures else 0)
