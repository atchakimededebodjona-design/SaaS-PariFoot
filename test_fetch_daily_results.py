"""
test_fetch_daily_results.py — Vérifie le rapprochement PredictionLog <->
résultats API-Football (fetch_daily_results.py), SANS jamais appeler le
vrai réseau (httpx.get mocké).

Base isolée (jamais api/app.db) : DATABASE_URL pointe vers un fichier
SQLite temporaire dédié à ce test, positionné avant tout import de
app.core.database (voir fetch_daily_results.run(), qui importe en local).

Usage : python test_fetch_daily_results.py
"""

import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

DB_PATH = Path(__file__).parent / "test_fetch_daily_results.db"
if DB_PATH.exists():
    DB_PATH.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["API_FOOTBALL_KEY"] = "test_dummy_key_never_sent_over_real_network"

sys.path.insert(0, str(Path(__file__).parent / "api"))

import fetch_daily_results
from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.prediction_log import PredictionLog
import app.core.api_football_config as api_football_config

TARGET_DATE = date(2026, 8, 16)

init_db()


class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _make_fixture(home: str, away: str, home_goals: int, away_goals: int, league_id: int = 0) -> dict:
    return {
        "league": {"id": league_id},
        "teams": {"home": {"name": home}, "away": {"name": away}},
        "goals": {"home": home_goals, "away": away_goals},
    }


def _paged_response(fixtures: list[dict]) -> FakeResponse:
    """Une seule page — suffisant pour ces tests (voir pagination gérée par
    _fetch_fixtures_by_league via paging.current/paging.total)."""
    return FakeResponse({"response": fixtures, "paging": {"current": 1, "total": 1}})


def _insert_log(league: str, home_team: str, away_team: str,
                 pick_1x2: str = "home", pick_btts: str = "yes", pick_over_2_5: str = "over") -> int:
    with Session(engine) as session:
        log = PredictionLog(
            league=league, match_date=TARGET_DATE, home_team=home_team, away_team=away_team,
            payload="{}", pick_1x2=pick_1x2, pick_btts=pick_btts, pick_over_2_5=pick_over_2_5,
            created_at=datetime.now(timezone.utc),
        )
        session.add(log)
        session.commit()
        session.refresh(log)
        return log.id


def _get_log(log_id: int) -> PredictionLog:
    with Session(engine) as session:
        return session.exec(select(PredictionLog).where(PredictionLog.id == log_id)).one()


def _cleanup_all_logs():
    with Session(engine) as session:
        for log in session.exec(select(PredictionLog)).all():
            session.delete(log)
        session.commit()


def test_no_pending_predictions_returns_0():
    _cleanup_all_logs()
    exit_code = fetch_daily_results.run(target_date=TARGET_DATE)
    assert exit_code == 0
    print("  [OK] aucune prédiction en attente -> code 0")


def test_missing_api_key_returns_1():
    _cleanup_all_logs()
    log_id = _insert_log("Ligue1", "Paris SG", "Marseille")
    old_key = api_football_config.API_FOOTBALL_KEY
    api_football_config.API_FOOTBALL_KEY = ""
    try:
        exit_code = fetch_daily_results.run(target_date=TARGET_DATE)
        assert exit_code == 1
        assert _get_log(log_id).result_fetched_at is None
        print("  [OK] API_FOOTBALL_KEY absente -> code 1, rien écrit")
    finally:
        api_football_config.API_FOOTBALL_KEY = old_key


def test_exact_and_aliased_names_matched_with_correct_picks():
    _cleanup_all_logs()
    # Nom canonique interne "Man United" (football-data.co.uk) — API-Football
    # renverrait "Manchester United" (alias connu, cf. API_FOOTBALL_TEAM_ALIASES).
    log_alias_id = _insert_log("PremierLeague", "Man United", "Arsenal",
                                pick_1x2="home", pick_btts="yes", pick_over_2_5="over")
    # Nom canonique exact, aucun alias nécessaire.
    log_exact_id = _insert_log("Ligue1", "Paris SG", "Marseille",
                                pick_1x2="away", pick_btts="no", pick_over_2_5="under")

    all_fixtures = [
        _make_fixture("Manchester United", "Arsenal", 3, 1,
                       league_id=api_football_config.API_FOOTBALL_LEAGUE_IDS["PremierLeague"]),
        _make_fixture("Paris SG", "Marseille", 0, 0,
                       league_id=api_football_config.API_FOOTBALL_LEAGUE_IDS["Ligue1"]),
        # Fixture d'une ligue non suivie, mélangée dans la même réponse
        # (comportement réel : la requête n'est PLUS filtrée par league côté
        # serveur, voir docstring de fetch_daily_results.py) — ne doit
        # jamais interférer avec le rapprochement des deux au-dessus.
        _make_fixture("Deportivo La Coruna", "Elche", 1, 1, league_id=999999),
    ]

    def fake_get(url, params=None, headers=None, timeout=None):
        return _paged_response(all_fixtures)

    with patch("httpx.get", side_effect=fake_get):
        exit_code = fetch_daily_results.run(target_date=TARGET_DATE)
    assert exit_code == 0, "tout aurait dû être rapproché avec succès"

    log_alias = _get_log(log_alias_id)
    assert (log_alias.result_home_goals, log_alias.result_away_goals) == (3, 1)
    assert log_alias.correct_1x2 is True    # pick "home", résultat 3-1 -> home
    assert log_alias.correct_btts is True   # pick "yes", 3 et 1 marqués -> yes
    assert log_alias.correct_over_2_5 is True  # pick "over", 4 buts -> over
    print(f"  [OK] 'Manchester United' (API-Football) rapproché à 'Man United' (canonique) -> {log_alias.result_home_goals}-{log_alias.result_away_goals}, picks corrects")

    log_exact = _get_log(log_exact_id)
    assert (log_exact.result_home_goals, log_exact.result_away_goals) == (0, 0)
    assert log_exact.correct_1x2 is False    # pick "away", résultat 0-0 -> draw
    assert log_exact.correct_btts is True    # pick "no", 0-0 -> effectivement no
    assert log_exact.correct_over_2_5 is True  # pick "under", 0 but -> under
    print(f"  [OK] correspondance exacte 'Paris SG'/'Marseille' -> {log_exact.result_home_goals}-{log_exact.result_away_goals}")


def test_unmatched_fixture_leaves_result_null_and_returns_partial_success():
    _cleanup_all_logs()
    log_id = _insert_log("SerieA", "Juventus", "Inter")

    def fake_get(url, params=None, headers=None, timeout=None):
        # Aucune fixture ne correspond à Juventus-Inter ce jour-là.
        return _paged_response([_make_fixture("Napoli", "Roma", 1, 1,
                                                league_id=api_football_config.API_FOOTBALL_LEAGUE_IDS["SerieA"])])

    with patch("httpx.get", side_effect=fake_get):
        exit_code = fetch_daily_results.run(target_date=TARGET_DATE)

    assert exit_code == 2, "match non rapproché -> succès partiel attendu"
    assert _get_log(log_id).result_fetched_at is None
    print("  [OK] fixture non rapprochée -> code 2, PredictionLog non modifié")


def test_already_resolved_prediction_is_not_refetched():
    _cleanup_all_logs()
    log_id = _insert_log("Bundesliga", "Bayern Munich", "Dortmund")
    with Session(engine) as session:
        log = session.exec(select(PredictionLog).where(PredictionLog.id == log_id)).one()
        log.result_home_goals, log.result_away_goals = 2, 2
        log.result_fetched_at = datetime.now(timezone.utc)
        session.add(log)
        session.commit()

    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(params)
        return FakeResponse({"response": [_make_fixture("Bayern Munich", "Dortmund", 5, 0)]})

    with patch("httpx.get", side_effect=fake_get):
        exit_code = fetch_daily_results.run(target_date=TARGET_DATE)

    assert exit_code == 0
    assert not calls, "aucun appel API-Football attendu : plus rien en attente pour cette date"
    assert (_get_log(log_id).result_home_goals, _get_log(log_id).result_away_goals) == (2, 2), \
        "un résultat déjà connu ne doit jamais être réécrasé"
    print("  [OK] prédiction déjà résolue -> aucun appel API, résultat inchangé")


if __name__ == "__main__":
    failures = 0
    tests = [
        test_no_pending_predictions_returns_0,
        test_missing_api_key_returns_1,
        test_exact_and_aliased_names_matched_with_correct_picks,
        test_unmatched_fixture_leaves_result_null_and_returns_partial_success,
        test_already_resolved_prediction_is_not_refetched,
    ]
    for t in tests:
        print(f"\n=== {t.__name__} ===")
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"  [ECHEC] {e}")

    if DB_PATH.exists():
        try:
            DB_PATH.unlink()
        except PermissionError:
            pass

    print(f"\n{'='*60}\n{len(tests)-failures}/{len(tests)} tests reussis\n{'='*60}")
    sys.exit(1 if failures else 0)
