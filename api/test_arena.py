"""
test_arena.py — Xfoot AI Arena (Phase 5) : GET /models/performance et
GET /models/{model_version_id} (voir app/ai/arena/).

Couvre :
  1. Base vide (aucune ligne model_versions/prediction_log) -> l'entrée de
     production Dixon-Coles apparaît quand même, metrics_available=False.
  2. Endpoint public, sans authentification (comme /ratings).
  3. Versions backtestées (model_versions) réutilisées telles quelles :
     is_active, notes en texte brut, PAS de métriques structurées inventées.
  4. Plusieurs versions du même model_type toutes retournées séparément.
  5. Plusieurs entrées is_active=True à la fois -> active_model_type="ambiguous",
     jamais tranché arbitrairement.
  6. Accuracy Dixon-Coles calculée depuis prediction_log une fois des
     prédictions résolues -> metrics_available bascule à True.
  7. team_ratings_count reflète les lignes réellement en base.
  8. GET /models/{id} : id réel -> 200, id inconnu -> 404 explicite.

Usage : python api/test_arena.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db, register_and_login, activate_subscription

DB_PATH = configure_test_env("test_arena.db")

from fastapi.testclient import TestClient
from sqlmodel import Session, select
from main import app, engine, LEAGUE_MODELS
from app.models.team_rating import ModelVersion, TeamRating
from app.models.prediction_log import PredictionLog

AUTH_HEADER: dict = {}


def test_performance_public_no_auth_and_production_entry_present(client):
    """Base vide de model_versions/prediction_log au premier appel : seule
    l'entrée de production Dixon-Coles doit apparaître, sans auth requise."""
    r = client.get("/models/performance")  # volontairement SANS AUTH_HEADER
    assert r.status_code == 200, r.text
    body = r.json()

    dc_entries = [m for m in body["models"] if m["model_type"] == "dixon_coles"]
    assert len(dc_entries) == 1, f"Attendu 1 entrée dixon_coles, trouvé {len(dc_entries)}"
    dc = dc_entries[0]
    assert dc["source"] == "model_artifact"
    assert dc["model_version_id"] is None
    assert dc["is_active"] is True
    assert dc["metrics_available"] is False, "aucune prédiction résolue encore -> pas de métrique"
    assert dc["metrics"]["sample_size"] == 0
    assert {l["league"] for l in dc["leagues"]} == set(LEAGUE_MODELS.keys())

    assert body["active_model_type"] == "dixon_coles"
    assert body["best_model_by_metric"] == "insufficient_data"
    print(f"  [OK] /models/performance sans auth -> entrée production dixon_coles présente, "
          f"{len(dc['leagues'])} ligues, metrics_available=False")


def test_backtested_versions_show_raw_notes_no_invented_metrics(client):
    with Session(engine) as session:
        elo = ModelVersion(
            name="xfoot-elo-v1", model_type="elo", is_active=False,
            trained_at=datetime.now(timezone.utc),
            notes="Brier moyen Elo=0.2041 vs Dixon-Coles=0.1998 -> PAS DE GAIN, reste inactif.",
        )
        session.add(elo)
        session.commit()

    r = client.get("/models/performance")
    assert r.status_code == 200, r.text
    body = r.json()

    elo_entries = [m for m in body["models"] if m["model_type"] == "elo"]
    assert len(elo_entries) == 1
    e = elo_entries[0]
    assert e["source"] == "model_versions"
    assert e["is_active"] is False
    assert e["metrics_available"] is False
    assert e["metrics"] == {"accuracy_1x2": None, "accuracy_btts": None,
                             "accuracy_over_under_2_5": None, "sample_size": 0}
    assert "PAS DE GAIN" in e["notes"], "les notes brutes du backtest doivent être exposées telles quelles"
    assert e["team_ratings_count"] == 0
    print(f"  [OK] version elo backtestée -> is_active=False, metrics_available=False, "
          f"notes brutes préservées : {e['notes'][:40]}...")


def test_multiple_versions_same_model_type_all_returned(client):
    with Session(engine) as session:
        v1 = ModelVersion(name="xfoot-xgboost-v1", model_type="xgboost", is_active=False,
                           trained_at=datetime.now(timezone.utc), notes="run 1 — pas de gain")
        v2 = ModelVersion(name="xfoot-xgboost-v2", model_type="xgboost", is_active=False,
                           trained_at=datetime.now(timezone.utc), notes="run 2 — pas de gain non plus")
        session.add(v1)
        session.add(v2)
        session.commit()

    r = client.get("/models/performance")
    body = r.json()
    xgb_versions = sorted(
        (m["version"] for m in body["models"] if m["model_type"] == "xgboost")
    )
    assert xgb_versions == ["xfoot-xgboost-v1", "xfoot-xgboost-v2"], xgb_versions
    print(f"  [OK] 2 versions xgboost distinctes toutes retournées séparément : {xgb_versions}")


def test_two_active_entries_reported_as_ambiguous(client):
    """Si une version backtestée est ELLE AUSSI marquée is_active=True (en
    plus de la production, toujours active) -> active_model_type="ambiguous",
    jamais tranché arbitrairement (règle §9 du ticket)."""
    with Session(engine) as session:
        v = ModelVersion(
            name="xfoot-dixon-coles-v2", model_type="dixon_coles", is_active=True,
            trained_at=datetime.now(timezone.utc),
            notes="Entraîné depuis match/match_stats ; non-régression vs production validée.",
        )
        session.add(v)
        session.commit()

    r = client.get("/models/performance")
    body = r.json()
    assert body["active_model_type"] == "ambiguous"
    print("  [OK] production + version model_versions actives simultanément -> active_model_type='ambiguous'")


def test_accuracy_available_once_predictions_resolved(client):
    r = client.get("/predictions/Ligue1/Paris SG/Marseille", headers=AUTH_HEADER)
    assert r.status_code == 200, r.text

    with Session(engine) as session:
        log = session.exec(
            select(PredictionLog).where(
                PredictionLog.league == "Ligue1",
                PredictionLog.home_team == "Paris SG",
                PredictionLog.away_team == "Marseille",
            )
        ).one()
        log.result_home_goals, log.result_away_goals = 2, 0
        log.result_fetched_at = datetime.now(timezone.utc)
        log.correct_1x2 = (log.pick_1x2 == "home")
        log.correct_btts = (log.pick_btts == "no")
        log.correct_over_2_5 = (log.pick_over_2_5 == "under")
        session.add(log)
        session.commit()

    r = client.get("/models/performance")
    body = r.json()
    dc = next(m for m in body["models"] if m["model_type"] == "dixon_coles")
    assert dc["metrics_available"] is True
    assert dc["metrics"]["sample_size"] == 1
    assert dc["metrics"]["accuracy_1x2"] in (0.0, 1.0)
    print(f"  [OK] 1 prédiction résolue -> metrics_available=True, accuracy_1x2={dc['metrics']['accuracy_1x2']}")


def _create_model_version_with_ratings(name: str) -> int:
    with Session(engine) as session:
        v = ModelVersion(name=name, model_type="elo", is_active=False,
                          trained_at=datetime.now(timezone.utc), notes="run avec ratings")
        session.add(v)
        session.commit()
        session.refresh(v)
        for team in ("Paris SG", "Marseille", "Lyon"):
            session.add(TeamRating(team=team, league="Ligue1", attack=0.1, defense=0.05, model_version_id=v.id))
        session.commit()
        return v.id


def test_team_ratings_count_reflects_real_rows(client):
    version_id = _create_model_version_with_ratings("xfoot-elo-v2")

    r = client.get("/models/performance")
    body = r.json()
    entry = next(m for m in body["models"] if m["model_version_id"] == version_id)
    assert entry["team_ratings_count"] == 3
    print(f"  [OK] team_ratings_count reflète les 3 lignes réellement écrites (version #{version_id})")


def test_model_detail_endpoint(client):
    version_id = _create_model_version_with_ratings("xfoot-elo-v3")

    r = client.get(f"/models/{version_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model_version_id"] == version_id
    assert body["model_type"] == "elo"
    print(f"  [OK] GET /models/{version_id} -> détail correct")

    r2 = client.get("/models/999999")
    assert r2.status_code == 404, r2.text
    assert "Dixon-Coles" in r2.json()["detail"]
    print("  [OK] GET /models/999999 (inexistant) -> 404 avec message explicite")


if __name__ == "__main__":
    failures = 0
    with TestClient(app) as client:
        user_id, token = register_and_login(client, "arena-tests@example.com", "correct-horse-battery-staple")
        activate_subscription(client, token, user_id, email="arena-tests@example.com",
                               webhook_secret="whsec_test_secret_for_signature_verification")
        AUTH_HEADER["Authorization"] = f"Bearer {token}"

        tests = [
            test_performance_public_no_auth_and_production_entry_present,
            test_backtested_versions_show_raw_notes_no_invented_metrics,
            test_multiple_versions_same_model_type_all_returned,
            test_two_active_entries_reported_as_ambiguous,
            test_accuracy_available_once_predictions_resolved,
            test_team_ratings_count_reflects_real_rows,
            test_model_detail_endpoint,
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
