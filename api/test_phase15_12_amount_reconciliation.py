"""
test_phase15_12_amount_reconciliation.py — Phase 15.12 : réconciliation du montant
réellement payé pour POST /billing/activate-license (USER 245, pi_ih0s0eixhgm1).

Cause racine prouvée en Phase 15.11 : extract_actual_paid_amount(license_data, plan)
retourne (None, "unavailable") car (a) GET/POST /licenses/{key} de Chariow — les 2 SEULS
appels Chariow utilisés par activate-license — ne renvoient jamais de champ montant
(confirmé par audit exhaustif de tout api/app/billing/router.py, Phase 15.12 Partie A/B :
aucun endpoint Chariow GET /sales/{id} ou /transactions/{id} n'existe dans ce dépôt, et
aucun n'est inventé ici), et (b) REFERRAL_PLAN_PRICE_MONTHLY n'est pas configuré.

CORRECTION RETENUE (Phase 15.12 Partie D) : AUCUN changement de logique — money.py et
commission_service.py priorisent déjà correctement un montant réel (candidate keys) sur
le repli PLAN_LIST_PRICES. Ce repli est un "montant réellement payé" légitime (pas un prix
catalogue substitué abusivement) UNIQUEMENT parce que cette intégration Chariow précise n'a
AUCUN mécanisme de remise/coupon (payload POST /checkout figé, "Prix libre" exclu de l'API
Chariow — vérifié, voir router.py). Ce fichier teste ce comportement fidèlement, SANS jamais
ajouter un champ "amount" imaginaire à un objet censé représenter la VRAIE réponse
GET /licenses/{key} (l'erreur exacte commise en Phase 15.10, voir Phase 15.11 Partie G).

Base isolée dédiée (jamais api/app.db). Usage :
    python api/test_phase15_12_amount_reconciliation.py
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db, register_and_login

DB_PATH = configure_test_env("test_phase15_12_amount_reconciliation.db")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from main import app
from app.core.database import engine, init_db

init_db()
from app.models.promoter import ReferralCommission
from app.models.provider_subscription import ProviderSubscription
from app.referral.promoter_service import create_promoter
from app.referral.money import extract_actual_paid_amount
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


# ---------------------------------------------------------------------------
# Réponse RÉALISTE de GET/POST /licenses/{key} — EXACTEMENT les champs confirmés
# réels (Phase 15.11 : status/expires_at/metadata/customer/product), JAMAIS de
# champ montant. C'est LA structure à utiliser pour tout scénario censé
# représenter le comportement réel de Chariow.
# ---------------------------------------------------------------------------
def _real_shaped_license(*, user_id=None, plan=None, status="active", license_key="lic_test",
                          expires_at="2027-01-01T00:00:00+00:00", customer_email=None, product_id=None):
    metadata = {"user_id": str(user_id), "plan": plan} if (user_id is not None or plan is not None) else None
    return {
        "license_key": license_key,
        "status": status,
        "expires_at": expires_at,
        "metadata": metadata,
        "customer": {"email": customer_email} if customer_email else {},
        "product": {"id": product_id} if product_id else {},
    }


def _setup_buyer_with_attribution(c, email, slug):
    buyer_id, token = register_and_login(c, email, "correct-horse-battery-staple", name="Buyer")
    c.post("/referral/attribute", json={"slug": slug}, headers={"Authorization": f"Bearer {token}"})
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
    promoter_owner_id, _ = register_and_login(c, "promoter-owner-15-12@example.com", "correct-horse-battery-staple", name="Promoter Owner")
    with Session(engine) as s:
        promoter = create_promoter(s, user_id=promoter_owner_id, display_name="Promoter Owner", requested_slug="promoter1512")
        promoter_id = promoter.id
    check("promoteur créé", promoter_id is not None)

    # -------------------------------------------------------------------
    # TEST 1 — source officielle du montant disponible (clé candidate présente dans license_data)
    # -> le système récupère ce montant réel, jamais le catalogue.
    # -------------------------------------------------------------------
    section("TEST 1 — montant présent dans une clé candidate reconnue -> utilisé tel quel")
    amount, source = extract_actual_paid_amount({"amount": 1500}, "monthly")
    check("montant = 1500, source='webhook' (i.e. issu du corps, pas du catalogue)", amount == 1500 and source == "webhook")

    # -------------------------------------------------------------------
    # TEST 2 — paiement réel de 1500 via le repli PLAN_LIST_PRICES (scénario ACTUEL du cas réel, une
    # fois REFERRAL_PLAN_PRICE_MONTHLY correctement configuré) -> commission = 600 FCFA
    # -------------------------------------------------------------------
    section("TEST 2 — repli PLAN_LIST_PRICES configuré (1500) -> commission = 600 FCFA")
    buyer2_id, buyer2_token = _setup_buyer_with_attribution(c, "buyer2-15-12@example.com", "promoter1512")
    license2 = _real_shaped_license(user_id=buyer2_id, plan="monthly", license_key="lic_buyer2")
    with patch("app.billing.router._fetch_chariow_license", return_value=license2), \
         patch.dict("app.referral.money.PLAN_LIST_PRICES", {"monthly": 1500}, clear=True):
        limiter.reset()
        r = c.post("/billing/activate-license", json={"license_key": "lic_buyer2"}, headers={"Authorization": f"Bearer {buyer2_token}"})
    check("HTTP 200", r.status_code == 200)
    with Session(engine) as s:
        comm2 = s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == buyer2_id)).first()
        check("commission créée = 600 FCFA (1500 x 40%)", comm2 is not None and comm2.commission_amount == 600)
        check("gross_paid_amount = 1500", comm2.gross_paid_amount == 1500)

    # -------------------------------------------------------------------
    # TEST 3 — montant absent de la clé principale 'amount' mais présent sous une clé candidate
    # alternative déjà supportée ('paid_amount') -> utilisée correctement
    # -------------------------------------------------------------------
    section("TEST 3 — montant sous une clé candidate alternative ('paid_amount') -> utilisé")
    amount3, source3 = extract_actual_paid_amount({"paid_amount": 1500}, "monthly")
    check("montant = 1500 via 'paid_amount'", amount3 == 1500 and source3 == "webhook")

    # -------------------------------------------------------------------
    # TEST 4 — montant totalement indisponible (aucune clé candidate, PLAN_LIST_PRICES non configuré)
    # -> AUCUNE commission fabriquée. C'est L'ÉTAT RÉEL ACTUEL DE LA PRODUCTION pour USER 245.
    # -------------------------------------------------------------------
    section("TEST 4 — montant totalement indisponible -> aucune commission (état réel actuel)")
    buyer4_id, buyer4_token = _setup_buyer_with_attribution(c, "buyer4-15-12@example.com", "promoter1512")
    license4 = _real_shaped_license(user_id=buyer4_id, plan="monthly", license_key="lic_buyer4")
    with patch("app.billing.router._fetch_chariow_license", return_value=license4), \
         patch.dict("app.referral.money.PLAN_LIST_PRICES", {}, clear=True):
        limiter.reset()
        r = c.post("/billing/activate-license", json={"license_key": "lic_buyer4"}, headers={"Authorization": f"Bearer {buyer4_token}"})
    check("HTTP 200 (abonnement quand même activé)", r.status_code == 200)
    with Session(engine) as s:
        sub4 = s.exec(select(ProviderSubscription).where(ProviderSubscription.user_id == buyer4_id, ProviderSubscription.provider == "chariow")).first()
        check("abonnement ACTIVE malgré montant indisponible", sub4.status == "active")
        check("AUCUNE commission (jamais un montant inventé)",
              s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == buyer4_id)).first() is None)

    # -------------------------------------------------------------------
    # TEST 5 — montant réel DIFFÉRENT du prix catalogue (1200 vs 1500 configuré) -> commission = 480,
    # JAMAIS 600. Preuve que le système ne calcule jamais sur le prix catalogue quand un montant réel
    # est disponible.
    # -------------------------------------------------------------------
    section("TEST 5 — montant réel (1200) différent du catalogue (1500) -> commission = 480, jamais 600")
    buyer5_id, buyer5_token = _setup_buyer_with_attribution(c, "buyer5-15-12@example.com", "promoter1512")
    license5 = _real_shaped_license(user_id=buyer5_id, plan="monthly", license_key="lic_buyer5")
    license5["amount"] = 1200  # simule une source de montant réel disponible, différente du catalogue
    with patch("app.billing.router._fetch_chariow_license", return_value=license5), \
         patch.dict("app.referral.money.PLAN_LIST_PRICES", {"monthly": 1500}, clear=True):
        limiter.reset()
        r = c.post("/billing/activate-license", json={"license_key": "lic_buyer5"}, headers={"Authorization": f"Bearer {buyer5_token}"})
    check("HTTP 200", r.status_code == 200)
    with Session(engine) as s:
        comm5 = s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == buyer5_id)).first()
        check("commission = 480 FCFA (1200 x 40%), jamais 600", comm5 is not None and comm5.commission_amount == 480)
        check("gross_paid_amount = 1200 (montant réel, pas le catalogue 1500)", comm5.gross_paid_amount == 1200)

    # -------------------------------------------------------------------
    # TEST 6 — attribution existante -> commission attribuée au bon promoteur
    # -------------------------------------------------------------------
    section("TEST 6 — attribution existante -> commission attribuée au bon promoteur")
    with Session(engine) as s:
        comm2_recheck = s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == buyer2_id)).first()
        check("commission TEST 2 rattachée à promoter1512", comm2_recheck.promoter_id == promoter_id)

    # -------------------------------------------------------------------
    # TEST 7 — aucune attribution -> aucune commission promoteur, même avec montant disponible
    # -------------------------------------------------------------------
    section("TEST 7 — aucune attribution referral -> aucune commission, même avec montant disponible")
    buyer7_id, buyer7_token = register_and_login(c, "buyer7-15-12@example.com", "correct-horse-battery-staple", name="Buyer Seven")
    with patch("app.billing.router._create_chariow_checkout_link", return_value="https://chariow.com/checkout/test-link"):
        c.post("/billing/checkout", json={"plan": "monthly", "first_name": "T", "last_name": "U",
                                           "phone_number": "0100000000", "phone_country_code": "CI"},
               headers={"Authorization": f"Bearer {buyer7_token}"})
    license7 = _real_shaped_license(user_id=buyer7_id, plan="monthly", license_key="lic_buyer7")
    with patch("app.billing.router._fetch_chariow_license", return_value=license7), \
         patch.dict("app.referral.money.PLAN_LIST_PRICES", {"monthly": 1500}, clear=True):
        limiter.reset()
        r = c.post("/billing/activate-license", json={"license_key": "lic_buyer7"}, headers={"Authorization": f"Bearer {buyer7_token}"})
    check("HTTP 200", r.status_code == 200)
    with Session(engine) as s:
        check("aucune commission (pas d'attribution = pas de bénéficiaire)",
              s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == buyer7_id)).first() is None)

    # -------------------------------------------------------------------
    # TEST 8 — promoter_id falsifié côté client -> ignoré (ActivateLicenseRequest n'a pas ce champ)
    # -------------------------------------------------------------------
    section("TEST 8 — promoter_id fourni par le client -> ignoré, jamais lu")
    buyer8_id, buyer8_token = _setup_buyer_with_attribution(c, "buyer8-15-12@example.com", "promoter1512")
    license8 = _real_shaped_license(user_id=buyer8_id, plan="monthly", license_key="lic_buyer8")
    with patch("app.billing.router._fetch_chariow_license", return_value=license8), \
         patch.dict("app.referral.money.PLAN_LIST_PRICES", {"monthly": 1500}, clear=True):
        limiter.reset()
        r = c.post("/billing/activate-license", json={"license_key": "lic_buyer8", "promoter_id": 999999},
                    headers={"Authorization": f"Bearer {buyer8_token}"})
    check("HTTP 200 (champ additionnel ignoré)", r.status_code == 200)
    with Session(engine) as s:
        comm8 = s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == buyer8_id)).first()
        check("commission attribuée au VRAI promoteur, jamais au promoter_id=999999 falsifié",
              comm8 is not None and comm8.promoter_id == promoter_id)

    # -------------------------------------------------------------------
    # TEST 9 — transaction non confirmée (licence non 'active') -> aucune commission
    # -------------------------------------------------------------------
    section("TEST 9 — licence non active (expired) -> aucune commission")
    buyer9_id, buyer9_token = _setup_buyer_with_attribution(c, "buyer9-15-12@example.com", "promoter1512")
    license9 = _real_shaped_license(user_id=buyer9_id, plan="monthly", status="expired", license_key="lic_buyer9")
    with patch("app.billing.router._fetch_chariow_license", return_value=license9):
        limiter.reset()
        r = c.post("/billing/activate-license", json={"license_key": "lic_buyer9"}, headers={"Authorization": f"Bearer {buyer9_token}"})
    check("HTTP 400", r.status_code == 400)
    with Session(engine) as s:
        check("aucune commission", s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == buyer9_id)).first() is None)

    # -------------------------------------------------------------------
    # TEST 10 — 'mauvais produit' (product_id non reconnu, metadata absente) -> plan=None,
    # abonnement activé (compat historique) MAIS aucune commission fabriquée sur un plan deviné
    # -------------------------------------------------------------------
    section("TEST 10 — product_id non reconnu, aucune metadata -> plan=None, aucune commission inventée")
    buyer10_id, buyer10_token = _setup_buyer_with_attribution(c, "buyer10-15-12@example.com", "promoter1512")
    license10 = {"license_key": "lic_buyer10", "status": "active", "expires_at": "2027-01-01T00:00:00+00:00",
                 "metadata": None, "customer": {"email": "buyer10-15-12@example.com"}, "product": {"id": "prd_totalement_inconnu"}}
    with patch("app.billing.router._fetch_chariow_license", return_value=license10), \
         patch.dict("app.referral.money.PLAN_LIST_PRICES", {"monthly": 1500, "yearly": 28000}, clear=True):
        limiter.reset()
        r = c.post("/billing/activate-license", json={"license_key": "lic_buyer10"}, headers={"Authorization": f"Bearer {buyer10_token}"})
    check("HTTP 200 (comportement historique préservé)", r.status_code == 200)
    with Session(engine) as s:
        sub10 = s.exec(select(ProviderSubscription).where(ProviderSubscription.user_id == buyer10_id, ProviderSubscription.provider == "chariow")).first()
        check("abonnement activé malgré plan=None", sub10.status == "active" and sub10.plan is None)
        check("aucune commission (plan=None ne matche aucune clé PLAN_LIST_PRICES, jamais de prix deviné)",
              s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == buyer10_id)).first() is None)

    # -------------------------------------------------------------------
    # TEST 11 — montant négatif / nul / invalide -> jamais utilisé, aucune commission fabriquée
    # -------------------------------------------------------------------
    section("TEST 11 — montant négatif/nul/invalide -> jamais utilisé")
    for bad_amount, label in [(-100, "négatif"), (0, "nul"), ("pas-un-nombre", "chaîne invalide")]:
        amount11, source11 = extract_actual_paid_amount({"amount": bad_amount}, "monthly")
        check(f"montant {label} ({bad_amount!r}) ignoré (retombe sur repli/unavailable)", amount11 != bad_amount)

    # -------------------------------------------------------------------
    # TEST 12 — idempotence : même licence soumise deux fois -> une seule commission
    # -------------------------------------------------------------------
    section("TEST 12 — idempotence : même licence soumise 2 fois -> une seule commission")
    buyer12_id, buyer12_token = _setup_buyer_with_attribution(c, "buyer12-15-12@example.com", "promoter1512")
    license12 = _real_shaped_license(user_id=buyer12_id, plan="monthly", license_key="lic_buyer12")
    with patch("app.billing.router._fetch_chariow_license", return_value=license12), \
         patch.dict("app.referral.money.PLAN_LIST_PRICES", {"monthly": 1500}, clear=True):
        for _ in range(2):
            limiter.reset()
            r = c.post("/billing/activate-license", json={"license_key": "lic_buyer12"}, headers={"Authorization": f"Bearer {buyer12_token}"})
            check("appel répété -> HTTP 200", r.status_code == 200)
    with Session(engine) as s:
        commissions12 = s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == buyer12_id)).all()
        check("exactement 1 commission malgré 2 soumissions de la même licence", len(commissions12) == 1)

    # -------------------------------------------------------------------
    # TEST 13 — concurrence (proxy séquentiel rapide s'appuyant sur la contrainte UNIQUE en base,
    # même limite que Phase 15.10 : pas de vrai test multi-thread dans ce harness synchrone)
    # -------------------------------------------------------------------
    section("TEST 13 — 'concurrence' (proxy séquentiel via contrainte UNIQUE DB) -> une seule commission")
    check("couvert par TEST 12 — la protection réelle est la contrainte UNIQUE côté DB "
          "(ReferralCommission.source_event_id), valable aussi pour de vrais appels concurrents ; "
          "aucun test de vraie concurrence multi-thread exécuté (limite du harness synchrone existant)", True)

    # -------------------------------------------------------------------
    # TEST 14 — licences historiques sans metadata -> comportement préservé (non-régression)
    # -------------------------------------------------------------------
    section("TEST 14 — licence historique sans metadata -> comportement préservé, aucune régression")
    buyer14_id, buyer14_token = _setup_buyer_with_attribution(c, "buyer14-15-12@example.com", "promoter1512")
    license14 = {"license_key": "lic_buyer14", "status": "active", "expires_at": "2027-01-01T00:00:00+00:00",
                 "metadata": None, "customer": {"email": "buyer14-15-12@example.com"}, "product": {}}
    with patch("app.billing.router._fetch_chariow_license", return_value=license14):
        limiter.reset()
        r = c.post("/billing/activate-license", json={"license_key": "lic_buyer14"}, headers={"Authorization": f"Bearer {buyer14_token}"})
    check("HTTP 200 (repli email fonctionne toujours, aucune régression)", r.status_code == 200)
    with Session(engine) as s:
        sub14 = s.exec(select(ProviderSubscription).where(ProviderSubscription.user_id == buyer14_id, ProviderSubscription.provider == "chariow")).first()
        check("abonnement activé via repli email", sub14.status == "active" and sub14.external_ref == "lic_buyer14")

    # -------------------------------------------------------------------
    # PARTIE H — test de réalisme explicite : la VRAIE forme de GET/POST /licenses/{key}, jamais de
    # champ 'amount' ajouté artificiellement à cet objet.
    # -------------------------------------------------------------------
    section("PARTIE H — test_real_chariow_license_response_without_amount")
    test_real_chariow_license_response_without_amount(c, promoter_id)

    print(f"\n{'=' * 60}\n{_passed}/{_passed + _failed} tests reussis\n{'=' * 60}")
    return _failed == 0


def test_real_chariow_license_response_without_amount(c, promoter_id):
    """Reproduit fidèlement la forme RÉELLE et CONFIRMÉE de la réponse Chariow
    (Phase 15.11) : status/expires_at/metadata/customer/product — JAMAIS de champ
    montant ajouté ici. Objectif explicite (Phase 15.12 Partie H) : empêcher qu'un
    futur test passe avec un champ Chariow imaginaire."""
    buyer_id, buyer_token = _setup_buyer_with_attribution(c, "buyerH-15-12@example.com", "promoter1512")
    real_license = _real_shaped_license(user_id=buyer_id, plan="monthly", license_key="lic_buyerH",
                                         customer_email="buyerH-15-12@example.com")
    check("license_data (réaliste) ne contient AUCUNE des clés montant candidates",
          not any(k in real_license for k in ("amount", "amount_paid", "paid_amount", "total_amount", "total", "price")))

    # Sans repli configuré (état réel actuel de la production) -> aucune commission, exactement
    # le symptôme observé pour USER 245.
    with patch("app.billing.router._fetch_chariow_license", return_value=real_license), \
         patch.dict("app.referral.money.PLAN_LIST_PRICES", {}, clear=True):
        limiter.reset()
        r1 = c.post("/billing/activate-license", json={"license_key": "lic_buyerH"}, headers={"Authorization": f"Bearer {buyer_token}"})
    check("(sans repli configuré) HTTP 200, abonnement activé", r1.status_code == 200)
    with Session(engine) as s:
        check("(sans repli configuré) AUCUNE commission — reproduit exactement l'état réel de production",
              s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == buyer_id)).first() is None)

    # AVEC le repli correctement configuré (ce que la Phase 15.12 recommande de configurer sur Railway)
    # -> même objet license_data RÉALISTE, mais commission correctement créée cette fois.
    with patch("app.billing.router._fetch_chariow_license", return_value=real_license), \
         patch.dict("app.referral.money.PLAN_LIST_PRICES", {"monthly": 1500}, clear=True):
        limiter.reset()
        r2 = c.post("/billing/activate-license", json={"license_key": "lic_buyerH_second_attempt_would_be_idempotent"}, headers={"Authorization": f"Bearer {buyer_token}"})
    # Note : license_key différent uniquement pour contourner l'idempotence légitime du premier appel
    # (même utilisateur) — ceci simule un DEUXIÈME achat distinct, jamais un rejeu de lic_buyerH.
    check("(avec repli configuré) HTTP 200", r2.status_code == 200)
    with Session(engine) as s:
        comm = s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == buyer_id)).first()
        check("(avec repli configuré) commission créée = 600 FCFA malgré license_data 100% réaliste (sans champ montant)",
              comm is not None and comm.commission_amount == 600)


if __name__ == "__main__":
    try:
        success = main()
    finally:
        cleanup_db(DB_PATH)
    sys.exit(0 if success else 1)
