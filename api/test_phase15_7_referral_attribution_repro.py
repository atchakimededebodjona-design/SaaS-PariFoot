"""
test_phase15_7_referral_attribution_repro.py — Phase 15.7 : reproduction du
parcours réel de production (/promoter-2 -> inscription -> attribution) tel
qu'exécuté par le frontend (login.html + api.js), afin d'isoler si la cause
du bug observé (demo@payement.com/236, demo11@test.com/238,
demo2@payement.com/239 : PROMOTEUR="—" en Admin -> Abonnés malgré un passage
par /promoter-2) est côté backend (ce fichier) ou côté navigateur (hors de
portée d'un test backend, voir rapport Phase 15.7).

Reproduit fidèlement l'ORDRE EXACT et le FORMAT EXACT des appels que fait le
navigateur réel :
  1. POST /referral/resolve/{slug} SANS authentification, AVANT inscription
     (mime captureReferralFromUrl(), api.js:128-149).
  2. POST /auth/register puis POST /auth/login (mime register()+login(),
     login.html:157-167, 143-155).
  3. POST /referral/attribute AVEC authentification, avec un captured_at au
     format EXACT produit par `new Date().toISOString()` côté JS (suffixe
     "Z", jamais testé ailleurs dans ce dépôt — tous les autres fichiers de
     test utilisent Python `datetime.now(UTC).isoformat()`, qui produit un
     suffixe "+00:00" et non "Z" — écart de couverture comblé ici).

Base isolée dédiée (jamais api/app.db) — même discipline que les autres
fichiers de test.

Usage : python api/test_phase15_7_referral_attribution_repro.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db, register_and_login

DB_PATH = configure_test_env("test_phase15_7_referral_attribution_repro.db")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from main import app
from app.core.database import engine, init_db

init_db()
from app.models.promoter import ReferralAttribution
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


def js_iso_now() -> str:
    """Format EXACT produit par `new Date().toISOString()` côté navigateur :
    millisecondes à 3 chiffres + suffixe 'Z' (jamais '+00:00') — ce que le
    frontend envoie réellement, jamais reproduit ailleurs dans ce dépôt."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _make_promoter_2(c) -> tuple[int, str]:
    """Crée un promoteur avec le slug EXACT 'promoter-2' (chemin admin réel
    simulé via create_promoter, comme test_referral_promoter_platform.py)."""
    user_id, _token = register_and_login(c, "promoter2-owner@repro157.example.com", "correct-horse-battery-staple", name="Promoter Two")
    with Session(engine) as s:
        promoter = create_promoter(s, user_id=user_id, display_name="Promoter Two", requested_slug="promoter-2")
        assert promoter.slug == "promoter-2", f"slug inattendu: {promoter.slug}"
        assert promoter.status == "ACTIVE"
        promoter_id = promoter.id
    return user_id, promoter_id


def main():
    c = client()

    section("SETUP — promoter-2 (ACTIVE)")
    promoter_user_id, promoter_id = _make_promoter_2(c)
    check("promoter-2 créé et ACTIVE", promoter_id is not None)

    # -------------------------------------------------------------------
    # TEST 1 — reproduction fidèle de l'ordre + format RÉELS du navigateur.
    # C'est LE test qui doit échouer si le bug est reproductible côté backend.
    # -------------------------------------------------------------------
    section("TEST 1 — reproduction exacte du parcours réel (resolve -> register -> login -> attribute, timestamp format 'Z')")

    visitor_id = "repro-visitor-001"

    # Étape A/B : captureReferralFromUrl() — appelée AVANT toute inscription, SANS token.
    r_resolve = c.post("/referral/resolve/promoter-2", json={"visitor_id": visitor_id})
    check("resolve HTTP 200", r_resolve.status_code == 200)
    check("resolve valid=true", r_resolve.json().get("valid") is True)

    # Le frontend stocke le slug + un timestamp AU FORMAT JS EXACT en localStorage à ce stade
    # (mimé ici par une simple variable Python, puisqu'aucun localStorage n'existe côté serveur).
    captured_slug = "promoter-2"
    captured_at_js_format = js_iso_now()
    check("format capturé se termine bien par 'Z' (comme new Date().toISOString())", captured_at_js_format.endswith("Z"))

    # Étape D : inscription (register() puis login(), login.html:157-167 + 143-155).
    new_user_id, token = register_and_login(c, "repro-new-buyer@repro157.example.com", "correct-horse-battery-staple", name="Repro Buyer")
    check("nouvel utilisateur créé avec un user_id distinct du promoteur", new_user_id != promoter_user_id)

    # Étape E/F : attributeReferralIfPresent() -> POST /referral/attribute, AVEC le token fraîchement posé,
    # EXACTEMENT le payload envoyé par api.js:159-163 (slug, captured_at, visitor_id).
    r_attribute = c.post(
        "/referral/attribute",
        json={"slug": captured_slug, "captured_at": captured_at_js_format, "visitor_id": visitor_id},
        headers={"Authorization": f"Bearer {token}"},
    )
    check("attribute HTTP 200 (pas d'exception serveur sur le format 'Z')", r_attribute.status_code == 200)
    check("attribute attributed=true", r_attribute.json().get("attributed") is True)

    # Étape G : vérification DB — LA preuve qui compte.
    with Session(engine) as s:
        attribution = s.exec(select(ReferralAttribution).where(ReferralAttribution.converted_user_id == new_user_id)).first()
    check("ReferralAttribution persistée pour le nouvel utilisateur", attribution is not None)
    if attribution is not None:
        check("promoter_id correct", attribution.promoter_id == promoter_id)
        check("converted_user_id correct", attribution.converted_user_id == new_user_id)
        check("visitor_id correctement propagé", attribution.visitor_id == visitor_id)
        # SQLite (base de test) ne préserve pas le tzinfo au retour de lecture (comportement connu de
        # SQLAlchemy/SQLite, sans rapport avec le bug diagnostiqué ici, PostgreSQL en production le
        # préserve) — on vérifie donc la VALEUR (à la seconde près) plutôt que la présence de tzinfo.
        captured_at_naive = attribution.captured_at.replace(tzinfo=None) if attribution.captured_at else None
        expected_naive = datetime.strptime(captured_at_js_format, "%Y-%m-%dT%H:%M:%S.%fZ")
        check("captured_at correctement parsé et persisté (valeur exacte à la seconde près)",
              captured_at_naive is not None and abs((captured_at_naive - expected_naive).total_seconds()) < 1)

    # -------------------------------------------------------------------
    # TEST 2 — Admin -> Abonnés retrouve bien le promoteur pour ce nouvel utilisateur,
    # UNE FOIS qu'un ProviderSubscription existe (checkout mocké, sans paiement).
    # -------------------------------------------------------------------
    section("TEST 2 — Admin -> Abonnés retrouve le promoteur (après un checkout, sans paiement)")
    from unittest.mock import patch
    with patch("app.billing.router._create_chariow_checkout_link", return_value="https://chariow.com/checkout/test-link"):
        r_checkout = c.post("/billing/checkout", json={
            "plan": "monthly", "first_name": "Repro", "last_name": "Buyer",
            "phone_number": "0100000000", "phone_country_code": "CI",
        }, headers={"Authorization": f"Bearer {token}"})
    check("checkout mocké HTTP 200 (crée la ProviderSubscription, sans paiement)", r_checkout.status_code == 200)

    import os
    os.environ["ADMIN_EMAILS"] = "admin@repro157.example.com"
    admin_user_id, admin_token = register_and_login(c, "admin@repro157.example.com", "correct-horse-battery-staple", name="Admin Repro")
    r_subs = c.get("/admin/subscribers", headers={"Authorization": f"Bearer {admin_token}"})
    check("GET /admin/subscribers HTTP 200", r_subs.status_code == 200)
    row = next((s for s in r_subs.json() if s["user_id"] == new_user_id), None)
    check("le nouvel utilisateur apparaît dans Admin -> Abonnés", row is not None)
    if row is not None:
        check("promoter_slug affiché = 'promoter-2' (PAS '—'/None)", row.get("promoter_slug") == "promoter-2")

    # -------------------------------------------------------------------
    # TEST 3 — un utilisateur SANS referral (jamais passé par /promoter-2) reste sans promoteur.
    # -------------------------------------------------------------------
    section("TEST 3 — utilisateur sans referral reste sans promoteur")
    no_ref_user_id, no_ref_token = register_and_login(c, "no-referral@repro157.example.com", "correct-horse-battery-staple", name="No Referral")
    with Session(engine) as s:
        attribution_none = s.exec(select(ReferralAttribution).where(ReferralAttribution.converted_user_id == no_ref_user_id)).first()
    check("aucune ReferralAttribution créée sans passage par un lien referral", attribution_none is None)

    # -------------------------------------------------------------------
    # TEST 4 — referral vers un promoteur inexistant est refusé, aucune attribution.
    # -------------------------------------------------------------------
    section("TEST 4 — referral vers un slug inexistant refusé")
    ghost_user_id, ghost_token = register_and_login(c, "ghost-slug@repro157.example.com", "correct-horse-battery-staple", name="Ghost Slug")
    r_ghost = c.post("/referral/attribute", json={"slug": "does-not-exist-slug"}, headers={"Authorization": f"Bearer {ghost_token}"})
    check("attribute HTTP 200 (jamais une exception)", r_ghost.status_code == 200)
    check("attributed=false, reason=INVALID_OR_INACTIVE_PROMOTER", r_ghost.json() == {"attributed": False, "reason": "INVALID_OR_INACTIVE_PROMOTER"})
    with Session(engine) as s:
        attribution_ghost = s.exec(select(ReferralAttribution).where(ReferralAttribution.converted_user_id == ghost_user_id)).first()
    check("aucune ReferralAttribution créée pour un slug inexistant", attribution_ghost is None)

    # -------------------------------------------------------------------
    # TEST 5 — self-referral toujours bloqué (promoteur ne peut pas s'auto-attribuer).
    # -------------------------------------------------------------------
    section("TEST 5 — self-referral toujours bloqué")
    r_login_owner = c.post("/auth/login", data={"username": "promoter2-owner@repro157.example.com", "password": "correct-horse-battery-staple"})
    owner_token = r_login_owner.json()["access_token"]
    r_self = c.post("/referral/attribute", json={"slug": "promoter-2"}, headers={"Authorization": f"Bearer {owner_token}"})
    check("self-referral -> attributed=false, reason=SELF_REFERRAL_REJECTED", r_self.json() == {"attributed": False, "reason": "SELF_REFERRAL_REJECTED"})

    # -------------------------------------------------------------------
    # TEST 6 — une attribution existante n'est jamais écrasée par un second appel.
    # -------------------------------------------------------------------
    section("TEST 6 — attribution existante non écrasable")
    r_reattempt = c.post("/referral/attribute", json={"slug": "promoter-2"}, headers={"Authorization": f"Bearer {token}"})
    check("second appel -> attributed=false, reason=ALREADY_ATTRIBUTED (jamais réécrit)", r_reattempt.json() == {"attributed": False, "reason": "ALREADY_ATTRIBUTED"})
    with Session(engine) as s:
        attribution_after = s.exec(select(ReferralAttribution).where(ReferralAttribution.converted_user_id == new_user_id)).first()
    check("promoter_id inchangé après la tentative de ré-attribution", attribution_after.promoter_id == promoter_id)

    # -------------------------------------------------------------------
    # TEST 7 — le checkout n'accepte/ne transmet jamais un promoter_id fourni par le client.
    # -------------------------------------------------------------------
    section("TEST 7 — CheckoutRequest ne porte aucun champ promoter_id exploitable")
    from app.billing.router import CheckoutRequest
    check("CheckoutRequest n'a pas de champ promoter_id", "promoter_id" not in CheckoutRequest.model_fields)

    print(f"\n{'=' * 60}\n{_passed}/{_passed + _failed} tests reussis\n{'=' * 60}")
    return _failed == 0


if __name__ == "__main__":
    try:
        success = main()
    finally:
        cleanup_db(DB_PATH)
    sys.exit(0 if success else 1)
