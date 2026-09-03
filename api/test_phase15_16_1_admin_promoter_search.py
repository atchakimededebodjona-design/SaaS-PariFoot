"""
test_phase15_16_1_admin_promoter_search.py — Phase 15.16.1 : audit de
"Commissions reversées" (Partie A/B, non-régression uniquement — aucun
changement de logique n'a été nécessaire) + nouvelle recherche admin par
promoteur avec résumé financier (Partie C/D).

Toutes les ReferralCommission sont créées via un VRAI Pulse successful.sale
signé (même helper que test_referral_promoter_platform.py/
test_phase15_14_manual_withdrawal.py), jamais une ligne fabriquée à la main.
Le cas réel promoter-2 n'est PAS touché ici — TEST 15 reproduit les mêmes
montants (600/600/0/0) avec un promoteur de test entièrement isolé.

Base isolée dédiée (jamais api/app.db). Usage :
    python api/test_phase15_16_1_admin_promoter_search.py
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db, sign_payload, make_event, register_and_login

WEBHOOK_SECRET = "pulse_test_secret_phase15_16_1"
DB_PATH = configure_test_env("test_phase15_16_1_admin_promoter_search.db", webhook_secret=WEBHOOK_SECRET)

import os
os.environ["ADMIN_EMAILS"] = "admin-15161@xfootadmin.example.com"

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from main import app
from app.core.database import engine, init_db

init_db()
from app.models.promoter import Promoter, PromoterWithdrawal, ReferralCommission
from app.referral.promoter_service import create_promoter
from app.referral.withdrawal_service import compute_promoter_available_amount, confirm_withdrawal_paid, reject_withdrawal
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


def _send_sale_pulse(c, *, user_id: int, plan: str, delivery_id: str, amount: int, email: str = ""):
    sale = {"custom_metadata": {"user_id": str(user_id), "plan": plan}, "amount": amount}
    payload = make_event("successful.sale", sale=sale, customer={"email": email})
    sig = sign_payload(payload, WEBHOOK_SECRET)
    r = c.post("/billing/pulse", content=payload, headers={
        "x-chariow-signature": sig, "x-pulse-delivery-id": delivery_id, "content-type": "application/json",
    })
    assert r.status_code == 200, r.text
    return r


def _make_promoter(c, email, name, slug_hint=None):
    user_id, token = register_and_login(c, email, "correct-horse-battery-staple", name=name)
    with Session(engine) as s:
        promoter = create_promoter(s, user_id=user_id, display_name=name, requested_slug=slug_hint)
        promoter_id, slug = promoter.id, promoter.slug
    return user_id, token, promoter_id, slug


def _paid_referral(c, promoter_slug, buyer_email, *, amount, delivery_id, buyer_name="Buyer"):
    buyer_id, buyer_token = register_and_login(c, buyer_email, "correct-horse-battery-staple", name=buyer_name)
    c.post("/referral/attribute", json={"slug": promoter_slug}, headers={"Authorization": f"Bearer {buyer_token}"})
    with patch("app.billing.router._create_chariow_checkout_link", return_value="https://chariow.com/checkout/test-link"):
        r = c.post("/billing/checkout", json={
            "plan": "monthly", "first_name": "B", "last_name": "U",
            "phone_number": "0100000000", "phone_country_code": "CI",
        }, headers={"Authorization": f"Bearer {buyer_token}"})
    assert r.status_code == 200, r.text
    _send_sale_pulse(c, user_id=buyer_id, plan="monthly", delivery_id=delivery_id, amount=amount, email=buyer_email)
    return buyer_id, buyer_token


def _request_withdrawal(c, token, amount):
    limiter.reset()
    return c.post("/promoter/me/withdrawals", json={"amount": amount}, headers={"Authorization": f"Bearer {token}"})


def main():
    c = client()

    section("SETUP — admin")
    admin_id, admin_token = register_and_login(c, "admin-15161@xfootadmin.example.com", "correct-horse-battery-staple", name="Admin")
    normal_user_id, normal_token = register_and_login(c, "normal-user-15161@example.com", "correct-horse-battery-staple", name="Normal User")

    # -------------------------------------------------------------------
    # PARTIE A — AUDIT "Commissions reversées" : preuve de code, aucune régression
    # -------------------------------------------------------------------
    section("PARTIE A — audit 'Commissions reversées' : source = ReferralCommission.status=='REVERSED' uniquement")
    import inspect
    from app.referral import stats as stats_module
    src = inspect.getsource(stats_module.compute_admin_totals)
    check("compute_admin_totals calcule reversed_commissions UNIQUEMENT depuis status=='REVERSED'",
          'r.status == "REVERSED"' in src)
    check("'reversed_commissions' n'a AUCUN lien avec PromoterWithdrawal (aucune référence dans la fonction)",
          "PromoterWithdrawal" not in src and "withdrawal" not in src.lower())
    # Preuve fonctionnelle : un promoteur avec une commission ACCRUED payée intégralement via retrait
    # (jamais remboursée) doit avoir reversed_commissions == 0 -- exactement le cas réel promoter-2.
    pAudit_user, pAudit_token, pAudit_id, pAudit_slug = _make_promoter(c, "prom-audit-15161@example.com", "Promoter Audit", "promaudit15161")
    _paid_referral(c, pAudit_slug, "buyer-audit-15161@example.com", amount=1500, delivery_id="pulse-15161-audit")
    r = _request_withdrawal(c, pAudit_token, 600)
    wid = r.json()["id"]
    c.post(f"/admin/withdrawals/{wid}/confirm-paid", json={"confirm": True, "external_reference": "AUDIT-REF"}, headers={"Authorization": f"Bearer {admin_token}"})
    from app.referral.stats import compute_admin_totals
    with Session(engine) as s:
        totals = compute_admin_totals(s)
    check("reversed_commissions == 0 pour une commission ACCRUED intégralement versée via retrait (jamais remboursée) -- 0 est la valeur CORRECTE, pas un bug",
          totals["reversed_commissions"] == 0)
    check("total_commissions (ACCRUED) == 600 -- inchangé par le retrait, la commission historique n'est ni supprimée ni modifiée",
          totals["total_commissions"] == 600)

    # -------------------------------------------------------------------
    # TEST 1 — commission mais aucun retrait
    # -------------------------------------------------------------------
    section("TEST 1 — promoteur avec commission mais aucun retrait")
    p1_user, p1_token, p1_id, p1_slug = _make_promoter(c, "prom1-15161@example.com", "Promoter One", "prom1x15161")
    _paid_referral(c, p1_slug, "buyer1-15161@example.com", amount=1500, delivery_id="pulse-15161-1")
    with Session(engine) as s:
        a1 = compute_promoter_available_amount(s, p1_id)
    check("commission générée = 600", a1["commission_accrued"] == 600)
    check("total demandé = 0", a1["commission_total_requested"] == 0)
    check("versé = 0", a1["commission_paid_out"] == 0)
    check("en attente = 0", a1["commission_pending_withdrawal"] == 0)
    check("disponible = 600", a1["commission_available"] == 600)

    # -------------------------------------------------------------------
    # TEST 2 — retrait PENDING
    # -------------------------------------------------------------------
    section("TEST 2 — promoteur avec retrait PENDING")
    r = _request_withdrawal(c, p1_token, 600)
    check("demande créée -> 201", r.status_code == 201)
    with Session(engine) as s:
        a2 = compute_promoter_available_amount(s, p1_id)
    check("total demandé = 600", a2["commission_total_requested"] == 600)
    check("en attente = 600", a2["commission_pending_withdrawal"] == 600)
    check("disponible = 0", a2["commission_available"] == 0)

    # -------------------------------------------------------------------
    # TEST 3 — retrait PAID
    # -------------------------------------------------------------------
    section("TEST 3 — promoteur avec retrait PAID")
    p1_withdrawal_id = r.json()["id"]
    r2 = c.post(f"/admin/withdrawals/{p1_withdrawal_id}/confirm-paid", json={"confirm": True, "external_reference": "REF-P1"}, headers={"Authorization": f"Bearer {admin_token}"})
    check("confirmation -> 200 PAID", r2.status_code == 200 and r2.json()["status"] == "PAID")
    with Session(engine) as s:
        a3 = compute_promoter_available_amount(s, p1_id)
    check("versé = 600", a3["commission_paid_out"] == 600)
    check("en attente = 0", a3["commission_pending_withdrawal"] == 0)

    # -------------------------------------------------------------------
    # TEST 4 — plusieurs retraits (PAID + REJECTED + nouvelle commission + PENDING)
    # -------------------------------------------------------------------
    section("TEST 4 — promoteur avec plusieurs retraits (historique mixte)")
    p4_user, p4_token, p4_id, p4_slug = _make_promoter(c, "prom4-15161@example.com", "Promoter Four", "prom4x15161")
    _paid_referral(c, p4_slug, "buyer4a-15161@example.com", amount=1500, delivery_id="pulse-15161-4a")  # +600
    r4a = _request_withdrawal(c, p4_token, 600)
    c.post(f"/admin/withdrawals/{r4a.json()['id']}/reject", json={"admin_note": "test"}, headers={"Authorization": f"Bearer {admin_token}"})
    r4b = _request_withdrawal(c, p4_token, 600)  # re-demande après refus
    c.post(f"/admin/withdrawals/{r4b.json()['id']}/confirm-paid", json={"confirm": True}, headers={"Authorization": f"Bearer {admin_token}"})
    _paid_referral(c, p4_slug, "buyer4b-15161@example.com", amount=1000, delivery_id="pulse-15161-4b")  # +400 (nouvelle commission)
    r4c = _request_withdrawal(c, p4_token, 400)
    with Session(engine) as s:
        a4 = compute_promoter_available_amount(s, p4_id)
    check("commission générée = 1000 (600+400)", a4["commission_accrued"] == 1000)
    check("total demandé (historique, 3 demandes) = 1600 (600 rejetée + 600 payée + 400 pending)", a4["commission_total_requested"] == 1600)
    check("versé = 600", a4["commission_paid_out"] == 600)
    check("en attente = 400", a4["commission_pending_withdrawal"] == 400)
    check("disponible = 0 (1000 - 600 - 400)", a4["commission_available"] == 0)

    # -------------------------------------------------------------------
    # TEST 5 — promoteur sans commission
    # -------------------------------------------------------------------
    section("TEST 5 — promoteur sans commission")
    p5_user, p5_token, p5_id, p5_slug = _make_promoter(c, "prom5-15161@example.com", "Promoter Five", "prom5x15161")
    with Session(engine) as s:
        a5 = compute_promoter_available_amount(s, p5_id)
    check("tout à 0 pour un promoteur sans vente", all(a5[k] == 0 for k in ("commission_accrued", "commission_paid_out", "commission_pending_withdrawal", "commission_total_requested", "commission_available")))
    r = c.get(f"/admin/promoters/{p5_id}", headers={"Authorization": f"Bearer {admin_token}"})
    check("détail admin accessible même sans commission -> 200", r.status_code == 200)

    # -------------------------------------------------------------------
    # TEST 6/7 — recherche par slug / email
    # -------------------------------------------------------------------
    section("TEST 6/7 — recherche admin par slug et par email")
    r = c.get(f"/admin/promoters?q={p4_slug}", headers={"Authorization": f"Bearer {admin_token}"})
    check("recherche par slug -> trouve p4", r.status_code == 200 and any(row["id"] == p4_id for row in r.json()))
    r = c.get("/admin/promoters?q=prom4-15161@example.com", headers={"Authorization": f"Bearer {admin_token}"})
    check("recherche par email -> trouve p4", r.status_code == 200 and any(row["id"] == p4_id for row in r.json()))

    # -------------------------------------------------------------------
    # TEST 8 — isolation entre deux promoteurs (filtre admin par promoter_id)
    # -------------------------------------------------------------------
    section("TEST 8 — filtre /admin/withdrawals?promoter_id=X isolé entre promoteurs")
    r = c.get(f"/admin/withdrawals?promoter_id={p1_id}", headers={"Authorization": f"Bearer {admin_token}"})
    p1_rows = r.json()
    check("toutes les lignes renvoyées appartiennent à p1", all(row["promoter_id"] == p1_id for row in p1_rows))
    check("aucune ligne de p4 dans le résultat filtré sur p1", all(row["promoter_id"] != p4_id for row in p1_rows))

    # -------------------------------------------------------------------
    # TEST 9/10/11/12 — cohérence des 4 calculs (déjà vérifiés ci-dessus individuellement, revérifiés via l'API detail)
    # -------------------------------------------------------------------
    section("TEST 9/10/11/12 — cohérence via GET /admin/promoters/{id} (endpoint utilisé par le frontend)")
    r = c.get(f"/admin/promoters/{p4_id}", headers={"Authorization": f"Bearer {admin_token}"})
    detail = r.json()
    check("commission générée (API) = 1000", detail["total_commission_accrued"] == 1000)
    check("total versé (API) = 600", detail["commission_paid_out"] == 600)
    check("en attente (API) = 400", detail["commission_pending_withdrawal"] == 400)
    check("disponible (API) = 0", detail["commission_available"] == 0)

    # -------------------------------------------------------------------
    # TEST 13 — cohérence avec le ledger (requête DB directe vs valeur calculée)
    # -------------------------------------------------------------------
    section("TEST 13 — cohérence stricte avec le ledger ReferralCommission")
    with Session(engine) as s:
        ledger_sum = sum(
            r.commission_amount for r in
            s.exec(select(ReferralCommission).where(ReferralCommission.promoter_id == p4_id, ReferralCommission.status == "ACCRUED")).all()
        )
    check("commission générée == somme directe du ledger ACCRUED", ledger_sum == detail["total_commission_accrued"] == 1000)

    # -------------------------------------------------------------------
    # TEST 14 — aucun double comptage (REJECTED jamais compté en versé/en attente, mais compté en historique)
    # -------------------------------------------------------------------
    section("TEST 14 — aucun double comptage : la demande REJECTED de p4 n'est ni versée ni en attente")
    with Session(engine) as s:
        rejected_rows = s.exec(select(PromoterWithdrawal).where(PromoterWithdrawal.promoter_id == p4_id, PromoterWithdrawal.status == "REJECTED")).all()
    check("exactement 1 demande REJECTED existe pour p4", len(rejected_rows) == 1 and rejected_rows[0].amount == 600)
    check("cette demande REJECTED n'est comptée ni dans versé (600, pas 1200) ni dans en attente (400, pas 1000)",
          a4["commission_paid_out"] == 600 and a4["commission_pending_withdrawal"] == 400)

    # -------------------------------------------------------------------
    # TEST 15 — cas réel promoter-2 reproduit localement (isolé, ne touche jamais la vraie production)
    # -------------------------------------------------------------------
    section("TEST 15 — reproduction locale du cas réel promoter-2 (600/600/0/0)")
    pR_user, pR_token, pR_id, pR_slug = _make_promoter(c, "prom-repro-15161@example.com", "Promoter Repro", "promrepro15161")
    _paid_referral(c, pR_slug, "buyer-repro-15161@example.com", amount=1500, delivery_id="pulse-15161-repro")
    rR = _request_withdrawal(c, pR_token, 600)
    c.post(f"/admin/withdrawals/{rR.json()['id']}/confirm-paid", json={"confirm": True, "external_reference": "REPRO-REF"}, headers={"Authorization": f"Bearer {admin_token}"})
    with Session(engine) as s:
        aR = compute_promoter_available_amount(s, pR_id)
    check("commission générée = 600", aR["commission_accrued"] == 600)
    check("total versé = 600", aR["commission_paid_out"] == 600)
    check("en attente = 0", aR["commission_pending_withdrawal"] == 0)
    check("disponible = 0", aR["commission_available"] == 0)

    # -------------------------------------------------------------------
    # SÉCURITÉ — filtre promoter_id réservé admin, jamais accessible sans authentification/rôle
    # -------------------------------------------------------------------
    section("SÉCURITÉ — /admin/withdrawals?promoter_id= réservé admin")
    r = c.get(f"/admin/withdrawals?promoter_id={p1_id}")
    check("non authentifié -> 401", r.status_code == 401)
    r = c.get(f"/admin/withdrawals?promoter_id={p1_id}", headers={"Authorization": f"Bearer {normal_token}"})
    check("utilisateur normal -> 403", r.status_code == 403)
    r = c.get(f"/admin/withdrawals?promoter_id={p1_id}", headers={"Authorization": f"Bearer {p1_token}"})
    check("promoteur (non admin) -> 403", r.status_code == 403)

    print(f"\n{'=' * 60}\n{_passed}/{_passed + _failed} tests reussis\n{'=' * 60}")
    return _failed == 0


if __name__ == "__main__":
    try:
        success = main()
    finally:
        cleanup_db(DB_PATH)
    sys.exit(0 if success else 1)
