"""
fetch_daily_results.py — Rapproche les prédictions loguées la veille
(api/app/models/prediction_log.py) avec les scores réels obtenus via
API-Football (api-football.com), pour la page Historique & Performance
(GET /predictions/history dans api/main.py).
=============================================================================

Tourne comme un SERVICE CRON RAILWAY SÉPARÉ du service web (voir
RAILWAY_CRON_SETUP.md pour le pourquoi : filesystems non partagés entre
services Railway, seule la base Postgres l'est) — ce script n'écrit
jamais rien sur disque, uniquement en base.

Étapes :
  a. Charge les PredictionLog de la date cible (hier, UTC, par défaut)
     dont le résultat n'est pas encore connu (result_fetched_at IS NULL).
  b. UN SEUL appel GET /fixtures (API-Football, date+status=FT, TOUTES
     ligues confondues), puis regroupement côté client par ligue interne
     via league.id (api/app/core/api_football_config.py) — 1 requête/jour,
     largement dans le tier gratuit.

     IMPORTANT (vérifié empiriquement, pas dans la doc) : le plan Free
     d'API-Football REFUSE le filtre `league`+`season` combiné pour la
     saison en cours ("Free plans do not have access to this season, try
     from 2022 to 2024") — donc PAS de filtre `league=<id>` côté serveur,
     même si l'API l'accepte en théorie. Une requête par date SEULE (sans
     `league`) fonctionne et renvoie bien les données de la saison en
     cours, toutes ligues confondues — d'où le filtrage par league.id fait
     ici après coup plutôt que côté serveur.
  c. Rapproche chaque PredictionLog à une fixture via ses deux noms
     d'équipe (normalisation + petite table d'alias + fuzzy en dernier
     recours — mêmes noms canoniques que api/model_artifacts/*.json,
     jamais les mêmes que ceux renvoyés par API-Football). PAS besoin de
     la liste complète des équipes de la ligue : on compare directement
     le nom de la fixture à celui déjà stocké dans le PredictionLog.
  d. Écrit result_home_goals/result_away_goals/result_fetched_at et les
     3 correct_* (comparaison au pick stocké au moment de la prédiction),
     un par un, commit après chaque match rapproché (best effort — un
     échec sur un match n'empêche pas les suivants).
  e. Log récapitulatif (rapprochés / non rapprochés / erreurs API), alerte
     webhook si échec total ou partiel (ALERT_WEBHOOK_URL, déjà utilisé
     par refresh_and_retrain.py).

Codes de sortie :
  0 = tout rapproché avec succès (ou rien à faire)
  1 = échec total (clé API absente, ou toutes les ligues concernées en erreur)
  2 = succès partiel (au moins une ligue en erreur ou un match non rapproché)

Usage :
    python fetch_daily_results.py                    # date cible = hier (UTC)
    python fetch_daily_results.py --date 2026-08-16   # date cible explicite (tests manuels)
"""

import argparse
import difflib
import logging
import os
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

logger = logging.getLogger("fetch_daily_results")

ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "")

# Nommage API-Football -> nommage canonique interne (football-data.co.uk,
# cf. api/model_artifacts/*.json) pour les cas où normalisation+fuzzy ne
# suffisent pas seuls (abréviations/noms officiels trop différents). Liste
# non exhaustive au démarrage — à enrichir empiriquement au fil des
# exécutions réelles (les cas non rapprochés sont loggés en warning avec
# les deux noms, pour repérage facile).
API_FOOTBALL_TEAM_ALIASES = {
    "manchester united": "Man United",
    "manchester city": "Man City",
    "nottingham forest": "Nott'm Forest",
    "west bromwich albion": "West Brom",
    "wolverhampton wanderers": "Wolves",
    "newcastle united": "Newcastle",
    "leicester city": "Leicester",
    "sheffield utd": "Sheffield United",
    "brighton & hove albion": "Brighton",
    "tottenham hotspur": "Tottenham",
    "paris saint germain": "Paris SG",
    "saint-etienne": "St Etienne",
    "olympique de marseille": "Marseille",
    "olympique lyonnais": "Lyon",
    "borussia monchengladbach": "M'gladbach",
    "bayer leverkusen": "Leverkusen",
    "internazionale": "Inter",
    "ac milan": "Milan",
}


def _normalize(name: str) -> str:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return n.lower().strip()


def _names_match(api_football_name: str, canonical_name: str, cutoff: float = 0.6) -> bool:
    """Le nom canonique est déjà connu (celui stocké dans PredictionLog au
    moment de la prédiction) — on vérifie juste que le nom renvoyé par
    API-Football désigne la même équipe, jamais l'inverse (pas besoin de
    choisir parmi toute la ligue)."""
    na, nb = _normalize(api_football_name), _normalize(canonical_name)
    if na == nb:
        return True
    alias = API_FOOTBALL_TEAM_ALIASES.get(na)
    if alias is not None and _normalize(alias) == nb:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= cutoff


def _compute_correctness(pick_1x2: str, pick_btts: str, pick_over_2_5: str,
                          home_goals: int, away_goals: int) -> tuple[bool, bool, bool]:
    if home_goals > away_goals:
        actual_1x2 = "home"
    elif away_goals > home_goals:
        actual_1x2 = "away"
    else:
        actual_1x2 = "draw"
    actual_btts = "yes" if home_goals > 0 and away_goals > 0 else "no"
    actual_over_2_5 = "over" if (home_goals + away_goals) > 2.5 else "under"

    return pick_1x2 == actual_1x2, pick_btts == actual_btts, pick_over_2_5 == actual_over_2_5


def _fetch_fixtures_by_league(target_date: date, base_url: str, api_key: str,
                               league_ids_to_name: dict[int, str]) -> dict[str, list[dict]]:
    """
    Un seul appel (paginé si besoin) pour TOUTES les ligues à `target_date`
    — voir docstring module pour pourquoi PAS de filtre `league` côté
    serveur. Regroupe ensuite les fixtures par ligue interne via
    fixture["league"]["id"], en ignorant celles qui ne concernent aucune
    des 5 ligues suivies (api_football_config.API_FOOTBALL_LEAGUE_IDS).
    """
    all_fixtures: list[dict] = []
    page = 1
    while True:
        # NE JAMAIS envoyer "page" à la 1ère requête : ce plan/endpoint
        # rejette le paramètre avec errors={"page": "The Page field do not
        # exist."} — silencieusement (HTTP 200, results=0), vérifié en
        # conditions réelles. On ne l'ajoute qu'à partir de la 2e page,
        # SI jamais paging.total > 1 un jour (jamais observé jusqu'ici sur
        # une requête par date, mais gardé par prudence).
        params = {"date": target_date.isoformat(), "status": "FT"}
        if page > 1:
            params["page"] = page
        resp = httpx.get(
            f"{base_url}/fixtures", params=params,
            headers={"x-apisports-key": api_key}, timeout=20.0,
        )
        resp.raise_for_status()
        data = resp.json()
        # API-Football renvoie ses erreurs (quota/plan/paramètres invalides)
        # en HTTP 200 avec un champ "errors" non vide — jamais un code HTTP
        # d'erreur — donc raise_for_status() seul ne suffit pas à les
        # détecter (vérifié en conditions réelles, cf. restriction league+
        # season plus haut).
        errors = data.get("errors")
        if errors:
            raise RuntimeError(f"réponse API-Football en erreur : {errors}")
        all_fixtures.extend(data.get("response", []))
        paging = data.get("paging") or {"current": 1, "total": 1}
        if paging.get("current", 1) >= paging.get("total", 1):
            break
        page += 1

    fixtures_by_league: dict[str, list[dict]] = {}
    for fixture in all_fixtures:
        league_id = fixture.get("league", {}).get("id")
        league_name = league_ids_to_name.get(league_id)
        if league_name is not None:
            fixtures_by_league.setdefault(league_name, []).append(fixture)
    return fixtures_by_league


def _send_alert(message: str) -> None:
    """Best effort, identique à refresh_and_retrain.py::_send_alert."""
    if not ALERT_WEBHOOK_URL:
        return
    try:
        httpx.post(ALERT_WEBHOOK_URL, json={"text": message, "content": message}, timeout=10.0)
    except Exception as e:
        logger.warning(f"Envoi de l'alerte webhook impossible (non bloquant) : {e}")


def _setup_logging(log_dir: Path) -> Path:
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / "fetch_daily_results.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
        force=True,
    )
    return log_path


def run(target_date: date | None = None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "api"))
    from sqlmodel import Session, select
    from app.core.database import engine, init_db
    from app.models.prediction_log import PredictionLog
    from app.core.api_football_config import API_FOOTBALL_KEY, API_FOOTBALL_BASE_URL, API_FOOTBALL_LEAGUE_IDS

    target_date = target_date or (datetime.now(timezone.utc).date() - timedelta(days=1))
    logger.info("=" * 80)
    logger.info(f"DÉBUT DU JOB — cible : {target_date.isoformat()}")
    logger.info("=" * 80)

    if not API_FOOTBALL_KEY:
        logger.error("API_FOOTBALL_KEY absente de l'environnement — job arrêté.")
        _send_alert("[Xfoot] fetch_daily_results : API_FOOTBALL_KEY absente, job arrêté.")
        return 1

    init_db()
    with Session(engine) as session:
        pending = session.exec(
            select(PredictionLog).where(
                PredictionLog.match_date == target_date,
                PredictionLog.result_fetched_at.is_(None),
            )
        ).all()

    if not pending:
        logger.info("Aucune prédiction en attente de résultat pour cette date. Rien à faire.")
        return 0

    leagues_needed = sorted({log.league for log in pending})
    logger.info(f"{len(pending)} prédiction(s) en attente sur {len(leagues_needed)} ligue(s) : {leagues_needed}")

    league_ids_to_name = {v: k for k, v in API_FOOTBALL_LEAGUE_IDS.items()}
    try:
        fixtures_by_league = _fetch_fixtures_by_league(target_date, API_FOOTBALL_BASE_URL, API_FOOTBALL_KEY,
                                                         league_ids_to_name)
    except Exception as e:
        logger.error(f"Appel API-Football échoué : {e}")
        _send_alert(f"[Xfoot] fetch_daily_results : échec total pour {target_date.isoformat()} — {e}")
        return 1

    for league in leagues_needed:
        logger.info(f"[{league}] {len(fixtures_by_league.get(league, []))} match(s) terminé(s) reçu(s) d'API-Football")

    league_errors = [league for league in leagues_needed if league not in API_FOOTBALL_LEAGUE_IDS]
    for league in league_errors:
        logger.warning(f"[{league}] pas d'id API-Football connu (voir api_football_config.py) — ignoré.")

    matched, unmatched = 0, []
    with Session(engine) as session:
        for log in pending:
            fixtures = fixtures_by_league.get(log.league, [])
            fixture = next(
                (f for f in fixtures
                 if _names_match(f.get("teams", {}).get("home", {}).get("name", ""), log.home_team)
                 and _names_match(f.get("teams", {}).get("away", {}).get("name", ""), log.away_team)),
                None,
            )
            if fixture is None:
                unmatched.append(f"[{log.league}] {log.home_team} vs {log.away_team}")
                continue

            home_goals = fixture.get("goals", {}).get("home")
            away_goals = fixture.get("goals", {}).get("away")
            if home_goals is None or away_goals is None:
                unmatched.append(f"[{log.league}] {log.home_team} vs {log.away_team} (score absent de la fixture)")
                continue

            correct_1x2, correct_btts, correct_over_2_5 = _compute_correctness(
                log.pick_1x2, log.pick_btts, log.pick_over_2_5, home_goals, away_goals,
            )

            db_log = session.get(PredictionLog, log.id)
            db_log.result_home_goals = home_goals
            db_log.result_away_goals = away_goals
            db_log.result_fetched_at = datetime.now(timezone.utc)
            db_log.correct_1x2 = correct_1x2
            db_log.correct_btts = correct_btts
            db_log.correct_over_2_5 = correct_over_2_5
            session.add(db_log)
            session.commit()
            matched += 1

    logger.info("=" * 80)
    logger.info("RÉCAPITULATIF")
    logger.info(f"  Rapprochés  : {matched}/{len(pending)}")
    if unmatched:
        logger.warning(f"  Non rapprochés ({len(unmatched)}) :")
        for m in unmatched:
            logger.warning(f"    {m}")
    if league_errors:
        logger.warning(f"  Ligues en erreur : {league_errors}")
    logger.info("=" * 80)

    if unmatched or league_errors:
        logger.warning("JOB TERMINÉ EN SUCCÈS PARTIEL.")
        _send_alert(f"[Xfoot] fetch_daily_results {target_date.isoformat()} — succès partiel : "
                    f"{matched}/{len(pending)} rapprochés, ligues en erreur : {league_errors}, "
                    f"non rapprochés : {unmatched}")
        return 2

    logger.info("JOB TERMINÉ AVEC SUCCÈS COMPLET.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="Date cible YYYY-MM-DD (défaut : hier, UTC)")
    parser.add_argument("--log-dir", default="logs", help="Dossier des logs (défaut : logs/)")
    args = parser.parse_args()

    log_path = _setup_logging(Path(args.log_dir))
    logger.info(f"Log écrit dans : {log_path}")

    target_date = date.fromisoformat(args.date) if args.date else None
    sys.exit(run(target_date=target_date))


if __name__ == "__main__":
    main()
