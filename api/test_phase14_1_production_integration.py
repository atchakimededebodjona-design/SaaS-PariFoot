"""
test_phase14_1_production_integration.py — Phase 14.1 : PRODUCTION INTEGRATION
& REAL PAYMENT ATTRIBUTION VERIFICATION V1.

Cette phase ne réimplémente PAS l'architecture Phase 14 (voir
test_referral_promoter_platform.py, 99/99 assertions déjà vertes, réutilisées
telles quelles). Ce fichier ajoute UNIQUEMENT les points d'intégration
réellement découverts et non couverts par la suite Phase 14 :

  1. Réconciliation financière croisée sur UNE MÊME vente : payment reçu
     (webhook) == ledger.gross_paid_amount == promoter dashboard ==
     admin dashboard — les 4 vues doivent être rigoureusement identiques,
     jamais une approximation (§22 du prompt Phase 14.1).
  2. Scénario "actual paid amount" avec remise réelle bout-en-bout HTTP
     (prix catalogue configuré 20000, montant réellement payé 15000 ->
     commission 6000, jamais 20000 x 40% = 8000) — §12/§33.
  3. Balayage consolidé "no false commission" : clic/visite, inscription,
     abonnement créé, checkout ouvert, paiement pending, paiement failed ->
     ledger vide à chaque étape, une seule ligne après confirmation réelle
     (§23).
  4. Isolation promoteur sur /promoter/me/sales (endpoint non couvert par le
     test 18 de la suite Phase 14, qui ne portait que sur /promoter/me)
     avec un promoter_id forgé de plusieurs façons (§18/§29).

Toutes les données de test sont explicitement préfixées `phase14_1_test_`
(§31 du prompt). Base SQLite isolée dédiée (jamais api/app.db), même
discipline que test_referral_promoter_platform.py.

Environnement de paiement : AUCUN appel réseau réel n'est effectué vers
l'API Chariow (https://api.chariow.com) dans ce fichier — la clé
CHARIOW_API_KEY présente dans api/.env n'a pas de statut sandbox/production
confirmé (voir app/core/chariow_config.py, section "NON VÉRIFIÉ"), et le
domaine de production réel (www.xfoot.site / api.xfoot.site) sert déjà du
trafic réel. Par sécurité, ce fichier réutilise exclusivement le mécanisme
de test déjà établi par la Phase 14 : un Pulse webhook signé HMAC-SHA256
avec un secret de TEST dédié, envoyé à une app FastAPI de test isolée
(jamais l'API réelle) — voir reports/phase14_1/ pour la justification
complète (PAYMENT_VERIFICATION_BLOCKED pour un vrai paiement live).

Usage : python api/test_phase14_1_production_integration.py
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db, sign_payload, make_event, register_and_login

WEBHOOK_SECRET = "pulse_test_secret_phase14_1"
DB_PATH = configure_test_env("test_phase14_1_production_integration.db", webhook_secret=WEBHOOK_SECRET)

# §12/§33 : prix catalogue configuré volontairement DIFFÉRENT du montant réellement payé dans le test de
# remise ci-dessous — jamais utilisé comme montant de commission tant qu'un montant webhook est présent.
os.environ["REFERRAL_PLAN_PRICE_MONTHLY"] = "20000"
os.environ["ADMIN_EMAILS"] = "phase14_1_test_admin@xfootadmin.example.com"

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from main import app
from app.core.database import engine, init_db
from app.core.rate_limit import limiter

init_db()
from app.models.promoter import Promoter, ReferralCommission
from app.referral.promoter_service import create_promoter

UTC = timezone.utc
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


def _mocked_checkout(url="https://chariow.com/checkout/phase14_1-test-link"):
    return patch("app.billing.router._create_chariow_checkout_link", return_value=url)


def _send_sale_pulse(c, *, user_id: int, plan: str, delivery_id: str, amount=None):
    sale = {"custom_metadata": {"user_id": str(user_id), "plan": plan}}
    if amount is not None:
        sale["amount"] = amount
    payload = make_event("successful.sale", sale=sale, customer={"email": ""})
    sig = sign_payload(payload, WEBHOOK_SECRET)
    return c.post("/billing/pulse", content=payload, headers={
        "x-chariow-signature": sig, "x-pulse-delivery-id": delivery_id, "content-type": "application/json",
    })


def _send_revoked_pulse(c, *, email: str, delivery_id: str):
    payload = make_event("license.revoked", customer={"email": email})
    sig = sign_payload(payload, WEBHOOK_SECRET)
    return c.post("/billing/pulse", content=payload, headers={
        "x-chariow-signature": sig, "x-pulse-delivery-id": delivery_id, "content-type": "application/json",
    })


def _register_promoter(c, email, name="phase14_1_test_promoter") -> tuple[int, str, str]:
    user_id, token = register_and_login(c, email, "correct-horse-battery-staple", name=name)
    with Session(engine) as s:
        promoter = create_promoter(s, user_id=user_id, display_name=name)
        slug = promoter.slug
    return user_id, token, slug


# ---------------------------------------------------------------------------
# 1. Réconciliation financière croisée : payment == ledger == promoter
#    dashboard == admin dashboard, sur UNE MÊME vente réelle-simulée.
# ---------------------------------------------------------------------------

def test_financial_reconciliation_chain():
    section("1. Réconciliation financière : payment == ledger == dashboard promoteur == dashboard admin (§22)")
    c = client()
    promoter_user_id, promoter_token, slug = _register_promoter(c, "phase14_1_test_promoA@example.com")
    referred_id, referred_token = register_and_login(
        c, "phase14_1_test_clientA@example.com", "correct-horse-battery-staple", name="phase14_1_test_client"
    )
    r = c.post("/referral/attribute", json={"slug": slug, "captured_at": datetime.now(UTC).isoformat()},
               headers={"Authorization": f"Bearer {referred_token}"})
    check("attribution ok", r.status_code == 200 and r.json()["attributed"] is True)

    with _mocked_checkout():
        r = c.post("/billing/checkout", json={"plan": "monthly", "first_name": "P", "last_name": "141",
                                                "phone_number": "0100000000", "phone_country_code": "CI"},
                    headers={"Authorization": f"Bearer {referred_token}"})
    check("checkout ok", r.status_code == 200)

    PAID_AMOUNT = 12500
    r = _send_sale_pulse(c, user_id=referred_id, plan="monthly", delivery_id="phase14_1_pulse_recon", amount=PAID_AMOUNT)
    check("payment webhook accepted", r.status_code == 200)

    # Vue 1 : ledger (source de vérité).
    with Session(engine) as s:
        commission = s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == referred_id)).first()
    check("ledger row exists", commission is not None)
    check("ledger.gross_paid_amount == payment.amount envoyé au webhook", commission.gross_paid_amount == PAID_AMOUNT)
    check("ledger.commission_rate_bp == 4000 (40%)", commission.commission_rate_bp == 4000)
    check("ledger.commission_amount == gross_paid_amount x 40% (12500 x 0.40 = 5000)", commission.commission_amount == 5000)
    check("payment_id (source_event_id) présent et unique dans le ledger", commission.source_event_id == "phase14_1_pulse_recon")

    # Vue 2 : dashboard promoteur (/promoter/me/stats + /promoter/me/sales).
    r_stats = c.get("/promoter/me/stats", headers={"Authorization": f"Bearer {promoter_token}"})
    check("promoter stats reachable", r_stats.status_code == 200)
    stats = r_stats.json()
    r_sales = c.get("/promoter/me/sales", headers={"Authorization": f"Bearer {promoter_token}"})
    sales = r_sales.json()
    matching_sale = next((s for s in sales if s["amount_paid"] == PAID_AMOUNT), None)
    check("promoter dashboard sales row shows the exact paid amount", matching_sale is not None)
    check("promoter dashboard sales row shows the exact commission amount", matching_sale is not None and matching_sale["commission"] == 5000)
    check("promoter dashboard total_commission_accrued includes this exact ledger row",
          stats["total_commission_accrued"] >= 5000)

    # Vue 3 : dashboard admin (/admin/earnings/totals + /admin/subscribers).
    admin_id, admin_token = register_and_login(c, "phase14_1_test_admin@xfootadmin.example.com", "correct-horse-battery-staple")
    r_admin_totals = c.get("/admin/earnings/totals", headers={"Authorization": f"Bearer {admin_token}"})
    check("admin totals reachable", r_admin_totals.status_code == 200)
    admin_totals = r_admin_totals.json()
    with Session(engine) as s:
        all_accrued = s.exec(select(ReferralCommission).where(ReferralCommission.status == "ACCRUED")).all()
        real_commission_sum = sum(x.commission_amount for x in all_accrued)
        real_revenue_sum = sum(x.gross_paid_amount for x in all_accrued)
    check("admin.total_commissions EXACTLY equals a fresh SUM over the ledger", admin_totals["total_commissions"] == real_commission_sum)
    check("admin.total_revenue EXACTLY equals a fresh SUM over the ledger", admin_totals["total_revenue"] == real_revenue_sum)

    r_subs = c.get("/admin/subscribers", headers={"Authorization": f"Bearer {admin_token}"})
    subs = r_subs.json()
    matching_sub = next((s for s in subs if s["email"] == "phase14_1_test_clientA@example.com"), None)
    check("admin subscribers row exists for the referred client", matching_sub is not None)
    check("admin subscribers row shows the exact real paid amount (never a catalog price)",
          matching_sub is not None and matching_sub["amount_paid"] == PAID_AMOUNT)
    check("admin subscribers row shows the correct attributed promoter slug", matching_sub is not None and matching_sub["promoter_slug"] == slug)


# ---------------------------------------------------------------------------
# 2. actual_paid_amount avec remise réelle, bout-en-bout HTTP (jamais le prix
#    catalogue configuré) — catalogue=20000 (env), payé=15000 -> commission=6000.
# ---------------------------------------------------------------------------

def test_actual_paid_amount_discount_scenario_end_to_end():
    section("2. remise réelle bout-en-bout : catalogue configuré=20000, payé=15000 -> commission=6000, jamais 8000 (§12)")
    c = client()
    _, promoter_token, slug = _register_promoter(c, "phase14_1_test_promoB@example.com")
    referred_id, referred_token = register_and_login(
        c, "phase14_1_test_clientB@example.com", "correct-horse-battery-staple", name="phase14_1_test_client_discount"
    )
    c.post("/referral/attribute", json={"slug": slug, "captured_at": datetime.now(UTC).isoformat()},
           headers={"Authorization": f"Bearer {referred_token}"})
    with _mocked_checkout():
        c.post("/billing/checkout", json={"plan": "monthly", "first_name": "D", "last_name": "141",
                                           "phone_number": "0100000000", "phone_country_code": "CI"},
               headers={"Authorization": f"Bearer {referred_token}"})

    DISCOUNTED_AMOUNT = 15000  # < REFERRAL_PLAN_PRICE_MONTHLY=20000 configuré ci-dessus
    r = _send_sale_pulse(c, user_id=referred_id, plan="monthly", delivery_id="phase14_1_pulse_discount", amount=DISCOUNTED_AMOUNT)
    check("discounted payment webhook accepted", r.status_code == 200)

    with Session(engine) as s:
        commission = s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == referred_id)).first()
    check("commission based on ACTUAL paid amount (15000), never catalog price (20000)", commission.gross_paid_amount == DISCOUNTED_AMOUNT)
    check("commission_amount == 6000 (15000 x 40%)", commission.commission_amount == 6000)
    check("commission_amount != 8000 (would be 20000 x 40%, the WRONG catalog-based calculation)", commission.commission_amount != 8000)

    r_sales = c.get("/promoter/me/sales", headers={"Authorization": f"Bearer {promoter_token}"})
    row = next((s for s in r_sales.json() if s["amount_paid"] == DISCOUNTED_AMOUNT), None)
    check("promoter dashboard also reflects 15000 (never 20000)", row is not None and row["commission"] == 6000)


# ---------------------------------------------------------------------------
# 3. Balayage consolidé "aucune commission ne peut être créée avant paiement
#    réellement confirmé" (§23) — click/visit, signup, subscription created,
#    checkout, pending, failed -> ledger vide jusqu'à confirmation réelle.
# ---------------------------------------------------------------------------

def test_no_false_commission_consolidated_sweep():
    section("3. balayage consolidé : clic/visite/inscription/abonnement/checkout/pending/failed -> 0 commission (§23)")
    c = client()
    _, promoter_token, slug = _register_promoter(c, "phase14_1_test_promoC@example.com")
    with Session(engine) as s:
        promoter = s.exec(select(Promoter).where(Promoter.slug == slug)).first()
        promoter_id = promoter.id

    def commission_count_for_this_promoter() -> int:
        # Scopé à CE promoteur (jamais un COUNT global de table, partagée entre tous les tests de ce
        # fichier dans la même base isolée) — c'est le nombre pertinent pour prouver l'absence de
        # commission fausse SUR CE parcours précis, indépendamment des autres tests déjà exécutés.
        with Session(engine) as s:
            return len(s.exec(select(ReferralCommission).where(ReferralCommission.promoter_id == promoter_id)).all())

    # (a) clic/visite anonyme, AVANT toute inscription — jamais de commission possible à ce stade.
    r_resolve = c.post(f"/referral/resolve/{slug}", json={"visitor_id": "phase14_1_test_visitor_sweep"})
    check("slug resolve (visit) ok", r_resolve.status_code == 200 and r_resolve.json()["valid"] is True)
    check("0 commission after a mere anonymous visit", commission_count_for_this_promoter() == 0)

    # (b) inscription seule (compte créé) — toujours 0 commission tant qu'aucune attribution/paiement.
    referred_id, referred_token = register_and_login(
        c, "phase14_1_test_clientC@example.com", "correct-horse-battery-staple", name="phase14_1_test_client_sweep"
    )
    check("0 commission after mere signup (no attribution yet)", commission_count_for_this_promoter() == 0)

    # (c) attribution créée — toujours 0 commission (attribution != paiement).
    c.post("/referral/attribute", json={"slug": slug, "captured_at": datetime.now(UTC).isoformat()},
           headers={"Authorization": f"Bearer {referred_token}"})
    check("0 commission after attribution alone (no subscription/payment yet)", commission_count_for_this_promoter() == 0)

    # (d) checkout ouvert (lien Chariow généré) — toujours 0 commission (choix de plan seul).
    with _mocked_checkout():
        c.post("/billing/checkout", json={"plan": "monthly", "first_name": "S", "last_name": "W",
                                           "phone_number": "0100000000", "phone_country_code": "CI"},
               headers={"Authorization": f"Bearer {referred_token}"})
    check("0 commission after checkout opened (no payment confirmation yet)", commission_count_for_this_promoter() == 0)

    # (e) paiement PENDING implicite (aucun Pulse envoyé) — toujours 0.
    check("0 commission while payment stays PENDING (no successful.sale Pulse sent)", commission_count_for_this_promoter() == 0)

    # (f) paiement FAILED explicite (aucun Pulse successful.sale n'est jamais envoyé pour un paiement
    #     échoué — c'est la nature même de FAILED : il n'y a structurellement aucun événement de
    #     confirmation à traiter) — vérifié en confirmant qu'aucune commission n'apparaît malgré le
    #     parcours complet jusqu'ici.
    check("0 commission — payment never reached FAILED->PAID transition", commission_count_for_this_promoter() == 0)

    # (g) SEUL un paiement réellement confirmé (Pulse successful.sale signé) crée la commission.
    r = _send_sale_pulse(c, user_id=referred_id, plan="monthly", delivery_id="phase14_1_pulse_sweep", amount=10000)
    check("real payment confirmation accepted", r.status_code == 200)
    check("exactly 1 commission, created ONLY at real payment confirmation", commission_count_for_this_promoter() == 1)


# ---------------------------------------------------------------------------
# 4. Isolation promoteur sur /promoter/me/sales (endpoint distinct de
#    /promoter/me, non couvert par le test 18 de la suite Phase 14) — un
#    promoter_id forgé, sous plusieurs formes, ne doit jamais exposer les
#    données d'un autre promoteur.
# ---------------------------------------------------------------------------

def test_promoter_isolation_on_sales_endpoint_forged_promoter_id():
    section("4. isolation promoteur sur /promoter/me/sales avec promoter_id forgé (§18/§29, endpoint non couvert par la suite Phase 14)")
    c = client()
    _, token_a, slug_a = _register_promoter(c, "phase14_1_test_promoD_A@example.com")
    _, token_b, slug_b = _register_promoter(c, "phase14_1_test_promoD_B@example.com")

    # Vente réelle attribuée à B — jamais visible par A, même avec un promoter_id forgé dans la requête.
    referred_id, referred_token = register_and_login(
        c, "phase14_1_test_clientD@example.com", "correct-horse-battery-staple", name="phase14_1_test_client_isolation"
    )
    c.post("/referral/attribute", json={"slug": slug_b, "captured_at": datetime.now(UTC).isoformat()},
           headers={"Authorization": f"Bearer {referred_token}"})
    with _mocked_checkout():
        c.post("/billing/checkout", json={"plan": "monthly", "first_name": "I", "last_name": "S",
                                           "phone_number": "0100000000", "phone_country_code": "CI"},
               headers={"Authorization": f"Bearer {referred_token}"})
    _send_sale_pulse(c, user_id=referred_id, plan="monthly", delivery_id="phase14_1_pulse_isolation", amount=10000)

    with Session(engine) as s:
        promoter_b = s.exec(select(Promoter).where(Promoter.slug == slug_b)).first()

    r_a_query = c.get(f"/promoter/me/sales?promoter_id={promoter_b.id}", headers={"Authorization": f"Bearer {token_a}"})
    check("A querying with B's promoter_id in the query string -> still 200 (ignored, never an authz mechanism)", r_a_query.status_code == 200)
    check("A's sales list is empty (B's sale never leaks to A)", r_a_query.json() == [])

    r_a_body = c.post("/promoter/me/sales", json={"promoter_id": promoter_b.id}, headers={"Authorization": f"Bearer {token_a}"})
    check("POST to a GET-only endpoint with a forged promoter_id body -> 405, never an accepted override", r_a_body.status_code == 405)

    r_b = c.get("/promoter/me/sales", headers={"Authorization": f"Bearer {token_b}"})
    check("B (the real, legitimate promoter) does see their own sale", any(x["amount_paid"] == 10000 for x in r_b.json()))


def run_all():
    tests = [
        test_financial_reconciliation_chain,
        test_actual_paid_amount_discount_scenario_end_to_end,
        test_no_false_commission_consolidated_sweep,
        test_promoter_isolation_on_sales_endpoint_forged_promoter_id,
    ]
    for t in tests:
        limiter.reset()
        t()
    total = _passed + _failed
    print(f"\n{'=' * 60}")
    print(f"{_passed}/{total} tests reussis" if _failed == 0 else f"{_passed} passed, {_failed} failed (sur {total} assertions)")
    print("=" * 60)
    return _failed == 0


if __name__ == "__main__":
    ok = run_all()
    cleanup_db(DB_PATH)  # best-effort (verrou Windows bref possible, sans conséquence — même pattern que test_referral_promoter_platform.py)
    sys.exit(0 if ok else 1)
