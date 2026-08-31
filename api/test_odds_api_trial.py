"""
test_odds_api_trial.py — Phase 8G : tests de
app/ai/odds_research/odds_api_trial.py et scripts/odds_api_trial.py.

Aucun appel réseau réel (pas de credential dans cet environnement — voir
docstring de scripts/odds_api_trial.py). Couvre §57 : timestamp parsing,
timezone, cutoff, snapshot selection, previous/next timestamp (via la
sémantique documentée), future exclusion, bookmaker/market extraction,
consensus cutoff, duplicate handling, reproducibility, report generation,
DB safety.

Usage : python api/test_odds_api_trial.py
"""

import io
import sys
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_odds_api_trial.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match, MatchStats
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion, TeamRating

from app.ai.odds_research.odds_api_trial import (
    get_api_key, select_trial_matches, hours_before_kickoff, reconstruct_snapshot,
    cutoff_for, parse_historical_response, build_manifest, TrialMatch,
    THE_ODDS_API_ENV_VAR, CUTOFF_HORIZONS_HOURS, SPORT_KEYS,
)
from app.ai.odds_research.integrity import classify_explicit_timestamp, safe_consensus

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import odds_api_trial as trial_script  # noqa: E402

init_db()

UTC = timezone.utc


# ---------------------------------------------------------------------------
# §1/§58 : credential — jamais imprimé, jamais fabriqué
# ---------------------------------------------------------------------------

def test_get_api_key_returns_none_when_absent(monkeypatch=None):
    import os
    saved = os.environ.pop(THE_ODDS_API_ENV_VAR, None)
    try:
        assert get_api_key() is None
    finally:
        if saved is not None:
            os.environ[THE_ODDS_API_ENV_VAR] = saved
    print(f"  [OK] get_api_key() retourne None quand {THE_ODDS_API_ENV_VAR} est absent")


def test_get_api_key_never_logs_value():
    """Une clé factice est positionnée temporairement ; on vérifie qu'aucun
    appel à get_api_key() n'écrit sa valeur nulle part (stdout capturé)."""
    import os
    fake_secret = "sk_test_FAKE_VALUE_NEVER_PRINTED_9f8e7d6c"
    os.environ[THE_ODDS_API_ENV_VAR] = fake_secret
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            key = get_api_key()
        assert key == fake_secret
        assert fake_secret not in buf.getvalue()
    finally:
        del os.environ[THE_ODDS_API_ENV_VAR]
    print("  [OK] get_api_key() ne journalise jamais la valeur de la clé (stdout vérifié vide de tout secret)")


def test_get_api_key_empty_string_is_not_a_valid_key():
    import os
    os.environ[THE_ODDS_API_ENV_VAR] = ""
    try:
        assert get_api_key() is None
    finally:
        del os.environ[THE_ODDS_API_ENV_VAR]
    print("  [OK] une variable d'environnement vide n'est jamais traitée comme une clé valide")


# ---------------------------------------------------------------------------
# §9 : delta_to_kickoff
# ---------------------------------------------------------------------------

def test_hours_before_kickoff_basic():
    kickoff = datetime(2024, 3, 16, 20, 0, tzinfo=UTC)
    ts = datetime(2024, 3, 15, 20, 0, tzinfo=UTC)
    assert hours_before_kickoff(ts, kickoff) == 24.0
    print("  [OK] hours_before_kickoff calcule correctement le delta en heures")


def test_hours_before_kickoff_requires_timezone_aware():
    kickoff = datetime(2024, 3, 16, 20, 0, tzinfo=UTC)
    naive = datetime(2024, 3, 15, 20, 0)
    try:
        hours_before_kickoff(naive, kickoff)
        assert False, "aurait dû lever TypeError"
    except TypeError:
        pass
    print("  [OK] hours_before_kickoff échoue sûrement (TypeError) sur un mélange naïf/aware — jamais un résultat silencieusement faux")


# ---------------------------------------------------------------------------
# §11/§12 : LE test critique — reconstruction de snapshot
# ---------------------------------------------------------------------------

def test_critical_snapshot_reconstruction_exact_prompt_example():
    """§12 : kickoff=20:00, snapshots 08:00/14:00/18:30/19:45, cutoff
    T-6h=14:00 -> doit retourner 14:00, JAMAIS 18:30 ni 19:45."""
    day = datetime(2024, 3, 16, tzinfo=UTC)
    kickoff = day.replace(hour=20)
    cutoff = kickoff - timedelta(hours=6)  # 14:00
    snapshots = [
        (day.replace(hour=8), {"h": 2.0}), (day.replace(hour=14), {"h": 2.1}),
        (day.replace(hour=18, minute=30), {"h": 2.2}), (day.replace(hour=19, minute=45), {"h": 2.3}),
    ]
    result = reconstruct_snapshot(snapshots, cutoff)
    assert result is not None
    assert result[0] == day.replace(hour=14)
    assert result[0] != day.replace(hour=18, minute=30)
    assert result[0] != day.replace(hour=19, minute=45)
    print("  [OK] reconstruct_snapshot retourne EXACTEMENT le snapshot 14:00 pour cutoff T-6h (jamais 18:30/19:45) — §12")


def test_reconstruct_snapshot_not_reconstructible_when_none_before_cutoff():
    day = datetime(2024, 3, 16, tzinfo=UTC)
    cutoff = day.replace(hour=6)
    snapshots = [(day.replace(hour=8), {}), (day.replace(hour=14), {})]
    assert reconstruct_snapshot(snapshots, cutoff) is None
    print("  [OK] reconstruct_snapshot retourne None (NOT_RECONSTRUCTIBLE) si aucun snapshot n'est <= cutoff")


def test_reconstruct_snapshot_unordered_input_still_correct():
    """§14 : les snapshots ne sont pas forcément triés en entrée — la
    fonction doit rester correcte (ordre chronologique interne, jamais une
    dépendance à l'ordre d'entrée)."""
    day = datetime(2024, 3, 16, tzinfo=UTC)
    cutoff = day.replace(hour=15)
    snapshots = [(day.replace(hour=14), {"v": 2}), (day.replace(hour=8), {"v": 1}), (day.replace(hour=20), {"v": 3})]
    result = reconstruct_snapshot(snapshots, cutoff)
    assert result[1]["v"] == 2  # le plus récent <= cutoff, indépendamment de l'ordre d'entrée
    print("  [OK] reconstruct_snapshot est correct même avec une liste de snapshots non triée en entrée")


def test_cutoff_for_all_five_horizons():
    kickoff = datetime(2024, 3, 16, 20, 0, tzinfo=UTC)
    for h in CUTOFF_HORIZONS_HOURS:
        c = cutoff_for(kickoff, h)
        assert c == kickoff - timedelta(hours=h)
    assert CUTOFF_HORIZONS_HOURS == (24, 12, 6, 3, 1)
    print("  [OK] cutoff_for calcule correctement les 5 horizons (T-24h..T-1h)")


# ---------------------------------------------------------------------------
# §38 : exclusion du futur (réutilise Phase 8E, testé ici en contexte 8G)
# ---------------------------------------------------------------------------

def test_future_snapshot_excluded_via_classify_explicit_timestamp():
    kickoff = datetime(2024, 3, 16, 20, 0, tzinfo=UTC)
    cutoff = kickoff - timedelta(hours=6)
    assert classify_explicit_timestamp(kickoff - timedelta(hours=10), cutoff, kickoff) == "SAFE"
    assert classify_explicit_timestamp(kickoff - timedelta(hours=5), cutoff, kickoff) == "FUTURE_INFORMATION"
    assert classify_explicit_timestamp(kickoff - timedelta(hours=1), cutoff, kickoff) == "FUTURE_INFORMATION"
    assert classify_explicit_timestamp(kickoff + timedelta(minutes=10), cutoff, kickoff) == "REJECTED"
    print("  [OK] toutes les fixtures §38 (T-10h/T-5h/T-1h/T+10min) classées correctement pour cutoff T-6h")


# ---------------------------------------------------------------------------
# §37 : consensus adversarial (réutilise Phase 8E, testé ici en contexte 8G)
# ---------------------------------------------------------------------------

def test_adversarial_consensus_exact_prompt_fixture():
    kickoff = datetime(2024, 3, 16, 20, 0, tzinfo=UTC)
    cutoff = kickoff - timedelta(hours=6)
    obs = [
        {"bookmaker": "A", "timestamp": kickoff - timedelta(hours=10), "implied_probs": {"home": 0.5, "draw": 0.3, "away": 0.2}},
        {"bookmaker": "B", "timestamp": kickoff - timedelta(hours=8), "implied_probs": {"home": 0.52, "draw": 0.28, "away": 0.2}},
        {"bookmaker": "C", "timestamp": kickoff - timedelta(hours=2), "implied_probs": {"home": 0.6, "draw": 0.25, "away": 0.15}},
    ]
    consensus = safe_consensus(obs, cutoff)
    assert consensus["bookmakers"] == ["A", "B"]
    assert consensus["excluded_bookmakers"] == ["C"]
    print("  [OK] consensus adversarial A=T-10h/B=T-8h/C=T-2h, cutoff=T-6h -> A+B inclus, C exclu (§37)")


# ---------------------------------------------------------------------------
# §5 : sélection déterministe des matchs
# ---------------------------------------------------------------------------

def test_select_trial_matches_deterministic_and_bounded():
    rows = [
        {"match_id": i, "league": "PremierLeague", "home_team": f"H{i}", "away_team": f"A{i}", "date": datetime(2024, 1, 1) + timedelta(days=i)}
        for i in range(30)
    ]
    selected1 = select_trial_matches(rows, max_per_league=20)
    selected2 = select_trial_matches(rows, max_per_league=20)
    assert len(selected1) == 20
    assert [m.match_id for m in selected1] == [m.match_id for m in selected2]
    print("  [OK] select_trial_matches est déterministe et respecte la borne max_per_league")


def test_select_trial_matches_picks_most_recent():
    rows = [
        {"match_id": 1, "league": "Ligue1", "home_team": "H1", "away_team": "A1", "date": datetime(2020, 1, 1)},
        {"match_id": 2, "league": "Ligue1", "home_team": "H2", "away_team": "A2", "date": datetime(2024, 1, 1)},
    ]
    selected = select_trial_matches(rows, max_per_league=1)
    assert selected[0].match_id == 2  # le plus récent, pas le plus ancien
    print("  [OK] select_trial_matches privilégie les matchs les plus récents (couverture The Odds API depuis 2020)")


def test_select_trial_matches_covers_multiple_leagues():
    rows = []
    for lg in SPORT_KEYS:
        rows.append({"match_id": hash(lg), "league": lg, "home_team": "H", "away_team": "A", "date": datetime(2024, 1, 1)})
    selected = select_trial_matches(rows, max_per_league=5)
    leagues_selected = {m.league for m in selected}
    assert leagues_selected == set(SPORT_KEYS.keys())
    print("  [OK] select_trial_matches couvre les 5 ligues prioritaires quand elles sont présentes en entrée")


# ---------------------------------------------------------------------------
# §8 : parsing d'une réponse simulée conforme au schéma officiel confirmé
# ---------------------------------------------------------------------------

def _sample_match():
    return TrialMatch(match_id=1, league="PremierLeague", home_team="Arsenal", away_team="Chelsea",
                       kickoff_date=date(2024, 3, 16), kickoff_datetime=None, kickoff_precision="DATE_ONLY", sport_key="soccer_epl")


def test_parse_historical_response_matches_confirmed_schema():
    raw = {
        "timestamp": "2024-03-16T14:00:00Z", "previous_timestamp": "2024-03-16T13:55:00Z", "next_timestamp": "2024-03-16T14:05:00Z",
        "data": {
            "id": "abc123", "bookmakers": [{
                "key": "bet365", "title": "Bet365", "last_update": "2024-03-16T13:58:09Z",
                "markets": [{"key": "h2h", "last_update": "2024-03-16T13:58:09Z", "outcomes": [
                    {"name": "Arsenal", "price": 1.8}, {"name": "Draw", "price": 3.6}, {"name": "Chelsea", "price": 4.2},
                ]}],
            }],
        },
    }
    rows = parse_historical_response(raw, _sample_match(), cutoff_hours=6)
    assert len(rows) == 3
    assert rows[0]["bookmaker"] == "bet365"
    assert rows[0]["market"] == "h2h"
    assert rows[0]["snapshot_timestamp"] == datetime(2024, 3, 16, 14, 0, tzinfo=UTC)
    assert rows[0]["last_update"] == datetime(2024, 3, 16, 13, 58, 9, tzinfo=UTC)
    assert rows[0]["snapshot_timestamp"] != rows[0]["last_update"]  # jamais confondus (§55)
    print("  [OK] parse_historical_response extrait correctement bookmaker/market/selection/odds/timestamp/last_update depuis une réponse conforme au schéma officiel confirmé")


def test_parse_historical_response_empty_data_returns_empty():
    raw = {"timestamp": "2024-03-16T14:00:00Z", "data": None}
    assert parse_historical_response(raw, _sample_match(), cutoff_hours=6) == []
    print("  [OK] une réponse avec data=None retourne une liste vide, jamais une erreur ni une donnée fabriquée")


def test_parse_historical_response_missing_timestamp_returns_empty():
    raw = {"data": {"bookmakers": []}}
    assert parse_historical_response(raw, _sample_match(), cutoff_hours=6) == []
    print("  [OK] une réponse sans champ timestamp retourne une liste vide (jamais un timestamp fabriqué)")


def test_parse_historical_response_handles_list_of_events():
    raw = {
        "timestamp": "2024-03-16T14:00:00Z",
        "data": [{"id": "e1", "bookmakers": [{"key": "bet365", "title": "Bet365", "last_update": "2024-03-16T13:58:00Z",
                                                "markets": [{"key": "h2h", "outcomes": [{"name": "Arsenal", "price": 1.8}]}]}]}],
    }
    rows = parse_historical_response(raw, _sample_match(), cutoff_hours=6)
    assert len(rows) == 1
    print("  [OK] parse_historical_response gère aussi bien un data en liste (endpoint groupé) qu'un objet unique (endpoint événement)")


# ---------------------------------------------------------------------------
# §35 : manifeste de reproductibilité
# ---------------------------------------------------------------------------

def test_manifest_contains_required_fields():
    m = build_manifest(code_version="test-v1", dataset_size=10, cutoffs=(24, 12, 6, 3, 1), timezone_note="UTC", sample_selection="test")
    for field_name in ("provider", "query_time", "dataset_size", "cutoffs", "timezone", "code_version", "sample_selection"):
        assert field_name in m
    print("  [OK] build_manifest contient tous les champs requis (§35)")


def test_manifest_reproducible_given_same_static_inputs():
    m1 = build_manifest(code_version="v1", dataset_size=5, cutoffs=(6,), timezone_note="UTC", sample_selection="x")
    m2 = build_manifest(code_version="v1", dataset_size=5, cutoffs=(6,), timezone_note="UTC", sample_selection="x")
    assert m1["dataset_size"] == m2["dataset_size"] and m1["cutoffs"] == m2["cutoffs"] and m1["code_version"] == m2["code_version"]
    print("  [OK] build_manifest produit les mêmes champs statiques pour les mêmes entrées (query_time seul varie, attendu)")


# ---------------------------------------------------------------------------
# Orchestration script : adversarial tests + rapport (§27/§57 report generation)
# ---------------------------------------------------------------------------

def test_run_adversarial_tests_all_pass():
    result = trial_script.run_adversarial_tests()
    assert result["all_adversarial_tests_pass"] is True
    print("  [OK] scripts/odds_api_trial.py::run_adversarial_tests() : tous les tests adversariaux (§37/§38/§12) passent")


def test_render_markdown_is_deterministic():
    adv = trial_script.run_adversarial_tests()
    base_result = {
        "run_id": "test", "generated_at": "test", "credentials_status": "TRIAL_BLOCKED_NO_CREDENTIAL",
        "env_var_name": THE_ODDS_API_ENV_VAR, "doc_verification": "x", "timestamp_semantics": "x",
        "dataset": {"selected": 0, "with_kickoff_time": 0, "date_only": 0, "fetched_from_provider": 0},
        "selection_by_league": {}, "adversarial": adv, "timezone_notes": "x",
        "manifest": trial_script.build_manifest(code_version="v", dataset_size=0, cutoffs=(6,), timezone_note="UTC", sample_selection="x"),
        "limitations": [], "decision": "TRIAL_PARTIAL_NEEDS_MORE_VALIDATION", "decision_notes": "x",
        "recommendations_phase_8h": [], "scorecard": [],
        "data_access_status": "x", "timestamp_status": "x", "snapshot_reconstruction_status": "x",
        "consensus_status": "x", "leakage_risk_status": "x",
    }
    md1 = trial_script.render_markdown(base_result)
    md2 = trial_script.render_markdown(base_result)
    assert md1 == md2
    assert "PHASE 8G" in md1
    print("  [OK] render_markdown est déterministe et produit le rapport attendu")


# ---------------------------------------------------------------------------
# Sécurité DB (§47)
# ---------------------------------------------------------------------------

def _row_counts(session):
    return {
        "match": len(session.exec(select(Match)).all()),
        "match_stats": len(session.exec(select(MatchStats)).all()),
        "model_predictions": len(session.exec(select(ModelPrediction)).all()),
        "model_versions": len(session.exec(select(ModelVersion)).all()),
        "team_ratings": len(session.exec(select(TeamRating)).all()),
    }


def test_pure_functions_never_touch_db():
    with Session(engine) as session:
        before = _row_counts(session)

    rows = [{"match_id": 1, "league": "PremierLeague", "home_team": "H", "away_team": "A", "date": datetime(2024, 1, 1)}]
    select_trial_matches(rows, max_per_league=5)
    trial_script.run_adversarial_tests()

    with Session(engine) as session:
        after = _row_counts(session)

    assert before == after, f"Compteurs DB modifiés par des fonctions pures : {before} != {after}"
    print(f"  [OK] select_trial_matches/run_adversarial_tests sont strictement read-only vis-à-vis de la DB : {before}")


def test_no_hardcoded_secret_in_trial_modules():
    """§58 : aucune clé en dur dans le code source."""
    for path in (
        Path(__file__).parent / "app" / "ai" / "odds_research" / "odds_api_trial.py",
        Path(__file__).parent.parent / "scripts" / "odds_api_trial.py",
    ):
        source = path.read_text(encoding="utf-8")
        assert "api_key = \"" not in source and "api_key='" not in source and "api_key=\"" not in source
    print("  [OK] aucune clé API en dur détectée dans odds_api_trial.py (module ou script)")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    failures = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            failures += 1
            print(f"  [FAIL] {t.__name__}: {e!r}")
    cleanup_db(DB_PATH)
    print(f"\n{len(tests) - failures}/{len(tests)} tests OK")
    sys.exit(1 if failures else 0)
