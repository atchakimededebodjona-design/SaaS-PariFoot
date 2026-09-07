"""
test_phase16_6_2_security_headers.py — Phase 16.6.2 : non-régression des 3
headers de sécurité HTTP ajoutés (api/main.py::add_security_headers),
identifiés sans risque fonctionnel par l'audit read-only Phase 16.6.1 :

    X-Content-Type-Options: nosniff
    X-Frame-Options: DENY
    Referrer-Policy: strict-origin-when-cross-origin

Vérifie leur présence sur tous les types de réponse (200, erreur 401/404,
préflight CORS OPTIONS) et confirme explicitement qu'ils ne cassent PAS le
comportement CORS existant (access-control-allow-origin doit rester intact
sur un préflight — c'était le principal risque de régression identifié
avant l'implémentation, déjà vérifié manuellement, formalisé ici).

HSTS et un CSP applicatif renforcé sont hors périmètre (différés, cf. Phase
16.6.1) — volontairement absents, non testés ici.

Usage : python api/test_phase16_6_2_security_headers.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db, register_and_login

DB_PATH = configure_test_env("test_phase16_6_2_security_headers.db")
os.environ["ALLOWED_ORIGINS"] = "https://xfoot.site"  # nécessaire pour le test de préflight CORS ci-dessous

from fastapi.testclient import TestClient
from main import app

EXPECTED_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
}

_passed = 0
_failed = 0


def check(name: str, cond: bool) -> None:
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  [ECHEC] {name}")


def _check_headers_present(label: str, response) -> None:
    for header, expected_value in EXPECTED_HEADERS.items():
        check(f"{label} : {header} == {expected_value!r}", response.headers.get(header) == expected_value)


def test_headers_on_public_200(client):
    r = client.get("/health")
    check("GET /health -> 200", r.status_code == 200)
    _check_headers_present("GET /health (200)", r)


def test_headers_on_404(client):
    r = client.get("/route-qui-nexiste-pas-phase16-6-2")
    check("GET route inexistante -> 404", r.status_code == 404)
    _check_headers_present("GET 404", r)


def test_headers_on_401_unauthenticated(client):
    r = client.get("/predictions/Ligue1/PSG/Marseille")
    check("GET /predictions/... sans auth -> 401", r.status_code == 401)
    _check_headers_present("GET 401", r)


def test_headers_present_and_cors_unaffected_on_preflight(client):
    """Preuve explicite qu'ajouter ces headers n'a pas cassé le CORS
    existant — vérifié manuellement avant ce test, formalisé ici."""
    r = client.options("/auth/login", headers={
        "Origin": "https://xfoot.site",
        "Access-Control-Request-Method": "POST",
    })
    check("OPTIONS /auth/login (préflight CORS) -> 200", r.status_code == 200)
    check("CORS toujours fonctionnel : access-control-allow-origin présent",
          r.headers.get("access-control-allow-origin") == "https://xfoot.site")
    _check_headers_present("OPTIONS préflight", r)


def test_headers_on_authenticated_200(client):
    """Vérifie aussi une réponse 200 authentifiée (chemin applicatif complet,
    pas seulement les cas d'erreur/préflight ci-dessus)."""
    user_id, token = register_and_login(client, "phase16_6_2_user@example.com", "correct-horse-battery-staple")
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    check("GET /auth/me authentifié -> 200", r.status_code == 200)
    _check_headers_present("GET /auth/me (200 authentifié)", r)


if __name__ == "__main__":
    with TestClient(app) as client:
        steps = [
            ("test_headers_on_public_200", lambda: test_headers_on_public_200(client)),
            ("test_headers_on_404", lambda: test_headers_on_404(client)),
            ("test_headers_on_401_unauthenticated", lambda: test_headers_on_401_unauthenticated(client)),
            ("test_headers_present_and_cors_unaffected_on_preflight", lambda: test_headers_present_and_cors_unaffected_on_preflight(client)),
            ("test_headers_on_authenticated_200", lambda: test_headers_on_authenticated_200(client)),
        ]
        for name, fn in steps:
            print(f"\n=== {name} ===")
            try:
                fn()
            except Exception as e:
                _failed += 1
                print(f"  [ERREUR INATTENDUE] {type(e).__name__}: {e}")

    cleanup_db(DB_PATH)
    total = _passed + _failed
    print(f"\n{'='*60}\n{_passed}/{total} assertions reussies\n{'='*60}")
    sys.exit(1 if _failed else 0)
