"""
test_provider_subscription_backfill.py — Tests du backfill Chariow
(Subscription -> ProviderSubscription -> Entitlement), voir
app/billing/backfill.py.

N'utilise PAS TestClient/l'API HTTP : sème directement d'anciennes lignes
Subscription (simulant l'état d'une base pré-migration) puis appelle les
fonctions de backfill telles quelles — exactement ce que fait la migration
Alembic ecf43e1d3a33 (voir api/alembic/versions/), sans dupliquer sa
logique.

Usage : python api/test_provider_subscription_backfill.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_provider_subscription_backfill.db")

from datetime import datetime, timezone
from sqlmodel import Session, select

from app.core.database import engine, init_db
from app.models.user import User
from app.models.subscription import Subscription
from app.models.provider_subscription import ProviderSubscription
from app.models.entitlement import Entitlement, EntitlementEvent
from app.billing.backfill import backfill_provider_subscriptions, backfill_entitlements

init_db()


def _make_user(session: Session, email: str) -> User:
    user = User(email=email, name="Test", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def test_backfill_copies_every_subscription_row(client=None):
    with Session(engine) as session:
        u1 = _make_user(session, "backfill-active@example.com")
        u2 = _make_user(session, "backfill-none@example.com")
        u3 = _make_user(session, "backfill-expired@example.com")

        session.add(Subscription(
            user_id=u1.id, chariow_license_key="lic_backfill_1", status="active",
            plan="monthly", current_period_end=datetime(2027, 1, 1, tzinfo=timezone.utc),
            days_until_expiry=None,
        ))
        session.add(Subscription(user_id=u2.id, status="none"))  # checkout initié, jamais payé
        session.add(Subscription(
            user_id=u3.id, chariow_license_key="lic_backfill_3", status="expired", plan="yearly",
        ))
        session.commit()

        n_created = backfill_provider_subscriptions(session)
        assert n_created == 3, f"attendu 3 lignes créées, obtenu {n_created}"

        ps1 = session.exec(select(ProviderSubscription).where(ProviderSubscription.user_id == u1.id)).first()
        assert ps1.provider == "chariow"
        assert ps1.external_ref == "lic_backfill_1"
        assert ps1.status == "active"
        assert ps1.plan == "monthly"
        assert ps1.auto_renewing is False
        assert ps1.current_period_end.replace(tzinfo=timezone.utc) == datetime(2027, 1, 1, tzinfo=timezone.utc)

        ps2 = session.exec(select(ProviderSubscription).where(ProviderSubscription.user_id == u2.id)).first()
        assert ps2.status == "none"
        assert ps2.external_ref is None

        ps3 = session.exec(select(ProviderSubscription).where(ProviderSubscription.user_id == u3.id)).first()
        assert ps3.status == "expired"
        assert ps3.external_ref == "lic_backfill_3"

    print("  [OK] backfill_provider_subscriptions copie fidèlement chaque ligne Subscription (active/none/expired)")


def test_backfill_is_idempotent():
    with Session(engine) as session:
        before = len(session.exec(select(ProviderSubscription)).all())
        n_created_again = backfill_provider_subscriptions(session)
        after = len(session.exec(select(ProviderSubscription)).all())
    assert n_created_again == 0, "un rejeu du backfill ne doit créer aucune nouvelle ligne"
    assert before == after
    print("  [OK] backfill_provider_subscriptions est idempotent (rejeu sans duplication)")


def test_backfill_entitlements_matches_status():
    with Session(engine) as session:
        n_users = backfill_entitlements(session)
        assert n_users == 3

        u1 = session.exec(select(User).where(User.email == "backfill-active@example.com")).first()
        u2 = session.exec(select(User).where(User.email == "backfill-none@example.com")).first()
        u3 = session.exec(select(User).where(User.email == "backfill-expired@example.com")).first()

        ent1 = session.get(Entitlement, u1.id)
        assert ent1.premium is True
        assert "chariow" in ent1.active_sources

        ent2 = session.get(Entitlement, u2.id)
        assert ent2.premium is False

        ent3 = session.get(Entitlement, u3.id)
        assert ent3.premium is False

    print("  [OK] backfill_entitlements calcule premium=True/False conformément au statut Chariow")


def test_backfill_never_creates_entitlement_for_untouched_users():
    with Session(engine) as session:
        untouched = _make_user(session, "never-touched-billing@example.com")
        # Pas de Subscription pour cet utilisateur -> pas de ProviderSubscription
        # après un nouveau backfill -> pas d'Entitlement non plus.
        backfill_provider_subscriptions(session)
        backfill_entitlements(session)
        ent = session.get(Entitlement, untouched.id)
    assert ent is None, "un utilisateur n'ayant jamais touché à la facturation ne doit recevoir aucune ligne Entitlement"
    print("  [OK] aucun Entitlement créé pour un utilisateur sans historique de facturation")


if __name__ == "__main__":
    failures = 0
    steps = [
        ("test_backfill_copies_every_subscription_row", test_backfill_copies_every_subscription_row),
        ("test_backfill_is_idempotent", test_backfill_is_idempotent),
        ("test_backfill_entitlements_matches_status", test_backfill_entitlements_matches_status),
        ("test_backfill_never_creates_entitlement_for_untouched_users", test_backfill_never_creates_entitlement_for_untouched_users),
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
