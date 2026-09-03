"""
test_phase15_9_webhook_hardening.py — Phase 15.9 : durcissement du webhook Chariow
(POST /billing/pulse) après le paiement réel pi_ih0s0eixhgm1 (USER 245) confirmé par
Chariow mais jamais reflété côté Xfoot (Phase 15.8-DIAG).

Cause suspectée, prouvée au niveau du CODE (Phase 15.8-DIAG) : un event_type non
reconnu OU un custom_metadata.user_id absent produisait un HTTP 200 sans AUCUNE trace
exploitable — indiscernable d'un succès. Ce fichier teste le comportement DURCI :

  - event absent / JSON invalide -> 400 explicite (jamais un 200 silencieux, jamais
    une 500 non gérée) ;
  - event_type reconnu mais non géré -> reste 200 (évite les retries indéfinis déjà
    documentés par Chariow) MAIS désormais journalisé explicitement (capturé ici via
    le module logging standard, pas une simple lecture de code) ;
  - custom_metadata.user_id absent -> repli EMAIL via _find_provider_subscription_by_email
    (mécanisme déjà existant et testé pour les Pulses license.*, jamais un second
    système inventé) ;
  - aucune régression sur l'idempotence, la commission 40%, ou l'isolation référeurs.

Base isolée dédiée (jamais api/app.db). Usage : python api/test_phase15_9_webhook_hardening.py
"""

import logging
import sys
from datetime import timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db, sign_payload, make_event, register_and_login

WEBHOOK_SECRET = "pulse_test_secret_phase15_9"
DB_PATH = configure_test_env("test_phase15_9_webhook_hardening.db", webhook_secret=WEBHOOK_SECRET)

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from main import app
from app.core.database import engine, init_db

init_db()
from app.models.promoter import ReferralCommission
from app.models.provider_subscription import ProviderSubscription
from app.models.subscription import ProcessedPulseDelivery
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


class _LogCapture:
    """Capture réelle des enregistrements logging du logger de billing/router.py — preuve
    exécutée qu'un événement est désormais 'loggable', pas une simple lecture de code."""

    def __init__(self, logger_name="app.billing.router"):
        self.records = []
        self._handler = logging.Handler()
        self._handler.emit = lambda record: self.records.append(record)
        self._logger = logging.getLogger(logger_name)

    def __enter__(self):
        self._logger.addHandler(self._handler)
        self._logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        self._logger.removeHandler(self._handler)

    def has(self, level, substring):
        return any(r.levelno == level and substring in r.getMessage() for r in self.records)


def _sale_event(*, user_id=None, plan="monthly", amount=1500, email=None, extra_sale_fields=None) -> bytes:
    custom_metadata = {}
    if user_id is not None:
        custom_metadata["user_id"] = str(user_id)
    if plan is not None:
        custom_metadata["plan"] = plan
    sale = {"amount": amount}
    if custom_metadata:
        sale["custom_metadata"] = custom_metadata
    if email is not None:
        sale["customer"] = {"email": email}
    if extra_sale_fields:
        sale.update(extra_sale_fields)
    return make_event("successful.sale", sale=sale, customer={"email": email} if email else {})


def _checkout_and_get_sub(c, token) -> None:
    """Crée la ProviderSubscription(status='none') via le VRAI chemin /billing/checkout
    (mocké réseau), exactement comme un utilisateur réel avant paiement."""
    from unittest.mock import patch
    with patch("app.billing.router._create_chariow_checkout_link", return_value="https://chariow.com/checkout/test-link"):
        r = c.post("/billing/checkout", json={
            "plan": "monthly", "first_name": "T", "last_name": "U",
            "phone_number": "0100000000", "phone_country_code": "CI",
        }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text


def main():
    c = client()

    # -------------------------------------------------------------------
    # SETUP — promoteur + attribution referral (pour les tests 1/2/6/12)
    # -------------------------------------------------------------------
    section("SETUP")
    promoter_owner_id, _ = register_and_login(c, "promoter-owner-15-9@example.com", "correct-horse-battery-staple", name="Promoter Owner")
    with Session(engine) as s:
        promoter = create_promoter(s, user_id=promoter_owner_id, display_name="Promoter Owner", requested_slug="promoter159")
        promoter_id = promoter.id
    check("promoteur créé", promoter_id is not None)

    # -------------------------------------------------------------------
    # TEST 1 — successful.sale valide + custom_metadata.user_id -> traitement réussi
    # -------------------------------------------------------------------
    section("TEST 1 — successful.sale valide + user_id -> traitement réussi")
    buyer1_id, buyer1_token = register_and_login(c, "buyer1-15-9@example.com", "correct-horse-battery-staple", name="Buyer One")
    c.post("/referral/attribute", json={"slug": "promoter159"}, headers={"Authorization": f"Bearer {buyer1_token}"})
    _checkout_and_get_sub(c, buyer1_token)
    payload1 = _sale_event(user_id=buyer1_id, plan="monthly", amount=1500)
    sig1 = sign_payload(payload1, WEBHOOK_SECRET)
    r = c.post("/billing/pulse", content=payload1, headers={"x-chariow-signature": sig1, "x-pulse-delivery-id": "delivery-test1", "content-type": "application/json"})
    check("HTTP 200", r.status_code == 200)
    with Session(engine) as s:
        sub1 = s.exec(select(ProviderSubscription).where(ProviderSubscription.user_id == buyer1_id, ProviderSubscription.provider == "chariow")).first()
        check("ProviderSubscription active", sub1 is not None and sub1.status == "active")
        comm1 = s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == buyer1_id)).first()
        check("commission créée = 600 FCFA (1500 x 40%)", comm1 is not None and comm1.commission_amount == 600)

    # -------------------------------------------------------------------
    # TEST 2 — successful.sale SANS custom_metadata.user_id, AVEC email -> repli email réussi
    # -------------------------------------------------------------------
    section("TEST 2 — repli email (custom_metadata absente) -> traitement réussi")
    buyer2_id, buyer2_token = register_and_login(c, "buyer2-15-9@example.com", "correct-horse-battery-staple", name="Buyer Two")
    c.post("/referral/attribute", json={"slug": "promoter159"}, headers={"Authorization": f"Bearer {buyer2_token}"})
    _checkout_and_get_sub(c, buyer2_token)
    payload2 = _sale_event(user_id=None, plan=None, amount=1500, email="buyer2-15-9@example.com")
    sig2 = sign_payload(payload2, WEBHOOK_SECRET)
    with _LogCapture() as logs:
        r = c.post("/billing/pulse", content=payload2, headers={"x-chariow-signature": sig2, "x-pulse-delivery-id": "delivery-test2", "content-type": "application/json"})
    check("HTTP 200", r.status_code == 200)
    check("log INFO mentionnant le repli email", logs.has(logging.INFO, "repli email"))
    with Session(engine) as s:
        sub2 = s.exec(select(ProviderSubscription).where(ProviderSubscription.user_id == buyer2_id, ProviderSubscription.provider == "chariow")).first()
        check("ProviderSubscription active via repli email", sub2 is not None and sub2.status == "active")
        comm2 = s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == buyer2_id)).first()
        check("commission créée via repli email = 600 FCFA", comm2 is not None and comm2.commission_amount == 600)

    # -------------------------------------------------------------------
    # TEST 3 — event_type inconnu -> PAS de 200 silencieux (200 conservé, mais désormais journalisé)
    # -------------------------------------------------------------------
    section("TEST 3 — event_type inconnu -> réponse toujours 200 (anti-retry-storm) MAIS journalisée")
    payload3 = make_event("refund.issued", sale={"id": "irrelevant"})
    sig3 = sign_payload(payload3, WEBHOOK_SECRET)
    with _LogCapture() as logs:
        r = c.post("/billing/pulse", content=payload3, headers={"x-chariow-signature": sig3, "x-pulse-delivery-id": "delivery-test3", "content-type": "application/json"})
    check("HTTP 200 (évite un retry indéfini de Chariow pour un type qu'on ne gère pas encore)", r.status_code == 200)
    check("log INFO explicite mentionnant 'refund.issued' (jamais un silence total comme avant)", logs.has(logging.INFO, "refund.issued"))
    with Session(engine) as s:
        check("delivery marquée traitée (idempotence préservée même pour un type ignoré)",
              s.exec(select(ProcessedPulseDelivery).where(ProcessedPulseDelivery.pulse_delivery_id == "delivery-test3")).first() is not None)

    # -------------------------------------------------------------------
    # TEST 4 — champ 'event' absent -> rejet explicite 400 (jamais un 200 silencieux)
    # -------------------------------------------------------------------
    section("TEST 4 — champ 'event' absent -> 400 explicite")
    import json as _json
    payload4 = _json.dumps({"sale": {"amount": 1500}}).encode()  # pas de clé 'event' du tout
    sig4 = sign_payload(payload4, WEBHOOK_SECRET)
    r = c.post("/billing/pulse", content=payload4, headers={"x-chariow-signature": sig4, "x-pulse-delivery-id": "delivery-test4", "content-type": "application/json"})
    check("HTTP 400 (jamais 200, jamais 500 non gérée)", r.status_code == 400)

    # -------------------------------------------------------------------
    # TEST 4b — JSON invalide -> 400 explicite (jamais une 500 JSONDecodeError non gérée)
    # -------------------------------------------------------------------
    section("TEST 4b — corps JSON invalide -> 400 explicite")
    payload4b = b"{not valid json"
    sig4b = sign_payload(payload4b, WEBHOOK_SECRET)
    r = c.post("/billing/pulse", content=payload4b, headers={"x-chariow-signature": sig4b, "x-pulse-delivery-id": "delivery-test4b", "content-type": "application/json"})
    check("HTTP 400 (jamais une 500)", r.status_code == 400)

    # -------------------------------------------------------------------
    # TEST 5 — successful.sale SANS custom_metadata NI email exploitable -> rejet explicite journalisé, jamais un succès silencieux
    # -------------------------------------------------------------------
    section("TEST 5 — aucune metadata, aucun email exploitable -> échec explicite journalisé")
    payload5 = _sale_event(user_id=None, plan=None, amount=1500, email="personne-inconnue-15-9@example.com")
    sig5 = sign_payload(payload5, WEBHOOK_SECRET)
    with _LogCapture() as logs:
        r = c.post("/billing/pulse", content=payload5, headers={"x-chariow-signature": sig5, "x-pulse-delivery-id": "delivery-test5", "content-type": "application/json"})
    check("HTTP 200 (accusé de réception Chariow — mais AUCUN traitement financier)", r.status_code == 200)
    check("log WARNING explicite 'IGNORÉ' (jamais un silence total)", logs.has(logging.WARNING, "IGNORÉ"))
    with Session(engine) as s:
        check("AUCUNE commission fantôme créée pour cet email inconnu",
              len(s.exec(select(ReferralCommission)).all()) == 2)  # seulement les 2 déjà créées par TEST 1/2

    # -------------------------------------------------------------------
    # TEST 6 — user_id absent MAIS email valide -> repli conforme (redondant avec TEST 2, reformulé explicitement)
    # -------------------------------------------------------------------
    section("TEST 6 — user_id absent, email valide -> repli conforme (déjà démontré au TEST 2)")
    check("couvert par TEST 2 (repli email réussi) — pas de duplication de scénario", True)

    # -------------------------------------------------------------------
    # TEST 7 — signature invalide -> 401 (non-régression)
    # -------------------------------------------------------------------
    section("TEST 7 — signature invalide -> 401")
    payload7 = _sale_event(user_id=buyer1_id, plan="monthly", amount=1500)
    r = c.post("/billing/pulse", content=payload7, headers={"x-chariow-signature": "sha256=deadbeef", "x-pulse-delivery-id": "delivery-test7", "content-type": "application/json"})
    check("HTTP 401", r.status_code == 401)

    # -------------------------------------------------------------------
    # TEST 8/9 — payment ID (delivery_id) rejoué, même contenu OU contenu contradictoire -> aucun doublon
    # -------------------------------------------------------------------
    section("TEST 8/9 — delivery_id rejoué (même contenu puis contenu contradictoire) -> aucun doublon")
    with Session(engine) as s:
        commissions_avant = len(s.exec(select(ReferralCommission)).all())
    # Renvoi EXACT du même événement TEST 1 (même delivery_id)
    r = c.post("/billing/pulse", content=payload1, headers={"x-chariow-signature": sig1, "x-pulse-delivery-id": "delivery-test1", "content-type": "application/json"})
    check("re-livraison identique -> 200 duplicate=true", r.status_code == 200 and r.json().get("duplicate") is True)
    # Renvoi du MÊME delivery_id mais avec un montant/utilisateur CONTRADICTOIRE
    payload9 = _sale_event(user_id=buyer2_id, plan="yearly", amount=28000)
    sig9 = sign_payload(payload9, WEBHOOK_SECRET)
    r = c.post("/billing/pulse", content=payload9, headers={"x-chariow-signature": sig9, "x-pulse-delivery-id": "delivery-test1", "content-type": "application/json"})
    check("re-livraison CONTRADICTOIRE même delivery_id -> 200 duplicate=true (jamais retraité)", r.status_code == 200 and r.json().get("duplicate") is True)
    with Session(engine) as s:
        commissions_apres = len(s.exec(select(ReferralCommission)).all())
        check("AUCUNE nouvelle commission créée par les re-livraisons (même contradictoires)", commissions_apres == commissions_avant)
        sub1_recheck = s.exec(select(ProviderSubscription).where(ProviderSubscription.user_id == buyer1_id, ProviderSubscription.provider == "chariow")).first()
        check("ProviderSubscription de buyer1 non altérée par le rejeu contradictoire (toujours son propre plan)", sub1_recheck.plan == "monthly")

    # -------------------------------------------------------------------
    # TEST 10 — 'produit inconnu' : plan non standard dans custom_metadata -> stocké tel quel, jamais de crash,
    # AUCUN risque financier (la commission ne dérive jamais du plan, uniquement du montant réel du webhook).
    # NOTE : successful.sale ne reçoit jamais de product_id Chariow à valider — 'plan' est une valeur
    # ENTIÈREMENT auto-générée par Xfoot lui-même à la création du checkout (déjà validée contre PRODUCT_IDS
    # à CE moment-là, voir create_checkout_session) — un 'produit inconnu' à ce stade ne peut provenir que
    # d'une metadata altérée/tronquée, jamais d'une vraie tentative d'achat légitime.
    # -------------------------------------------------------------------
    section("TEST 10 — plan non reconnu dans custom_metadata -> stocké tel quel, aucun risque financier")
    buyer10_id, buyer10_token = register_and_login(c, "buyer10-15-9@example.com", "correct-horse-battery-staple", name="Buyer Ten")
    _checkout_and_get_sub(c, buyer10_token)
    payload10 = _sale_event(user_id=buyer10_id, plan="plan-jamais-vu-ailleurs", amount=1500)
    sig10 = sign_payload(payload10, WEBHOOK_SECRET)
    r = c.post("/billing/pulse", content=payload10, headers={"x-chariow-signature": sig10, "x-pulse-delivery-id": "delivery-test10", "content-type": "application/json"})
    check("HTTP 200, aucun crash", r.status_code == 200)
    with Session(engine) as s:
        sub10 = s.exec(select(ProviderSubscription).where(ProviderSubscription.user_id == buyer10_id, ProviderSubscription.provider == "chariow")).first()
        check("abonnement activé (le paiement réel prime, jamais bloqué par un libellé de plan inconnu)", sub10.status == "active")

    # -------------------------------------------------------------------
    # TEST 11 — montant absent/invalide -> abonnement activé mais AUCUNE commission (déjà garanti par
    # money.py::extract_actual_paid_amount + commission_service.py, re-testé ici après durcissement)
    # -------------------------------------------------------------------
    section("TEST 11 — montant absent -> aucune commission, abonnement quand même activé")
    buyer11_id, buyer11_token = register_and_login(c, "buyer11-15-9@example.com", "correct-horse-battery-staple", name="Buyer Eleven")
    c.post("/referral/attribute", json={"slug": "promoter159"}, headers={"Authorization": f"Bearer {buyer11_token}"})
    _checkout_and_get_sub(c, buyer11_token)
    payload11 = make_event("successful.sale", sale={"custom_metadata": {"user_id": str(buyer11_id), "plan": "monthly"}})  # pas de champ montant du tout
    sig11 = sign_payload(payload11, WEBHOOK_SECRET)
    r = c.post("/billing/pulse", content=payload11, headers={"x-chariow-signature": sig11, "x-pulse-delivery-id": "delivery-test11", "content-type": "application/json"})
    check("HTTP 200", r.status_code == 200)
    with Session(engine) as s:
        sub11 = s.exec(select(ProviderSubscription).where(ProviderSubscription.user_id == buyer11_id, ProviderSubscription.provider == "chariow")).first()
        check("abonnement activé malgré montant absent", sub11.status == "active")
        comm11 = s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == buyer11_id)).first()
        check("AUCUNE commission créée (montant introuvable, §43 PAYMENT_CONFIRMATION_UNAVAILABLE)", comm11 is None)

    # -------------------------------------------------------------------
    # TEST 12 — paiement confirmé + referral existant -> exactement 1 commission de 40% (couvert par TEST 1/2)
    # -------------------------------------------------------------------
    section("TEST 12 — paiement confirmé + referral existant -> 600 FCFA (déjà démontré TEST 1 et TEST 2)")
    check("couvert par TEST 1 (600 FCFA) et TEST 2 (600 FCFA via repli email)", True)

    # -------------------------------------------------------------------
    # TEST 13 — paiement non confirmé -> aucune commission
    # -------------------------------------------------------------------
    section("TEST 13 — aucun paiement confirmé -> aucune commission")
    buyer13_id, buyer13_token = register_and_login(c, "buyer13-15-9@example.com", "correct-horse-battery-staple", name="Buyer Thirteen")
    c.post("/referral/attribute", json={"slug": "promoter159"}, headers={"Authorization": f"Bearer {buyer13_token}"})
    _checkout_and_get_sub(c, buyer13_token)
    with Session(engine) as s:
        comm13 = s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == buyer13_id)).first()
        check("aucune commission avant tout paiement", comm13 is None)

    # -------------------------------------------------------------------
    # TEST 14 — même événement envoyé plusieurs fois (3x) -> une seule vente et une seule commission
    # -------------------------------------------------------------------
    section("TEST 14 — événement rejoué 3 fois -> une seule vente, une seule commission")
    buyer14_id, buyer14_token = register_and_login(c, "buyer14-15-9@example.com", "correct-horse-battery-staple", name="Buyer Fourteen")
    c.post("/referral/attribute", json={"slug": "promoter159"}, headers={"Authorization": f"Bearer {buyer14_token}"})
    _checkout_and_get_sub(c, buyer14_token)
    payload14 = _sale_event(user_id=buyer14_id, plan="monthly", amount=1500)
    sig14 = sign_payload(payload14, WEBHOOK_SECRET)
    for _ in range(3):
        c.post("/billing/pulse", content=payload14, headers={"x-chariow-signature": sig14, "x-pulse-delivery-id": "delivery-test14", "content-type": "application/json"})
    with Session(engine) as s:
        commissions14 = s.exec(select(ReferralCommission).where(ReferralCommission.referred_user_id == buyer14_id)).all()
        check("exactement 1 commission malgré 3 envois du même événement", len(commissions14) == 1)
        check("commission = 600 FCFA", commissions14[0].commission_amount == 600)

    print(f"\n{'=' * 60}\n{_passed}/{_passed + _failed} tests reussis\n{'=' * 60}")
    return _failed == 0


if __name__ == "__main__":
    try:
        success = main()
    finally:
        cleanup_db(DB_PATH)
    sys.exit(0 if success else 1)
