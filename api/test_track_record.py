"""
test_track_record.py — Phase 7 : tests de app/ai/arena/track_record.py et
du mode LIVE de scripts/model_selection_shadow.py (main_live,
resolve_pending_shadow_predictions étendu, immutabilité, idempotence,
anti-fuite, sécurité DB).

Base isolée dédiée (jamais api/app.db) — même précaution que
test_model_selection.py (voir _test_support.py).

Usage : python api/test_track_record.py
"""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_track_record.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match
from app.models.model_prediction import ModelPrediction
from app.models.prediction_log import PredictionLog
from app.models.team_rating import ModelVersion, TeamRating
from app.models.model_selection_decision import ModelSelectionDecision
from app.models.shadow_selection_prediction import ShadowSelectionPrediction
from app.ai.arena.prediction_logging import PredictionRecord, log_prediction, resolve_prediction
from app.ai.arena import research, track_record

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import model_selection_shadow  # noqa: E402

init_db()


def _clean_all():
    with Session(engine) as session:
        for row in session.exec(select(ShadowSelectionPrediction)).all():
            session.delete(row)
        for row in session.exec(select(ModelSelectionDecision)).all():
            session.delete(row)
        for row in session.exec(select(ModelPrediction)).all():
            session.delete(row)
        for row in session.exec(select(PredictionLog)).all():
            session.delete(row)
        for row in session.exec(select(TeamRating)).all():
            session.delete(row)
        for v in session.exec(select(ModelVersion)).all():
            session.delete(v)
        for m in session.exec(select(Match)).all():
            session.delete(m)
        session.commit()


def _make_version(session, model_type, is_active=True):
    v = ModelVersion(name=f"test-{model_type}-{datetime.now(timezone.utc).timestamp()}",
                      model_type=model_type, trained_at=datetime.now(timezone.utc), is_active=is_active)
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


def _log_resolved(session, model_type, version_id, match_date, home_team, away_team, p_true,
                   league="Ligue1", source="backtest", home_goals=2, away_goals=0):
    other = (1.0 - p_true) / 2
    record = PredictionRecord(league=league, match_date=match_date, home_team=home_team, away_team=away_team,
                               model_type=model_type, prob_home=p_true, prob_draw=other, prob_away=other, source=source)
    row = log_prediction(session, record, version_id)
    resolve_prediction(row, home_goals, away_goals)
    session.add(row)
    session.commit()
    return row


def _seed_matches(session, league, teams, n_rounds, start_date):
    d = start_date
    for r in range(n_rounds):
        for i in range(len(teams)):
            home, away = teams[i], teams[(i + 1) % len(teams)]
            session.add(Match(league=league, date=datetime.combine(d, datetime.min.time()),
                               home_team=home, away_team=away, home_goals=(r + i) % 3, away_goals=(r + i + 1) % 3))
            d += timedelta(days=1)
    session.commit()


def _row_counts(session):
    return {
        "model_predictions": len(session.exec(select(ModelPrediction)).all()),
        "model_versions": len(session.exec(select(ModelVersion)).all()),
        "team_ratings": len(session.exec(select(TeamRating)).all()),
        "prediction_log": len(session.exec(select(PredictionLog)).all()),
        "match": len(session.exec(select(Match)).all()),
    }


def _seed_live_fixture(session, as_of, n_historical=40, p_elo=0.85, p_xgb=0.30, p_lgb=0.32, future_home="Future Home", future_away="Future Away"):
    """Historique résolu (permettant au Model Selection Engine de rendre
    une décision) + une fixture FUTURE (relative à `as_of`) déjà pending en
    production pour les 4 modèles — même convention que
    test_model_selection.py::_seed_uniform_fixture, étendue avec la
    fixture pending nécessaire au mode live (Phase 7)."""
    teams = ["Team A", "Team B", "Team C", "Team D"]
    _seed_matches(session, "Ligue1", teams, n_rounds=6, start_date=date(2025, 1, 1))

    v_elo = _make_version(session, "elo")
    v_xgb = _make_version(session, "xgboost")
    v_lgb = _make_version(session, "lightgbm")

    match_date = date(2025, 6, 1)
    for i in range(n_historical):
        d = match_date + timedelta(days=i)
        home, away = f"H{i}", f"A{i}"
        for mt, vid, p in (("elo", v_elo.id, p_elo), ("xgboost", v_xgb.id, p_xgb), ("lightgbm", v_lgb.id, p_lgb)):
            jitter = 0.01 * ((i % 3) - 1)
            _log_resolved(session, mt, vid, d, home, away, p_true=min(max(p + jitter, 0.05), 0.95), league="Ligue1")

    future_date = as_of + timedelta(days=2)
    for mt, vid, p in (("elo", v_elo.id, 0.6), ("xgboost", v_xgb.id, 0.5), ("lightgbm", v_lgb.id, 0.55)):
        record = PredictionRecord(league="Ligue1", match_date=future_date, home_team=future_home, away_team=future_away,
                                   model_type=mt, prob_home=p, prob_draw=(1 - p) / 2, prob_away=(1 - p) / 2, source="live")
        log_prediction(session, record, vid)  # reste status="pending" -- jamais résolu ici
    session.add(PredictionLog(
        league="Ligue1", match_date=future_date, home_team=future_home, away_team=future_away,
        payload=json.dumps({"home_win": 0.5, "draw": 0.3, "away_win": 0.2, "btts_yes": 0.5, "btts_no": 0.5, "over_2_5": 0.5, "under_2_5": 0.5}),
        pick_1x2="home", pick_btts="yes", pick_over_2_5="over",
    ))
    session.commit()
    return future_date, ("Ligue1", future_date, future_home, future_away), {"elo": v_elo, "xgboost": v_xgb, "lightgbm": v_lgb}


def _insert_resolved_shadow_row(session, decision_id, league, match_date, home, away, market,
                                 candidate_probs, production_probs, home_goals, away_goals,
                                 candidate_model_type="elo", calibration_applied="none", candidate_probs_raw=None):
    """Construit directement une ligne ShadowSelectionPrediction RÉSOLUE,
    pour tester track_record.py sans repasser par tout le pipeline
    main_live() à chaque fois — pattern déjà utilisé en Phase 5.7/6 pour
    isoler le test des primitives statistiques de leur orchestration."""
    actual = research.actual_outcome(market, home_goals, away_goals)
    pick_c = max(candidate_probs, key=candidate_probs.get)
    pick_p = max(production_probs, key=production_probs.get) if production_probs else None
    row = ShadowSelectionPrediction(
        selection_decision_id=decision_id, league=league, match_date=match_date, home_team=home, away_team=away,
        market=market, candidate_model_type=candidate_model_type, calibration_applied=calibration_applied,
        candidate_probs=json.dumps(candidate_probs),
        candidate_probs_raw=json.dumps(candidate_probs_raw) if candidate_probs_raw else None,
        production_model_type="dixon_coles" if production_probs else None,
        production_probs=json.dumps(production_probs) if production_probs else None,
        status="resolved", result_home_goals=home_goals, result_away_goals=away_goals,
        candidate_correct=(pick_c == actual), production_correct=(pick_p == actual) if pick_p else None,
        resolved_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _make_decision(session, market="1X2", status="selected", selected="elo", as_of=date(2025, 8, 1)):
    d = ModelSelectionDecision(run_id="test-run", mode="shadow", market=market, as_of=as_of, status=status,
                                selected_model_type=selected if status == "selected" else None,
                                windows_evaluated=3, reason="fixture", calibration_choice="none")
    session.add(d)
    session.commit()
    session.refresh(d)
    return d


# ---------------------------------------------------------------------------
# 1. Mode LIVE — création (§3/§26)
# ---------------------------------------------------------------------------

def test_main_live_creates_pending_shadow_prediction_for_selected_candidate():
    _clean_all()
    as_of = date(2025, 8, 1)
    with Session(engine) as session:
        _seed_live_fixture(session, as_of, n_historical=40, p_elo=0.85, p_xgb=0.30, p_lgb=0.32)

    result = model_selection_shadow.main_live(market="1X2", n_windows=4, min_sample_size=5, as_of=as_of)
    assert result["status"] == "ok"
    assert result["decision_status"] == "selected"
    assert result["shadow_predictions_written"] == 1

    with Session(engine) as session:
        rows = session.exec(select(ShadowSelectionPrediction)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.status == "pending", "un match futur ne doit jamais être résolu à la création (§4)"
        assert row.result_home_goals is None and row.candidate_correct is None
        assert row.candidate_model_type == "elo"
        assert row.home_team == "Future Home"
    print("  [OK] main_live : crée une prédiction shadow PENDING pour un candidat sélectionné, sur une fixture réellement future")


def test_main_live_no_upcoming_fixtures_reports_no_shadow_data_and_writes_nothing():
    _clean_all()
    as_of = date(2025, 8, 1)
    with Session(engine) as session:
        # historique seul, AUCUNE fixture pending future
        teams = ["Team A", "Team B", "Team C", "Team D"]
        _seed_matches(session, "Ligue1", teams, n_rounds=6, start_date=date(2025, 1, 1))
        v = _make_version(session, "elo")
        for i in range(10):
            _log_resolved(session, "elo", v.id, date(2025, 6, 1) + timedelta(days=i), f"H{i}", f"A{i}", 0.6)

    result = model_selection_shadow.main_live(market="1X2", n_windows=4, min_sample_size=5, as_of=as_of)
    assert result["status"] == "no_upcoming_fixtures"

    with Session(engine) as session:
        assert len(session.exec(select(ShadowSelectionPrediction)).all()) == 0
        assert len(session.exec(select(ModelSelectionDecision)).all()) == 0, (
            "aucune fixture éligible -> aucun calcul de décision inutile, jamais une décision fabriquée sans raison d'être"
        )
    print("  [OK] main_live : aucune fixture future pending -> NO SHADOW DATA, rien écrit (ni décision, ni prédiction)")


def test_main_live_stale_pending_rows_excluded_from_upcoming_fixtures():
    """Une ligne model_predictions status=pending mais dont match_date est
    ANTÉRIEURE à as_of est orpheline (jamais résolue par
    fetch_daily_results.py) -- ne doit jamais être traitée comme une
    fixture à venir (audit empirique documenté dans le module)."""
    _clean_all()
    as_of = date(2025, 8, 1)
    with Session(engine) as session:
        teams = ["Team A", "Team B", "Team C", "Team D"]
        _seed_matches(session, "Ligue1", teams, n_rounds=6, start_date=date(2025, 1, 1))
        v = _make_version(session, "elo")
        for i in range(10):
            _log_resolved(session, "elo", v.id, date(2025, 6, 1) + timedelta(days=i), f"H{i}", f"A{i}", 0.6)
        stale_date = as_of - timedelta(days=10)
        record = PredictionRecord(league="Ligue1", match_date=stale_date, home_team="Stale Home", away_team="Stale Away",
                                   model_type="elo", prob_home=0.5, prob_draw=0.25, prob_away=0.25, source="live")
        log_prediction(session, record, v.id)
        session.commit()

    with Session(engine) as session:
        keys = model_selection_shadow._find_upcoming_fixture_keys(session, as_of)
    assert ("Ligue1", stale_date, "Stale Home", "Stale Away") not in keys
    print("  [OK] main_live : une ligne pending antérieure à as_of (orpheline) est exclue des fixtures à venir")


# ---------------------------------------------------------------------------
# 2. Immutabilité + idempotence (§4/§27)
# ---------------------------------------------------------------------------

def test_shadow_prediction_immutable_across_reruns():
    _clean_all()
    as_of = date(2025, 8, 1)
    with Session(engine) as session:
        _seed_live_fixture(session, as_of, n_historical=40, p_elo=0.85, p_xgb=0.30, p_lgb=0.32)

    model_selection_shadow.main_live(market="1X2", n_windows=4, min_sample_size=5, as_of=as_of)
    with Session(engine) as session:
        row = session.exec(select(ShadowSelectionPrediction)).first()
        snapshot = (row.candidate_probs, row.candidate_model_type, row.selection_decision_id, row.created_at)

    result2 = model_selection_shadow.main_live(market="1X2", n_windows=4, min_sample_size=5, as_of=as_of)
    assert result2["shadow_predictions_written"] == 0, "idempotent -- rien de nouveau écrit au second run"

    with Session(engine) as session:
        rows = session.exec(select(ShadowSelectionPrediction)).all()
        assert len(rows) == 1
        row2 = rows[0]
        assert (row2.candidate_probs, row2.candidate_model_type, row2.selection_decision_id, row2.created_at) == snapshot, (
            "probabilité/modèle/décision/timestamp doivent rester STRICTEMENT identiques (§4 immutabilité)"
        )
    print("  [OK] ShadowSelectionPrediction : probs/modèle/décision/timestamp immuables sur exécutions répétées (idempotence §27)")


# ---------------------------------------------------------------------------
# 3. Résolution (§28) — sources multiples, jamais de mutation d'une ligne résolue
# ---------------------------------------------------------------------------

def test_resolve_pending_via_model_predictions_source():
    _clean_all()
    as_of = date(2025, 8, 1)
    with Session(engine) as session:
        _, key, versions = _seed_live_fixture(session, as_of, n_historical=40, p_elo=0.85, p_xgb=0.30, p_lgb=0.32)
    model_selection_shadow.main_live(market="1X2", n_windows=4, min_sample_size=5, as_of=as_of)

    with Session(engine) as session:
        mp = session.exec(select(ModelPrediction).where(
            ModelPrediction.model_type == "elo", ModelPrediction.home_team == "Future Home",
        )).first()
        resolve_prediction(mp, 3, 1)
        session.add(mp)
        session.commit()

        original_probs = session.exec(select(ShadowSelectionPrediction)).first().candidate_probs
        n = model_selection_shadow.resolve_pending_shadow_predictions(session)

    assert n == 1
    with Session(engine) as session:
        row = session.exec(select(ShadowSelectionPrediction)).first()
        assert row.status == "resolved"
        assert (row.result_home_goals, row.result_away_goals) == (3, 1)
        assert row.candidate_correct is True  # home_win prédit, home_win réel
        assert row.candidate_probs == original_probs, "les probabilités candidates ne doivent JAMAIS changer à la résolution (§4/§28)"
    print("  [OK] resolve_pending_shadow_predictions : résout via model_predictions, ne modifie jamais candidate_probs/modèle/décision")


def test_resolve_pending_never_mutates_already_resolved_row():
    _clean_all()
    with Session(engine) as session:
        decision = _make_decision(session)
        resolved_row = _insert_resolved_shadow_row(
            session, decision.id, "Ligue1", date(2025, 6, 1), "H0", "A0", "1X2",
            {"home_win": 0.6, "draw": 0.25, "away_win": 0.15}, None, 2, 0,
        )
        resolved_id = resolved_row.id
        pending_row = ShadowSelectionPrediction(
            selection_decision_id=decision.id, league="Ligue1", match_date=date(2025, 6, 2),
            home_team="H1", away_team="A1", market="1X2", candidate_model_type="elo",
            candidate_probs=json.dumps({"home_win": 0.6, "draw": 0.25, "away_win": 0.15}), status="pending",
        )
        session.add(pending_row)
        session.add(Match(league="Ligue1", date=datetime.combine(date(2025, 6, 2), datetime.min.time()),
                           home_team="H1", away_team="A1", home_goals=1, away_goals=1))
        session.commit()
        n = model_selection_shadow.resolve_pending_shadow_predictions(session)

    assert n == 1
    with Session(engine) as session:
        still = session.get(ShadowSelectionPrediction, resolved_id)
        assert still.status == "resolved" and still.result_home_goals == 2  # inchangé
    print("  [OK] resolve_pending_shadow_predictions : une ligne déjà résolue n'est jamais retouchée")


# ---------------------------------------------------------------------------
# 4. Anti-fuite (§36) — production snapshot avant résultat, décision avant futur
# ---------------------------------------------------------------------------

def test_production_snapshot_captured_before_result_known():
    _clean_all()
    as_of = date(2025, 8, 1)
    with Session(engine) as session:
        _seed_live_fixture(session, as_of, n_historical=40, p_elo=0.85, p_xgb=0.30, p_lgb=0.32)
    model_selection_shadow.main_live(market="1X2", n_windows=4, min_sample_size=5, as_of=as_of)

    with Session(engine) as session:
        row = session.exec(select(ShadowSelectionPrediction)).first()
        # au moment de l'écriture shadow, le match n'a pas encore de résultat nulle part
        mp_rows = session.exec(select(ModelPrediction).where(ModelPrediction.home_team == "Future Home")).all()
        assert all(r.result_home_goals is None for r in mp_rows), "précondition du test : le match n'est pas encore résolu"
        assert row.status == "pending" and row.result_home_goals is None
        # le snapshot production (dixon_coles ici) a bien été capturé malgré l'absence de résultat
        assert row.production_probs is not None
        assert row.production_model_type == "dixon_coles"
    print("  [OK] Anti-fuite : le snapshot production est capturé alors que le résultat est encore inconnu partout")


def test_selection_windows_never_include_the_future_fixture():
    """La fenêtre de test (crédibilité) et les fenêtres de stabilité sont
    construites UNIQUEMENT à partir de matchs déjà résolus (common_keys,
    intersection de model_predictions déjà résolus) -- la fixture future
    pending ne peut structurellement jamais y apparaître."""
    _clean_all()
    as_of = date(2025, 8, 1)
    with Session(engine) as session:
        _seed_live_fixture(session, as_of, n_historical=40, p_elo=0.85, p_xgb=0.30, p_lgb=0.32)
        ctx = model_selection_shadow._compute_decision_and_calibration(session, "1X2", 4, 5)
    assert ("Ligue1", as_of + timedelta(days=2), "Future Home", "Future Away") not in ctx["ordered_keys"]
    print("  [OK] Anti-fuite : la fixture future ne peut jamais entrer dans les fenêtres de sélection (non résolue -> jamais dans common_keys)")


# ---------------------------------------------------------------------------
# 5. Comparaison Production vs Shadow (§6/§17/§18/§19)
# ---------------------------------------------------------------------------

def test_compare_production_vs_shadow_same_sample_only():
    _clean_all()
    with Session(engine) as session:
        decision = _make_decision(session)
        # ligne 1 : shadow ET production -> incluse
        _insert_resolved_shadow_row(session, decision.id, "Ligue1", date(2025, 6, 1), "H0", "A0", "1X2",
                                     {"home_win": 0.7, "draw": 0.2, "away_win": 0.1},
                                     {"home_win": 0.5, "draw": 0.3, "away_win": 0.2}, 2, 0)
        # ligne 2 : shadow SEUL (pas de snapshot production) -> exclue de la comparaison
        _insert_resolved_shadow_row(session, decision.id, "Ligue1", date(2025, 6, 2), "H1", "A1", "1X2",
                                     {"home_win": 0.6, "draw": 0.25, "away_win": 0.15}, None, 1, 1)
        result = track_record.compare_production_vs_shadow(session, "1X2", min_sample_size=1)
    assert result.sample_size == 1, "seule la ligne avec production ET shadow résolus doit compter (§6)"
    print("  [OK] compare_production_vs_shadow : n'utilise que l'intersection production+shadow, jamais un échantillon partiel")


def test_compare_production_vs_shadow_sign_convention_and_delta():
    _clean_all()
    with Session(engine) as session:
        decision = _make_decision(session)
        for i in range(60):
            # shadow prédit "home_win" avec grande confiance et a systématiquement raison (0.9) ;
            # production prédit "home_win" avec confiance modeste (0.5) -- shadow doit être MEILLEUR (log_loss plus bas)
            _insert_resolved_shadow_row(session, decision.id, "Ligue1", date(2025, 6, 1) + timedelta(days=i), f"H{i}", f"A{i}", "1X2",
                                         {"home_win": 0.9, "draw": 0.06, "away_win": 0.04},
                                         {"home_win": 0.5, "draw": 0.3, "away_win": 0.2}, 2, 0)
        result = track_record.compare_production_vs_shadow(session, "1X2", min_sample_size=50)
    assert result.status == "ok"
    assert result.delta["log_loss"] < 0, "shadow meilleur -> delta log_loss négatif (shadow - production), convention documentée"
    assert result.conclusion == "BETTER"
    assert result.significance["bootstrap_log_loss_diff"]["significant"] is True
    print(f"  [OK] compare_production_vs_shadow : convention de signe correcte (delta={result.delta['log_loss']}), conclusion=BETTER")


def test_compare_production_vs_shadow_insufficient_data():
    _clean_all()
    with Session(engine) as session:
        decision = _make_decision(session)
        _insert_resolved_shadow_row(session, decision.id, "Ligue1", date(2025, 6, 1), "H0", "A0", "1X2",
                                     {"home_win": 0.7, "draw": 0.2, "away_win": 0.1},
                                     {"home_win": 0.5, "draw": 0.3, "away_win": 0.2}, 2, 0)
        result = track_record.compare_production_vs_shadow(session, "1X2", min_sample_size=100)
    assert result.status == "insufficient_data"
    assert result.conclusion == "INSUFFICIENT_DATA"
    print("  [OK] compare_production_vs_shadow : échantillon sous le seuil -> INSUFFICIENT_DATA, jamais une conclusion forcée")


def test_compare_production_vs_shadow_no_data_at_all():
    _clean_all()
    with Session(engine) as session:
        result = track_record.compare_production_vs_shadow(session, "1X2")
    assert result.status == "no_shadow_data"
    print("  [OK] compare_production_vs_shadow : base vide -> no_shadow_data, jamais fabriqué")


# ---------------------------------------------------------------------------
# 6. Track record cumulatif + fenêtres glissantes (§8/§9)
# ---------------------------------------------------------------------------

def test_cumulative_track_record_grows_from_individual_observations():
    _clean_all()
    with Session(engine) as session:
        decision = _make_decision(session)
        for i in range(30):
            _insert_resolved_shadow_row(session, decision.id, "Ligue1", date(2025, 6, 1) + timedelta(days=i), f"H{i}", f"A{i}", "1X2",
                                         {"home_win": 0.7, "draw": 0.2, "away_win": 0.1}, None, 2, 0)
        checkpoints = [date(2025, 6, 5), date(2025, 6, 15), date(2025, 6, 30)]
        points = track_record.compute_cumulative_track_record(session, "1X2", checkpoints, min_sample_size=1)
    sizes = [p.sample_size for p in points]
    assert sizes == sorted(sizes), "le sample_size cumulatif doit croître (ou rester stable), jamais diminuer avec le temps"
    assert sizes[0] == 5 and sizes[-1] == 30
    # vérifie que ce n'est pas une simple répétition de la même moyenne (recalcul depuis les observations individuelles)
    for p in points:
        assert p.accuracy == 1.0  # toutes les prédictions sont "home_win" correct ici
    print(f"  [OK] compute_cumulative_track_record : croît avec chaque checkpoint depuis les observations individuelles ({sizes})")


def test_rolling_window_last_n_respects_boundary():
    _clean_all()
    with Session(engine) as session:
        decision = _make_decision(session)
        for i in range(20):
            _insert_resolved_shadow_row(session, decision.id, "Ligue1", date(2025, 6, 1) + timedelta(days=i), f"H{i}", f"A{i}", "1X2",
                                         {"home_win": 0.7, "draw": 0.2, "away_win": 0.1}, None, 2, 0)
        result_10 = track_record.compute_track_record(session, "1X2", last_n=10, min_sample_size=1)
        result_all = track_record.compute_track_record(session, "1X2", min_sample_size=1)
    assert result_10.sample_size == 10
    assert result_all.sample_size == 20
    print("  [OK] compute_track_record(last_n=...) : fenêtre glissante correctement bornée")


def test_rolling_window_since_until_date_filter():
    _clean_all()
    with Session(engine) as session:
        decision = _make_decision(session)
        for i in range(20):
            _insert_resolved_shadow_row(session, decision.id, "Ligue1", date(2025, 6, 1) + timedelta(days=i), f"H{i}", f"A{i}", "1X2",
                                         {"home_win": 0.7, "draw": 0.2, "away_win": 0.1}, None, 2, 0)
        result = track_record.compute_track_record(session, "1X2", since=date(2025, 6, 10), until=date(2025, 6, 15), min_sample_size=1)
    assert result.sample_size == 6  # 10,11,12,13,14,15 inclus
    print("  [OK] compute_track_record(since=, until=) : filtre de dates correctement inclusif des deux bornes")


# ---------------------------------------------------------------------------
# 7. League (§20)
# ---------------------------------------------------------------------------

def test_league_specific_vs_global_track_record():
    _clean_all()
    with Session(engine) as session:
        decision = _make_decision(session)
        for i in range(10):
            _insert_resolved_shadow_row(session, decision.id, "Ligue1", date(2025, 6, 1) + timedelta(days=i), f"H{i}", f"A{i}", "1X2",
                                         {"home_win": 0.7, "draw": 0.2, "away_win": 0.1}, None, 2, 0)
        for i in range(3):
            _insert_resolved_shadow_row(session, decision.id, "PremierLeague", date(2025, 7, 1) + timedelta(days=i), f"PH{i}", f"PA{i}", "1X2",
                                         {"home_win": 0.7, "draw": 0.2, "away_win": 0.1}, None, 2, 0)
        global_result = track_record.compute_track_record(session, "1X2", min_sample_size=1)
        ligue1_result = track_record.compute_track_record(session, "1X2", league="Ligue1", min_sample_size=1)
        pl_result = track_record.compute_track_record(session, "1X2", league="PremierLeague", min_sample_size=5)
    assert global_result.sample_size == 13
    assert ligue1_result.sample_size == 10
    assert pl_result.status == "insufficient_data", "petit échantillon ligue -> insufficient_data, jamais une conclusion tirée quand même"
    print("  [OK] compute_track_record(league=) : global vs par ligue corrects, petit échantillon ligue marqué insufficient_data")


# ---------------------------------------------------------------------------
# 8. Sélection distribution / stability tracking (§13/§14)
# ---------------------------------------------------------------------------

def test_selection_distribution_only_counts_selected_status():
    _clean_all()
    with Session(engine) as session:
        _make_decision(session, status="selected", selected="elo")
        _make_decision(session, status="selected", selected="elo")
        _make_decision(session, status="selected", selected="xgboost")
        d = _make_decision(session, status="insufficient_data")
        d.selected_model_type = None
        session.add(d)
        session.commit()
        dist = track_record.compute_selection_distribution(session)
    assert dist["status"] == "ok"
    assert dist["total_selected"] == 3, "insufficient_data ne doit JAMAIS être compté dans la distribution (§14)"
    assert dist["counts"] == {"elo": 2, "xgboost": 1}
    assert dist["distribution"]["elo"] == round(2 / 3, 4)
    print("  [OK] compute_selection_distribution : ignore les décisions non 'selected', distribution correcte")


def test_stability_tracking_derives_implied_candidate_from_top_rank_counts():
    _clean_all()
    with Session(engine) as session:
        unstable = ModelSelectionDecision(
            run_id="r1", mode="shadow", market="1X2", as_of=date(2025, 8, 1), status="unstable",
            windows_evaluated=3, reason="fixture",
            metrics=json.dumps({"top_rank_counts": {"elo": 3, "xgboost": 0}, "log_loss_cv": {}}),
            calibration_choice="none",
        )
        session.add(unstable)
        insufficient = ModelSelectionDecision(
            run_id="r1", mode="shadow", market="1X2", as_of=date(2025, 8, 2), status="insufficient_data",
            windows_evaluated=0, reason="fixture", calibration_choice="none",
        )
        session.add(insufficient)
        session.commit()
        stability = track_record.compute_stability_tracking(session)
    assert stability["per_model"]["elo"]["unstable"] == 1, "candidat implicite dérivé de top_rank_counts (elo a gagné le plus de fenêtres)"
    assert stability["unattributed"]["insufficient_data"] == 1
    print("  [OK] compute_stability_tracking : candidat quasi-sélectionné dérivé de top_rank_counts, insufficient_data jamais attribué")


# ---------------------------------------------------------------------------
# 9. Calibration tracking (§15/§16)
# ---------------------------------------------------------------------------

def test_calibration_tracking_frequency():
    _clean_all()
    with Session(engine) as session:
        for choice in ("none", "platt", "platt", "isotonic"):
            d = ModelSelectionDecision(run_id="r", mode="shadow", market="1X2", as_of=date(2025, 8, 1), status="selected",
                                        selected_model_type="elo", windows_evaluated=3, reason="fixture",
                                        calibration_choice=choice, calibration_verdict="HELPFUL" if choice != "none" else None)
            session.add(d)
        session.commit()
        tracking = track_record.compute_calibration_tracking(session)
    assert tracking["choice_counts"] == {"none": 1, "platt": 2, "isotonic": 1}
    assert tracking["choice_frequency"]["platt"] == 0.5
    print("  [OK] compute_calibration_tracking : fréquence NONE/PLATT/ISOTONIC correcte")


def test_compare_raw_vs_calibrated_only_uses_actually_calibrated_rows():
    _clean_all()
    with Session(engine) as session:
        decision = _make_decision(session)
        # 45 matchs où le pick ("home_win") est CORRECT, 15 où il est FAUX (actual="away_win") --
        # un mélange réaliste est nécessaire : sur-confiance brute (raw=0.95) est récompensée quand
        # le pick est juste mais SÉVÈREMENT pénalisée quand il est faux (log(0.02) très négatif) ;
        # la version calibrée (0.7, moins confiante) perd un peu sur les corrects mais perd beaucoup
        # moins sur les faux -- résultat net attendu : raw pire que calibré en moyenne, comme un
        # vrai bénéfice de calibration, jamais garanti par construction si TOUJOURS correct (piège
        # évité ici : un raw toujours correct est récompensé par la sur-confiance, pas pénalisé).
        for i in range(45):
            _insert_resolved_shadow_row(
                session, decision.id, "Ligue1", date(2025, 6, 1) + timedelta(days=i), f"H{i}", f"A{i}", "1X2",
                {"home_win": 0.7, "draw": 0.2, "away_win": 0.1}, None, 2, 0,
                calibration_applied="platt", candidate_probs_raw={"home_win": 0.95, "draw": 0.03, "away_win": 0.02},
            )
        for i in range(15):
            _insert_resolved_shadow_row(
                session, decision.id, "Ligue1", date(2025, 8, 1) + timedelta(days=i), f"W{i}", f"L{i}", "1X2",
                {"home_win": 0.7, "draw": 0.2, "away_win": 0.1}, None, 0, 1,  # away_win réel -> pick "home_win" faux
                calibration_applied="platt", candidate_probs_raw={"home_win": 0.95, "draw": 0.03, "away_win": 0.02},
            )
        for i in range(5):
            # non calibré -- ne doit jamais entrer dans la comparaison
            _insert_resolved_shadow_row(session, decision.id, "Ligue1", date(2025, 9, 1) + timedelta(days=i), f"N{i}", f"M{i}", "1X2",
                                         {"home_win": 0.6, "draw": 0.25, "away_win": 0.15}, None, 2, 0)
        result = track_record.compare_raw_vs_calibrated(session, "1X2", min_sample_size=50)
    assert result["status"] == "ok"
    assert result["sample_size"] == 60, "seules les lignes calibration_applied != 'none' doivent compter"
    assert result["raw"]["log_loss"] > result["calibrated"]["log_loss"], (
        f"raw={result['raw']['log_loss']} calibrated={result['calibrated']['log_loss']}"
    )
    print(f"  [OK] compare_raw_vs_calibrated : n'utilise que les prédictions réellement calibrées (N={result['sample_size']}), "
          f"raw={result['raw']['log_loss']} > calibrated={result['calibrated']['log_loss']}")


# ---------------------------------------------------------------------------
# 10. Reproductibilité (§37)
# ---------------------------------------------------------------------------

def test_decision_reproducible_same_inputs_same_cutoff():
    _clean_all()
    as_of = date(2025, 8, 1)
    with Session(engine) as session:
        _seed_live_fixture(session, as_of, n_historical=40, p_elo=0.85, p_xgb=0.30, p_lgb=0.32)
        ctx1 = model_selection_shadow._compute_decision_and_calibration(session, "1X2", 4, 5)
        ctx2 = model_selection_shadow._compute_decision_and_calibration(session, "1X2", 4, 5)
    assert ctx1["decision"].status == ctx2["decision"].status
    assert ctx1["decision"].selected_model_type == ctx2["decision"].selected_model_type
    assert ctx1["decision"].top_rank_counts == ctx2["decision"].top_rank_counts
    assert ctx1["decision"].credibility == ctx2["decision"].credibility
    print("  [OK] _compute_decision_and_calibration : même dataset/config/cutoff -> même décision, reproductible")


# ---------------------------------------------------------------------------
# 11. Sécurité DB (§35)
# ---------------------------------------------------------------------------

def test_main_live_and_track_record_never_touch_production_tables():
    _clean_all()
    as_of = date(2025, 8, 1)
    with Session(engine) as session:
        _seed_live_fixture(session, as_of, n_historical=40, p_elo=0.85, p_xgb=0.30, p_lgb=0.32)
        before = _row_counts(session)
        # snapshot des lignes model_predictions/prediction_log elles-mêmes (contenu, pas seulement le compte)
        before_mp_status = sorted((r.id, r.status, r.result_home_goals) for r in session.exec(select(ModelPrediction)).all())
        before_pl = sorted((r.id, r.result_home_goals) for r in session.exec(select(PredictionLog)).all())

    model_selection_shadow.main_live(market="1X2", n_windows=4, min_sample_size=5, as_of=as_of)

    with Session(engine) as session:
        after = _row_counts(session)
        after_mp_status = sorted((r.id, r.status, r.result_home_goals) for r in session.exec(select(ModelPrediction)).all())
        after_pl = sorted((r.id, r.result_home_goals) for r in session.exec(select(PredictionLog)).all())

    assert before == after, f"main_live() a modifié une table de production : avant={before} après={after}"
    assert before_mp_status == after_mp_status, "aucune ligne model_predictions ne doit être modifiée par le mode shadow live"
    assert before_pl == after_pl, "aucune ligne prediction_log ne doit être modifiée par le mode shadow live"
    print("  [OK] main_live() : model_predictions/model_versions/team_ratings/prediction_log/match strictement inchangés (contenu ET compte)")


def test_track_record_services_are_read_only():
    _clean_all()
    with Session(engine) as session:
        decision = _make_decision(session)
        _insert_resolved_shadow_row(session, decision.id, "Ligue1", date(2025, 6, 1), "H0", "A0", "1X2",
                                     {"home_win": 0.7, "draw": 0.2, "away_win": 0.1}, None, 2, 0)
        before = _row_counts(session)
        before_shadow = len(session.exec(select(ShadowSelectionPrediction)).all())
        before_decisions = len(session.exec(select(ModelSelectionDecision)).all())

        track_record.compute_track_record(session, "1X2")
        track_record.compute_cumulative_track_record(session, "1X2", [date(2025, 6, 1)])
        track_record.compare_production_vs_shadow(session, "1X2")
        track_record.compute_selection_distribution(session)
        track_record.compute_stability_tracking(session)
        track_record.compute_calibration_tracking(session)
        track_record.compare_raw_vs_calibrated(session, "1X2")

        after = _row_counts(session)
        after_shadow = len(session.exec(select(ShadowSelectionPrediction)).all())
        after_decisions = len(session.exec(select(ModelSelectionDecision)).all())

    assert before == after
    assert before_shadow == after_shadow and before_decisions == after_decisions
    print("  [OK] track_record.py : tous les services sont strictement lecture seule (aucune écriture, y compris tables Phase 6)")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    failures = 0
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  [FAIL] {t.__name__}: {e!r}")
    cleanup_db(DB_PATH)
    print(f"\n{len(tests) - failures}/{len(tests)} tests OK")
    sys.exit(1 if failures else 0)
