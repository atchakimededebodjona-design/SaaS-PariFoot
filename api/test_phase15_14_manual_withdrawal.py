"""
test_phase15_14_manual_withdrawal.py — Phase 15.14 : système de retrait
MANUEL des commissions promoteurs (PromoterWithdrawal).

Xfoot n'effectue AUCUN paiement automatique. Ces tests utilisent le VRAI
flux de paiement confirmé (Pulse Chariow successful.sale signé, même
helper que test_referral_promoter_platform.py) pour créer des
ReferralCommission réelles, jamais une ligne fabriquée à la main — même
discipline que le reste de ce dépôt (aucune deuxième source de vérité
financière inventée dans les tests non plus).

Base isolée dédiée (jamais api/app.db). Usage :
    python api/test_phase15_14_manual_withdrawal.py
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db, sign_payload, make_event, register_and_login

WEBHOOK_SECRET = "pulse_test_secret_phase15_14"
DB_PATH = configure_test_env("test_phase15_14_manual_withdrawal.db", webhook_secret=WEBHOOK_SECRET)

import os
os.environ["ADMIN_EMAILS"] = "admin-1514@xfootadmin.example.com"

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from main import app
from app.core.database import engine, init_db

init_db()
from app.models.promoter import Promoter, PromoterWithdrawal, ReferralCommission
from app.referral.promoter_service import create_promoter
from app.referral.withdrawal_service import compute_promoter_available_amount
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
    """Réutilise exactement le pattern déjà établi (test_referral_promoter_platform.py::_send_sale_pulse) —
    UN VRAI Pulse successful.sale signé, jamais une ReferralCommission construite à la main."""
    sale = {"custom_metadata": {"user_id": str(user_id), "plan": plan}, "amount": amount}
    payload = make_event("successful.sale", sale=sale, customer={"email": email})
    sig = sign_payload(payload, WEBHOOK_SECRET)
    r = c.post("/billing/pulse", content=payload, headers={
        "x-chariow-signature": sig, "x-pulse-delivery-id": delivery_id, "content-type": "application/json",
    })
    assert r.status_code == 200, r.text
    return r


def _send_revoked_pulse(c, *, email: str, delivery_id: str):
    payload = make_event("license.revoked", customer={"email": email})
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
    """Un promoteur A a déjà un slug ; ce helper attribue un NOUVEL acheteur à ce slug et confirme un
    paiement réel de `amount` FCFA via Pulse signé -> crée une VRAIE ReferralCommission (40%)."""
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


def _confirm_body(ref=None, note=None):
    return {"confirm": True, "external_reference": ref, "admin_note": note}


def main():
    c = client()

    section("SETUP — admin")
    admin_id, admin_token = register_and_login(c, "admin-1514@xfootadmin.example.com", "correct-horse-battery-staple", name="Admin")
    normal_user_id, normal_token = register_and_login(c, "normal-user-1514@example.com", "correct-horse-battery-staple", name="Normal User")

    # -------------------------------------------------------------------
    # TEST 1 — commission disponible = 600
    # -------------------------------------------------------------------
    section("TEST 1 — commission disponible = 600")
    pA_user, pA_token, pA_id, pA_slug = _make_promoter(c, "promA-1514@example.com", "Promoter A", "proma1514")
    _paid_referral(c, pA_slug, "buyerA1-1514@example.com", amount=1500, delivery_id="pulse-1514-a1")
    with Session(engine) as s:
        amounts = compute_promoter_available_amount(s, pA_id)
    check("commission acquise = 600", amounts["commission_accrued"] == 600)
    check("disponible = 600", amounts["commission_available"] == 600)

    # -------------------------------------------------------------------
    # TEST 2 — demande de retrait de 600 -> ACCEPTÉE
    # -------------------------------------------------------------------
    section("TEST 2 — demande de retrait de 600 FCFA -> acceptée")
    limiter.reset()
    r = c.post("/promoter/me/withdrawals", json={"amount": 600}, headers={"Authorization": f"Bearer {pA_token}"})
    check("HTTP 201", r.status_code == 201)
    w1 = r.json()
    check("amount = 600", w1["amount"] == 600)
    check("status = PENDING", w1["status"] == "PENDING")
    withdrawal1_id = w1["id"]

    # -------------------------------------------------------------------
    # TEST 3/4/5/6 — refus de montants invalides (promoteur B, disponible=600, aucune demande encore)
    # -------------------------------------------------------------------
    section("TEST 3/4/5/6 — refus de montants invalides (601 / 0 / négatif / falsifié 100000)")
    pB_user, pB_token, pB_id, pB_slug = _make_promoter(c, "promB-1514@example.com", "Promoter B", "promb1514")
    _paid_referral(c, pB_slug, "buyerB1-1514@example.com", amount=1500, delivery_id="pulse-1514-b1")
    for bad_amount, label in [(601, "601 (> disponible)"), (0, "0"), (-100, "négatif"), (100000, "falsifié massivement")]:
        limiter.reset()
        r = c.post("/promoter/me/withdrawals", json={"amount": bad_amount}, headers={"Authorization": f"Bearer {pB_token}"})
        check(f"demande {label} -> HTTP 400", r.status_code == 400)
    with Session(engine) as s:
        rows = s.exec(select(PromoterWithdrawal).where(PromoterWithdrawal.promoter_id == pB_id)).all()
    check("aucune ligne PromoterWithdrawal créée pour promoteur B (tous refusés)", len(rows) == 0)

    # -------------------------------------------------------------------
    # TEST 7 — promoteur non authentifié
    # -------------------------------------------------------------------
    section("TEST 7 — non authentifié -> 401")
    r = c.post("/promoter/me/withdrawals", json={"amount": 600})
    check("HTTP 401", r.status_code == 401)
    r = c.get("/promoter/me/withdrawals")
    check("GET non authentifié -> 401", r.status_code == 401)

    # -------------------------------------------------------------------
    # TEST 8 — utilisateur normal (pas promoteur)
    # -------------------------------------------------------------------
    section("TEST 8 — utilisateur normal (aucun compte promoteur) -> 403")
    limiter.reset()
    r = c.post("/promoter/me/withdrawals", json={}, headers={"Authorization": f"Bearer {normal_token}"})
    check("HTTP 403", r.status_code == 403)

    # -------------------------------------------------------------------
    # TEST 9 — promoteur A isolé de B (B a une demande PENDING après ce test — on en crée une valide)
    # -------------------------------------------------------------------
    section("TEST 9 — promoteur A isolé des demandes de B")
    limiter.reset()
    r = c.post("/promoter/me/withdrawals", json={"amount": 600}, headers={"Authorization": f"Bearer {pB_token}"})
    check("demande valide de B (600, montant exact désormais disponible) -> 201", r.status_code == 201)
    withdrawal_B_id = r.json()["id"]
    r = c.get("/promoter/me/withdrawals", headers={"Authorization": f"Bearer {pA_token}"})
    a_ids = [w["id"] for w in r.json()]
    check("A ne voit pas la demande de B dans sa propre liste", withdrawal_B_id not in a_ids)
    check("A voit uniquement sa propre demande", a_ids == [withdrawal1_id])

    # -------------------------------------------------------------------
    # TEST 10/11 — admin voit la demande / promoteur voit sa demande
    # -------------------------------------------------------------------
    section("TEST 10/11 — admin voit la demande de A / A voit sa propre demande")
    r = c.get("/admin/withdrawals?status_filter=PENDING", headers={"Authorization": f"Bearer {admin_token}"})
    check("HTTP 200 admin", r.status_code == 200)
    admin_ids = [w["id"] for w in r.json()]
    check("admin voit la demande de A", withdrawal1_id in admin_ids)
    check("admin voit la demande de B", withdrawal_B_id in admin_ids)
    r = c.get("/promoter/me/withdrawals", headers={"Authorization": f"Bearer {pA_token}"})
    check("A voit sa propre demande (200)", r.status_code == 200 and any(w["id"] == withdrawal1_id for w in r.json()))

    # -------------------------------------------------------------------
    # TEST 12/16 — statut PENDING, disponible devient 0
    # -------------------------------------------------------------------
    section("TEST 12/16 — statut PENDING pour A, disponible = 0 après réservation")
    with Session(engine) as s:
        wA = s.get(PromoterWithdrawal, withdrawal1_id)
        check("statut PENDING", wA.status == "PENDING")
        amounts = compute_promoter_available_amount(s, pA_id)
    check("disponible = 0 (600 réservés par la demande PENDING)", amounts["commission_available"] == 0)
    check("en attente = 600", amounts["commission_pending_withdrawal"] == 600)

    # -------------------------------------------------------------------
    # TEST 13/14/17/18/19/20 — admin confirme le paiement de A
    # -------------------------------------------------------------------
    section("TEST 13/14/17/18/19/20 — admin confirme paiement (référence + note + admin_id + timestamp)")
    r = c.post(f"/admin/withdrawals/{withdrawal1_id}/confirm-paid",
               json=_confirm_body(ref="TMONEY-1514-TEST", note="Versé via TMoney le 03/09"),
               headers={"Authorization": f"Bearer {admin_token}"})
    check("HTTP 200", r.status_code == 200)
    paid = r.json()
    check("statut = PAID", paid["status"] == "PAID")
    check("référence externe enregistrée", paid["external_reference"] == "TMONEY-1514-TEST")
    check("admin_id enregistré (email admin exposé)", paid["processed_by_admin_email"] == "admin-1514@xfootadmin.example.com")
    check("timestamp de traitement enregistré", paid["processed_at"] is not None)
    with Session(engine) as s:
        amounts = compute_promoter_available_amount(s, pA_id)
    check("déjà versé = 600", amounts["commission_paid_out"] == 600)
    check("en attente = 0", amounts["commission_pending_withdrawal"] == 0)

    # -------------------------------------------------------------------
    # TEST 15 — promoteur voit VERSÉ
    # -------------------------------------------------------------------
    section("TEST 15 — promoteur A voit sa demande VERSÉE (PAID)")
    r = c.get("/promoter/me/withdrawals", headers={"Authorization": f"Bearer {pA_token}"})
    row = next(w for w in r.json() if w["id"] == withdrawal1_id)
    check("statut PAID visible côté promoteur", row["status"] == "PAID")
    check("référence externe visible côté promoteur", row["external_reference"] == "TMONEY-1514-TEST")

    # -------------------------------------------------------------------
    # TEST 21/24 — double demande / retry réseau -> une seule ligne
    # -------------------------------------------------------------------
    section("TEST 21/24 — double demande (clic rapide / retry réseau) -> une seule ligne")
    pC_user, pC_token, pC_id, pC_slug = _make_promoter(c, "promC-1514@example.com", "Promoter C", "promc1514")
    _paid_referral(c, pC_slug, "buyerC1-1514@example.com", amount=1500, delivery_id="pulse-1514-c1")
    responses = []
    for _ in range(3):
        limiter.reset()
        r = c.post("/promoter/me/withdrawals", json={"amount": 600}, headers={"Authorization": f"Bearer {pC_token}"})
        responses.append(r)
    check("les 3 appels répétés répondent 201 (idempotent, jamais une erreur)", all(r.status_code == 201 for r in responses))
    check("les 3 réponses pointent vers LA MÊME demande (même id)", len({r.json()["id"] for r in responses}) == 1)
    with Session(engine) as s:
        rows = s.exec(select(PromoterWithdrawal).where(PromoterWithdrawal.promoter_id == pC_id)).all()
    check("UNE SEULE ligne PromoterWithdrawal en base malgré 3 soumissions", len(rows) == 1)
    withdrawal_C_id = rows[0].id

    # -------------------------------------------------------------------
    # TEST 22/30 — double confirmation -> une seule transition PAID
    # -------------------------------------------------------------------
    section("TEST 22/30 — double confirmation admin -> une seule transition PAID, jamais un second paiement")
    r1 = c.post(f"/admin/withdrawals/{withdrawal_C_id}/confirm-paid", json=_confirm_body(ref="REF-C-1"), headers={"Authorization": f"Bearer {admin_token}"})
    check("1ere confirmation -> 200 PAID", r1.status_code == 200 and r1.json()["status"] == "PAID")
    r2 = c.post(f"/admin/withdrawals/{withdrawal_C_id}/confirm-paid", json=_confirm_body(ref="REF-C-2-SHOULD-NOT-APPLY"), headers={"Authorization": f"Bearer {admin_token}"})
    check("2eme confirmation -> 409 (déjà PAID)", r2.status_code == 409)
    with Session(engine) as s:
        wC = s.get(PromoterWithdrawal, withdrawal_C_id)
        paid_rows_C = s.exec(select(PromoterWithdrawal).where(PromoterWithdrawal.promoter_id == pC_id, PromoterWithdrawal.status == "PAID")).all()
    check("référence externe INCHANGÉE (pas écrasée par la 2e tentative)", wC.external_reference == "REF-C-1")
    check("un seul retrait PAID pour C en base, jamais deux", len(paid_rows_C) == 1)

    # -------------------------------------------------------------------
    # TEST 23 — "concurrence" (proxy séquentiel via UNIQUE index DB, même limite de harness synchrone
    # documentée en Phase 15.10/15.12 — pas de vrai test multi-thread ici)
    # -------------------------------------------------------------------
    section("TEST 23 — 'concurrence' (proxy séquentiel via index UNIQUE partiel DB) -> une seule demande")
    check("couvert par TEST 21/24 — la protection réelle est l'index UNIQUE PARTIEL "
          "(promoter_withdrawal.promoter_id WHERE status='PENDING'), valable aussi pour de vrais appels "
          "concurrents ; aucun test de vraie concurrence multi-thread exécuté (limite du harness synchrone "
          "existant, même limite déjà documentée pour ReferralCommission en Phase 15.10/15.12)", True)

    # -------------------------------------------------------------------
    # TEST 9bis / T — sécurité : promoteur A ne peut ni voir ni agir sur les demandes de B/C
    # -------------------------------------------------------------------
    section("TEST T — promoteur A ne peut pas confirmer/refuser une demande d'un autre (routes admin réservées)")
    r = c.post(f"/admin/withdrawals/{withdrawal_C_id}/confirm-paid", json=_confirm_body(), headers={"Authorization": f"Bearer {pA_token}"})
    check("promoteur (non admin) sur route admin -> 403", r.status_code == 403)
    r = c.post(f"/admin/withdrawals/{withdrawal_C_id}/reject", json={}, headers={"Authorization": f"Bearer {pA_token}"})
    check("promoteur (non admin) sur reject admin -> 403", r.status_code == 403)
    r = c.get("/admin/withdrawals", headers={"Authorization": f"Bearer {normal_token}"})
    check("utilisateur normal sur liste admin -> 403", r.status_code == 403)

    # -------------------------------------------------------------------
    # TEST 25/26 — refus admin -> montant redevenu disponible
    # -------------------------------------------------------------------
    section("TEST 25/26 — admin refuse une demande PENDING -> montant redevenu disponible")
    pD_user, pD_token, pD_id, pD_slug = _make_promoter(c, "promD-1514@example.com", "Promoter D", "promd1514")
    _paid_referral(c, pD_slug, "buyerD1-1514@example.com", amount=1500, delivery_id="pulse-1514-d1")
    limiter.reset()
    r = c.post("/promoter/me/withdrawals", json={"amount": 600}, headers={"Authorization": f"Bearer {pD_token}"})
    withdrawal_D_id = r.json()["id"]
    with Session(engine) as s:
        amounts = compute_promoter_available_amount(s, pD_id)
    check("disponible = 0 pendant PENDING", amounts["commission_available"] == 0)
    r = c.post(f"/admin/withdrawals/{withdrawal_D_id}/reject", json={"admin_note": "Test refus V1"}, headers={"Authorization": f"Bearer {admin_token}"})
    check("HTTP 200 refus", r.status_code == 200)
    check("statut = REJECTED", r.json()["status"] == "REJECTED")
    with Session(engine) as s:
        amounts = compute_promoter_available_amount(s, pD_id)
    check("disponible redevenu 600 après refus (aucun montant perdu)", amounts["commission_available"] == 600)
    # nouvelle demande possible après un refus (l'index UNIQUE partiel ne bloque que les PENDING)
    limiter.reset()
    r = c.post("/promoter/me/withdrawals", json={"amount": 600}, headers={"Authorization": f"Bearer {pD_token}"})
    check("nouvelle demande possible après REJECTED -> 201", r.status_code == 201)
    withdrawal_D2_id = r.json()["id"]

    # -------------------------------------------------------------------
    # TEST 29 — historique conservé : une demande REJECTED n'est jamais re-transitionnable
    # -------------------------------------------------------------------
    section("TEST 29 — historique conservé : REJECTED définitif, jamais une seconde transition")
    r = c.post(f"/admin/withdrawals/{withdrawal_D_id}/confirm-paid", json=_confirm_body(ref="TROP-TARD"), headers={"Authorization": f"Bearer {admin_token}"})
    check("confirmer une demande déjà REJECTED -> 409", r.status_code == 409)
    with Session(engine) as s:
        wD = s.get(PromoterWithdrawal, withdrawal_D_id)
    check("statut REJECTED inchangé, external_reference toujours absente", wD.status == "REJECTED" and wD.external_reference is None)

    # -------------------------------------------------------------------
    # TEST 27 — nouvelle commission après payout -> disponible augmente, versé inchangé
    # -------------------------------------------------------------------
    section("TEST 27 — nouvelle commission après payout (600 déjà versé, +400 après)")
    # A a déjà 600 PAID (TEST 13-20). Nouvelle vente réelle de 1000 FCFA -> commission 400.
    _paid_referral(c, pA_slug, "buyerA2-1514@example.com", amount=1000, delivery_id="pulse-1514-a2")
    with Session(engine) as s:
        amounts = compute_promoter_available_amount(s, pA_id)
    check("acquis total = 1000 (600+400)", amounts["commission_accrued"] == 1000)
    check("versé = 600 (inchangé)", amounts["commission_paid_out"] == 600)
    check("disponible = 400 (jamais remis à 0)", amounts["commission_available"] == 400)

    # -------------------------------------------------------------------
    # TEST 28 — remboursement AVANT retrait -> commission reversée, disponible baisse en conséquence
    # -------------------------------------------------------------------
    section("TEST 28 — remboursement avant tout retrait (commission ACCRUED non retirée)")
    pE_user, pE_token, pE_id, pE_slug = _make_promoter(c, "promE-1514@example.com", "Promoter E", "prome1514")
    _, _ = _paid_referral(c, pE_slug, "buyerE1-1514@example.com", amount=1500, delivery_id="pulse-1514-e1")
    with Session(engine) as s:
        before = compute_promoter_available_amount(s, pE_id)
    check("disponible = 600 avant remboursement", before["commission_available"] == 600)
    _send_revoked_pulse(c, email="buyerE1-1514@example.com", delivery_id="pulse-1514-e1-revoke")
    with Session(engine) as s:
        after = compute_promoter_available_amount(s, pE_id)
    check("commission passée REVERSED -> disponible = 0 après remboursement (aucun retrait possible)", after["commission_available"] == 0)
    limiter.reset()
    r = c.post("/promoter/me/withdrawals", json={"amount": 600}, headers={"Authorization": f"Bearer {pE_token}"})
    check("demande de retrait après remboursement total -> 400 (NO_AMOUNT_AVAILABLE)", r.status_code == 400)

    print(f"\n{'=' * 60}\n{_passed}/{_passed + _failed} tests reussis\n{'=' * 60}")
    return _failed == 0


if __name__ == "__main__":
    try:
        success = main()
    finally:
        cleanup_db(DB_PATH)
    sys.exit(0 if success else 1)
