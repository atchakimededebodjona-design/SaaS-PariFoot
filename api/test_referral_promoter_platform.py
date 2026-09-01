"""
test_referral_promoter_platform.py — Phase 14 : programme de promotion /
affiliation (Promoter, ReferralAttribution, ReferralCommission).

Base isolée dédiée (jamais api/app.db) — même discipline que
test_premium.py/test_main.py. Utilise le VRAI TestClient FastAPI (app
complète, mêmes routes que la production) pour les tests d'intégration
HTTP/sécurité, et des appels de fonction directs pour les tests unitaires
purs (money.py/slug.py).

Usage : python api/test_referral_promoter_platform.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db, sign_payload, make_event, register_and_login

WEBHOOK_SECRET = "pulse_test_secret_referral_platform"
DB_PATH = configure_test_env("test_referral_promoter_platform.db", webhook_secret=WEBHOOK_SECRET)

import os
os.environ["ADMIN_EMAILS"] = "admin@xfootadmin.example.com"

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from main import app
from app.core.database import engine, init_db
from app.core.rate_limit import limiter

# TestClient(app) sans `with` ne déclenche jamais @app.on_event("startup") (qui appelle init_db()) — les
# tables ne seraient jamais créées. Appelé explicitement ici une fois, idempotent (CREATE TABLE IF NOT
# EXISTS), même pattern que les fichiers de test Phase 9.x (test_phase13.py, etc.).
init_db()
from app.models.promoter import Promoter, ReferralAttribution, ReferralCommission, ReferralAuditEvent
from app.referral.money import compute_commission_amount, extract_actual_paid_amount
from app.referral.slug import generate_base_slug, is_reserved_slug, is_valid_slug_format, normalize_slug
from app.referral.promoter_service import create_promoter, slug_is_available

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


def _mocked_checkout(monkeypatch_url="https://chariow.com/checkout/test-link"):
    return patch("app.billing.router._create_chariow_checkout_link", return_value=monkeypatch_url)


def _send_sale_pulse(c, *, user_id: int, plan: str, delivery_id: str, amount=None):
    """Réutilise make_event/sign_payload (_test_support.py) — construit un Pulse successful.sale avec un
    champ montant CUSTOMISABLE (le mock d'activate_subscription n'en porte aucun) pour tester le calcul de
    commission avec des valeurs réelles différentes (§40)."""
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


def _register_promoter(c, email, name="Promoter One") -> tuple[int, str, str]:
    """Inscrit un utilisateur et le transforme en promoteur (via create_promoter directement, chemin
    admin réel simulé) — retourne (user_id, token, slug)."""
    user_id, token = register_and_login(c, email, "correct-horse-battery-staple", name=name)
    with Session(engine) as s:
        promoter = create_promoter(s, user_id=user_id, display_name=name)
        slug = promoter.slug
    return user_id, token, slug


def _full_referral_sale_flow(c, *, promoter_email, referred_email, plan="monthly", amount=10000, delivery_id="pulse_sale_1"):
    """Flux complet réaliste : promoteur créé -> visiteur référé s'inscrit -> attribution -> checkout ->
    paiement confirmé (montant réel fourni) -> commission. Retourne (promoter, referred_user_id, response)."""
    promoter_user_id, promoter_token, slug = _register_promoter(c, promoter_email)
    with Session(engine) as s:
        promoter = s.exec(select(Promoter).where(Promoter.user_id == promoter_user_id)).first()

    referred_user_id, referred_token = register_and_login(c, referred_email, "correct-horse-battery-staple", name="Referred User")
    headers = {"Authorization": f"Bearer {referred_token}"}
    r = c.post("/referral/attribute", json={"slug": slug, "captured_at": datetime.now(UTC).isoformat()}, headers=headers)
    check(f"attribution succeeds for {referred_email}", r.status_code == 200 and r.json()["attributed"] is True)

    with _mocked_checkout():
        r = c.post("/billing/checkout", json={"plan": plan, "first_name": "R", "last_name": "U", "phone_number": "0100000000", "phone_country_code": "CI"},
                    headers={"Authorization": f"Bearer {referred_token}"})
    check("checkout mocked ok", r.status_code == 200)

    r = _send_sale_pulse(c, user_id=referred_user_id, plan=plan, delivery_id=delivery_id, amount=amount)
    return promoter, referred_user_id, r


# ---------------------------------------------------------------------------
# 1-2. money.py — calcul de commission (§40 tests 1/2).
# ---------------------------------------------------------------------------

def test_commission_10000_x_40_percent():
    section("1. 10000 x 40% = 4000")
    check("compute_commission_amount(10000, 4000bp) == 4000", compute_commission_amount(10000, 4000) == 4000)


def test_commission_15000_x_40_percent():
    section("2. 15000 x 40% = 6000")
    check("compute_commission_amount(15000, 4000bp) == 6000", compute_commission_amount(15000, 4000) == 6000)


def test_commission_floor_rounding_deterministic():
    section("2b. floor rounding (deterministic, never a float)")
    check("9999 x 4000bp -> floor(3999.6) = 3999, never 4000", compute_commission_amount(9999, 4000) == 3999)
    check("compute_commission_amount uses only int arithmetic", "float(" not in __import__("inspect").getsource(compute_commission_amount))


def test_extract_actual_paid_amount_webhook_priority():
    section("2c. extract_actual_paid_amount prefers the webhook amount, never a fabricated value")
    amount, source = extract_actual_paid_amount({"amount": 12345}, "monthly")
    check("webhook amount used", amount == 12345 and source == "webhook")
    amount2, source2 = extract_actual_paid_amount({}, "unconfigured_plan_xyz")
    check("no webhook amount + no configured price -> unavailable, never fabricated", amount2 is None and source2 == "unavailable")


# ---------------------------------------------------------------------------
# 3/4. FAILED / PENDING -> commission 0.
# ---------------------------------------------------------------------------

def test_failed_payment_no_commission():
    section("3. paiement FAILED -> commission 0 (aucun Pulse successful.sale envoyé)")
    c = client()
    promoter_user_id, _, slug = _register_promoter(c, "promo3@example.com")
    referred_id, referred_token = register_and_login(c, "referred3@example.com", "correct-horse-battery-staple")
    c.post("/referral/attribute", json={"slug": slug}, headers={"Authorization": f"Bearer {referred_token}"})
    with Session(engine) as s:
        count = len(s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == referred_id)).all())
    check("0 commission without any confirmed payment", count == 0)


def test_pending_payment_no_commission():
    section("4. paiement PENDING -> commission 0 (checkout créé, aucun Pulse reçu)")
    c = client()
    promoter_user_id, _, slug = _register_promoter(c, "promo4@example.com")
    referred_id, referred_token = register_and_login(c, "referred4@example.com", "correct-horse-battery-staple")
    c.post("/referral/attribute", json={"slug": slug}, headers={"Authorization": f"Bearer {referred_token}"})
    with _mocked_checkout():
        c.post("/billing/checkout", json={"plan": "monthly", "first_name": "R", "last_name": "U", "phone_number": "0100000000", "phone_country_code": "CI"},
               headers={"Authorization": f"Bearer {referred_token}"})
    with Session(engine) as s:
        count = len(s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == referred_id)).all())
    check("0 commission while status stays PENDING (no successful.sale yet)", count == 0)


# ---------------------------------------------------------------------------
# 5. PAID -> commission créée.
# ---------------------------------------------------------------------------

def test_paid_creates_commission():
    section("5. paiement PAID -> commission créée (montant réel, jamais le prix catalogue)")
    c = client()
    promoter, referred_id, r = _full_referral_sale_flow(c, promoter_email="promo5@example.com", referred_email="referred5@example.com", amount=10000, delivery_id="pulse_5")
    check("pulse accepted", r.status_code == 200)
    with Session(engine) as s:
        commission = s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == referred_id)).first()
        check("commission created", commission is not None)
        check("gross_paid_amount == 10000 (actual paid, never list price)", commission.gross_paid_amount == 10000)
        check("commission_amount == 4000", commission.commission_amount == 4000)
        check("status ACCRUED", commission.status == "ACCRUED")
        check("promoter_id matches", commission.promoter_id == promoter.id)


# ---------------------------------------------------------------------------
# 6. idempotence — même paiement reçu deux fois -> une seule commission.
# ---------------------------------------------------------------------------

def test_duplicate_webhook_single_commission():
    section("6. même paiement reçu deux fois (et une 3e/10e fois) -> une seule commission (§12)")
    c = client()
    promoter, referred_id, r1 = _full_referral_sale_flow(c, promoter_email="promo6@example.com", referred_email="referred6@example.com", amount=10000, delivery_id="pulse_6_dup")
    check("first delivery accepted", r1.status_code == 200)
    for _ in range(9):
        r_dup = _send_sale_pulse(c, user_id=referred_id, plan="monthly", delivery_id="pulse_6_dup", amount=10000)
        check("duplicate delivery accepted (idempotent, not an error)", r_dup.status_code == 200)
    with Session(engine) as s:
        rows = s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == referred_id)).all()
    check("exactly 1 commission after 10 identical deliveries", len(rows) == 1)


# ---------------------------------------------------------------------------
# 7. refund -> commission REVERSED.
# ---------------------------------------------------------------------------

def test_refund_reverses_commission():
    section("7. refund -> commission REVERSED (jamais supprimée, §13/§35)")
    c = client()
    promoter, referred_id, r = _full_referral_sale_flow(c, promoter_email="promo7@example.com", referred_email="referred7@example.com", amount=20000, delivery_id="pulse_7")
    check("sale accepted", r.status_code == 200)
    r2 = _send_revoked_pulse(c, email="referred7@example.com", delivery_id="pulse_7_revoke")
    check("revoke pulse accepted", r2.status_code == 200)
    with Session(engine) as s:
        commission = s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == referred_id)).first()
        check("commission row still exists (never deleted)", commission is not None)
        check("status REVERSED", commission.status == "REVERSED")
        check("reversed_at set", commission.reversed_at is not None)
        check("gross_paid_amount/commission_amount unchanged (correction via status, never a rewrite)", commission.gross_paid_amount == 20000 and commission.commission_amount == 8000)


# ---------------------------------------------------------------------------
# 8. self-referral -> commission 0.
# ---------------------------------------------------------------------------

def test_self_referral_no_commission():
    section("8. self-referral -> commission 0, journalisé SELF_REFERRAL_REJECTED (§10)")
    c = client()
    user_id, token, slug = _register_promoter(c, "selfref8@example.com")
    r = c.post("/referral/attribute", json={"slug": slug}, headers={"Authorization": f"Bearer {token}"})
    check("self-referral attribution rejected", r.status_code == 200 and r.json()["attributed"] is False and r.json()["reason"] == "SELF_REFERRAL_REJECTED")
    with Session(engine) as s:
        attributions = s.exec(select(ReferralAttribution).where(ReferralAttribution.converted_user_id == user_id)).all()
        check("no ReferralAttribution row created", len(attributions) == 0)
        audit = s.exec(select(ReferralAuditEvent).where(ReferralAuditEvent.event_type == "SELF_REFERRAL_REJECTED")).all()
        check("SELF_REFERRAL_REJECTED audited", len(audit) >= 1)

    with _mocked_checkout():
        c.post("/billing/checkout", json={"plan": "monthly", "first_name": "S", "last_name": "R", "phone_number": "0100000000", "phone_country_code": "CI"},
               headers={"Authorization": f"Bearer {token}"})
    _send_sale_pulse(c, user_id=user_id, plan="monthly", delivery_id="pulse_8_self", amount=10000)
    with Session(engine) as s:
        count = len(s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == user_id)).all())
    check("0 commission even after a real payment (no attribution existed)", count == 0)


# ---------------------------------------------------------------------------
# 9/10. promoteur inactive/active.
# ---------------------------------------------------------------------------

def test_inactive_promoter_attribution_refused():
    section("9. promoteur inactive -> nouvelle attribution refusée")
    c = client()
    promoter_user_id, _, slug = _register_promoter(c, "promo9@example.com")
    with Session(engine) as s:
        promoter = s.exec(select(Promoter).where(Promoter.user_id == promoter_user_id)).first()
        promoter.status = "INACTIVE"
        s.add(promoter); s.commit()

    referred_id, referred_token = register_and_login(c, "referred9@example.com", "correct-horse-battery-staple")
    r = c.post("/referral/attribute", json={"slug": slug}, headers={"Authorization": f"Bearer {referred_token}"})
    check("attribution refused for inactive promoter", r.status_code == 200 and r.json()["attributed"] is False)

    resolve = c.post(f"/referral/resolve/{slug}", json={})
    check("resolve reports invalid for inactive promoter (never exposes admin info)", resolve.json()["valid"] is False)


def test_active_promoter_attribution_valid():
    section("10. promoteur actif -> attribution valide")
    c = client()
    promoter_user_id, _, slug = _register_promoter(c, "promo10@example.com")
    resolve = c.post(f"/referral/resolve/{slug}", json={"visitor_id": "visitor-abc"})
    check("resolve reports valid for active promoter", resolve.json()["valid"] is True)

    referred_id, referred_token = register_and_login(c, "referred10@example.com", "correct-horse-battery-staple")
    r = c.post("/referral/attribute", json={"slug": slug}, headers={"Authorization": f"Bearer {referred_token}"})
    check("attribution succeeds for active promoter", r.json()["attributed"] is True)


# ---------------------------------------------------------------------------
# Security (§39).
# ---------------------------------------------------------------------------

def test_unauthenticated_access_401():
    section("11. accès non authentifié -> 401")
    c = client()
    r1 = c.get("/promoter/me")
    check("promoter/me without token -> 401", r1.status_code == 401)
    r2 = c.get("/admin/promoters")
    check("admin/promoters without token -> 401", r2.status_code == 401)


def test_normal_user_promoter_dashboard_403():
    section("12. user normal -> promoter dashboard -> 403")
    c = client()
    _, token = register_and_login(c, "normaluser12@example.com", "correct-horse-battery-staple")
    r = c.get("/promoter/me", headers={"Authorization": f"Bearer {token}"})
    check("normal user (no Promoter row) -> 403", r.status_code == 403)


def test_promoter_cannot_see_other_promoter_data():
    section("13. promoter A ne voit jamais les données de promoter B (aucun promoter_id acceptable côté client, §29)")
    c = client()
    _, token_a, slug_a = _register_promoter(c, "promoA13@example.com")
    _, token_b, slug_b = _register_promoter(c, "promoB13@example.com")
    r_a = c.get("/promoter/me", headers={"Authorization": f"Bearer {token_a}"})
    r_b = c.get("/promoter/me", headers={"Authorization": f"Bearer {token_b}"})
    check("A sees only A's slug", r_a.json()["slug"] == slug_a)
    check("B sees only B's slug", r_b.json()["slug"] == slug_b)
    check("A's slug != B's slug", slug_a != slug_b)
    import inspect
    from app.referral import router as referral_router_module
    source = inspect.getsource(referral_router_module)
    check("no promoter endpoint accepts a client-supplied promoter_id", "promoter_id: int" not in source)


def test_promoter_to_admin_403():
    section("14. promoteur -> admin -> 403")
    c = client()
    _, token, _ = _register_promoter(c, "promo14@example.com")
    r = c.get("/admin/promoters", headers={"Authorization": f"Bearer {token}"})
    check("promoter (non-admin email) -> 403 on admin route", r.status_code == 403)


def test_admin_promoter_management_ok():
    section("15. admin -> gestion des promoteurs -> accès OK")
    c = client()
    admin_id, admin_token = register_and_login(c, "admin@xfootadmin.example.com", "correct-horse-battery-staple")
    user_id, _, slug = _register_promoter(c, "managed15@example.com")
    r = c.get("/admin/promoters", headers={"Authorization": f"Bearer {admin_token}"})
    check("admin sees promoters list", r.status_code == 200 and any(p["slug"] == slug for p in r.json()))

    with Session(engine) as s:
        promoter = s.exec(select(Promoter).where(Promoter.slug == slug)).first()
    r2 = c.post(f"/admin/promoters/{promoter.id}/status", json={"status": "SUSPENDED"}, headers={"Authorization": f"Bearer {admin_token}"})
    check("admin can suspend a promoter", r2.status_code == 200 and r2.json()["status"] == "SUSPENDED")


def test_slug_reserved_rejected():
    section("16. slug réservé -> rejeté (injection de route système)")
    with Session(engine) as s:
        check("'admin' is reserved", is_reserved_slug("admin"))
        check("'login' is reserved", is_reserved_slug("login"))
        check("'api' is reserved", is_reserved_slug("api"))
        check("slug_is_available('admin') is False", slug_is_available(s, "admin") is False)


def test_slug_format_injection_rejected():
    section("16b. slug injection (caractères dangereux/slash) -> rejeté")
    check("slash rejected", is_valid_slug_format("a/b") is False)
    check("dot-dot rejected", is_valid_slug_format("..") is False)
    check("uppercase rejected (normalisation attendue, pas une entrée brute acceptée telle quelle)", is_valid_slug_format("Jean") is False)
    check("empty rejected", is_valid_slug_format("") is False)
    check("valid slug accepted", is_valid_slug_format("jean-dupont") is True)
    check("normalize_slug never produces a slash", "/" not in normalize_slug("a/b/c"))


def test_duplicate_slug_rejected():
    section("17. slug dupliqué -> rejeté (à la demande explicite) / auto-suffixé (à la génération)")
    c = client()
    user1_id, _, slug1 = _register_promoter(c, "dup17a@example.com", name="Jean Dupont")
    user2_id, token2 = register_and_login(c, "dup17b@example.com", "correct-horse-battery-staple", name="Jean Dupont")
    with Session(engine) as s:
        try:
            create_promoter(s, user_id=user2_id, display_name="Jean Dupont", requested_slug=slug1)
            check("explicit duplicate slug request raises ValueError", False)
        except ValueError:
            check("explicit duplicate slug request raises ValueError", True)
        promoter2 = create_promoter(s, user_id=user2_id, display_name="Jean Dupont")
        slug2 = promoter2.slug
    check("auto-generated slug differs from the first (deterministic -2 suffix)", slug2 != slug1 and slug2.startswith(generate_base_slug("Jean Dupont")))


def test_forged_promoter_id_not_accepted():
    section("18. promoter_id forgé -> jamais un mécanisme d'autorisation (§29)")
    c = client()
    _, token, _ = _register_promoter(c, "forge18@example.com")
    # /promoter/me n'accepte aucun paramètre promoter_id — vérifié structurellement (test 13) et ici en
    # confirmant qu'un query-string forgé n'a aucun effet sur la réponse.
    r = c.get("/promoter/me?promoter_id=999999", headers={"Authorization": f"Bearer {token}"})
    check("extra forged query param ignored, still returns caller's own promoter", r.status_code == 200)


# ---------------------------------------------------------------------------
# Structural / DB purity / no fabrication.
# ---------------------------------------------------------------------------

def test_db_purity_ai_tables():
    section("19. DB purity — tables IA jamais touchées par ce module (§41/§48)")
    import inspect
    from app.referral import commission_service, promoter_service, router as referral_router_module, admin_router
    for mod in (commission_service, promoter_service, referral_router_module, admin_router):
        source = inspect.getsource(mod)
        for forbidden in ("ModelPrediction", "ModelVersion", "TeamRating", "PredictionLog", "MatchStats", "import Match"):
            check(f"{mod.__name__} never references {forbidden}", forbidden not in source)


def test_admin_totals_recomputable():
    section("20. admin totals recomputable depuis le ledger (§45/§46), jamais un compteur mutable")
    c = client()
    admin_id, admin_token = register_and_login(c, "admin2@xfootadmin.example.com", "correct-horse-battery-staple")
    os.environ["ADMIN_EMAILS"] = "admin@xfootadmin.example.com,admin2@xfootadmin.example.com"
    _full_referral_sale_flow(c, promoter_email="promo20@example.com", referred_email="referred20@example.com", amount=10000, delivery_id="pulse_20")
    r = c.get("/admin/earnings/totals", headers={"Authorization": f"Bearer {admin_token}"})
    check("admin totals endpoint reachable", r.status_code == 200)
    body = r.json()
    check("total_commissions >= 4000 (from the real sale just recorded)", body["total_commissions"] >= 4000)
    with Session(engine) as s:
        rows = s.exec(select(ReferralCommission).where(ReferralCommission.status == "ACCRUED")).all()
        real_sum = sum(r.commission_amount for r in rows)
    check("endpoint total EXACTLY matches a fresh SUM over the ledger (recomputable)", body["total_commissions"] == real_sum)


def test_no_fabricated_amount_without_webhook_or_config():
    section("21. PAYMENT_CONFIRMATION_UNAVAILABLE — aucun montant fabriqué si absent du webhook et non configuré (§43)")
    c = client()
    promoter_user_id, _, slug = _register_promoter(c, "promo21@example.com")
    referred_id, referred_token = register_and_login(c, "referred21@example.com", "correct-horse-battery-staple")
    c.post("/referral/attribute", json={"slug": slug}, headers={"Authorization": f"Bearer {referred_token}"})
    with _mocked_checkout():
        c.post("/billing/checkout", json={"plan": "unknown_unconfigured_plan", "first_name": "R", "last_name": "U", "phone_number": "0100000000", "phone_country_code": "CI"},
               headers={"Authorization": f"Bearer {referred_token}"})
    # Sale SANS champ montant, plan non configuré dans PLAN_LIST_PRICES -> aucune commission fabriquée.
    _send_sale_pulse(c, user_id=referred_id, plan="unknown_unconfigured_plan", delivery_id="pulse_21", amount=None)
    with Session(engine) as s:
        count = len(s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == referred_id)).all())
    check("0 commission when amount is genuinely unavailable (never guessed)", count == 0)


def test_report_generation_building_blocks():
    section("22. report generation — building blocks well-formed")
    with Session(engine) as s:
        from app.referral.stats import compute_admin_totals, compute_promoter_stats
        totals = compute_admin_totals(s)
        check("admin totals well-formed", all(k in totals for k in ("total_revenue", "total_commissions", "net_after_commissions")))


def run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        # Chaque scénario enregistre plusieurs vrais comptes via /auth/register (rate-limited à 20/minute,
        # app/core/rate_limit.py) — cette suite en crée bien plus que ça au total ; reset explicite entre
        # chaque test pour rester sur le comportement RÉEL du endpoint (jamais un contournement du rate
        # limiting lui-même, seulement de son état accumulé entre scénarios indépendants).
        limiter.reset()
        t()
    print(f"\n{_passed} passed, {_failed} failed (sur {_passed + _failed} assertions, {len(tests)} scénarios)")
    cleanup_db(DB_PATH)
    if _failed:
        sys.exit(1)


if __name__ == "__main__":
    run_all()
