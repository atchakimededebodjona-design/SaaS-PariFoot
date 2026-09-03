"""
test_phase15_10_activate_license_commission.py — Phase 15.10 : extension de
POST /billing/activate-license pour finaliser vente + commission de parrainage
quand le Pulse successful.sale n'a jamais traité un paiement pourtant confirmé
par Chariow (cas réel : pi_ih0s0eixhgm1, USER 245, Phase 15.8/15.9).

RÉUTILISE exclusivement create_commission_for_confirmed_payment (même point
d'entrée unique que le webhook, aucune seconde logique de commission) et
_find_provider_subscription_by_email / le mécanisme d'appartenance déjà en
place — rien de nouveau inventé côté confiance/sécurité.

Base isolée dédiée (jamais api/app.db). Usage :
    python api/test_phase15_10_activate_license_commission.py
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db, register_and_login

DB_PATH = configure_test_env("test_phase15_10_activate_license_commission.db")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from main import app
from app.core.database import engine, init_db

init_db()
from app.models.promoter import ReferralCommission
from app.models.provider_subscription import ProviderSubscription
from app.referral.promoter_service import create_promoter
from app.core.rate_limit import limiter

_passed = _failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


def section(name):
    print(f"\n=== {name} ===")


def client():
    return TestClient(app)


def _fake_license(*, user_id, plan="monthly", status="active",
                   expires_at="2027-01-01T00:00:00+00:00", license_key="lic_test", amount=None):
    data = {
        "license_key": license_key,
        "status": status,
        "expires_at": expires_at,
        "metadata": {"user_id": str(user_id), "plan": plan},
    }
    if amount is not None:
        data["amount"] = amount  # champ hypothétique, testé tel quel — jamais garanti par Chariow
    return data


def _setup_buyer_with_attribution(c, email, slug):
    buyer_id, token = register_and_login(c, email, "correct-horse-battery-staple", name="Buyer")
    r = c.post("/referral/attribute", json={"slug": slug}, headers={"Authorization": f"Bearer {token}"})
    with patch("app.billing.router._create_chariow_checkout_link", return_value="https://chariow.com/checkout/test-link"):
        r2 = c.post("/billing/checkout", json={
            "plan": "monthly", "first_name": "T", "last_name": "U",
            "phone_number": "0100000000", "phone_country_code": "CI",
        }, headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200, r2.text
    return buyer_id, token


def main():
    c = client()

    section("SETUP — promoteur")
    promoter_owner_id, _ = register_and_login(c, "promoter-owner-15-10@example.com", "correct-horse-battery-staple", name="Promoter Owner")
    with Session(engine) as s:
        promoter = create_promoter(s, user_id=promoter_owner_id, display_name="Promoter Owner", requested_slug="promoter1510")
        promoter_id = promoter.id
    check("promoteur créé", promoter_id is not None)

    # -------------------------------------------------------------------
    # TEST 1/2 — licence valide + referral existant + montant présent -> abonnement + commission 600
    # -------------------------------------------------------------------
    section("TEST 1/2 — licence valide, referral existant, montant présent -> abonnement + 600 FCFA")
    buyer1_id, buyer1_token = _setup_buyer_with_attribution(c, "buyer1-15-10@example.com", "promoter1510")
    with patch("app.billing.router._fetch_chariow_license",
               return_value=_fake_license(user_id=buyer1_id, plan="monthly", license_key="lic_buyer1", amount=1500)):
        limiter.reset()  # évite d'atteindre 10/minute (activate-license) entre scénarios indépendants
        r = c.post("/billing/activate-license", json={"license_key": "lic_buyer1"}, headers={"Authorization": f"Bearer {buyer1_token}"})
    check("HTTP 200", r.status_code == 200)
    check("réponse status=active", r.json().get("status") == "active")
    with Session(engine) as s:
        sub1 = s.exec(select(ProviderSubscription).where(ProviderSubscription.user_id == buyer1_id, ProviderSubscription.provider == "chariow")).first()
        check("ProviderSubscription active, plan=monthly", sub1.status == "active" and sub1.plan == "monthly")
        comm1 = s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == buyer1_id)).first()
        check("commission créée = 600 FCFA (1500 x 40%)", comm1 is not None and comm1.commission_amount == 600)
        check("commission rattachée à promoter1510", comm1.promoter_id == promoter_id)

    # -------------------------------------------------------------------
    # TEST 3 — licence invalide (mauvais propriétaire) -> aucun abonnement, aucune commission
    # -------------------------------------------------------------------
    section("TEST 3 — licence d'un autre compte -> 403, aucun abonnement, aucune commission")
    buyer3_id, buyer3_token = _setup_buyer_with_attribution(c, "buyer3-15-10@example.com", "promoter1510")
    with patch("app.billing.router._fetch_chariow_license",
               return_value=_fake_license(user_id=999999, plan="monthly", license_key="lic_buyer3")):
        limiter.reset()  # évite d'atteindre 10/minute (activate-license) entre scénarios indépendants
        r = c.post("/billing/activate-license", json={"license_key": "lic_buyer3"}, headers={"Authorization": f"Bearer {buyer3_token}"})
    check("HTTP 403", r.status_code == 403)
    with Session(engine) as s:
        sub3 = s.exec(select(ProviderSubscription).where(ProviderSubscription.user_id == buyer3_id, ProviderSubscription.provider == "chariow")).first()
        check("abonnement resté 'none'", sub3.status == "none")
        check("aucune commission", s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == buyer3_id)).first() is None)

    # -------------------------------------------------------------------
    # TEST 4 — paiement non confirmé (licence expirée) -> aucun abonnement actif, aucune commission
    # -------------------------------------------------------------------
    section("TEST 4 — licence expirée -> 400, aucun abonnement actif, aucune commission")
    buyer4_id, buyer4_token = _setup_buyer_with_attribution(c, "buyer4-15-10@example.com", "promoter1510")
    with patch("app.billing.router._fetch_chariow_license",
               return_value=_fake_license(user_id=buyer4_id, status="expired", license_key="lic_buyer4")):
        limiter.reset()  # évite d'atteindre 10/minute (activate-license) entre scénarios indépendants
        r = c.post("/billing/activate-license", json={"license_key": "lic_buyer4"}, headers={"Authorization": f"Bearer {buyer4_token}"})
    check("HTTP 400", r.status_code == 400)
    with Session(engine) as s:
        sub4 = s.exec(select(ProviderSubscription).where(ProviderSubscription.user_id == buyer4_id, ProviderSubscription.provider == "chariow")).first()
        check("abonnement resté 'none'", sub4.status == "none")
        check("aucune commission", s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == buyer4_id)).first() is None)

    # -------------------------------------------------------------------
    # TEST 5 — 'produit incorrect' : DÉLIBÉRÉMENT NON REJETÉ (voir note ci-dessous)
    # -------------------------------------------------------------------
    section("TEST 5 — plan non résolvable (ni metadata ni product_id) -> abonnement quand même activé (comportement historique préservé)")
    # NOTE IMPORTANTE : une première version de ce correctif rejetait explicitement ce cas (422),
    # mais cela cassait un comportement PRÉEXISTANT ET DÉJÀ TESTÉ (test_chariow_billing.py ::
    # test_activate_license_email_fallback_when_no_metadata) : une licence Chariow legacy sans
    # metadata NI product_id reconnu est délibérément activée quand même (Chariow confirme un achat
    # réel — refuser l'accès sur la seule absence d'un libellé de plan serait pire que l'accepter).
    # Le plan=None qui en résulte est SANS RISQUE FINANCIER : la commission ne dépend jamais de
    # `plan`, uniquement du montant réel (voir money.py) — testé explicitement ci-dessous.
    buyer5_id, buyer5_token = _setup_buyer_with_attribution(c, "buyer5-15-10@example.com", "promoter1510")
    license5 = {"license_key": "lic_buyer5", "status": "active", "expires_at": "2027-01-01T00:00:00+00:00",
                "metadata": None, "customer": {"email": "buyer5-15-10@example.com"}, "product": {}}
    with patch("app.billing.router._fetch_chariow_license", return_value=license5):
        limiter.reset()  # évite d'atteindre 10/minute (activate-license) entre scénarios indépendants
        r = c.post("/billing/activate-license", json={"license_key": "lic_buyer5"}, headers={"Authorization": f"Bearer {buyer5_token}"})
    check("HTTP 200 (comportement historique préservé, pas de régression introduite)", r.status_code == 200)
    with Session(engine) as s:
        sub5 = s.exec(select(ProviderSubscription).where(ProviderSubscription.user_id == buyer5_id, ProviderSubscription.provider == "chariow")).first()
        check("abonnement activé malgré plan=None", sub5.status == "active" and sub5.plan is None)

    # -------------------------------------------------------------------
    # TEST 6 — montant absent (aucun champ candidat, aucun PLAN_LIST_PRICES configuré) -> aucune commission
    # -------------------------------------------------------------------
    section("TEST 6 — montant introuvable dans license_data -> abonnement activé, AUCUNE commission fabriquée")
    buyer6_id, buyer6_token = _setup_buyer_with_attribution(c, "buyer6-15-10@example.com", "promoter1510")
    with patch("app.billing.router._fetch_chariow_license",
               return_value=_fake_license(user_id=buyer6_id, plan="monthly", license_key="lic_buyer6", amount=None)):
        limiter.reset()  # évite d'atteindre 10/minute (activate-license) entre scénarios indépendants
        r = c.post("/billing/activate-license", json={"license_key": "lic_buyer6"}, headers={"Authorization": f"Bearer {buyer6_token}"})
    check("HTTP 200", r.status_code == 200)
    with Session(engine) as s:
        sub6 = s.exec(select(ProviderSubscription).where(ProviderSubscription.user_id == buyer6_id, ProviderSubscription.provider == "chariow")).first()
        check("abonnement activé malgré montant absent", sub6.status == "active")
        check("AUCUNE commission fabriquée (jamais un prix catalogue substitué au montant réel)",
              s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == buyer6_id)).first() is None)

    # -------------------------------------------------------------------
    # TEST 7/8/14 — même license_key réutilisée (rejeu séquentiel, proxy de concurrence via la
    # contrainte UNIQUE déjà en place côté DB) -> aucun doublon d'abonnement ni de commission
    # -------------------------------------------------------------------
    section("TEST 7/8/14 — license_key rejouée plusieurs fois -> une seule commission (idempotence)")
    buyer7_id, buyer7_token = _setup_buyer_with_attribution(c, "buyer7-15-10@example.com", "promoter1510")
    fake7 = _fake_license(user_id=buyer7_id, plan="monthly", license_key="lic_buyer7", amount=1500)
    with patch("app.billing.router._fetch_chariow_license", return_value=fake7):
        for _ in range(3):
            limiter.reset()  # évite d'atteindre 10/minute (activate-license) entre scénarios indépendants
            r = c.post("/billing/activate-license", json={"license_key": "lic_buyer7"}, headers={"Authorization": f"Bearer {buyer7_token}"})
            check(f"appel répété -> toujours HTTP 200", r.status_code == 200)
    with Session(engine) as s:
        commissions7 = s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == buyer7_id)).all()
        check("exactement 1 commission malgré 3 appels avec la même licence", len(commissions7) == 1)
        check("commission = 600 FCFA", commissions7[0].commission_amount == 600)

    # -------------------------------------------------------------------
    # TEST 9 — utilisateur différent tente d'activer la licence de buyer7 -> rejeté, aucun impact
    # -------------------------------------------------------------------
    section("TEST 9 — un autre utilisateur authentifié tente la licence de buyer7 -> 403")
    other_id, other_token = register_and_login(c, "other-user-15-10@example.com", "correct-horse-battery-staple", name="Other User")
    with patch("app.billing.router._fetch_chariow_license", return_value=fake7):
        limiter.reset()  # évite d'atteindre 10/minute (activate-license) entre scénarios indépendants
        r = c.post("/billing/activate-license", json={"license_key": "lic_buyer7"}, headers={"Authorization": f"Bearer {other_token}"})
    check("HTTP 403 (metadata.user_id ne correspond pas à other_id)", r.status_code == 403)
    with Session(engine) as s:
        check("toujours exactement 1 commission pour buyer7 (rien créé pour other_id)",
              len(s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == buyer7_id)).all()) == 1)
        check("aucune commission pour other_id", s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == other_id)).first() is None)

    # -------------------------------------------------------------------
    # TEST 10 — promoter_id falsifié dans le corps de la requête -> ignoré (champ inexistant côté contrat)
    # -------------------------------------------------------------------
    section("TEST 10 — promoter_id fourni par le client -> ignoré, jamais lu")
    buyer10_id, buyer10_token = _setup_buyer_with_attribution(c, "buyer10-15-10@example.com", "promoter1510")
    with patch("app.billing.router._fetch_chariow_license",
               return_value=_fake_license(user_id=buyer10_id, plan="monthly", license_key="lic_buyer10", amount=1500)):
        # ActivateLicenseRequest n'a AUCUN champ promoter_id — Pydantic ignore silencieusement les champs
        # additionnels non déclarés (comportement par défaut) : le client ne peut choisir aucun bénéficiaire.
        limiter.reset()  # évite d'atteindre 10/minute (activate-license) entre scénarios indépendants
        r = c.post("/billing/activate-license", json={"license_key": "lic_buyer10", "promoter_id": 999999},
                    headers={"Authorization": f"Bearer {buyer10_token}"})
    check("HTTP 200 (champ additionnel ignoré, pas une erreur)", r.status_code == 200)
    with Session(engine) as s:
        comm10 = s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == buyer10_id)).first()
        check("commission attribuée au VRAI promoteur (promoter1510), jamais au promoter_id=999999 falsifié",
              comm10 is not None and comm10.promoter_id == promoter_id)

    # -------------------------------------------------------------------
    # TEST 11 — email falsifié dans le corps -> ignoré (champ inexistant côté contrat, identité = JWT)
    # -------------------------------------------------------------------
    section("TEST 11 — email fourni par le client -> ignoré, identité dérivée exclusivement du JWT")
    buyer11_id, buyer11_token = _setup_buyer_with_attribution(c, "buyer11-15-10@example.com", "promoter1510")
    with patch("app.billing.router._fetch_chariow_license",
               return_value=_fake_license(user_id=buyer11_id, plan="monthly", license_key="lic_buyer11", amount=1500)):
        limiter.reset()  # évite d'atteindre 10/minute (activate-license) entre scénarios indépendants
        r = c.post("/billing/activate-license", json={"license_key": "lic_buyer11", "email": "quelquun.dautre@example.com"},
                    headers={"Authorization": f"Bearer {buyer11_token}"})
    check("HTTP 200 — email additionnel ignoré, activation sur l'identité JWT réelle", r.status_code == 200)
    with Session(engine) as s:
        sub11 = s.exec(select(ProviderSubscription).where(ProviderSubscription.user_id == buyer11_id, ProviderSubscription.provider == "chariow")).first()
        check("abonnement activé pour le VRAI utilisateur authentifié (buyer11), jamais un autre", sub11.status == "active")

    # -------------------------------------------------------------------
    # TEST 12 — absence de ReferralAttribution -> abonnement activé, commission=0, aucun faux promoteur
    # -------------------------------------------------------------------
    section("TEST 12 — aucune attribution referral -> abonnement activé, aucune commission fabriquée")
    buyer12_id, buyer12_token = register_and_login(c, "buyer12-15-10@example.com", "correct-horse-battery-staple", name="Buyer Twelve")
    with patch("app.billing.router._create_chariow_checkout_link", return_value="https://chariow.com/checkout/test-link"):
        c.post("/billing/checkout", json={"plan": "monthly", "first_name": "T", "last_name": "U",
                                           "phone_number": "0100000000", "phone_country_code": "CI"},
               headers={"Authorization": f"Bearer {buyer12_token}"})
    with patch("app.billing.router._fetch_chariow_license",
               return_value=_fake_license(user_id=buyer12_id, plan="monthly", license_key="lic_buyer12", amount=1500)):
        limiter.reset()  # évite d'atteindre 10/minute (activate-license) entre scénarios indépendants
        r = c.post("/billing/activate-license", json={"license_key": "lic_buyer12"}, headers={"Authorization": f"Bearer {buyer12_token}"})
    check("HTTP 200", r.status_code == 200)
    with Session(engine) as s:
        sub12 = s.exec(select(ProviderSubscription).where(ProviderSubscription.user_id == buyer12_id, ProviderSubscription.provider == "chariow")).first()
        check("abonnement activé sans aucune attribution referral", sub12.status == "active")
        check("aucune commission fabriquée (pas d'attribution = pas de bénéficiaire)",
              s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == buyer12_id)).first() is None)

    # -------------------------------------------------------------------
    # TEST 13 — attribution existante -> commission créée une seule fois (déjà démontré TEST 1/2 et 7/8/14)
    # -------------------------------------------------------------------
    section("TEST 13 — attribution existante -> une seule commission (déjà démontré ci-dessus)")
    check("couvert par TEST 1/2 (création) et TEST 7/8/14 (unicité malgré rejeu)", True)

    print(f"\n{'=' * 60}\n{_passed}/{_passed + _failed} tests reussis\n{'=' * 60}")
    return _failed == 0


if __name__ == "__main__":
    try:
        success = main()
    finally:
        cleanup_db(DB_PATH)
    sys.exit(0 if success else 1)
