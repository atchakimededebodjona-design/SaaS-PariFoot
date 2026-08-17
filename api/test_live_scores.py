"""
test_live_scores.py — Cache serveur partagé pour GET /live-scores (voir
_get_live_scores dans main.py) : SANS ce cache, chaque visite de la page
viderait le quota API-Football (100 requêtes/jour) en quelques minutes un
jour de match. Vérifie ici, avec httpx.get mocké (aucun appel réseau
réel) :

  1. Filtrage : seuls les matchs de nos 5 championnats suivis apparaissent,
     un match d'une autre ligue est ignoré.
  2. Le cache n'est PAS rafraîchi tant qu'il n'a pas expiré (aucun 2e appel
     réseau sur une requête rapprochée).
  3. TTL adaptatif : plus court quand des matchs de nos ligues sont en
     cours, plus long sinon.
  4. Résilience : si l'appel API-Football échoue, le dernier cache connu
     est renvoyé (jamais d'erreur remontée à l'utilisateur).

Usage : python api/test_live_scores.py
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db, register_and_login

DB_PATH = configure_test_env("test_live_scores.db")

from fastapi.testclient import TestClient
from main import app
import main as api_main

AUTH_HEADER: dict = {}


class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _make_fixture(league_id: int, home: str, away: str, home_goals: int, away_goals: int,
                   status_short: str = "1H", elapsed: int = 30) -> dict:
    return {
        "league": {"id": league_id},
        "teams": {"home": {"name": home}, "away": {"name": away}},
        "goals": {"home": home_goals, "away": away_goals},
        "fixture": {"status": {"short": status_short, "elapsed": elapsed}},
    }


def _reset_cache():
    api_main._live_scores_cache["matches"] = []
    api_main._live_scores_cache["next_fetch_at"] = None


def test_filters_to_our_five_leagues(client):
    _reset_cache()
    ligue1_id = api_main.API_FOOTBALL_LEAGUE_IDS["Ligue1"]
    fixtures = [
        _make_fixture(ligue1_id, "Paris SG", "Marseille", 1, 0),
        _make_fixture(999999, "Boca Juniors", "River Plate", 2, 2),  # hors périmètre
    ]

    with patch("httpx.get", return_value=FakeResponse({"response": fixtures, "errors": []})):
        r = client.get("/live-scores", headers=AUTH_HEADER)
    assert r.status_code == 200, r.text
    matches = r.json()
    assert len(matches) == 1
    assert matches[0]["league"] == "Ligue1"
    assert matches[0]["home_team"] == "Paris SG"
    assert matches[0]["home_goals"] == 1
    print(f"  [OK] 1 match Ligue1 retourné, 1 match hors périmètre (Argentine) ignoré -> {matches[0]}")


def test_cache_not_refetched_before_expiry(client):
    _reset_cache()
    ligue1_id = api_main.API_FOOTBALL_LEAGUE_IDS["Ligue1"]
    fixtures = [_make_fixture(ligue1_id, "Lyon", "Nice", 0, 0)]

    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(params)
        return FakeResponse({"response": fixtures, "errors": []})

    with patch("httpx.get", side_effect=fake_get):
        r1 = client.get("/live-scores", headers=AUTH_HEADER)
        r2 = client.get("/live-scores", headers=AUTH_HEADER)

    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()
    assert len(calls) == 1, f"attendu 1 seul appel réseau pour 2 requêtes rapprochées, obtenu {len(calls)}"
    print("  [OK] 2 requêtes rapprochées -> 1 seul appel API-Football (cache respecté)")


def test_ttl_shorter_when_matches_found_longer_when_empty(client):
    ligue1_id = api_main.API_FOOTBALL_LEAGUE_IDS["Ligue1"]

    _reset_cache()
    with patch("httpx.get", return_value=FakeResponse({
        "response": [_make_fixture(ligue1_id, "Lille", "Rennes", 1, 1)], "errors": [],
    })):
        client.get("/live-scores", headers=AUTH_HEADER)
    ttl_with_matches = api_main._live_scores_cache["next_fetch_at"]
    assert ttl_with_matches is not None

    _reset_cache()
    with patch("httpx.get", return_value=FakeResponse({"response": [], "errors": []})):
        client.get("/live-scores", headers=AUTH_HEADER)
    ttl_no_matches = api_main._live_scores_cache["next_fetch_at"]
    assert ttl_no_matches is not None

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    assert (ttl_with_matches - now) < (ttl_no_matches - now), \
        "le TTL avec matchs en cours doit être plus court que sans match (backoff)"
    print(f"  [OK] TTL avec matchs ({(ttl_with_matches - now).total_seconds():.0f}s) "
          f"< TTL sans match ({(ttl_no_matches - now).total_seconds():.0f}s)")


def test_api_failure_keeps_previous_cache(client):
    _reset_cache()
    ligue1_id = api_main.API_FOOTBALL_LEAGUE_IDS["Ligue1"]
    fixtures = [_make_fixture(ligue1_id, "Monaco", "Toulouse", 3, 1)]

    with patch("httpx.get", return_value=FakeResponse({"response": fixtures, "errors": []})):
        r1 = client.get("/live-scores", headers=AUTH_HEADER)
    assert r1.status_code == 200
    assert len(r1.json()) == 1

    # Force l'expiration immédiate du cache pour forcer une tentative de
    # rafraîchissement, PUIS simule un appel API-Football en échec.
    api_main._live_scores_cache["next_fetch_at"] = None
    with patch("httpx.get", side_effect=RuntimeError("réseau indisponible")):
        r2 = client.get("/live-scores", headers=AUTH_HEADER)

    assert r2.status_code == 200, "un échec réseau ne doit jamais remonter d'erreur à l'utilisateur"
    assert r2.json() == r1.json(), "le dernier cache connu doit être renvoyé tel quel"
    print("  [OK] échec de l'appel API-Football -> dernier cache conservé, pas d'erreur 500")


if __name__ == "__main__":
    failures = 0
    with TestClient(app) as client:
        _, token = register_and_login(client, "live-scores-tests@example.com", "correct-horse-battery-staple")
        AUTH_HEADER["Authorization"] = f"Bearer {token}"

        tests = [
            test_filters_to_our_five_leagues,
            test_cache_not_refetched_before_expiry,
            test_ttl_shorter_when_matches_found_longer_when_empty,
            test_api_failure_keeps_previous_cache,
        ]
        for t in tests:
            print(f"\n=== {t.__name__} ===")
            try:
                t(client)
            except AssertionError as e:
                failures += 1
                print(f"  [ECHEC] {e}")

    cleanup_db(DB_PATH)
    print(f"\n{'='*60}\n{len(tests)-failures}/{len(tests)} tests reussis\n{'='*60}")
    sys.exit(1 if failures else 0)
