"""
test_prediction_history.py — Historique des prédictions et % de réussite
(voir _log_prediction/_pick_1x2 et GET /predictions/history dans main.py).

Couvre :
  1. GET /predictions/history est accessible à tout compte connecté (PAS
     require_active_subscription — décision produit : sert de preuve
     sociale pour les non-abonnés), mais reste 401 sans authentification.
  2. Chaque prédiction générée via /predictions/{league}/{home}/{away} est
     loguée automatiquement, avec les bons picks (1X2/BTTS/O-U 2.5).
  3. Dédoublonnage : rejouer la même requête le même jour ne crée pas une
     deuxième ligne.
  4. `accuracy` ne compte que les prédictions déjà résolues (result_fetched_at
     renseigné par fetch_daily_results.py, simulé ici en écrivant
     directement en base) — les matchs sans résultat connu apparaissent en
     historique avec result=null mais hors du calcul du pourcentage.

Usage : python api/test_prediction_history.py
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db, register_and_login, activate_subscription

DB_PATH = configure_test_env("test_prediction_history.db")

from fastapi.testclient import TestClient
from sqlmodel import Session, select
from main import app, engine
from app.models.prediction_log import PredictionLog

AUTH_HEADER: dict = {}


def test_history_requires_auth_but_not_subscription(client):
    r = client.get("/predictions/history")
    assert r.status_code == 401, r.text
    print("  [OK] /predictions/history sans token -> 401")

    r = client.get("/predictions/history", headers=AUTH_HEADER)
    assert r.status_code == 200, r.text
    print("  [OK] /predictions/history avec compte connecté SANS abonnement actif -> 200 (pas de require_active_subscription)")


def test_prediction_gets_logged_with_correct_picks(client):
    r = client.get("/predictions/Ligue1/Paris SG/Marseille", headers=AUTH_HEADER)
    assert r.status_code == 200, r.text
    prediction = r.json()

    r = client.get("/predictions/history", headers=AUTH_HEADER)
    assert r.status_code == 200, r.text
    body = r.json()

    today = date.today().isoformat()
    day = next((d for d in body["days"] if d["date"] == today), None)
    assert day is not None, f"Aucune entrée pour aujourd'hui ({today}) dans {body['days']}"

    match = next(m for m in day["matches"] if m["home_team"] == "Paris SG" and m["away_team"] == "Marseille")
    assert match["result"] is None
    assert match["correct_1x2"] is None

    expected_1x2 = "home" if prediction["home_win"] >= max(prediction["draw"], prediction["away_win"]) else (
        "away" if prediction["away_win"] >= prediction["draw"] else "draw")
    assert match["pick_1x2"] == expected_1x2
    expected_btts = "yes" if prediction["btts_yes"] >= prediction["btts_no"] else "no"
    assert match["pick_btts"] == expected_btts
    expected_ou = "over" if prediction["over_2_5"] >= prediction["under_2_5"] else "under"
    assert match["pick_over_2_5"] == expected_ou
    print(f"  [OK] prédiction loguée avec picks corrects -> 1x2={match['pick_1x2']} btts={match['pick_btts']} ou={match['pick_over_2_5']}")


def test_replaying_same_prediction_does_not_duplicate(client):
    for _ in range(3):
        r = client.get("/predictions/Ligue1/Paris SG/Marseille", headers=AUTH_HEADER)
        assert r.status_code == 200

    r = client.get("/predictions/history", headers=AUTH_HEADER)
    body = r.json()
    today = date.today().isoformat()
    day = next(d for d in body["days"] if d["date"] == today)
    matches = [m for m in day["matches"] if m["home_team"] == "Paris SG" and m["away_team"] == "Marseille"]
    assert len(matches) == 1, f"Attendu 1 ligne malgré 3 requêtes identiques, trouvé {len(matches)}"
    print("  [OK] 3 requêtes identiques le même jour -> 1 seule ligne d'historique (dédoublonné)")


def test_accuracy_only_counts_resolved_predictions(client):
    r = client.get("/predictions/PremierLeague/Man City/Arsenal", headers=AUTH_HEADER)
    assert r.status_code == 200

    with Session(engine) as session:
        log = session.exec(
            select(PredictionLog).where(
                PredictionLog.league == "PremierLeague",
                PredictionLog.home_team == "Man City",
                PredictionLog.away_team == "Arsenal",
            )
        ).one()
        from datetime import datetime, timezone
        log.result_home_goals = 2
        log.result_away_goals = 0
        log.result_fetched_at = datetime.now(timezone.utc)
        log.correct_1x2 = (log.pick_1x2 == "home")
        log.correct_btts = (log.pick_btts == "no")
        log.correct_over_2_5 = (log.pick_over_2_5 == "under")
        session.add(log)
        session.commit()

    r = client.get("/predictions/history", headers=AUTH_HEADER)
    body = r.json()
    accuracy = body["accuracy"]

    assert accuracy["sample_size"] == 1, f"Une seule prédiction résolue (Paris SG/Marseille reste non résolue) : {accuracy}"
    assert accuracy["overall_1x2"] in (0.0, 1.0)
    print(f"  [OK] accuracy calculée uniquement sur la prédiction résolue -> {accuracy}")

    today = date.today().isoformat()
    day = next(d for d in body["days"] if d["date"] == today)
    resolved_match = next(m for m in day["matches"] if m["home_team"] == "Man City")
    assert resolved_match["result"] == {"home_goals": 2, "away_goals": 0}
    unresolved_match = next(m for m in day["matches"] if m["home_team"] == "Paris SG")
    assert unresolved_match["result"] is None
    print("  [OK] match résolu affiche le score réel, match non résolu affiche result=null")


if __name__ == "__main__":
    failures = 0
    with TestClient(app) as client:
        user_id, token = register_and_login(client, "history-tests@example.com", "correct-horse-battery-staple")
        AUTH_HEADER["Authorization"] = f"Bearer {token}"

        tests = [
            test_history_requires_auth_but_not_subscription,
        ]

        # Le reste des tests a besoin de générer de vraies prédictions ->
        # nécessite un abonnement actif (require_active_subscription sur
        # /predictions/{league}/{home}/{away}), activé APRÈS le premier test
        # ci-dessus pour bien vérifier que /predictions/history, lui, ne
        # l'exige pas.
        activate_subscription(client, token, user_id, email="history-tests@example.com",
                               webhook_secret="whsec_test_secret_for_signature_verification")

        tests += [
            test_prediction_gets_logged_with_correct_picks,
            test_replaying_same_prediction_does_not_duplicate,
            test_accuracy_only_counts_resolved_predictions,
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
