"""
test_phase15_1_new_offers.py — Phase 15.1 : intégration des 3 nouvelles
offres Chariow officielles (2 semaines / mensuel / annuel).

Ne réimplémente RIEN : réutilise exclusivement les fonctions déjà
testées et auditées de app/core/chariow_config.py (PRODUCT_IDS),
app/billing/router.py (checkout, inchangé sauf 1 commentaire) et
app/referral/money.py::compute_commission_amount (formule de commission,
totalement inchangée) — ce fichier vérifie uniquement que ces mécanismes
déjà en place fonctionnent correctement pour les 3 NOUVELLES clés de plan
et les 3 NOUVEAUX montants réels, sans créer de second système.

AUCUN appel réseau réel vers Chariow (mocké, même discipline que
test_chariow_billing.py). AUCUN paiement réel. AUCUNE transaction
historique touchée (aucune donnée pré-existante n'est lue ni modifiée par
ce fichier — base de test isolée, jamais api/app.db).

Usage : python api/test_phase15_1_new_offers.py
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

TEST_DB_PATH = Path(__file__).parent / "test_phase15_1_new_offers.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"
os.environ["JWT_SECRET_KEY"] = "test-only-secret-key-not-for-production-use"
os.environ["CHARIOW_API_KEY"] = "test_dummy_never_used_network_is_mocked"
os.environ["CHARIOW_PULSE_SECRET"] = "pulse_test_secret_phase15_1"
# Phase 15.1 : les 3 offres officielles — Product ID de test distincts pour
# vérifier que chaque plan est bien routé vers SON propre produit, jamais
# un autre (§ "aucune ancienne offre utilisée par erreur").
os.environ["CHARIOW_PRODUCT_ID_BIWEEKLY"] = "prd_1gje3jzz_test"
os.environ["CHARIOW_PRODUCT_ID_MONTHLY"] = "prd_sgvapilx_test"
os.environ["CHARIOW_PRODUCT_ID_YEARLY"] = "prd_f90jpbh3_test"

if TEST_DB_PATH.exists():
    TEST_DB_PATH.unlink()

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient

from main import app
from app.core.database import init_db

# TestClient(app) sans `with` ne déclenche jamais @app.on_event("startup") (qui appelle init_db()) — les
# tables ne seraient jamais créées. Appelé explicitement ici une fois, idempotent, même pattern que les
# autres fichiers de test de ce dépôt (test_referral_promoter_platform.py, etc.).
init_db()

from app.core.chariow_config import PRODUCT_IDS
from app.referral.money import compute_commission_amount

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


def register_and_login(c, email, password, name="Test User"):
    r = c.post("/auth/register", json={"name": name, "email": email, "password": password})
    assert r.status_code == 201, r.text
    r2 = c.post("/auth/login", data={"username": email, "password": password})
    assert r2.status_code == 200, r2.text
    return r.json()["id"], r2.json()["access_token"]


CHECKOUT_FIELDS = {"first_name": "Bob", "last_name": "Kouassi", "phone_number": "0700000000", "phone_country_code": "CI"}


# ---------------------------------------------------------------------------
# 1. PRODUCT_IDS — structure du dictionnaire (réutilisé de chariow_config.py,
#    jamais un second système de mapping).
# ---------------------------------------------------------------------------

def test_product_ids_dict_has_3_keys():
    section("1. PRODUCT_IDS contient exactement les 3 clés attendues (biweekly/monthly/yearly)")
    check("biweekly présent", "biweekly" in PRODUCT_IDS)
    check("monthly présent", "monthly" in PRODUCT_IDS)
    check("yearly présent", "yearly" in PRODUCT_IDS)
    check("biweekly = product ID de test attendu", PRODUCT_IDS["biweekly"] == "prd_1gje3jzz_test")
    check("monthly = product ID de test attendu", PRODUCT_IDS["monthly"] == "prd_sgvapilx_test")
    check("yearly = product ID de test attendu", PRODUCT_IDS["yearly"] == "prd_f90jpbh3_test")


# ---------------------------------------------------------------------------
# 2/3/4. Checkout — chaque plan doit router vers SON propre Product ID,
#    jamais un autre (test bout-en-bout via /billing/checkout, mocké au
#    niveau réseau uniquement — même discipline que test_chariow_billing.py).
# ---------------------------------------------------------------------------

def _checkout_for_plan(c, token, plan):
    with patch("app.billing.router._create_chariow_checkout_link",
               return_value=f"https://chariow.com/checkout/test-{plan}") as mocked:
        r = c.post("/billing/checkout", json={"plan": plan, **CHECKOUT_FIELDS},
                    headers={"Authorization": f"Bearer {token}"})
    return r, mocked


def test_biweekly_plan_recognized_correct_product_id():
    section("2. plan biweekly reconnu -> bon Product ID Chariow (prd_1gje3jzz)")
    c = client()
    _, token = register_and_login(c, "phase15_1_test_biweekly@example.com", "correct-horse-battery-staple")
    r, mocked = _checkout_for_plan(c, token, "biweekly")
    check("checkout accepté (200)", r.status_code == 200)
    mocked.assert_called_once()
    check("product_id envoyé = celui du plan 2 semaines, jamais un autre", mocked.call_args.kwargs["product_id"] == "prd_1gje3jzz_test")


def test_monthly_plan_recognized_correct_product_id():
    section("3. plan monthly reconnu -> bon Product ID Chariow (prd_sgvapilx)")
    c = client()
    _, token = register_and_login(c, "phase15_1_test_monthly@example.com", "correct-horse-battery-staple")
    r, mocked = _checkout_for_plan(c, token, "monthly")
    check("checkout accepté (200)", r.status_code == 200)
    mocked.assert_called_once()
    check("product_id envoyé = celui du plan mensuel, jamais un autre", mocked.call_args.kwargs["product_id"] == "prd_sgvapilx_test")


def test_yearly_plan_recognized_correct_product_id():
    section("4. plan yearly reconnu -> bon Product ID Chariow (prd_f90jpbh3)")
    c = client()
    _, token = register_and_login(c, "phase15_1_test_yearly@example.com", "correct-horse-battery-staple")
    r, mocked = _checkout_for_plan(c, token, "yearly")
    check("checkout accepté (200)", r.status_code == 200)
    mocked.assert_called_once()
    check("product_id envoyé = celui du plan annuel, jamais un autre", mocked.call_args.kwargs["product_id"] == "prd_f90jpbh3_test")


def test_unknown_plan_still_rejected_400():
    section("5. plan inconnu (ancienne clé hypothétique ou faute de frappe) -> toujours 400, jamais une offre par défaut")
    c = client()
    _, token = register_and_login(c, "phase15_1_test_unknown@example.com", "correct-horse-battery-staple")
    r = c.post("/billing/checkout", json={"plan": "biannual", **CHECKOUT_FIELDS},
               headers={"Authorization": f"Bearer {token}"})
    check("plan inconnu -> 400 (jamais une offre choisie par défaut/erreur)", r.status_code == 400)


# ---------------------------------------------------------------------------
# 6/7/8. Commission théorique — 40% du montant RÉELLEMENT payé (jamais
#    calculée depuis un prix catalogue) — réutilise compute_commission_amount
#    telle quelle, aucune duplication de la formule.
# ---------------------------------------------------------------------------

def test_commission_biweekly_1000_x_40_percent_400():
    section("6. commission théorique 2 semaines : 1000 x 40% = 400 FCFA")
    check("compute_commission_amount(1000, 4000bp) == 400", compute_commission_amount(1000, 4000) == 400)


def test_commission_monthly_1500_x_40_percent_600():
    section("7. commission théorique mensuel : 1500 x 40% = 600 FCFA")
    check("compute_commission_amount(1500, 4000bp) == 600", compute_commission_amount(1500, 4000) == 600)


def test_commission_yearly_28000_x_40_percent_11200():
    section("8. commission théorique annuel : 28000 x 40% = 11200 FCFA")
    check("compute_commission_amount(28000, 4000bp) == 11200", compute_commission_amount(28000, 4000) == 11200)


def test_no_real_commission_generated_by_this_file():
    section("9. aucune commission RÉELLE générée par ce fichier (calculs théoriques uniquement, aucun paiement/webhook réel simulé)")
    from sqlmodel import Session, select
    from app.core.database import engine
    from app.models.promoter import ReferralCommission
    with Session(engine) as s:
        rows = s.exec(select(ReferralCommission)).all()
    check("0 ligne ReferralCommission dans cette base de test isolée (jamais api/app.db)", len(rows) == 0)


def run_all():
    tests = [
        test_product_ids_dict_has_3_keys,
        test_biweekly_plan_recognized_correct_product_id,
        test_monthly_plan_recognized_correct_product_id,
        test_yearly_plan_recognized_correct_product_id,
        test_unknown_plan_still_rejected_400,
        test_commission_biweekly_1000_x_40_percent_400,
        test_commission_monthly_1500_x_40_percent_600,
        test_commission_yearly_28000_x_40_percent_11200,
        test_no_real_commission_generated_by_this_file,
    ]
    for t in tests:
        t()
    total = _passed + _failed
    print(f"\n{'=' * 60}")
    print(f"{_passed}/{total} tests reussis" if _failed == 0 else f"{_passed} passed, {_failed} failed (sur {total} assertions)")
    print("=" * 60)
    return _failed == 0


if __name__ == "__main__":
    ok = run_all()
    try:
        if TEST_DB_PATH.exists():
            TEST_DB_PATH.unlink()
    except PermissionError:
        pass
    sys.exit(0 if ok else 1)
