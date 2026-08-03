"""
test_auth.py — Tests du système d'authentification JWT via TestClient.

IMPORTANT : utilise `with TestClient(app) as client:` — nécessaire pour
déclencher l'event startup (@app.on_event("startup") -> init_db()) et
créer les tables, sinon "no such table: user".

Utilise une base SQLite ISOLÉE (test_auth.db, dans ce dossier) — jamais
api/app.db (la base de développement réelle). La variable DATABASE_URL
doit être positionnée AVANT le premier import de app.core.database (le
moteur SQLAlchemy est créé une fois au chargement du module).

Usage : python api/test_auth.py
"""

import os
import sys
from pathlib import Path

TEST_DB_PATH = Path(__file__).parent / "test_auth.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"
os.environ["JWT_SECRET_KEY"] = "test-only-secret-key-not-for-production-use"

if TEST_DB_PATH.exists():
    TEST_DB_PATH.unlink()  # base propre à chaque exécution

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient
from main import app

EMAIL = "alice@example.com"
PASSWORD = "correct-horse-battery-staple"


def test_register_success(client):
    r = client.post("/auth/register", json={"email": EMAIL, "password": PASSWORD})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == EMAIL
    assert body["is_active"] is True
    assert "hashed_password" not in body  # ne doit jamais être exposé
    print(f"  [OK] inscription réussie -> id={body['id']}")


def test_register_duplicate_refused(client):
    r = client.post("/auth/register", json={"email": EMAIL, "password": "autre-mot-de-passe"})
    assert r.status_code == 400, r.text
    print(f"  [OK] inscription en double refusée -> 400, detail: {r.json()['detail']}")


def test_login_success(client):
    r = client.post("/auth/login", data={"username": EMAIL, "password": PASSWORD})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"
    print(f"  [OK] connexion réussie -> token_type={body['token_type']}, token (tronqué)={body['access_token'][:20]}...")
    return body["access_token"]


def test_login_wrong_password_no_enumeration(client):
    r_wrong_pw = client.post("/auth/login", data={"username": EMAIL, "password": "mauvais-mot-de-passe"})
    r_unknown_email = client.post("/auth/login", data={"username": "inconnu@example.com", "password": "peu importe"})

    assert r_wrong_pw.status_code == 401
    assert r_unknown_email.status_code == 401
    assert r_wrong_pw.json()["detail"] == r_unknown_email.json()["detail"], (
        "le message doit être IDENTIQUE entre mauvais mot de passe et email inconnu "
        "(pas d'énumération de comptes)"
    )
    print(f"  [OK] mauvais mot de passe -> 401, message identique à 'email inconnu' : "
          f"'{r_wrong_pw.json()['detail']}'")


def test_me_without_token_401(client):
    r = client.get("/auth/me")
    assert r.status_code == 401, r.text
    print(f"  [OK] /auth/me sans token -> 401")


def test_me_with_valid_token_200(client, token):
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] == EMAIL
    print(f"  [OK] /auth/me avec token valide -> 200, email={body['email']}")


def test_ratings_endpoint_still_public(client):
    """/ratings n'a jamais dépendu de l'auth ni de l'abonnement (vitrine
    gratuite) — reste accessible sans token. NOTE : /predictions/* est
    devenu payant (require_active_subscription, cf. app/billing/dependencies.py)
    depuis l'ajout de la facturation — ce n'est plus un endpoint public
    "existant" à re-tester ici, voir api/test_premium.py pour ses codes
    401/402/200 spécifiques."""
    r = client.get("/ratings/Ligue1")
    assert r.status_code == 200, r.text
    ratings = r.json()
    assert len(ratings) > 0
    print(f"  [OK] /ratings/Ligue1 toujours public (sans token) -> 200, {len(ratings)} équipes")


if __name__ == "__main__":
    failures = 0
    with TestClient(app) as client:
        steps = [
            ("test_register_success", lambda: test_register_success(client)),
            ("test_register_duplicate_refused", lambda: test_register_duplicate_refused(client)),
            ("test_login_success", lambda: test_login_success(client)),
            ("test_login_wrong_password_no_enumeration", lambda: test_login_wrong_password_no_enumeration(client)),
            ("test_me_without_token_401", lambda: test_me_without_token_401(client)),
            ("test_ratings_endpoint_still_public", lambda: test_ratings_endpoint_still_public(client)),
        ]
        token = None
        for name, fn in steps:
            print(f"\n=== {name} ===")
            try:
                result = fn()
                if name == "test_login_success":
                    token = result
            except AssertionError as e:
                failures += 1
                print(f"  [ECHEC] {e}")

        print(f"\n=== test_me_with_valid_token_200 ===")
        try:
            test_me_with_valid_token_200(client, token)
        except AssertionError as e:
            failures += 1
            print(f"  [ECHEC] {e}")

    try:
        if TEST_DB_PATH.exists():
            TEST_DB_PATH.unlink()
    except PermissionError:
        # Windows garde parfois un verrou bref sur le fichier SQLite tant que
        # le moteur SQLAlchemy (créé une fois au chargement du module) n'a
        # pas été explicitement libéré — sans conséquence, le fichier est de
        # toute façon supprimé au début de la prochaine exécution (voir plus haut).
        pass

    n_tests = 7
    print(f"\n{'='*60}\n{n_tests - failures}/{n_tests} tests reussis\n{'='*60}")
    sys.exit(1 if failures else 0)
