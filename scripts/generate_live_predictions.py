"""
scripts/generate_live_predictions.py — Phase 9, Partie A : scheduler de
prédictions LIVE pour les matchs à venir.
=============================================================================

Tourne comme un SERVICE CRON RAILWAY SÉPARÉ du service web (même raison que
fetch_daily_results.py — filesystems non partagés entre services Railway,
seule la base Postgres l'est), recommandé AVANT le cron de résolution
quotidienne (voir railway.cron.live_predictions.json, 05:30 UTC vs 06:30 UTC
pour fetch_daily_results.py).

Étapes :
  a. Récupère les fixtures "not started" des 5 ligues suivies dans la
     fenêtre `LIVE_PREDICTION_WINDOW_HOURS` (défaut 48h) via
     app.core.api_football_client.fetch_upcoming_fixtures — 1-2 requêtes/jour
     (aujourd'hui + demain), largement dans le tier gratuit (voir ce module).
  b. Rapproche chaque fixture aux noms canoniques internes (LEAGUE_MODELS)
     via app.core.team_name_matching (même logique que fetch_daily_results.py,
     extraite pour être réutilisée ici SANS DUPLICATION — Phase 9, §2).
  c. Construit les UpcomingFixture et appelle
     app.ai.arena.scheduler.generate_live_predictions(), qui délègue
     ENTIÈREMENT à ModelOrchestrator.predict_all() (Phase 7, inchangé) —
     aucune deuxième logique de prédiction ici.
  d. Idempotent par construction (contrainte UNIQUE existante sur
     model_predictions, Phase 6) — un run relancé plusieurs fois ne crée
     jamais de doublon (§7 du ticket Phase 9).

Codes de sortie (même convention que fetch_daily_results.py) :
  0 = tout traité avec succès (ou rien à faire)
  1 = échec total (clé API absente, ou appel API-Football en erreur)
  2 = succès partiel (au moins une fixture non rapprochée à un nom canonique)

Usage :
    python scripts/generate_live_predictions.py                    # fenêtre par défaut (48h)
    python scripts/generate_live_predictions.py --window-hours 24
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("generate_live_predictions")

ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "")


def _send_alert(message: str) -> None:
    """Best effort, identique à fetch_daily_results.py::_send_alert /
    refresh_and_retrain.py::_send_alert (copie volontaire, voir leur
    docstring pour pourquoi une 3e extraction n'a pas été jugée utile)."""
    if not ALERT_WEBHOOK_URL:
        return
    try:
        httpx.post(ALERT_WEBHOOK_URL, json={"text": message, "content": message}, timeout=10.0)
    except Exception as e:
        logger.warning(f"Envoi de l'alerte webhook impossible (non bloquant) : {e}")


def run(window_hours: int) -> int:
    from sqlmodel import Session
    from app.core.database import engine, init_db
    from app.core.api_football_config import API_FOOTBALL_KEY, API_FOOTBALL_LEAGUE_IDS
    from app.core.api_football_client import fetch_upcoming_fixtures
    from app.core.team_name_matching import names_match
    from app.ai.arena.orchestrator import ModelOrchestrator, default_models
    from app.ai.arena.scheduler import UpcomingFixture, generate_live_predictions
    from main import load_league_models

    logger.info("=" * 80)
    logger.info(f"DÉBUT DU JOB — fenêtre : {window_hours}h")
    logger.info("=" * 80)

    if not API_FOOTBALL_KEY:
        logger.error("API_FOOTBALL_KEY absente de l'environnement — job arrêté.")
        _send_alert("[Xfoot] generate_live_predictions : API_FOOTBALL_KEY absente, job arrêté.")
        return 1

    init_db()
    league_models = load_league_models()
    orchestrator = ModelOrchestrator(default_models(league_models))
    league_ids_to_name = {v: k for k, v in API_FOOTBALL_LEAGUE_IDS.items()}

    try:
        raw_fixtures = fetch_upcoming_fixtures(window_hours)
    except Exception as e:
        logger.error(f"Appel API-Football échoué : {e}")
        _send_alert(f"[Xfoot] generate_live_predictions : échec total — {e}")
        return 1

    logger.info(f"{len(raw_fixtures)} fixture(s) à venir reçue(s) d'API-Football sur la fenêtre.")

    fixtures: list[UpcomingFixture] = []
    unmatched: list[str] = []
    for raw in raw_fixtures:
        league_id = raw.get("league", {}).get("id")
        league = league_ids_to_name.get(league_id)
        if league is None or league not in league_models:
            continue

        home_api = raw.get("teams", {}).get("home", {}).get("name", "")
        away_api = raw.get("teams", {}).get("away", {}).get("name", "")
        teams = league_models[league].teams
        home = next((t for t in teams if names_match(home_api, t)), None)
        away = next((t for t in teams if names_match(away_api, t)), None)
        if home is None or away is None:
            unmatched.append(f"[{league}] {home_api} vs {away_api}")
            continue

        kickoff_raw = raw.get("fixture", {}).get("date")
        kickoff = datetime.fromisoformat(kickoff_raw.replace("Z", "+00:00"))
        fixtures.append(UpcomingFixture(league=league, home_team=home, away_team=away, match_date=kickoff))

    logger.info(f"{len(fixtures)} fixture(s) rapprochée(s) à un nom canonique connu, {len(unmatched)} non rapprochée(s).")
    if unmatched:
        for m in unmatched:
            logger.warning(f"  non rapproché : {m}")

    with Session(engine) as session:
        result = generate_live_predictions(session, orchestrator, fixtures, now=datetime.now(timezone.utc))

    logger.info("=" * 80)
    logger.info("RÉCAPITULATIF")
    logger.info(f"  Fixtures considérées : {result.fixtures_considered}")
    logger.info(f"  Ignorées (déjà passées) : {result.fixtures_skipped_past}")
    logger.info(f"  Prédictions shadow journalisées : {result.shadow_predictions_logged}")
    logger.info("=" * 80)

    if unmatched or result.shadow_errors:
        logger.warning("JOB TERMINÉ EN SUCCÈS PARTIEL.")
        _send_alert(
            f"[Xfoot] generate_live_predictions — succès partiel : "
            f"{len(unmatched)} fixture(s) non rapprochée(s), {len(result.shadow_errors)} erreur(s) shadow."
        )
        return 2

    logger.info("JOB TERMINÉ AVEC SUCCÈS COMPLET.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--window-hours", type=int, default=None,
                         help="Fenêtre de prédiction en heures (défaut : LIVE_PREDICTION_WINDOW_HOURS ou 48).")
    args = parser.parse_args()

    from app.ai.arena.scheduler import DEFAULT_LIVE_PREDICTION_WINDOW_HOURS
    window = args.window_hours if args.window_hours is not None else DEFAULT_LIVE_PREDICTION_WINDOW_HOURS
    sys.exit(run(window))
