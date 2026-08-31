"""
odds_api_trial.py — Phase 8G : XFOOT THE ODDS API PROOF-OF-DATA TRIAL V1.

Fonctions du trial technique. AUCUNE fonction ici n'est appelée depuis la
production (§49 : isolation stricte). Réutilise VOLONTAIREMENT les fonctions
déjà validées de Phase 8E (api/app/ai/odds_research/integrity.py ::
classify_explicit_timestamp, safe_consensus) plutôt que de les
réimplémenter (§46).

=== Credential (§1) ===

La clé The Odds API est lue UNIQUEMENT via la variable d'environnement
`THE_ODDS_API_KEY` (mécanisme sécurisé standard du dépôt, même pattern que
API_FOOTBALL_KEY — voir api/app/core/api_football_config.py). Jamais
hardcodée, jamais écrite dans un fichier, jamais journalisée. Si absente :
`get_api_key()` retourne None — l'appelant doit alors produire
TRIAL_BLOCKED_NO_CREDENTIAL et continuer avec le reste du trial qui ne
nécessite pas d'accès réel (voir scripts/odds_api_trial.py).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from app.ai.odds_research.integrity import classify_explicit_timestamp, safe_consensus  # noqa: F401 — réutilisées telles quelles (§46)
from app.ai.odds_research.core import match_key, normalize_team_name  # noqa: F401

THE_ODDS_API_ENV_VAR = "THE_ODDS_API_KEY"
THE_ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4"

# Clés de sport officielles confirmées Phase 8F/8G pour les 5 ligues prioritaires (§4).
SPORT_KEYS = {
    "PremierLeague": "soccer_epl",
    "Ligue1": "soccer_france_ligue_one",
    "Bundesliga": "soccer_germany_bundesliga",
    "SerieA": "soccer_italy_serie_a",
    "LaLiga": "soccer_spain_la_liga",
}


def get_api_key() -> Optional[str]:
    """Lit UNIQUEMENT depuis l'environnement — jamais un fichier committé,
    jamais une valeur en dur. Retourne None si absente (jamais une chaîne
    vide traitée comme une clé valide)."""
    key = os.environ.get(THE_ODDS_API_ENV_VAR)
    return key if key else None


# ---------------------------------------------------------------------------
# §5 : sélection déterministe des matchs du trial (lecture DB seule)
# ---------------------------------------------------------------------------

@dataclass
class TrialMatch:
    match_id: int
    league: str
    home_team: str
    away_team: str
    kickoff_date: date
    kickoff_datetime: Optional[datetime]  # enrichi via le cache football-data.co.uk (Time), None si non trouvé
    kickoff_precision: str  # "DATE_AND_TIME" | "DATE_ONLY"
    sport_key: Optional[str]


def select_trial_matches(match_rows: list[dict], max_per_league: int = 20) -> list[TrialMatch]:
    """
    §5 : sélection déterministe (les matchs les plus RÉCENTS par ligue,
    triés par date décroissante puis équipes — jamais un tirage aléatoire),
    couvrant les 5 ligues prioritaires (§4), plusieurs dates/saisons par
    construction (les N matchs les plus récents s'étalent naturellement sur
    plusieurs journées).

    `match_rows` : liste de dicts {"match_id","league","home_team",
    "away_team","date"} (déjà lus en DB par l'appelant, LECTURE SEULE —
    cette fonction ne touche jamais la DB elle-même, §fonction pure).
    """
    by_league: dict[str, list[dict]] = {}
    for row in match_rows:
        by_league.setdefault(row["league"], []).append(row)

    selected: list[TrialMatch] = []
    for league, rows in by_league.items():
        rows_sorted = sorted(rows, key=lambda r: (r["date"], r["home_team"], r["away_team"]), reverse=True)
        for row in rows_sorted[:max_per_league]:
            selected.append(TrialMatch(
                match_id=row["match_id"], league=league, home_team=row["home_team"], away_team=row["away_team"],
                kickoff_date=row["date"].date() if hasattr(row["date"], "date") else row["date"],
                kickoff_datetime=None, kickoff_precision="DATE_ONLY",
                sport_key=SPORT_KEYS.get(league),
            ))
    return selected


def enrich_kickoff_times(matches: list[TrialMatch], fd_rows_by_key: dict[tuple, str]) -> list[TrialMatch]:
    """
    Enrichit `kickoff_datetime` en réutilisant le cache football-data.co.uk
    déjà téléchargé en Phase 8D (colonne `Time`, kickoff RÉEL — jamais un
    timestamp d'odds, voir docstring de core.combine_date_time). N'invente
    RIEN : un match non trouvé dans le cache reste kickoff_precision=
    "DATE_ONLY" (minuit conventionnel, jamais une heure fabriquée).

    `fd_rows_by_key` : dict match_key(league, date, home, away) -> "HH:MM"
    (ou None), construit par l'appelant à partir du cache déjà présent.
    """
    from app.ai.odds_research.integrity import combine_date_time

    out = []
    for m in matches:
        key = match_key(m.league, m.kickoff_date, m.home_team, m.away_team)
        time_str = fd_rows_by_key.get(key)
        if time_str:
            dt = combine_date_time(m.kickoff_date.strftime("%d/%m/%Y"), time_str)
            if dt is not None and dt.time() != datetime.min.time():
                out.append(TrialMatch(**{**vars(m), "kickoff_datetime": dt.replace(tzinfo=timezone.utc), "kickoff_precision": "DATE_AND_TIME"}))
                continue
        out.append(m)
    return out


# ---------------------------------------------------------------------------
# §9-§12 : validation de timestamp, reconstruction de snapshot
# ---------------------------------------------------------------------------

def hours_before_kickoff(timestamp: datetime, kickoff: datetime) -> float:
    """§9 : delta_to_kickoff en heures — jamais un timestamp fabriqué,
    exige deux datetimes timezone-aware (une comparaison naïf/aware lève
    TypeError nativement, jamais capturée pour produire une fausse réponse,
    même discipline que Phase 8E)."""
    return (kickoff - timestamp).total_seconds() / 3600.0


def reconstruct_snapshot(snapshots: list[tuple[datetime, dict]], cutoff: datetime) -> Optional[tuple[datetime, dict]]:
    """
    §11/§12 — LE test critique de cette phase. `snapshots` : liste de
    (timestamp, payload) pour UN match, PAS nécessairement triée. Retourne
    le snapshot dont le timestamp est <= cutoff et le PLUS RÉCENT parmi
    ceux-là (le dernier snapshot connu à l'instant du cutoff) — jamais un
    snapshot postérieur, jamais le plus ancien.

    Exemple exact du prompt (§12) : kickoff=20:00, snapshots à 08:00/14:00/
    18:30/19:45, cutoff T-6h=14:00 -> doit retourner le snapshot de 14:00,
    JAMAIS 18:30 ni 19:45 (postérieurs au cutoff, donc FUTURE_INFORMATION
    par rapport à ce cutoff — voir classify_explicit_timestamp).
    """
    eligible = [(ts, payload) for ts, payload in snapshots if ts <= cutoff]
    if not eligible:
        return None
    return max(eligible, key=lambda pair: pair[0])


def cutoff_for(kickoff: datetime, hours_before: int) -> datetime:
    return kickoff - timedelta(hours=hours_before)


CUTOFF_HORIZONS_HOURS = (24, 12, 6, 3, 1)


# ---------------------------------------------------------------------------
# §8 : normalisation d'une réponse historique brute (fonction PURE, testable
# avec une réponse simulée conforme au schéma officiel confirmé §2/§ce fichier)
# ---------------------------------------------------------------------------

def parse_historical_response(raw: dict, match: TrialMatch, cutoff_hours: int) -> list[dict]:
    """
    Normalise une réponse `/v4/historical/sports/{sport}/odds` (ou
    `/events/{eventId}/odds`) vers la structure de recherche isolée
    (§8) : match + provider + bookmaker + market + selection + odds +
    timestamp. AUCUNE écriture DB — retourne une liste de dicts en mémoire.

    Le champ `timestamp` de la réponse (niveau snapshot) est distinct de
    `last_update` (niveau bookmaker) — les DEUX sont conservés séparément
    (§55 : ne jamais les confondre). `last_update` reste classé UNKNOWN
    quant à son origine (bookmaker vs ingestion) tant que non confirmé par
    le support (voir rapport Phase 8F/8G, §3).
    """
    snapshot_ts_raw = raw.get("timestamp")
    if not snapshot_ts_raw:
        return []
    snapshot_ts = datetime.fromisoformat(snapshot_ts_raw.replace("Z", "+00:00"))

    rows = []
    data = raw.get("data")
    events = data if isinstance(data, list) else ([data] if data else [])
    for event in events:
        for bookmaker in event.get("bookmakers", []):
            last_update_raw = bookmaker.get("last_update")
            last_update = datetime.fromisoformat(last_update_raw.replace("Z", "+00:00")) if last_update_raw else None
            for mkt in bookmaker.get("markets", []):
                for outcome in mkt.get("outcomes", []):
                    rows.append({
                        "match_id": match.match_id, "league": match.league, "provider": "the_odds_api",
                        "bookmaker": bookmaker.get("key"), "market": mkt.get("key"),
                        "selection": outcome.get("name"), "odds": outcome.get("price"),
                        "snapshot_timestamp": snapshot_ts, "last_update": last_update,
                        "requested_cutoff_hours": cutoff_hours,
                    })
    return rows


# ---------------------------------------------------------------------------
# SMOKE TEST RÉEL (post-credential) — les DEUX seuls appels réseau autorisés
# par scripts/odds_api_smoke_test.py : (1) /sports (GRATUIT, ne consomme
# aucun crédit — sert uniquement à valider l'authentification), (2) UN
# snapshot historique pour UN match réel, UN marché (h2h), UNE région (eu).
# Aucune autre fonction de ce module ne fait d'appel réseau — cette
# séparation explicite évite qu'un futur appelant déclenche un appel réel
# par erreur en import ant simplement le module (aucun appel au niveau
# module, uniquement à l'intérieur de ces deux fonctions, jamais exécuté
# sans appel explicite du script).
# ---------------------------------------------------------------------------

def fetch_sports(api_key: str, timeout: float = 20.0) -> tuple[Optional[list], dict, Optional[int]]:
    """GET /v4/sports — confirmé GRATUIT par la documentation officielle
    (ne compte pas dans le quota de crédits) : sert uniquement à valider que
    la clé authentifie réellement, sans consommer le budget du smoke test.
    Retourne (json_ou_None, headers_réponse, status_code) — la clé n'est
    JAMAIS incluse dans la valeur retournée ni journalisée par l'appelant."""
    import httpx

    try:
        resp = httpx.get(f"{THE_ODDS_API_BASE_URL}/sports", params={"apiKey": api_key}, timeout=timeout)
    except httpx.HTTPError as e:
        return None, {}, None
    try:
        body = resp.json() if resp.status_code == 200 else None
    except ValueError:
        body = None
    return body, dict(resp.headers), resp.status_code


def fetch_historical_odds_snapshot(
    api_key: str, sport_key: str, iso_date: str,
    regions: str = "eu", markets: str = "h2h", timeout: float = 20.0,
) -> tuple[Optional[dict], dict, Optional[int]]:
    """GET /v4/historical/sports/{sport}/odds — LE seul appel PAYANT du smoke
    test (§ prompt 17). Paramètres volontairement réduits au strict minimum
    (un seul marché h2h = 1X2, une seule région eu) pour ne consommer que le
    coût minimal d'UN snapshot pour UN match. Ne PAS élargir `regions`/
    `markets` sans nécessité démontrée — chaque marché/région supplémentaire
    multiplie le coût en crédits (voir §31 du rapport Phase 8G, docstring de
    scripts/odds_api_trial.py). Retourne (json_ou_None, headers_réponse,
    status_code) ; la clé n'est jamais incluse dans la valeur retournée."""
    import httpx

    params = {
        "apiKey": api_key, "date": iso_date, "regions": regions,
        "markets": markets, "oddsFormat": "decimal", "dateFormat": "iso",
    }
    try:
        resp = httpx.get(f"{THE_ODDS_API_BASE_URL}/historical/sports/{sport_key}/odds", params=params, timeout=timeout)
    except httpx.HTTPError:
        return None, {}, None
    try:
        body = resp.json() if resp.status_code == 200 else None
    except ValueError:
        body = None
    return body, dict(resp.headers), resp.status_code


def find_match_in_snapshot(raw: Optional[dict], home_team: str, away_team: str) -> Optional[dict]:
    """Cherche NOTRE match précis (par nom d'équipe normalisé, §26 — jamais
    fuzzy, voir core.py::normalize_team_name) parmi TOUS les événements
    renvoyés par un snapshot historique (l'API renvoie tous les matchs du
    sport à cet instant, jamais filtrés par match). None si absent du
    snapshot — un résultat honnête (couverture réelle inconnue tant que non
    testée), jamais une correspondance approximative forcée."""
    if not raw or not isinstance(raw.get("data"), list):
        return None
    target_home, target_away = normalize_team_name(home_team), normalize_team_name(away_team)
    for event in raw["data"]:
        if (normalize_team_name(event.get("home_team", "")) == target_home
                and normalize_team_name(event.get("away_team", "")) == target_away):
            return event
    return None


# ---------------------------------------------------------------------------
# §35 : manifeste de reproductibilité
# ---------------------------------------------------------------------------

def build_manifest(*, code_version: str, dataset_size: int, cutoffs: tuple, timezone_note: str, sample_selection: str) -> dict:
    return {
        "provider": "the_odds_api", "query_time": datetime.now(timezone.utc).isoformat(),
        "dataset_size": dataset_size, "cutoffs": list(cutoffs), "timezone": timezone_note,
        "code_version": code_version, "sample_selection": sample_selection,
    }
