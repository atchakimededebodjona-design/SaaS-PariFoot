"""
scripts/odds_api_smoke_test.py — Phase 8G, Prompt 17 : XFOOT THE ODDS API
SMOKE TEST V1 (1 SEUL MATCH, RÉEL).
=============================================================================
TECHNICAL VALIDATION ONLY. Distinct de scripts/odds_api_trial.py (le trial
100-500 matchs, §4 du prompt Phase 8G) : ce script ne consomme le budget
crédits que du strict nécessaire pour valider EMPIRIQUEMENT, sur UN SEUL
match réel de la base Xfoot, les questions laissées TRIAL_BLOCKED_NO_
CREDENTIAL par le run précédent (voir reports/odds_providers/
odds_api_trial_*.md, §3/§5/§9-§14/§29/§60).

Aucune écriture dans match / match_stats / model_predictions / model_versions
/ team_ratings / prediction_log. Aucun compte créé, aucune clé achetée ou
modifiée. La clé est lue UNIQUEMENT via la variable d'environnement
THE_ODDS_API_KEY (app.ai.odds_research.odds_api_trial.get_api_key) — jamais
journalisée, jamais écrite où que ce soit, jamais incluse dans un rapport.

=== Budget réseau (§ "ne pas dépasser le strict nécessaire") ===

EXACTEMENT DEUX appels HTTP réels, jamais plus :
  1. GET /v4/sports (GRATUIT, hors quota, confirmé par la doc officielle) —
     valide uniquement que la clé authentifie.
  2. GET /v4/historical/sports/{sport}/odds — UN seul snapshot, UN seul
     marché (h2h = 1X2), UNE seule région (eu), pour LE match sélectionné.
Si (1) échoue (clé invalide/quota épuisé), (2) n'est PAS appelé — protège le
budget crédits d'un appel payant inutile.

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/odds_api_smoke_test.py
"""

import json
import logging
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlmodel import Session  # noqa: E402

from app.core.database import engine, init_db  # noqa: E402
from app.ai.odds_research.odds_api_trial import (  # noqa: E402
    get_api_key, select_trial_matches, enrich_kickoff_times, cutoff_for,
    CUTOFF_HORIZONS_HOURS, SPORT_KEYS, THE_ODDS_API_ENV_VAR,
    fetch_sports, fetch_historical_odds_snapshot, find_match_in_snapshot,
    parse_historical_response,
)
from app.ai.odds_research.integrity import classify_explicit_timestamp  # noqa: E402

import feature_engineering_walkforward as fewf  # noqa: E402
from odds_api_trial import load_match_rows, load_kickoff_time_index  # noqa: E402  (réutilisées telles quelles, jamais réimplémentées)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
# CRITIQUE : httpx (et httpcore, sa dépendance) journalise par défaut chaque
# requête à INFO, URL COMPLÈTE INCLUSE — donc apiKey=... EN CLAIR dans les
# logs si leur niveau suit logging.basicConfig ci-dessus. La clé est censée
# n'être JAMAIS journalisée (voir docstring module) : ces deux loggers sont
# donc explicitement remontés à WARNING, indépendamment du niveau racine.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("odds_api_smoke_test")

CODE_VERSION = "phase8g-smoke-v1"
# Profondeur historique documentée Phase 8F/8G pour les 5 ligues prioritaires
# (depuis juin-juillet 2020) — filtre la sélection pour maximiser la chance
# réelle que le match choisi soit dans la couverture annoncée du fournisseur,
# jamais une garantie (§ le smoke test lui-même vérifie ce point).
HISTORICAL_COVERAGE_FLOOR = date(2020, 7, 1)
SMOKE_TEST_CUTOFF_HOURS = CUTOFF_HORIZONS_HOURS[0]  # T-24h — le plus conservateur (couvre l'incertitude de kickoff DATE_ONLY)
FALLBACK_KICKOFF_HOUR_UTC = 15  # convention documentée si aucun Kickoff réel trouvé (cache Phase 8D) — jamais fabriqué silencieusement


def _header(headers: dict, name: str) -> str | None:
    """Recherche insensible à la casse — les en-têtes HTTP ne sont pas
    garantis dans une casse fixe selon le client/serveur."""
    name_lower = name.lower()
    for k, v in headers.items():
        if k.lower() == name_lower:
            return v
    return None


def select_single_real_match():
    """Réutilise select_trial_matches (§5 du prompt Phase 8G, déjà validée)
    avec max_per_league=1, filtré à la profondeur de couverture documentée
    — puis choisit DÉTERMINISTEMENT le match le plus RÉCENT parmi les 5
    candidats (un par ligue), jamais un tirage aléatoire."""
    match_rows = load_match_rows()
    match_rows = [r for r in match_rows if r["date"].date() >= HISTORICAL_COVERAGE_FLOOR]
    candidates = select_trial_matches(match_rows, max_per_league=1)
    if not candidates:
        return None
    candidates.sort(key=lambda m: (m.kickoff_date, m.league, m.home_team, m.away_team), reverse=True)
    return candidates[0]


def resolve_kickoff_and_cutoff(match) -> tuple[datetime, str, datetime]:
    """Retourne (kickoff_datetime_aware, kickoff_source, cutoff_datetime).
    kickoff_source documente explicitement si l'heure vient du cache
    football-data.co.uk (Phase 8D, réel) ou d'une CONVENTION assumée
    (15:00 UTC, jamais fabriquée silencieusement — voir docstring module)."""
    if match.kickoff_precision == "DATE_AND_TIME" and match.kickoff_datetime is not None:
        kickoff = match.kickoff_datetime
        source = "FOOTBALL_DATA_CO_UK_CACHE_PHASE_8D"
    else:
        kickoff = datetime.combine(match.kickoff_date, time(FALLBACK_KICKOFF_HOUR_UTC, 0), tzinfo=timezone.utc)
        source = f"ASSUMED_{FALLBACK_KICKOFF_HOUR_UTC:02d}_00_UTC_NO_REAL_KICKOFF_TIME_FOUND"
    cutoff = cutoff_for(kickoff, SMOKE_TEST_CUTOFF_HOURS)
    return kickoff, source, cutoff


def main() -> dict:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    generated_at = datetime.now(timezone.utc).isoformat()

    init_db()
    with Session(engine) as session:
        db_before = fewf.snapshot_db_counts(session)

    api_key = get_api_key()
    if api_key is None:
        result = {
            "run_id": run_id, "generated_at": generated_at, "phase": "8G", "kind": "smoke_test_v1",
            "credentials_status": "TRIAL_BLOCKED_NO_CREDENTIAL",
            "env_var_name": THE_ODDS_API_ENV_VAR,
            "conclusion": "Aucune clé THE_ODDS_API_KEY détectée dans l'environnement du processus — smoke test non exécuté.",
        }
        _finish(result, run_id)
        return result

    logger.info("Credential détecté (présence uniquement — valeur jamais journalisée).")

    match = select_single_real_match()
    if match is None:
        result = {
            "run_id": run_id, "generated_at": generated_at, "phase": "8G", "kind": "smoke_test_v1",
            "credentials_status": "CREDENTIAL_AVAILABLE",
            "conclusion": "Aucun match éligible trouvé en base (>= 2020-07-01, 5 ligues prioritaires) — smoke test non exécuté.",
        }
        _finish(result, run_id)
        return result

    logger.info("Match sélectionné (déterministe) : %s — %s vs %s (%s)", match.league, match.home_team, match.away_team, match.kickoff_date)

    cache_dir = Path.home() / ".xfoot_research_cache" / "odds_football_data_co_uk"
    try:
        kickoff_index = load_kickoff_time_index(cache_dir)
        enriched = enrich_kickoff_times([match], kickoff_index)
        match = enriched[0] if enriched else match
    except Exception as e:  # noqa: BLE001
        logger.warning("Enrichissement kickoff impossible (%s) — repli sur convention documentée.", e)

    kickoff, kickoff_source, cutoff = resolve_kickoff_and_cutoff(match)
    iso_date = cutoff.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    sport_key = SPORT_KEYS[match.league]

    logger.info("Kickoff=%s (source=%s) ; cutoff T-%dh=%s ; sport_key=%s", kickoff.isoformat(), kickoff_source, SMOKE_TEST_CUTOFF_HOURS, iso_date, sport_key)

    # --- Appel réseau 1/2 : GRATUIT, valide l'authentification -------------
    logger.info("Appel 1/2 (GRATUIT, hors quota) : GET /v4/sports ...")
    sports_json, sports_headers, sports_status = fetch_sports(api_key)
    auth_ok = sports_status == 200 and isinstance(sports_json, list)
    sport_listed = auth_ok and any(isinstance(s, dict) and s.get("key") == sport_key for s in sports_json)
    logger.info("  -> status=%s auth_ok=%s sport_listed=%s", sports_status, auth_ok, sport_listed)

    if not auth_ok:
        result = {
            "run_id": run_id, "generated_at": generated_at, "phase": "8G", "kind": "smoke_test_v1",
            "credentials_status": "CREDENTIAL_AVAILABLE_BUT_AUTH_FAILED",
            "sports_call_status_code": sports_status,
            "conclusion": (
                f"L'appel GRATUIT /v4/sports a échoué (status={sports_status}) — clé invalide, expirée ou quota "
                "déjà épuisé. L'appel PAYANT (historical odds) n'a PAS été effectué (protection du budget crédits)."
            ),
            "db_safety": {"before": db_before, "after": db_before, "unchanged": True},
        }
        _finish(result, run_id)
        return result

    # --- Appel réseau 2/2 : PAYANT, UN snapshot pour LE match sélectionné --
    logger.info("Appel 2/2 (PAYANT — 1 marché h2h, 1 région eu, 1 date) : GET /v4/historical/sports/%s/odds ...", sport_key)
    hist_json, hist_headers, hist_status = fetch_historical_odds_snapshot(
        api_key, sport_key, iso_date, regions="eu", markets="h2h",
    )
    logger.info("  -> status=%s", hist_status)

    matched_event = find_match_in_snapshot(hist_json, match.home_team, match.away_team)
    event_found = matched_event is not None

    parsed_rows = []
    if event_found:
        scoped_raw = {"timestamp": hist_json.get("timestamp") if hist_json else None, "data": [matched_event]}
        parsed_rows = parse_historical_response(scoped_raw, match, cutoff_hours=SMOKE_TEST_CUTOFF_HOURS)

    snapshot_timestamp_raw = hist_json.get("timestamp") if hist_json else None
    snapshot_timestamp = None
    if snapshot_timestamp_raw:
        try:
            snapshot_timestamp = datetime.fromisoformat(snapshot_timestamp_raw.replace("Z", "+00:00"))
        except ValueError:
            snapshot_timestamp = None

    first_bookmaker_last_update = None
    first_bookmaker_key = None
    leakage_classification = None
    if parsed_rows:
        first_bookmaker_key = parsed_rows[0]["bookmaker"]
        lu = parsed_rows[0]["last_update"]
        first_bookmaker_last_update = lu.isoformat() if lu else None
        leakage_classification = classify_explicit_timestamp(lu, cutoff, kickoff) if lu else "REJECTED"

    with Session(engine) as session:
        db_after = fewf.snapshot_db_counts(session)
    db_unchanged = db_before == db_after

    result = {
        "run_id": run_id, "generated_at": generated_at, "phase": "8G", "kind": "smoke_test_v1",
        "credentials_status": "CREDENTIAL_AVAILABLE",
        "env_var_name": THE_ODDS_API_ENV_VAR,
        "code_version": CODE_VERSION,
        "network_calls_made": 2,
        "target_match": {
            "league": match.league, "home_team": match.home_team, "away_team": match.away_team,
            "kickoff_date": str(match.kickoff_date), "kickoff_datetime": kickoff.isoformat(),
            "kickoff_source": kickoff_source, "sport_key": sport_key,
        },
        "cutoff": {"horizon_hours": SMOKE_TEST_CUTOFF_HOURS, "cutoff_datetime": cutoff.isoformat(), "requested_date_param": iso_date},
        "sports_call": {
            "status_code": sports_status, "auth_ok": auth_ok, "sport_key_listed": sport_listed,
            "x_requests_remaining": _header(sports_headers, "x-requests-remaining"),
            "x_requests_used": _header(sports_headers, "x-requests-used"),
        },
        "historical_call": {
            "status_code": hist_status,
            "snapshot_timestamp_present": snapshot_timestamp_raw is not None,
            "snapshot_timestamp": snapshot_timestamp.isoformat() if snapshot_timestamp else None,
            "previous_timestamp": hist_json.get("previous_timestamp") if hist_json else None,
            "next_timestamp": hist_json.get("next_timestamp") if hist_json else None,
            "events_returned_total": len(hist_json.get("data", [])) if isinstance(hist_json, dict) and isinstance(hist_json.get("data"), list) else 0,
            "our_event_found": event_found,
            "our_event_bookmakers_count": len(matched_event.get("bookmakers", [])) if event_found else 0,
            "x_requests_remaining": _header(hist_headers, "x-requests-remaining"),
            "x_requests_used": _header(hist_headers, "x-requests-used"),
            "x_requests_last": _header(hist_headers, "x-requests-last"),
        },
        "first_bookmaker_observation": {
            "bookmaker": first_bookmaker_key,
            "last_update": first_bookmaker_last_update,
            "last_update_origin": "UNKNOWN (bookmaker vs ingestion — non confirmé, voir Phase 8F/8G §3/§14)",
            "leakage_classification_vs_cutoff": leakage_classification,
        } if parsed_rows else None,
        "db_safety": {"before": db_before, "after": db_after, "unchanged": db_unchanged},
        "conclusion": _build_conclusion(hist_status, event_found, bool(parsed_rows), db_unchanged),
    }
    _finish(result, run_id)
    return result


def _build_conclusion(hist_status: int | None, event_found: bool, has_bookmaker_data: bool, db_unchanged: bool) -> str:
    if not db_unchanged:
        return "ANOMALIE : les compteurs DB ont changé — à investiguer immédiatement (ce script ne doit jamais écrire en base)."
    if hist_status != 200:
        return f"Appel historique échoué (status={hist_status}) — snapshot non exploitable pour ce match/cutoff."
    if not event_found:
        return "Authentification RÉUSSIE, snapshot historique RÉCUPÉRÉ, mais NOTRE match précis n'a PAS été trouvé dans ce snapshot (couverture réelle du fournisseur pour ce match/cette date/cette ligue = NO, empiriquement, sur cet échantillon de 1)."
    if not has_bookmaker_data:
        return "Match trouvé dans le snapshot mais aucune donnée bookmaker exploitable n'a pu être extraite (marché h2h absent pour ce match à ce cutoff)."
    return "Authentification RÉUSSIE, snapshot historique RÉCUPÉRÉ, NOTRE match TROUVÉ avec au moins une observation bookmaker exploitable — reconstruction empirique validée sur cet échantillon de 1 match."


def _finish(result: dict, run_id: str) -> None:
    outdir = Path(__file__).resolve().parent.parent / "reports" / "odds_providers"
    outdir.mkdir(parents=True, exist_ok=True)
    json_path = outdir / f"odds_api_smoke_test_{run_id}.json"
    json_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    logger.info("Rapport écrit : %s", json_path)
    print("\n" + "=" * 80)
    print("PHASE 8G — SMOKE TEST V1 (1 MATCH) TERMINÉ.")
    print("AUCUNE INTÉGRATION PRODUCTION EFFECTUÉE. AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. AUCUNE ÉCRITURE DB.")
    print("=" * 80)


if __name__ == "__main__":
    main()
    sys.exit(0)
