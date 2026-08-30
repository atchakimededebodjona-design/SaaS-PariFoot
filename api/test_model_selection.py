"""
test_model_selection.py — Phase 6 : tests de app/ai/arena/model_selection.py,
app/ai/arena/calibration_engine.py, scripts/model_selection_research.py et
scripts/model_selection_shadow.py.

Base isolée dédiée (jamais api/app.db) — même précaution que
test_research_ensemble.py (voir _test_support.py).

Usage : python api/test_model_selection.py
"""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_model_selection.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion, TeamRating
from app.models.model_selection_decision import ModelSelectionDecision
from app.models.shadow_selection_prediction import ShadowSelectionPrediction
from app.ai.arena.prediction_logging import PredictionRecord, log_prediction, resolve_prediction
from app.ai.arena.schemas import MarketMetrics
from app.ai.arena import model_selection, calibration_engine

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import model_selection_research  # noqa: E402
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
        for row in session.exec(select(TeamRating)).all():
            session.delete(row)
        for v in session.exec(select(ModelVersion)).all():
            session.delete(v)
        for m in session.exec(select(Match)).all():
            session.delete(m)
        session.commit()


def _make_version(session, model_type: str, is_active: bool = False) -> ModelVersion:
    v = ModelVersion(
        name=f"test-{model_type}-{datetime.now(timezone.utc).timestamp()}",
        model_type=model_type, trained_at=datetime.now(timezone.utc), is_active=is_active,
    )
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


def _log_resolved(session, model_type, version_id, match_date, home_team, away_team, p_true,
                   league="Ligue1", source="backtest", home_goals=2, away_goals=0):
    other = (1.0 - p_true) / 2
    record = PredictionRecord(
        league=league, match_date=match_date, home_team=home_team, away_team=away_team,
        model_type=model_type, prob_home=p_true, prob_draw=other, prob_away=other, source=source,
    )
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
    }


def _m(sample_size, log_loss, brier=None, accuracy=None):
    return MarketMetrics(sample_size=sample_size, log_loss=log_loss, brier_score=brier or log_loss, accuracy=accuracy or 0.5,
                          correct_predictions=int(sample_size * (accuracy or 0.5)))


# ---------------------------------------------------------------------------
# 1. Les 3 portes de select_candidate_model
# ---------------------------------------------------------------------------

def test_select_candidate_insufficient_data_too_few_windows():
    decision = model_selection.select_candidate_model({"elo": [_m(200, 0.6)]}, "1X2")
    assert decision.status == "insufficient_data"
    assert "fenêtre" in decision.reason
    print("  [OK] select_candidate_model : moins de min_windows -> insufficient_data")


def test_select_candidate_insufficient_data_small_sample():
    windows = {"elo": [_m(10, 0.6), _m(10, 0.6), _m(10, 0.6)], "xgboost": [_m(5, 0.7), _m(5, 0.7), _m(5, 0.7)]}
    decision = model_selection.select_candidate_model(windows, "1X2", min_sample_size=100)
    assert decision.status == "insufficient_data"
    print("  [OK] select_candidate_model : sample_size < seuil sur toutes les fenêtres -> insufficient_data")


def test_select_candidate_unstable_no_consistent_leader():
    # 3 modèles, chacun meilleur sur exactement 1 fenêtre sur 3 -> aucun n'atteint 50% -> unstable
    windows = {
        "elo":      [_m(100, 0.50), _m(100, 0.90), _m(100, 0.90)],
        "xgboost":  [_m(100, 0.90), _m(100, 0.50), _m(100, 0.90)],
        "lightgbm": [_m(100, 0.90), _m(100, 0.90), _m(100, 0.50)],
    }
    decision = model_selection.select_candidate_model(windows, "1X2", min_sample_size=50)
    assert decision.status == "unstable"
    assert decision.selected_model_type is None
    print("  [OK] select_candidate_model : aucun modèle dominant sur les fenêtres -> unstable")


def test_select_candidate_unstable_high_cv():
    # "elo" gagne les 3 fenêtres (top-rank OK) mais son log_loss varie énormément -> CV trop élevé -> unstable
    windows = {
        "elo":     [_m(100, 0.10), _m(100, 0.90), _m(100, 0.10)],
        "xgboost": [_m(100, 0.50), _m(100, 0.95), _m(100, 0.50)],
    }
    decision = model_selection.select_candidate_model(windows, "1X2", min_sample_size=50, max_log_loss_cv=0.05)
    assert decision.status == "unstable"
    print("  [OK] select_candidate_model : coefficient de variation trop élevé -> unstable même si meilleur rang")


def test_select_candidate_not_significant_no_provider():
    windows = {
        "elo":     [_m(100, 0.50), _m(100, 0.52), _m(100, 0.51)],
        "xgboost": [_m(100, 0.70), _m(100, 0.72), _m(100, 0.71)],
    }
    decision = model_selection.select_candidate_model(windows, "1X2", min_sample_size=50, credibility_pairs_provider=None)
    assert decision.status == "not_significant"
    print("  [OK] select_candidate_model : stable mais aucun provider de crédibilité -> not_significant, jamais sélectionné par défaut")


def test_select_candidate_not_significant_no_clear_winner_in_bootstrap():
    windows = {
        "elo":     [_m(100, 0.50), _m(100, 0.51), _m(100, 0.50)],
        "xgboost": [_m(100, 0.70), _m(100, 0.71), _m(100, 0.70)],
    }
    # paires quasi identiques (delta ~0) -> bootstrap non significatif
    pairs_provider = lambda cand, runner_up: [(0.6, 0.6)] * 60  # noqa: E731
    decision = model_selection.select_candidate_model(windows, "1X2", min_sample_size=50, credibility_pairs_provider=pairs_provider)
    assert decision.status == "not_significant"
    print("  [OK] select_candidate_model : delta bootstrap non significatif -> not_significant")


def test_select_candidate_selected_full_pipeline():
    windows = {
        "elo":     [_m(100, 0.50), _m(100, 0.51), _m(100, 0.50)],
        "xgboost": [_m(100, 0.70), _m(100, 0.71), _m(100, 0.70)],
        "lightgbm": [_m(100, 0.65), _m(100, 0.66), _m(100, 0.65)],
    }
    calls = []

    def pairs_provider(candidate, runner_up):
        calls.append((candidate, runner_up))
        # "elo" systématiquement meilleur (log_loss plus bas) que son dauphin sur 80 matchs -> significatif
        return [(0.75, 0.50)] * 80

    decision = model_selection.select_candidate_model(windows, "1X2", min_sample_size=50, credibility_pairs_provider=pairs_provider)
    assert decision.status == "selected"
    assert decision.selected_model_type == "elo"
    assert decision.runner_up_model_type == "lightgbm"  # 2e meilleur log_loss moyen après elo
    assert calls == [("elo", "lightgbm")], "le provider ne doit être appelé qu'UNE fois, avec le candidat stable et son vrai dauphin"
    assert decision.credibility["significant"] is True
    print("  [OK] select_candidate_model : pipeline complet -> selected, avec le bon dauphin, provider appelé une seule fois")


# ---------------------------------------------------------------------------
# 2. Calibration Engine V1
# ---------------------------------------------------------------------------

def _obs(p_pick, actual, correct, other_key="draw"):
    if correct:
        probs = {"home_win": p_pick, other_key: (1 - p_pick) / 2, "away_win": (1 - p_pick) / 2}
        actual_key = "home_win"
    else:
        probs = {"home_win": p_pick, other_key: (1 - p_pick) * 0.7, "away_win": (1 - p_pick) * 0.3}
        actual_key = other_key
    return {"p_true": probs[actual_key], "probs": probs, "actual": actual_key, "correct": correct}


def test_calibration_engine_insufficient_data():
    train_obs = [_obs(0.6, "home_win", True)] * 5
    test_obs = [_obs(0.6, "home_win", True)] * 5
    result = calibration_engine.evaluate_calibration(train_obs, test_obs, min_sample_size=100)
    assert result.choice == "none"
    assert result.verdict == "INSUFFICIENT_DATA"
    print("  [OK] calibration_engine.evaluate_calibration : échantillon sous le seuil -> INSUFFICIENT_DATA, jamais forcé")


def test_calibration_engine_single_class_never_forced():
    train_obs = [_obs(0.6 + i * 0.001, "home_win", True) for i in range(150)]  # toujours correct -> une seule classe
    test_obs = [_obs(0.6, "home_win", True)] * 150
    result = calibration_engine.evaluate_calibration(train_obs, test_obs, min_sample_size=100)
    assert result.choice == "none"
    print("  [OK] calibration_engine.evaluate_calibration : une seule classe observée sur train -> jamais forcé (choice=none)")


def test_calibration_engine_reproducible_metrics_shape():
    import random
    rng = random.Random(7)
    train_obs = [_obs(0.5 + rng.random() * 0.4, "home_win", rng.random() < 0.55) for _ in range(200)]
    test_obs = [_obs(0.5 + rng.random() * 0.4, "home_win", rng.random() < 0.55) for _ in range(150)]
    result = calibration_engine.evaluate_calibration(train_obs, test_obs, min_sample_size=100)
    assert result.raw_metrics.sample_size == 150
    assert result.choice in ("none", "platt", "isotonic")
    assert result.verdict in ("HELPFUL", "NEUTRAL", "HARMFUL", "INSUFFICIENT_DATA")
    if result.choice != "none":
        assert result.platt_metrics is not None and result.isotonic_metrics is not None
    print("  [OK] calibration_engine.evaluate_calibration : forme du résultat cohérente sur données réalistes")


def test_produce_candidate_probability_none_choice_passthrough():
    from app.ai.arena.calibration_engine import CalibrationResult
    raw = {"home_win": 0.6, "draw": 0.25, "away_win": 0.15}
    fake_result = CalibrationResult(choice="none", verdict="INSUFFICIENT_DATA", raw_metrics=_m(0, None),
                                     platt_metrics=None, isotonic_metrics=None, raw_ece=None, platt_ece=None,
                                     isotonic_ece=None, train_sample_size=0, test_sample_size=0)
    out = calibration_engine.produce_candidate_probability(raw, fake_result, [])
    assert out == raw
    print("  [OK] produce_candidate_probability : choice=none -> probabilité brute inchangée")


def test_produce_candidate_probability_platt_sums_to_one():
    train_obs = [_obs(0.5 + (i % 40) * 0.01, "home_win", (i % 3 != 0)) for i in range(150)]
    from app.ai.arena.calibration_engine import CalibrationResult
    fake_result = CalibrationResult(choice="platt", verdict="HELPFUL", raw_metrics=_m(150, 0.6),
                                     platt_metrics=_m(150, 0.55), isotonic_metrics=_m(150, 0.58),
                                     raw_ece=0.1, platt_ece=0.05, isotonic_ece=0.06, train_sample_size=150, test_sample_size=150)
    raw = {"home_win": 0.7, "draw": 0.2, "away_win": 0.1}
    out = calibration_engine.produce_candidate_probability(raw, fake_result, train_obs)
    assert abs(sum(out.values()) - 1.0) < 1e-9
    print("  [OK] produce_candidate_probability : Platt appliqué à une prédiction unique, distribution valide (somme=1)")


# ---------------------------------------------------------------------------
# 3. Sécurité DB — scripts Phase 6 ne doivent JAMAIS écrire dans les tables
#    partagées, et le mode SHADOW doit être idempotent.
# ---------------------------------------------------------------------------

def _seed_full_fixture(session, n_matches=12, p_elo=0.55, p_xgb=0.60, p_lgb=0.50):
    teams = ["Team A", "Team B", "Team C", "Team D"]
    _seed_matches(session, "Ligue1", teams, n_rounds=6, start_date=date(2025, 1, 1))

    v_elo = _make_version(session, "elo", is_active=True)
    v_xgb = _make_version(session, "xgboost", is_active=True)
    v_lgb = _make_version(session, "lightgbm", is_active=True)

    match_date = date(2025, 6, 1)
    for i in range(n_matches):
        d = match_date + timedelta(days=i)
        home, away = f"H{i}", f"A{i}"
        for mt, vid, p in (("elo", v_elo.id, p_elo + 0.01 * i), ("xgboost", v_xgb.id, p_xgb - 0.01 * i), ("lightgbm", v_lgb.id, p_lgb + 0.02 * (i % 3))):
            _log_resolved(session, mt, vid, d, home, away, p_true=min(max(p, 0.05), 0.95), league="Ligue1")


def _seed_uniform_fixture(session, n_matches, p_elo, p_xgb, p_lgb):
    """Variante de _seed_full_fixture SANS dérive par match (probabilité
    quasi constante par modèle) — nécessaire pour les tests qui ont besoin
    d'un candidat STABLE (coefficient de variation bas) plutôt que
    seulement dominant, par exemple pour exercer le chemin 'selected'."""
    teams = ["Team A", "Team B", "Team C", "Team D"]
    _seed_matches(session, "Ligue1", teams, n_rounds=6, start_date=date(2025, 1, 1))

    v_elo = _make_version(session, "elo", is_active=True)
    v_xgb = _make_version(session, "xgboost", is_active=True)
    v_lgb = _make_version(session, "lightgbm", is_active=True)

    match_date = date(2025, 6, 1)
    for i in range(n_matches):
        d = match_date + timedelta(days=i)
        home, away = f"H{i}", f"A{i}"
        for mt, vid, p in (("elo", v_elo.id, p_elo), ("xgboost", v_xgb.id, p_xgb), ("lightgbm", v_lgb.id, p_lgb)):
            jitter = 0.01 * ((i % 3) - 1)  # léger bruit borné, jamais une dérive cumulative entre fenêtres
            _log_resolved(session, mt, vid, d, home, away, p_true=min(max(p + jitter, 0.05), 0.95), league="Ligue1")


def test_model_selection_research_never_writes_to_any_table():
    _clean_all()
    with Session(engine) as session:
        _seed_full_fixture(session)
        before = _row_counts(session)
        before_decisions = len(session.exec(select(ModelSelectionDecision)).all())
        before_shadow = len(session.exec(select(ShadowSelectionPrediction)).all())

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        model_selection_research.main(n_windows=3, min_sample_size=3, outdir=tmp)

    with Session(engine) as session:
        after = _row_counts(session)
        after_decisions = len(session.exec(select(ModelSelectionDecision)).all())
        after_shadow = len(session.exec(select(ShadowSelectionPrediction)).all())

    assert before == after, "le mode RECHERCHE ne doit écrire dans AUCUNE table (pas même les tables Phase 6)"
    assert before_decisions == after_decisions == 0
    assert before_shadow == after_shadow == 0
    print("  [OK] model_selection_research.main() : aucune écriture DB, même dans les tables Phase 6 (rapport fichier uniquement)")


def test_model_selection_shadow_writes_only_new_tables():
    _clean_all()
    with Session(engine) as session:
        _seed_full_fixture(session)
        before = _row_counts(session)

    model_selection_shadow.main(market="1X2", n_windows=3, min_sample_size=3)

    with Session(engine) as session:
        after = _row_counts(session)
        decisions = session.exec(select(ModelSelectionDecision)).all()
        shadow_rows = session.exec(select(ShadowSelectionPrediction)).all()

    assert before == after, f"le mode SHADOW ne doit jamais écrire dans model_predictions/model_versions/team_ratings : avant={before} après={after}"
    assert len(decisions) == 1, "exactement une ModelSelectionDecision par exécution (marché unique demandé), même en cas de refus"
    print(f"  [OK] model_selection_shadow.main() : model_predictions/model_versions/team_ratings inchangés "
          f"({len(decisions)} décision, {len(shadow_rows)} prédiction(s) shadow)")


def test_model_selection_shadow_decision_always_persisted_even_on_refusal():
    """§8/§4 du prompt : une décision de refus doit être enregistrée, jamais silencieuse."""
    _clean_all()
    with Session(engine) as session:
        _seed_full_fixture(session, n_matches=4)  # trop peu de matchs -> insufficient_data quasi certain

    result = model_selection_shadow.main(market="1X2", n_windows=5, min_sample_size=100)

    with Session(engine) as session:
        decisions = session.exec(select(ModelSelectionDecision)).all()
    assert len(decisions) == 1
    assert decisions[0].status in ("insufficient_data", "unstable", "not_significant", "selected")
    assert decisions[0].reason  # jamais une raison vide
    print(f"  [OK] model_selection_shadow.main() : décision de refus ({decisions[0].status}) persistée avec une raison explicite")


def test_shadow_selection_prediction_idempotent_across_reruns():
    _clean_all()
    with Session(engine) as session:
        # 40 matchs, 4 fenêtres de 10 -> 3 fenêtres de stabilité + 1 fenêtre de
        # test, "elo" nettement et constamment meilleur (p~0.85 vs ~0.30-0.35,
        # probabilité quasi constante par modèle -> CV bas) -> devrait franchir
        # les 3 portes et produire un "selected" réel.
        _seed_uniform_fixture(session, n_matches=40, p_elo=0.85, p_xgb=0.30, p_lgb=0.32)

    result_1 = model_selection_shadow.main(market="1X2", n_windows=4, min_sample_size=5)
    assert result_1["decision_status"] == "selected", (
        f"fixture attendue pour produire 'selected' (a produit {result_1['decision_status']}) "
        "-- ce test n'exercerait pas l'idempotence sans prédictions shadow réelles"
    )
    with Session(engine) as session:
        first_count = len(session.exec(select(ShadowSelectionPrediction)).all())
    assert first_count > 0, "la fixture doit produire au moins une prédiction shadow pour que ce test soit utile"

    model_selection_shadow.main(market="1X2", n_windows=4, min_sample_size=5)
    with Session(engine) as session:
        second_count = len(session.exec(select(ShadowSelectionPrediction)).all())
        all_rows = session.exec(select(ShadowSelectionPrediction)).all()
        keys = [(r.league, r.match_date, r.home_team, r.away_team, r.market) for r in all_rows]

    assert second_count == first_count, "une seconde exécution ne doit jamais dupliquer une prédiction shadow déjà écrite (idempotence)"
    assert len(keys) == len(set(keys)), "aucun doublon sur (league, match_date, home_team, away_team, market)"
    print(f"  [OK] ShadowSelectionPrediction : idempotent sur exécutions répétées ({first_count} lignes, jamais dupliquées)")


def test_resolve_pending_never_mutates_already_resolved_row():
    _clean_all()
    with Session(engine) as session:
        _seed_full_fixture(session, n_matches=4)
        decision = ModelSelectionDecision(
            run_id="test-run", mode="shadow", market="1X2", as_of=date(2025, 6, 10), status="selected",
            selected_model_type="elo", windows_evaluated=3, reason="test fixture",
        )
        session.add(decision)
        session.commit()
        session.refresh(decision)

        resolved_row = ShadowSelectionPrediction(
            selection_decision_id=decision.id, league="Ligue1", match_date=date(2025, 6, 1),
            home_team="H0", away_team="A0", market="1X2", candidate_model_type="elo",
            candidate_probs=json.dumps({"home_win": 0.6, "draw": 0.25, "away_win": 0.15}),
            status="resolved", result_home_goals=2, result_away_goals=0, candidate_correct=True,
            resolved_at=datetime.now(timezone.utc),
        )
        session.add(resolved_row)
        session.commit()
        resolved_id = resolved_row.id

        pending_row = ShadowSelectionPrediction(
            selection_decision_id=decision.id, league="Ligue1", match_date=date(2025, 6, 2),
            home_team="H1", away_team="A1", market="1X2", candidate_model_type="elo",
            candidate_probs=json.dumps({"home_win": 0.6, "draw": 0.25, "away_win": 0.15}),
            status="pending",
        )
        session.add(pending_row)
        # resolve_pending_shadow_predictions résout via la table `match` (résultats
        # canoniques, indépendants du modèle) -- _seed_full_fixture ne seed QUE des
        # ModelPrediction pour "H1"/"A1" (pas de ligne Match) : on l'ajoute ici
        # explicitement pour que la résolution trouve un résultat réel.
        session.add(Match(league="Ligue1", date=datetime.combine(date(2025, 6, 2), datetime.min.time()),
                           home_team="H1", away_team="A1", home_goals=3, away_goals=1))
        session.commit()

        n = model_selection_shadow.resolve_pending_shadow_predictions(session)

    with Session(engine) as session:
        still_resolved = session.get(ShadowSelectionPrediction, resolved_id)
        assert still_resolved.status == "resolved"
        assert still_resolved.result_home_goals == 2 and still_resolved.result_away_goals == 0
        assert still_resolved.candidate_correct is True  # jamais réécrit

        now_resolved = session.exec(
            select(ShadowSelectionPrediction).where(ShadowSelectionPrediction.home_team == "H1")
        ).first()
        assert now_resolved.status == "resolved"
        assert now_resolved.result_home_goals is not None

    assert n == 1, "seule la ligne pending doit avoir été résolue"
    print("  [OK] resolve_pending_shadow_predictions : ligne déjà résolue jamais mutée, seule la ligne pending est traitée")


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
