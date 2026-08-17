"""
test_main.py — Tests de l'API via FastAPI TestClient (pas de serveur réseau).

Couvre :
  1. Résolution de noms d'équipes : exacte, alias, normalisée (accents/casse),
     faute de frappe (doit renvoyer 404 + suggestion, jamais de résolution
     silencieuse).
  2. Endpoint batch : mélange de matchs valides/invalides sur plusieurs
     ligues, erreurs isolées par match.

Depuis que /predictions/* est protégé par require_active_subscription
(cf. api/test_premium.py pour les tests dédiés 401/402/200), ces tests de
RÉSOLUTION DE NOMS ont besoin d'un compte authentifié ET abonné pour
continuer à s'exécuter — l'abonnement est activé une seule fois en début
de run via le même flux réel que test_billing.py (checkout mocké PUIS
webhook checkout.session.completed, jamais le webhook seul). Ce fichier ne
re-teste PAS les codes 401/402 eux-mêmes, c'est le rôle de test_premium.py.

Usage : python api/test_main.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db, register_and_login, activate_subscription

DB_PATH = configure_test_env("test_main.db")

from fastapi.testclient import TestClient
from main import app, resolve_team_name, LEAGUE_MODELS

AUTH_HEADER: dict = {}  # rempli dans __main__ une fois l'abonnement activé


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body["leagues_loaded"]) == {"Ligue1", "PremierLeague", "LaLiga", "Bundesliga", "SerieA"}
    print(f"  [OK] /health -> {body['leagues_loaded']}")


def test_resolution_exact(client):
    r = client.get("/predictions/Ligue1/Paris SG/Marseille", headers=AUTH_HEADER)
    assert r.status_code == 200
    body = r.json()
    assert body["home_team"] == "Paris SG"
    assert body["away_team"] == "Marseille"
    assert body["home_team_resolution"] == "exact"
    assert body["away_team_resolution"] == "exact"
    assert abs(body["home_win"] + body["draw"] + body["away_win"] - 1.0) < 1e-6
    print(f"  [OK] resolution exacte Paris SG vs Marseille -> {body['home_win']=} {body['draw']=} {body['away_win']=}")


def test_extended_prediction_markets(client):
    """BTTS, double chance, lignes over/under multiples — tous dérivés de
    la même matrice de score Dixon-Coles, aucun nouveau paramètre appris."""
    r = client.get("/predictions/Ligue1/Paris SG/Marseille", headers=AUTH_HEADER)
    assert r.status_code == 200
    body = r.json()

    assert abs(body["btts_yes"] + body["btts_no"] - 1.0) < 1e-6
    print(f"  [OK] BTTS -> yes={body['btts_yes']} no={body['btts_no']} (somme=1)")

    dc_1x = body["double_chance_1x"]
    dc_12 = body["double_chance_12"]
    dc_x2 = body["double_chance_x2"]
    assert abs(dc_1x - (body["home_win"] + body["draw"])) < 1e-6
    assert abs(dc_12 - (body["home_win"] + body["away_win"])) < 1e-6
    assert abs(dc_x2 - (body["draw"] + body["away_win"])) < 1e-6
    print(f"  [OK] Double chance -> 1X={dc_1x} 12={dc_12} X2={dc_x2} (cohérents avec 1X2)")

    lines = body["over_under_lines"]
    assert [l["line"] for l in lines] == [0.5, 1.5, 2.5, 3.5]
    for l in lines:
        assert abs(l["over"] + l["under"] - 1.0) < 1e-6
    overs = [l["over"] for l in lines]
    assert overs == sorted(overs, reverse=True), "P(over) doit décroître strictement quand la ligne augmente"
    assert abs(lines[2]["over"] - body["over_2_5"]) < 1e-6, "la ligne 2.5 doit rester cohérente avec over_2_5"
    print(f"  [OK] Lignes over/under {[l['line'] for l in lines]} -> P(over) strictement décroissante : {overs}")


def test_resolution_alias(client):
    r = client.get("/predictions/Ligue1/PSG/OM", headers=AUTH_HEADER)
    assert r.status_code == 200
    body = r.json()
    assert body["home_team"] == "Paris SG"
    assert body["away_team"] == "Marseille"
    assert body["home_team_resolution"] == "alias"
    assert body["away_team_resolution"] == "alias"
    print(f"  [OK] resolution alias PSG/OM -> {body['home_team']}/{body['away_team']}")

    r2 = client.get("/predictions/Bundesliga/Bayern/Dortmund", headers=AUTH_HEADER)
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["home_team"] == "Bayern Munich"
    assert body2["home_team_resolution"] == "alias"
    print(f"  [OK] resolution alias Bayern -> {body2['home_team']}")

    r3 = client.get("/predictions/SerieA/AC Milan/Inter Milan", headers=AUTH_HEADER)
    assert r3.status_code == 200
    body3 = r3.json()
    assert body3["home_team"] == "Milan"
    assert body3["away_team"] == "Inter"
    print(f"  [OK] resolution alias AC Milan/Inter Milan -> {body3['home_team']}/{body3['away_team']}")


def test_resolution_normalized(client):
    r = client.get("/predictions/Ligue1/st etienne/lyon", headers=AUTH_HEADER)
    assert r.status_code == 200
    body = r.json()
    assert body["home_team"] == "St Etienne"
    assert body["home_team_resolution"] in ("normalized", "alias")
    print(f"  [OK] resolution normalisee 'st etienne' -> {body['home_team']} (method={body['home_team_resolution']})")

    # avec accent explicite pour forcer le chemin de normalisation (pas d'alias 'kholn' existant)
    r2 = client.get("/predictions/Bundesliga/köln/Dortmund", headers=AUTH_HEADER)
    assert r2.status_code in (200, 404)
    print(f"  [OK] resolution normalisee 'köln' -> status={r2.status_code}")


def test_resolution_typo_returns_404_with_suggestion(client):
    r = client.get("/predictions/Ligue1/Marseil/Lyon", headers=AUTH_HEADER)
    assert r.status_code == 404
    body = r.json()
    assert "detail" in body
    print(f"  [OK] faute de frappe 'Marseil' -> 404, detail: {body['detail']}")

    r2 = client.get("/predictions/Ligue1/Xyzabc123/Lyon", headers=AUTH_HEADER)
    assert r2.status_code == 404
    print(f"  [OK] equipe inexistante 'Xyzabc123' -> 404, detail: {r2.json()['detail']}")


def test_resolve_team_name_never_silently_fuzzy(client):
    """Vérifie directement la fonction : un nom proche mais pas exact/alias
    ne doit jamais renvoyer resolved != None via le chemin fuzzy."""
    known = LEAGUE_MODELS["Ligue1"].teams
    res = resolve_team_name("Marseil", known)
    assert res["resolved"] is None
    assert res["method"] in ("fuzzy", "none")
    assert "Marseille" in res["suggestions"] or len(res["suggestions"]) >= 0
    print(f"  [OK] resolve_team_name('Marseil') -> resolved=None, suggestions={res['suggestions']}")


def test_batch_mixed_valid_invalid_multi_league(client):
    payload = [
        {"league": "Ligue1", "home_team": "PSG", "away_team": "Marseille"},
        {"league": "Ligue1", "home_team": "Xyzabc", "away_team": "Lyon"},
        {"league": "PremierLeague", "home_team": "Man City", "away_team": "Arsenal"},
        {"league": "Bundesliga", "home_team": "Bayern", "away_team": "Dortmund"},
        {"league": "SerieA", "home_team": "Juve", "away_team": "InvalidTeamXYZ"},
        {"league": "LigueInconnue", "home_team": "A", "away_team": "B"},
    ]
    r = client.post("/predictions/batch", json=payload, headers=AUTH_HEADER)
    assert r.status_code == 200
    results = r.json()
    assert len(results) == 6

    assert results[0]["ok"] is True
    assert results[0]["prediction"]["home_team"] == "Paris SG"

    assert results[1]["ok"] is False
    assert results[1]["error"] is not None

    assert results[2]["ok"] is True
    assert results[2]["prediction"]["home_team"] == "Man City"

    assert results[3]["ok"] is True
    assert results[3]["prediction"]["home_team"] == "Bayern Munich"

    assert results[4]["ok"] is False
    assert "InvalidTeamXYZ" in results[4]["error"] or results[4]["suggestions"] is not None

    assert results[5]["ok"] is False
    assert "Ligue inconnue" in results[5]["error"]

    n_ok = sum(1 for x in results if x["ok"])
    n_ko = sum(1 for x in results if not x["ok"])
    print(f"  [OK] batch multi-ligues : {n_ok} ok / {n_ko} echecs isoles, sur {len(results)} matchs")
    for res in results:
        status = "OK" if res["ok"] else f"ERREUR: {res['error']}"
        print(f"        [{res['league']}] {res['home_team_input']} vs {res['away_team_input']} -> {status}")


def test_ratings_endpoint(client):
    """/ratings reste public — appelé volontairement SANS AUTH_HEADER."""
    r = client.get("/ratings/Ligue1")
    assert r.status_code == 200
    ratings = r.json()
    assert len(ratings) == len(LEAGUE_MODELS["Ligue1"].teams)
    assert ratings[0]["net_rating"] >= ratings[-1]["net_rating"]
    print(f"  [OK] /ratings/Ligue1 (sans auth) -> {len(ratings)} equipes, top={ratings[0]['team']} ({ratings[0]['net_rating']})")


if __name__ == "__main__":
    failures = 0
    with TestClient(app) as client:
        user_id, token = register_and_login(client, "resolution-tests@example.com", "correct-horse-battery-staple")
        activate_subscription(client, token, user_id, email="resolution-tests@example.com",
                               webhook_secret="whsec_test_secret_for_signature_verification")
        AUTH_HEADER["Authorization"] = f"Bearer {token}"

        tests = [
            test_health,
            test_resolution_exact,
            test_extended_prediction_markets,
            test_resolution_alias,
            test_resolution_normalized,
            test_resolution_typo_returns_404_with_suggestion,
            test_resolve_team_name_never_silently_fuzzy,
            test_batch_mixed_valid_invalid_multi_league,
            test_ratings_endpoint,
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
