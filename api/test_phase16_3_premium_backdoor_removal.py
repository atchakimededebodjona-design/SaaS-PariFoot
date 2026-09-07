"""
test_phase16_3_premium_backdoor_removal.py — Phase 16.3 : non-régression de la
suppression du Premium backdoor codé en dur (app/billing/dependencies.py::
require_active_subscription).

Avant correctif : `if current_user.email == "atchakimededebodjona@gmail.com":
return current_user` sautait intégralement is_premium(). Après correctif :
CE compte est traité comme n'importe quel autre utilisateur par
require_active_subscription — ses privilèges administratifs (require_admin,
via ADMIN_EMAILS) restent, eux, entièrement inchangés et indépendants.

N'appelle JAMAIS Chariow réellement (checkout/Pulses mockés/signés à la main,
comme test_premium.py). N'utilise JAMAIS le backdoor pour démontrer B (le
backdoor est justement ce qu'on prouve supprimé en A).

Usage : python api/test_phase16_3_premium_backdoor_removal.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import (
    configure_test_env, cleanup_db, register_and_login, activate_subscription,
)

WEBHOOK_SECRET = "pulse_test_secret_for_signature_verification"
DB_PATH = configure_test_env("test_phase16_3_premium_backdoor_removal.db", webhook_secret=WEBHOOK_SECRET)

from fastapi.testclient import TestClient
from main import app

FORMERLY_BACKDOORED_EMAIL = "atchakimededebodjona@gmail.com"
ADMIN_EMAIL_2 = "second.admin@example.com"  # deuxième adresse administrative confirmée en production, forme générique ici
PREMIUM_USER_EMAIL = "denise.premium@example.com"
NORMAL_USER_EMAIL = "gaston.normal@example.com"
PASSWORD = "correct-horse-battery-staple"

BATCH_PAYLOAD = [{"league": "Ligue1", "home_team": "PSG", "away_team": "Marseille"}]


def test_A_backdoor_removed_402_without_subscription(client, token):
    """L'ex-email privilégié, authentifié mais SANS aucun abonnement Premium,
    doit désormais être refusé exactement comme n'importe quel utilisateur —
    plus aucun retour anticipé dans require_active_subscription."""
    headers = {"Authorization": f"Bearer {token}"}
    r = client.get("/predictions/Ligue1/PSG/Marseille", headers=headers)
    assert r.status_code == 402, f"le backdoor Premium semble toujours actif : {r.text}"
    print(f"  [OK] {FORMERLY_BACKDOORED_EMAIL} sans abonnement -> 402 (le bypass Premium a bien disparu)")

    r_batch = client.post("/predictions/batch", json=BATCH_PAYLOAD, headers=headers)
    assert r_batch.status_code == 402, f"le backdoor Premium semble toujours actif sur /predictions/batch : {r_batch.text}"
    print(f"  [OK] {FORMERLY_BACKDOORED_EMAIL} sans abonnement -> /predictions/batch -> 402 également")

    r_ens = client.post("/models/ensemble/predict", json={
        "league": "Ligue1", "home_team": "PSG", "away_team": "Marseille",
    }, headers=headers)
    assert r_ens.status_code == 402, f"le backdoor Premium semble toujours actif sur /models/ensemble/predict : {r_ens.text}"
    print(f"  [OK] {FORMERLY_BACKDOORED_EMAIL} sans abonnement -> /models/ensemble/predict -> 402 également")


def test_B_admin_still_admin_via_admin_emails(client, token):
    """Le même compte doit rester administrateur — uniquement via ADMIN_EMAILS/
    require_admin, jamais via le backdoor Premium (supprimé, cf. test A ci-dessus,
    exécuté juste avant sur ce même compte sans jamais avoir été rendu Premium)."""
    headers = {"Authorization": f"Bearer {token}"}
    r = client.get("/admin/promoters", headers=headers)
    assert r.status_code == 200, (
        f"le compte configuré dans ADMIN_EMAILS devrait garder l'accès admin (require_admin), "
        f"indépendamment de tout statut Premium : {r.text}"
    )
    print(f"  [OK] {FORMERLY_BACKDOORED_EMAIL} (non Premium, cf. test A) -> GET /admin/promoters -> 200 "
          f"(require_admin/_admin_emails() fonctionne toujours, totalement indépendant du Premium)")


def test_B2_second_admin_email_also_works(client):
    """Une deuxième adresse admin (ADMIN_EMAILS peut en contenir plusieurs,
    CSV) doit aussi être reconnue par require_admin — preuve que le mécanisme
    lit bien la liste complète, pas seulement la première entrée."""
    user_id, token = register_and_login(client, ADMIN_EMAIL_2, PASSWORD)
    r = client.get("/admin/promoters", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, f"la deuxième adresse ADMIN_EMAILS devrait aussi être admin : {r.text}"
    print(f"  [OK] deuxième adresse admin ({ADMIN_EMAIL_2}) -> GET /admin/promoters -> 200")


def test_C_normal_premium_flow_unchanged(client):
    """Un utilisateur ORDINAIRE (pas dans ADMIN_EMAILS) avec un abonnement
    Chariow réellement activé (checkout + Pulses mockés/signés) doit continuer
    à passer require_active_subscription normalement — le correctif ne touche
    QUE le cas particulier de l'email codé en dur."""
    user_id, token = register_and_login(client, PREMIUM_USER_EMAIL, PASSWORD)
    activate_subscription(client, token, user_id, email=PREMIUM_USER_EMAIL, webhook_secret=WEBHOOK_SECRET,
                           stripe_subscription_id="lic_phase16_3_premium")
    headers = {"Authorization": f"Bearer {token}"}
    r = client.get("/predictions/Ligue1/PSG/Marseille", headers=headers)
    assert r.status_code == 200, f"un abonnement Premium réel devrait toujours donner accès : {r.text}"
    print(f"  [OK] utilisateur normal avec abonnement Premium réellement actif -> /predictions/... -> 200 (inchangé)")

    r_batch = client.post("/predictions/batch", json=BATCH_PAYLOAD, headers=headers)
    assert r_batch.status_code == 200, r_batch.text
    print(f"  [OK] utilisateur normal avec abonnement Premium réellement actif -> /predictions/batch -> 200 (inchangé)")


def test_D_normal_user_without_subscription_still_refused(client):
    """Un utilisateur ordinaire, sans email spécial et sans abonnement, doit
    rester refusé — comportement déjà existant, non affecté par ce correctif,
    vérifié ici comme témoin de contrôle."""
    user_id, token = register_and_login(client, NORMAL_USER_EMAIL, PASSWORD)
    r = client.get("/predictions/Ligue1/PSG/Marseille", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 402, r.text
    print(f"  [OK] utilisateur normal sans abonnement (témoin) -> 402 (comportement déjà existant, inchangé)")


def test_D2_normal_user_never_had_admin_access(client):
    """Contrôle négatif : un utilisateur normal, absent de ADMIN_EMAILS, ne
    doit PAS avoir accès aux endpoints admin — confirme que le correctif n'a
    pas élargi accidentellement l'accès admin."""
    r_login = client.post("/auth/login", data={"username": NORMAL_USER_EMAIL, "password": PASSWORD})
    token = r_login.json()["access_token"]
    r = client.get("/admin/promoters", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403, r.text
    print(f"  [OK] utilisateur normal (témoin) -> GET /admin/promoters -> 403 (inchangé)")


if __name__ == "__main__":
    # ADMIN_EMAILS positionnée ICI (jamais avant l'import de main : _admin_emails()
    # relit os.environ à CHAQUE requête, aucune mise en cache — donc pas besoin de la
    # positionner avant l'import de app.core.database, contrairement à DATABASE_URL).
    os.environ["ADMIN_EMAILS"] = f"{FORMERLY_BACKDOORED_EMAIL}, {ADMIN_EMAIL_2}"

    failures = 0
    with TestClient(app) as client:
        user_id, token = register_and_login(client, FORMERLY_BACKDOORED_EMAIL, PASSWORD)

        steps = [
            ("test_A_backdoor_removed_402_without_subscription", lambda: test_A_backdoor_removed_402_without_subscription(client, token)),
            ("test_B_admin_still_admin_via_admin_emails", lambda: test_B_admin_still_admin_via_admin_emails(client, token)),
            ("test_B2_second_admin_email_also_works", lambda: test_B2_second_admin_email_also_works(client)),
            ("test_C_normal_premium_flow_unchanged", lambda: test_C_normal_premium_flow_unchanged(client)),
            ("test_D_normal_user_without_subscription_still_refused", lambda: test_D_normal_user_without_subscription_still_refused(client)),
            ("test_D2_normal_user_never_had_admin_access", lambda: test_D2_normal_user_never_had_admin_access(client)),
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
