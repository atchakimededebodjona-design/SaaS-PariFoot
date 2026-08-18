"""
test_multi_model_prediction_logging.py — Phase 6 : logger commun
(app/ai/arena/prediction_logging.py), résolution multi-modèles
(fetch_daily_results.py étendu) et alimentation du benchmark (Phase 5)
depuis model_predictions.

Base isolée dédiée (jamais api/app.db) — même précaution que
test_fetch_daily_results.py/test_arena_benchmark.py.

Usage : python api/test_multi_model_prediction_logging.py
"""

import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))  # fetch_daily_results.py vit à la racine du dépôt

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_multi_model_prediction_logging.db")
# fetch_daily_results.py (importé plus bas) lit API_FOOTBALL_KEY au moment de
# l'import de app.core.api_football_config — doit être positionnée avant,
# même précaution que test_fetch_daily_results.py.
os.environ["API_FOOTBALL_KEY"] = "test_dummy_key_never_sent_over_real_network"

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion
from app.ai.arena.prediction_logging import (
    PredictionRecord,
    compute_correctness,
    get_or_create_active_model_version,
    log_prediction,
    resolve_prediction,
)

init_db()

MATCH_DATE = date(2026, 8, 16)


def _clean_all():
    with Session(engine) as session:
        for row in session.exec(select(ModelPrediction)).all():
            session.delete(row)
        for v in session.exec(select(ModelVersion)).all():
            session.delete(v)
        session.commit()


def _make_version(session, model_type: str, is_active: bool = False, name: str | None = None) -> int:
    v = ModelVersion(
        name=name or f"test-{model_type}-{datetime.now(timezone.utc).timestamp()}",
        model_type=model_type, trained_at=datetime.now(timezone.utc), is_active=is_active,
    )
    session.add(v)
    session.commit()
    session.refresh(v)
    return v.id


# ---------------------------------------------------------------------------
# 1. Logging — structure, contrat commun, les 4 types de modèle
# ---------------------------------------------------------------------------

def test_full_market_model_logs_all_probabilities():
    """Dixon-Coles (ou tout modèle full-market) : les 7 probabilités et les
    3 picks sont tous renseignés."""
    _clean_all()
    with Session(engine) as session:
        version_id = _make_version(session, "dixon_coles")
        record = PredictionRecord(
            league="Ligue1", match_date=MATCH_DATE, home_team="Paris SG", away_team="Marseille",
            model_type="dixon_coles",
            prob_home=0.6, prob_draw=0.25, prob_away=0.15,
            prob_btts_yes=0.55, prob_btts_no=0.45,
            prob_over_2_5=0.6, prob_under_2_5=0.4,
            source="live",
        )
        row = log_prediction(session, record, version_id)
        session.commit()

        assert row.model_type == "dixon_coles"
        assert row.model_version_id == version_id
        assert row.league == "Ligue1" and row.home_team == "Paris SG" and row.away_team == "Marseille"
        assert (row.prob_home, row.prob_draw, row.prob_away) == (0.6, 0.25, 0.15)
        assert (row.prob_btts_yes, row.prob_btts_no) == (0.55, 0.45)
        assert (row.prob_over_2_5, row.prob_under_2_5) == (0.6, 0.4)
        assert row.pick_1x2 == "home"
        assert row.pick_btts == "yes"
        assert row.pick_over_2_5 == "over"
        assert row.status == "pending"
    print("  [OK] modèle full-market (dixon_coles) -> toutes probabilités + picks présents, status=pending")


def test_1x2_only_model_logs_null_btts_and_over_under():
    """Elo/XGBoost/LightGBM : BTTS/O-U jamais fabriqués, restent None."""
    _clean_all()
    for model_type in ("elo", "xgboost", "lightgbm"):
        with Session(engine) as session:
            version_id = _make_version(session, model_type)
            record = PredictionRecord(
                league="Bundesliga", match_date=MATCH_DATE, home_team="Bayern Munich", away_team="Dortmund",
                model_type=model_type,
                prob_home=0.5, prob_draw=0.3, prob_away=0.2,
                source="backtest",
            )
            row = log_prediction(session, record, version_id)
            session.commit()

            assert row.model_type == model_type
            assert row.pick_1x2 == "home"
            assert row.prob_btts_yes is None and row.prob_btts_no is None
            assert row.prob_over_2_5 is None and row.prob_under_2_5 is None
            assert row.pick_btts is None, "un marché non modélisé ne doit jamais avoir de pick fabriqué"
            assert row.pick_over_2_5 is None
    print("  [OK] elo/xgboost/lightgbm -> prob_btts_*/prob_over_2_5/under_2_5 restent None, jamais fabriqués")


def test_match_identity_fields_are_exact():
    _clean_all()
    with Session(engine) as session:
        version_id = _make_version(session, "elo")
        record = PredictionRecord(
            league="SerieA", match_date=date(2026, 3, 1), home_team="Juventus", away_team="Milan",
            model_type="elo", prob_home=0.4, prob_draw=0.3, prob_away=0.3, source="backtest",
        )
        row = log_prediction(session, record, version_id)
        session.commit()
        assert row.league == "SerieA"
        assert row.match_date == date(2026, 3, 1)
        assert row.home_team == "Juventus" and row.away_team == "Milan"
        assert row.model_version_id == version_id
    print("  [OK] league/match_date/home_team/away_team/model_version_id exacts (identité du match)")


# ---------------------------------------------------------------------------
# 2. Résolution — pending -> resolved
# ---------------------------------------------------------------------------

def test_resolution_pending_to_resolved():
    _clean_all()
    with Session(engine) as session:
        version_id = _make_version(session, "dixon_coles")
        record = PredictionRecord(
            league="Ligue1", match_date=MATCH_DATE, home_team="Paris SG", away_team="Marseille",
            model_type="dixon_coles", prob_home=0.6, prob_draw=0.25, prob_away=0.15,
            prob_btts_yes=0.55, prob_btts_no=0.45, prob_over_2_5=0.6, prob_under_2_5=0.4, source="live",
        )
        row = log_prediction(session, record, version_id)
        session.commit()
        assert row.status == "pending"
        assert row.result_home_goals is None

        resolve_prediction(row, 2, 1)
        session.add(row)
        session.commit()

        assert row.status == "resolved"
        assert (row.result_home_goals, row.result_away_goals) == (2, 1)
        assert row.result_fetched_at is not None
        assert row.correct_1x2 is True   # pick "home", résultat 2-1 -> home
        assert row.correct_btts is True  # pick "yes", 2 et 1 marqués -> yes
        assert row.correct_over_2_5 is True  # pick "over", 3 buts -> over
    print("  [OK] pending -> resolved : result_*/correct_* correctement appliqués")


def test_resolution_none_pick_yields_none_correctness_never_false():
    _clean_all()
    with Session(engine) as session:
        version_id = _make_version(session, "elo")
        record = PredictionRecord(
            league="Bundesliga", match_date=MATCH_DATE, home_team="Bayern Munich", away_team="Dortmund",
            model_type="elo", prob_home=0.5, prob_draw=0.3, prob_away=0.2, source="backtest",
        )
        row = log_prediction(session, record, version_id)
        session.commit()

        resolve_prediction(row, 3, 0)
        session.add(row)
        session.commit()

        assert row.status == "resolved"
        assert row.correct_1x2 is True  # pick "home", 3-0 -> home
        assert row.correct_btts is None, "Elo ne prédit pas BTTS -> correct_btts doit rester None, jamais False"
        assert row.correct_over_2_5 is None
    print("  [OK] modèle sans BTTS/O-U -> correct_btts/correct_over_2_5=None après résolution (jamais False)")


def test_compute_correctness_full_picks_matches_historical_prediction_log_behavior():
    """Garde-fou de non-régression pour le refactor de fetch_daily_results.py :
    avec des picks toujours renseignés (cas prediction_log), le comportement
    est identique à l'ancienne _compute_correctness locale (booléens, jamais None)."""
    c1, cb, co = compute_correctness("home", "yes", "over", 2, 0)
    # 2-0 : 1x2=home (pick "home" -> correct), BTTS réel="no" (away_goals=0, pick "yes" -> faux),
    # total=2 buts (pick "over" -> faux, 2 n'est pas > 2.5)
    assert (c1, cb, co) == (True, False, False)
    c1, cb, co = compute_correctness("away", "no", "under", 0, 0)
    assert (c1, cb, co) == (False, True, True)  # pick "away" faux (0-0=draw), reste bool jamais None
    print("  [OK] compute_correctness avec picks toujours renseignés -> booléens (comportement prediction_log inchangé)")


def test_compute_correctness_none_pick_returns_none():
    c1, cb, co = compute_correctness("home", None, None, 2, 0)
    assert c1 is True
    assert cb is None
    assert co is None
    print("  [OK] compute_correctness(pick=None) -> None, jamais False (nouveau comportement Phase 6)")


def test_cannot_re_resolve_already_resolved_prediction():
    _clean_all()
    with Session(engine) as session:
        version_id = _make_version(session, "dixon_coles")
        record = PredictionRecord(
            league="Ligue1", match_date=MATCH_DATE, home_team="Paris SG", away_team="Marseille",
            model_type="dixon_coles", prob_home=0.6, prob_draw=0.25, prob_away=0.15,
            prob_btts_yes=0.55, prob_btts_no=0.45, prob_over_2_5=0.6, prob_under_2_5=0.4, source="live",
        )
        row = log_prediction(session, record, version_id)
        session.commit()
        resolve_prediction(row, 2, 1)
        session.add(row)
        session.commit()

        try:
            resolve_prediction(row, 5, 5)  # tentative de réécriture avec un AUTRE résultat
            assert False, "resolve_prediction() aurait dû lever RuntimeError sur une ligne déjà résolue"
        except RuntimeError:
            pass

        assert (row.result_home_goals, row.result_away_goals) == (2, 1), \
            "le résultat original ne doit JAMAIS être modifié par la tentative de re-résolution"
    print("  [OK] résoudre une prédiction déjà résolue -> RuntimeError, résultat original intact")


def test_probabilities_never_altered_by_resolution():
    """Anti-fuite direct : les probabilités stockées AVANT le match ne
    changent jamais lors de la résolution — seuls status/result_*/correct_*
    sont mutés."""
    _clean_all()
    with Session(engine) as session:
        version_id = _make_version(session, "dixon_coles")
        record = PredictionRecord(
            league="Ligue1", match_date=MATCH_DATE, home_team="Paris SG", away_team="Marseille",
            model_type="dixon_coles", prob_home=0.6413, prob_draw=0.2101, prob_away=0.1486,
            prob_btts_yes=0.5502, prob_btts_no=0.4498, prob_over_2_5=0.61, prob_under_2_5=0.39, source="live",
        )
        row = log_prediction(session, record, version_id)
        session.commit()
        original_probs = (row.prob_home, row.prob_draw, row.prob_away,
                           row.prob_btts_yes, row.prob_btts_no, row.prob_over_2_5, row.prob_under_2_5)

        resolve_prediction(row, 0, 3)  # résultat qui contredit fortement la prédiction (home favori, perd 0-3)
        session.add(row)
        session.commit()

        new_probs = (row.prob_home, row.prob_draw, row.prob_away,
                     row.prob_btts_yes, row.prob_btts_no, row.prob_over_2_5, row.prob_under_2_5)
        assert new_probs == original_probs, "les probabilités pré-match ne doivent JAMAIS être recalculées/altérées"
        assert row.correct_1x2 is False
    print("  [OK] résolution avec un résultat défavorable -> probabilités pré-match strictement inchangées")


# ---------------------------------------------------------------------------
# 3. Idempotence
# ---------------------------------------------------------------------------

def test_logging_same_match_same_version_twice_no_duplicate():
    _clean_all()
    with Session(engine) as session:
        version_id = _make_version(session, "xgboost")
        record = PredictionRecord(
            league="PremierLeague", match_date=MATCH_DATE, home_team="Man City", away_team="Arsenal",
            model_type="xgboost", prob_home=0.45, prob_draw=0.3, prob_away=0.25, source="backtest",
        )
        row1 = log_prediction(session, record, version_id)
        session.commit()
        row2 = log_prediction(session, record, version_id)
        session.commit()

        assert row1.id == row2.id, "un second appel identique doit retourner LA MÊME ligne, jamais un doublon"
        count = session.exec(
            select(ModelPrediction).where(
                ModelPrediction.league == "PremierLeague", ModelPrediction.home_team == "Man City",
                ModelPrediction.model_version_id == version_id,
            )
        ).all()
        assert len(count) == 1
    print("  [OK] même match + même version loggués 2 fois -> 1 seule ligne (idempotent)")


def test_new_model_version_creates_distinct_rows_not_duplicates():
    """Une NOUVELLE version (relance d'un backtest) produit de nouvelles
    lignes légitimes pour le même match — pas des doublons."""
    _clean_all()
    with Session(engine) as session:
        v1 = _make_version(session, "elo")
        v2 = _make_version(session, "elo")
        assert v1 != v2

        record = PredictionRecord(
            league="LaLiga", match_date=MATCH_DATE, home_team="Real Madrid", away_team="Barcelona",
            model_type="elo", prob_home=0.4, prob_draw=0.3, prob_away=0.3, source="backtest",
        )
        row_v1 = log_prediction(session, record, v1)
        row_v2 = log_prediction(session, record, v2)
        session.commit()

        assert row_v1.id != row_v2.id
        assert row_v1.model_version_id != row_v2.model_version_id
    print("  [OK] même match, 2 versions différentes -> 2 lignes distinctes (pas des doublons)")


def test_get_or_create_active_model_version_bootstraps_once():
    _clean_all()
    with Session(engine) as session:
        v1 = get_or_create_active_model_version(session, "dixon_coles", "xfoot-dixon-coles", notes="bootstrap test")
        v2 = get_or_create_active_model_version(session, "dixon_coles", "xfoot-dixon-coles", notes="bootstrap test")
        assert v1.id == v2.id, "un type de modèle sans version active ne doit être bootstrappé qu'UNE fois"
        assert v1.is_active is True

        all_versions = session.exec(select(ModelVersion).where(ModelVersion.model_type == "dixon_coles")).all()
        assert len(all_versions) == 1
    print("  [OK] get_or_create_active_model_version -> bootstrap une seule fois, réutilisé ensuite")


def test_get_or_create_active_model_version_reuses_existing_active():
    _clean_all()
    with Session(engine) as session:
        existing_id = _make_version(session, "elo", is_active=True, name="xfoot-elo-vX")
        found = get_or_create_active_model_version(session, "elo", "xfoot-elo", notes="ne doit pas être créé")
        assert found.id == existing_id, "une version active déjà créée par un script de backtest doit être réutilisée"

        all_versions = session.exec(select(ModelVersion).where(ModelVersion.model_type == "elo")).all()
        assert len(all_versions) == 1, "aucune version supplémentaire ne doit être créée"
    print("  [OK] version active déjà créée par un script de backtest -> réutilisée, jamais dupliquée")


# ---------------------------------------------------------------------------
# 4. Résolution multi-modèles via fetch_daily_results.py (§7, §12, §13)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._payload


def _fixture(home, away, home_goals, away_goals, league_id):
    return {"league": {"id": league_id}, "teams": {"home": {"name": home}, "away": {"name": away}},
            "goals": {"home": home_goals, "away": away_goals}}


def test_fetch_daily_results_resolves_model_predictions_too():
    import fetch_daily_results
    import app.core.api_football_config as cfg

    _clean_all()
    with Session(engine) as session:
        version_id = _make_version(session, "elo")
        record = PredictionRecord(
            league="Ligue1", match_date=MATCH_DATE, home_team="Paris SG", away_team="Marseille",
            model_type="elo", prob_home=0.55, prob_draw=0.25, prob_away=0.2, source="live",
        )
        row = log_prediction(session, record, version_id)
        session.commit()
        pred_id = row.id

    fixtures = [_fixture("Paris SG", "Marseille", 2, 1, cfg.API_FOOTBALL_LEAGUE_IDS["Ligue1"])]
    with patch("httpx.get", return_value=_FakeResponse({"response": fixtures, "paging": {"current": 1, "total": 1}})):
        exit_code = fetch_daily_results.run(target_date=MATCH_DATE)
    assert exit_code == 0, "tout aurait dû être rapproché (prediction_log vide + 1 model_predictions résolue)"

    with Session(engine) as session:
        resolved = session.get(ModelPrediction, pred_id)
        assert resolved.status == "resolved"
        assert (resolved.result_home_goals, resolved.result_away_goals) == (2, 1)
        assert resolved.correct_1x2 is True  # pick "home" (0.55 max), résultat 2-1 -> home
    print("  [OK] fetch_daily_results.py résout aussi les model_predictions 'live' en attente")


def test_fetch_daily_results_incomplete_fixture_leaves_pending():
    """Une fixture sans score (goals absents) ne doit jamais résoudre une
    prédiction avec un résultat incomplet."""
    import fetch_daily_results
    import app.core.api_football_config as cfg

    _clean_all()
    with Session(engine) as session:
        version_id = _make_version(session, "elo")
        record = PredictionRecord(
            league="SerieA", match_date=MATCH_DATE, home_team="Juventus", away_team="Milan",
            model_type="elo", prob_home=0.4, prob_draw=0.3, prob_away=0.3, source="live",
        )
        row = log_prediction(session, record, version_id)
        session.commit()
        pred_id = row.id

    incomplete_fixture = {
        "league": {"id": cfg.API_FOOTBALL_LEAGUE_IDS["SerieA"]},
        "teams": {"home": {"name": "Juventus"}, "away": {"name": "Milan"}},
        "goals": {"home": None, "away": None},  # match pas encore joué/score indisponible
    }
    with patch("httpx.get", return_value=_FakeResponse({"response": [incomplete_fixture], "paging": {"current": 1, "total": 1}})):
        exit_code = fetch_daily_results.run(target_date=MATCH_DATE)
    assert exit_code == 2, "score incomplet -> succès partiel, jamais une résolution silencieuse"

    with Session(engine) as session:
        still_pending = session.get(ModelPrediction, pred_id)
        assert still_pending.status == "pending"
        assert still_pending.result_home_goals is None
    print("  [OK] fixture au score incomplet -> prédiction reste 'pending', jamais résolue avec un résultat partiel")


def test_fetch_daily_results_rerun_is_idempotent_no_duplicate():
    import fetch_daily_results
    import app.core.api_football_config as cfg

    _clean_all()
    with Session(engine) as session:
        version_id = _make_version(session, "elo")
        record = PredictionRecord(
            league="Bundesliga", match_date=MATCH_DATE, home_team="Bayern Munich", away_team="Dortmund",
            model_type="elo", prob_home=0.6, prob_draw=0.25, prob_away=0.15, source="live",
        )
        row = log_prediction(session, record, version_id)
        session.commit()
        pred_id = row.id

    fixtures = [_fixture("Bayern Munich", "Dortmund", 4, 0, cfg.API_FOOTBALL_LEAGUE_IDS["Bundesliga"])]
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(1)
        return _FakeResponse({"response": fixtures, "paging": {"current": 1, "total": 1}})

    with patch("httpx.get", side_effect=fake_get):
        exit_code_1 = fetch_daily_results.run(target_date=MATCH_DATE)
    assert exit_code_1 == 0
    assert len(calls) == 1

    with patch("httpx.get", side_effect=fake_get):
        exit_code_2 = fetch_daily_results.run(target_date=MATCH_DATE)
    assert exit_code_2 == 0
    assert len(calls) == 1, "relancer le job sans rien de nouveau en attente ne doit déclencher AUCUN appel réseau"

    with Session(engine) as session:
        rows = session.exec(select(ModelPrediction).where(ModelPrediction.id == pred_id)).all()
        assert len(rows) == 1, "aucune ligne dupliquée après relance du job"
        assert (rows[0].result_home_goals, rows[0].result_away_goals) == (4, 0)
    print("  [OK] relancer fetch_daily_results.py 2 fois -> aucun doublon, aucun 2e appel réseau")


# ---------------------------------------------------------------------------
# 5. Benchmark multi-modèles réellement alimenté (§14 : "au moins deux
#    modèles ayant suffisamment de prédictions")
# ---------------------------------------------------------------------------

def test_benchmark_shows_real_comparison_with_two_sufficiently_populated_models(client):
    """Scénario de bout en bout : 2 versions de modèles (elo, xgboost) avec
    chacune >= MIN_BENCHMARK_SAMPLE_SIZE prédictions résolues -> GET
    /models/benchmark désigne réellement un meilleur modèle (status='ok'),
    jamais 'insufficient_data'."""
    from app.ai.arena.service import MIN_BENCHMARK_SAMPLE_SIZE

    _clean_all()
    n = MIN_BENCHMARK_SAMPLE_SIZE + 10
    with Session(engine) as session:
        elo_version = _make_version(session, "elo", name="xfoot-elo-benchmark-test")
        xgb_version = _make_version(session, "xgboost", name="xfoot-xgboost-benchmark-test")

        for i in range(n):
            # Elo : probabilité correcte la plupart du temps (accuracy/log-loss meilleurs)
            home_goals, away_goals = (2, 0) if i % 5 != 0 else (0, 1)
            record = PredictionRecord(
                league="Ligue1", match_date=date(2026, 1, 1), home_team=f"TeamA{i}", away_team=f"TeamB{i}",
                model_type="elo", prob_home=0.75, prob_draw=0.15, prob_away=0.10, source="backtest",
            )
            row = log_prediction(session, record, elo_version)
            resolve_prediction(row, home_goals, away_goals)
            session.add(row)

            # XGBoost : probabilité délibérément moins bonne (quasi uniforme -> log-loss/Brier pires)
            record2 = PredictionRecord(
                league="Ligue1", match_date=date(2026, 1, 1), home_team=f"TeamC{i}", away_team=f"TeamD{i}",
                model_type="xgboost", prob_home=0.34, prob_draw=0.33, prob_away=0.33, source="backtest",
            )
            row2 = log_prediction(session, record2, xgb_version)
            resolve_prediction(row2, home_goals, away_goals)
            session.add(row2)
        session.commit()

    r = client.get("/models/benchmark", params={"market": "1X2"})
    assert r.status_code == 200, r.text
    body = r.json()
    best = body["markets"]["1X2"]["best_model"]
    assert best["status"] == "ok", f"attendu une comparaison réelle avec {n} prédictions par modèle, obtenu : {best}"
    assert best["metric"] == "log_loss", "log_loss doit être priorisé (§9) quand disponible pour >= 2 modèles"
    assert best["best_model"] == "elo", "Elo (probabilité concentrée et majoritairement correcte) doit gagner sur log_loss"
    assert best["sample_size"] == n

    models = {m["model_type"]: m for m in body["markets"]["1X2"]["models"]}
    assert models["elo"]["sample_size"] == n
    assert models["xgboost"]["sample_size"] == n
    assert models["elo"]["log_loss"] < models["xgboost"]["log_loss"]
    print(f"  [OK] 2 modèles avec {n} prédictions chacun -> GET /models/benchmark désigne réellement 'elo' "
          f"comme meilleur sur log_loss (elo={models['elo']['log_loss']} < xgboost={models['xgboost']['log_loss']})")


UNIT_TESTS = [
    test_full_market_model_logs_all_probabilities,
    test_1x2_only_model_logs_null_btts_and_over_under,
    test_match_identity_fields_are_exact,
    test_resolution_pending_to_resolved,
    test_resolution_none_pick_yields_none_correctness_never_false,
    test_compute_correctness_full_picks_matches_historical_prediction_log_behavior,
    test_compute_correctness_none_pick_returns_none,
    test_cannot_re_resolve_already_resolved_prediction,
    test_probabilities_never_altered_by_resolution,
    test_logging_same_match_same_version_twice_no_duplicate,
    test_new_model_version_creates_distinct_rows_not_duplicates,
    test_get_or_create_active_model_version_bootstraps_once,
    test_get_or_create_active_model_version_reuses_existing_active,
    test_fetch_daily_results_resolves_model_predictions_too,
    test_fetch_daily_results_incomplete_fixture_leaves_pending,
    test_fetch_daily_results_rerun_is_idempotent_no_duplicate,
]

if __name__ == "__main__":
    failures = 0
    for t in UNIT_TESTS:
        print(f"\n=== {t.__name__} ===")
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"  [ECHEC] {e}")

    from fastapi.testclient import TestClient
    from main import app
    with TestClient(app) as client:
        print(f"\n=== test_benchmark_shows_real_comparison_with_two_sufficiently_populated_models ===")
        try:
            test_benchmark_shows_real_comparison_with_two_sufficiently_populated_models(client)
        except AssertionError as e:
            failures += 1
            print(f"  [ECHEC] {e}")

    total = len(UNIT_TESTS) + 1
    cleanup_db(DB_PATH)
    print(f"\n{'='*60}\n{total-failures}/{total} tests reussis\n{'='*60}")
    sys.exit(1 if failures else 0)
