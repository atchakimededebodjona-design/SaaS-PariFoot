"""
api_football_client.py — Phase 9, Partie A : récupération des fixtures À
VENIR (status "not started") depuis API-Football.

Avant ce ticket, RIEN dans ce dépôt ne récupérait de fixtures futures — seuls
existaient un appel "live=all" (matchs EN COURS, voir main.py::_get_live_scores)
et un appel "date+status=FT" (matchs TERMINÉS, voir fetch_daily_results.py).
Ce module suit EXACTEMENT le même schéma d'appel que
fetch_daily_results.py::_fetch_fixtures_by_league (une requête par date,
paginée si besoin, JAMAIS de filtre `league` côté serveur — le plan Free
d'API-Football le refuse pour la saison en cours, voir ce module) — seul le
`status` et la fenêtre de dates changent.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import httpx

from .api_football_config import API_FOOTBALL_BASE_URL, API_FOOTBALL_KEY, API_FOOTBALL_LEAGUE_IDS


def _fetch_fixtures_for_date(target_date: date, base_url: str, api_key: str, status: str) -> list[dict]:
    """Même pagination défensive que fetch_daily_results.py::_fetch_fixtures_by_league
    (jamais `page` sur la 1ère requête)."""
    all_fixtures: list[dict] = []
    page = 1
    while True:
        params = {"date": target_date.isoformat(), "status": status}
        if page > 1:
            params["page"] = page
        resp = httpx.get(f"{base_url}/fixtures", params=params, headers={"x-apisports-key": api_key}, timeout=20.0)
        resp.raise_for_status()
        data = resp.json()
        errors = data.get("errors")
        if errors:
            raise RuntimeError(f"réponse API-Football en erreur : {errors}")
        all_fixtures.extend(data.get("response", []))
        paging = data.get("paging") or {"current": 1, "total": 1}
        if paging.get("current", 1) >= paging.get("total", 1):
            break
        page += 1
    return all_fixtures


def fetch_upcoming_fixtures(
    window_hours: int,
    *,
    base_url: str = API_FOOTBALL_BASE_URL,
    api_key: str = API_FOOTBALL_KEY,
    league_ids: dict[str, int] = API_FOOTBALL_LEAGUE_IDS,
    now: datetime | None = None,
) -> list[dict]:
    """
    Renvoie les fixtures "not started" (status="NS") des 5 ligues suivies,
    dont le coup d'envoi tombe dans `[now, now + window_hours]` — jamais un
    match déjà commencé/terminé (filtré CÔTÉ CLIENT sur `fixture["date"]`,
    même prudence que fetch_daily_results.py : le statut renvoyé par
    l'API peut être légèrement périmé entre la requête et l'utilisation).

    Une requête par date calendaire couverte par la fenêtre (jamais de
    filtre `league` côté serveur, voir docstring module) — un match à
    23h50 avec une fenêtre de 48h peut nécessiter jusqu'à 3 dates
    calendaires distinctes.
    """
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(hours=window_hours)

    dates_needed: list[date] = []
    d = now.date()
    while d <= horizon.date():
        dates_needed.append(d)
        d += timedelta(days=1)

    league_ids_to_name = {v: k for k, v in league_ids.items()}
    raw_fixtures: list[dict] = []
    for target_date in dates_needed:
        raw_fixtures.extend(_fetch_fixtures_for_date(target_date, base_url, api_key, status="NS"))

    result: list[dict] = []
    for fixture in raw_fixtures:
        league_id = fixture.get("league", {}).get("id")
        if league_id not in league_ids_to_name:
            continue
        kickoff_raw = fixture.get("fixture", {}).get("date")
        if not kickoff_raw:
            continue
        try:
            kickoff = datetime.fromisoformat(kickoff_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if kickoff < now or kickoff > horizon:
            continue
        result.append(fixture)

    return result
