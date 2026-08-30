"""
test_research_ensemble.py — Phase 5.7 : tests de app/ai/arena/research.py et
scripts/research_ensemble.py.

Base isolée dédiée (jamais api/app.db) — même précaution que
test_ensemble_engine.py (voir _test_support.py). Aucune de ces suites
n'écrit dans model_predictions/model_versions/team_ratings en dehors des
fixtures explicitement construites par les tests eux-mêmes.

Usage : python api/test_research_ensemble.py
"""

import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_research_ensemble.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion, TeamRating
from app.ai.arena.prediction_logging import PredictionRecord, log_prediction, resolve_prediction
from app.ai.arena.ensemble import WeightResult, ModelWeight, InverseLogLossStrategy
from app.ai.arena.schemas import MarketMetrics
from app.ai.arena import research

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import research_ensemble  # noqa: E402
import walk_forward_ensemble as wfe  # noqa: E402

init_db()


def _clean_all():
    with Session(engine) as session:
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


def _row_counts(session):
    return {
        "model_predictions": len(session.exec(select(ModelPrediction)).all()),
        "model_versions": len(session.exec(select(ModelVersion)).all()),
        "team_ratings": len(session.exec(select(TeamRating)).all()),
    }


# ---------------------------------------------------------------------------
# 1. SimpleAverageStrategy — scoring + renormalisation (§32)
# ---------------------------------------------------------------------------

def test_simple_average_strategy_equal_scores():
    metrics = {
        "elo": MarketMetrics(accuracy=0.5, log_loss=0.9, brier_score=0.6, sample_size=100, correct_predictions=50),
        "xgboost": MarketMetrics(accuracy=0.6, log_loss=0.5, brier_score=0.4, sample_size=100, correct_predictions=60),
    }
    scores = research.SimpleAverageStrategy().compute_scores(metrics)
    assert scores == {"elo": 1.0, "xgboost": 1.0}, "score brut identique pour tous les modèles éligibles, quelle que soit leur performance"
    print("  [OK] SimpleAverageStrategy : score=1.0 pour tout modèle avec des métriques disponibles")


def test_simple_average_renormalizes_on_partial_model_availability():
    """combine() (ensemble.py, non modifié) doit renormaliser sur les seuls
    modèles ayant RÉELLEMENT une probabilité pour CE match, même avec des
    poids historiques égaux à 3 modèles (§12 du prompt)."""
    from app.ai.arena.ensemble import combine

    weight_result = WeightResult(
        market="1X2", until=date(2026, 1, 1), strategy="simple_average",
        weights={
            "elo": ModelWeight("elo", "v1", 1, weight=1 / 3, log_loss=0.6, sample_size=100),
            "xgboost": ModelWeight("xgboost", "v1", 2, weight=1 / 3, log_loss=0.6, sample_size=100),
            "lightgbm": ModelWeight("lightgbm", "v1", 3, weight=1 / 3, log_loss=0.6, sample_size=100),
        },
    )
    # Seul "elo" a réellement prédit ce match précis -> renormalisation à 1.0, jamais un poids fabriqué pour xgboost/lightgbm.
    combined = combine({"elo": {"home_win": 0.7, "draw": 0.2, "away_win": 0.1}}, weight_result)
    assert combined is not None and combined.models_used == 1 and combined.degraded is True
    assert combined.weights_used == {"elo": 1.0}
    print("  [OK] Simple Average + combine() : renormalise honnêtement sur le seul modèle réellement disponible pour ce match")


# ---------------------------------------------------------------------------
# 2. Statistiques — Wilson / bootstrap / McNemar (§20/§21/§22)
# ---------------------------------------------------------------------------

def test_wilson_interval_reasonable_bounds():
    lo, hi = research.wilson_interval(50, 100)
    assert lo is not None and hi is not None
    assert lo < 0.5 < hi, "l'intervalle doit contenir la proportion observée"
    lo_small, hi_small = research.wilson_interval(5, 10)
    assert (hi_small - lo_small) > (hi - lo), "un échantillon plus petit doit produire un intervalle plus large"
    assert research.wilson_interval(0, 0) == (None, None)
    print("  [OK] wilson_interval : bornes cohérentes, plus large sur petit échantillon, None si n=0")


def test_bootstrap_paired_diff_reproducible_and_zero_when_identical():
    pairs = [(0.5, 0.5)] * 50
    result_a = research.bootstrap_paired_diff(pairs, n_boot=500, seed=42)
    result_b = research.bootstrap_paired_diff(pairs, n_boot=500, seed=42)
    assert result_a == result_b, "même seed -> résultat strictement identique (§22 reproductibilité)"
    assert result_a["mean_diff"] == 0.0
    assert result_a["significant"] is False, "aucune différence réelle -> IC doit contenir 0"
    print("  [OK] bootstrap_paired_diff : reproductible (seed fixe) et non significatif quand a==b partout")


def test_bootstrap_paired_diff_detects_consistent_difference():
    pairs = [(0.9, 0.5)] * 50  # a systématiquement pire (log_loss plus élevé) que b
    result = research.bootstrap_paired_diff(pairs, n_boot=1000, seed=1)
    assert result["mean_diff"] == 0.4
    assert result["significant"] is True
    assert result["ci_low"] > 0
    print("  [OK] bootstrap_paired_diff : détecte une différence systématique comme significative")


def test_mcnemar_test_no_significance_when_symmetric():
    result = research.mcnemar_test(b=10, c=10)
    assert result["significant"] is False
    assert result["p_value"] > 0.05
    print("  [OK] mcnemar_test : discordances symétriques -> non significatif")


def test_mcnemar_test_significant_when_asymmetric():
    result = research.mcnemar_test(b=40, c=5)
    assert result["significant"] is True
    print("  [OK] mcnemar_test : forte asymétrie -> significatif")


# ---------------------------------------------------------------------------
# 3. Diagnostic des poids (§28)
# ---------------------------------------------------------------------------

def test_weight_diagnostics_single_vs_multi_model_fraction():
    wr_single = WeightResult(market="1X2", until=date(2026, 1, 1),
                              weights={"elo": ModelWeight("elo", "v1", 1, weight=1.0, log_loss=0.6, sample_size=100)})
    wr_multi = WeightResult(market="1X2", until=date(2026, 1, 2), weights={
        "elo": ModelWeight("elo", "v1", 1, weight=0.5, log_loss=0.6, sample_size=100),
        "xgboost": ModelWeight("xgboost", "v1", 2, weight=0.5, log_loss=0.6, sample_size=100),
    })
    wr_empty = WeightResult(market="1X2", until=date(2026, 1, 3), weights={})

    diag = research.weight_diagnostics([wr_single, wr_single, wr_single, wr_multi, wr_empty])
    assert diag["single_model_fold_count"] == 3
    assert diag["multi_model_fold_count"] == 1
    assert diag["zero_model_fold_count"] == 1
    assert diag["single_model_fraction"] == round(3 / 5, 4)
    assert diag["per_model"]["elo"]["count"] == 4
    print("  [OK] weight_diagnostics : quantifie correctement le mode dégradé (single-model) par stratégie")


# ---------------------------------------------------------------------------
# 4. Calibration — jamais forcée, jamais ajustée sur test (§16/§32)
# ---------------------------------------------------------------------------

def test_calibration_not_forced_on_homogeneous_sample():
    """Si train+validation n'a qu'une seule classe observée (toujours
    correct, ou toujours faux), aucune calibration n'est forcée (§16) :
    les probabilités de test doivent revenir INCHANGÉES."""
    train_conf = [0.6, 0.7, 0.8, 0.9]
    train_correct = [True, True, True, True]  # une seule classe -> pas assez d'information pour calibrer
    test_conf = [0.55, 0.95]
    assert research.platt_calibrate(train_conf, train_correct, test_conf) == test_conf
    assert research.isotonic_calibrate(train_conf, train_correct, test_conf) == test_conf
    print("  [OK] platt_calibrate/isotonic_calibrate : jamais forcés quand train+validation n'a qu'une seule classe")


def test_apply_pick_calibration_preserves_pick_and_probability_mass():
    observations = [{"p_true": 0.6, "probs": {"home_win": 0.6, "draw": 0.25, "away_win": 0.15}, "actual": "home_win", "correct": True}]
    calibrated = research.apply_pick_calibration(observations, [0.5])
    new_probs = calibrated[0]["probs"]
    assert abs(sum(new_probs.values()) - 1.0) < 1e-9, "la distribution recalibrée doit toujours sommer à 1"
    assert new_probs["home_win"] == 0.5, "la probabilité du pick doit être remplacée par la valeur recalibrée"
    assert calibrated[0]["p_true"] == new_probs["home_win"], "actual == pick ici -> p_true doit suivre la nouvelle probabilité du pick"
    ratio_draw_away = new_probs["draw"] / new_probs["away_win"]
    assert abs(ratio_draw_away - (0.25 / 0.15)) < 1e-9, "la masse restante doit être redistribuée proportionnellement aux probabilités d'origine"
    print("  [OK] apply_pick_calibration : pick recalibré, masse restante redistribuée proportionnellement, somme=1")


def test_expected_calibration_error_zero_when_perfectly_calibrated():
    bins = [
        {"bin_range": [0.4, 0.6], "predicted_confidence_avg": 0.5, "observed_frequency": 0.5, "count": 20},
        {"bin_range": [0.8, 1.0], "predicted_confidence_avg": 0.9, "observed_frequency": 0.9, "count": 30},
    ]
    assert research.expected_calibration_error(bins) == 0.0
    assert research.expected_calibration_error(None) is None
    print("  [OK] expected_calibration_error : 0 quand parfaitement calibré, None si pas de diagramme")


# ---------------------------------------------------------------------------
# 5. Dixon-Coles walk-forward — anti-fuite (§4/§21/§32)
# ---------------------------------------------------------------------------

def _seed_matches(session, league, teams, n_rounds, start_date):
    """Génère un petit historique round-robin déterministe pour `league` —
    suffisant pour que _FastDixonColesL2 converge (régularisé), sans viser
    le volume réel de production."""
    d = start_date
    for r in range(n_rounds):
        for i in range(len(teams)):
            home, away = teams[i], teams[(i + 1) % len(teams)]
            session.add(Match(league=league, date=datetime.combine(d, datetime.min.time()),
                               home_team=home, away_team=away, home_goals=(r + i) % 3, away_goals=(r + i + 1) % 3))
            d += timedelta(days=1)
    session.commit()


def test_dixon_coles_walk_forward_respects_leakage_cutoff():
    """Aucun match du fold lui-même (ni d'après) ne doit jamais entrer dans
    l'historique d'entraînement Dixon-Coles d'un fold — même principe que
    test_compute_market_weights_respects_leakage_cutoff (test_ensemble_engine.py)."""
    _clean_all()
    with Session(engine) as session:
        teams = ["Team A", "Team B", "Team C", "Team D"]
        _seed_matches(session, "TestLeague", teams, n_rounds=5, start_date=date(2026, 1, 1))

        all_rows = session.exec(select(Match).where(Match.league == "TestLeague").order_by(Match.date)).all()
        cutoff_row = all_rows[14]
        future_row = all_rows[15]
        fold_keys = [("TestLeague", future_row.date.date(), future_row.home_team, future_row.away_team)]

        dcwf = research.build_dixon_coles_walk_forward([fold_keys], min_train_matches=5)
        coverage = dcwf.coverage[0]
        expected_train_count = sum(1 for r in all_rows if r.date.date() <= cutoff_row.date.date())
        assert coverage.train_matches == expected_train_count, "l'entraînement doit s'arrêter STRICTEMENT à until (jour précédant le fold), jamais après"
        assert coverage.until == (future_row.date.date() - timedelta(days=1)).isoformat()
    print("  [OK] Dixon-Coles walk-forward : coupure until respectée (aucune fuite du fold évalué dans son propre entraînement)")


def test_dixon_coles_walk_forward_unknown_team_is_skipped_not_fabricated():
    _clean_all()
    with Session(engine) as session:
        teams = ["Team A", "Team B", "Team C", "Team D"]
        _seed_matches(session, "TestLeague", teams, n_rounds=5, start_date=date(2026, 1, 1))

        last_date = session.exec(select(Match).where(Match.league == "TestLeague").order_by(Match.date.desc())).first().date.date()
        future_date = last_date + timedelta(days=10)
        # "Nouvelle Équipe" n'a jamais joué dans le train tronqué -> doit être skip, jamais fabriqué.
        session.add(Match(league="TestLeague", date=datetime.combine(future_date, datetime.min.time()),
                           home_team="Nouvelle Équipe", away_team="Team A", home_goals=1, away_goals=1))
        session.commit()

        fold_keys = [("TestLeague", future_date, "Nouvelle Équipe", "Team A")]
        dcwf = research.build_dixon_coles_walk_forward([fold_keys], min_train_matches=5)
        assert len(dcwf.predictions) == 0
        assert dcwf.skip_reasons.get("unknown_team", 0) == 1
    print("  [OK] Dixon-Coles walk-forward : équipe inconnue du train tronqué -> skip compté, jamais une probabilité fabriquée")


def test_dixon_coles_walk_forward_insufficient_history_is_documented_not_simulated():
    _clean_all()
    with Session(engine) as session:
        _seed_matches(session, "TestLeague", ["Team A", "Team B"], n_rounds=2, start_date=date(2026, 1, 1))
        rows = session.exec(select(Match).where(Match.league == "TestLeague").order_by(Match.date)).all()
        future = rows[-1].date.date() + timedelta(days=5)
        session.add(Match(league="TestLeague", date=datetime.combine(future, datetime.min.time()),
                           home_team="Team A", away_team="Team B", home_goals=1, away_goals=0))
        session.commit()

        fold_keys = [("TestLeague", future, "Team A", "Team B")]
        dcwf = research.build_dixon_coles_walk_forward([fold_keys], min_train_matches=200)
        assert len(dcwf.predictions) == 0
        assert dcwf.coverage[0].available is False
        assert "non disponible pour ce fold" in dcwf.coverage[0].reason
    print("  [OK] Dixon-Coles walk-forward : historique insuffisant -> documenté explicitement, jamais simulé")


# ---------------------------------------------------------------------------
# 6. Fold construction — chronologique, non chevauchant (réutilise wfe)
# ---------------------------------------------------------------------------

def test_folds_are_chronological_and_non_overlapping():
    keys = [("Ligue1", date(2026, 1, i), f"H{i}", f"A{i}") for i in range(1, 31)]
    folds = wfe._make_folds(keys, n_folds=3)
    seen = set()
    for fold in folds:
        for k in fold:
            assert k not in seen, "un même match ne doit jamais apparaître dans deux folds"
            seen.add(k)
    all_dates = [k[1] for fold in folds for k in fold]
    assert all_dates == sorted(all_dates), "l'ordre chronologique global doit être préservé à travers les folds"
    print("  [OK] _make_folds (réutilisé) : folds chronologiques et strictement non chevauchants")


# ---------------------------------------------------------------------------
# 7. Sécurité DB — research_ensemble.main() ne doit JAMAIS écrire dans les
#    tables partagées (§27/§33 du prompt) — testé sur la base ISOLÉE de ce
#    module, jamais sur api/app.db.
# ---------------------------------------------------------------------------

def _seed_full_fixture(session):
    """Fixture partagée par les tests bout-en-bout (sécurité DB, reproductibilité) :
    12 matchs communs elo/xgboost/lightgbm (résolus, source=backtest) + un
    historique Match suffisant pour que Dixon-Coles walk-forward tourne
    (même s'il tombe en 'historique insuffisant', §4 — pas le sujet ici)."""
    teams = ["Team A", "Team B", "Team C", "Team D"]
    _seed_matches(session, "Ligue1", teams, n_rounds=6, start_date=date(2025, 1, 1))

    v_elo = _make_version(session, "elo", is_active=True)
    v_xgb = _make_version(session, "xgboost", is_active=True)
    v_lgb = _make_version(session, "lightgbm", is_active=True)

    match_date = date(2025, 6, 1)
    for i in range(12):
        d = match_date + timedelta(days=i)
        home, away = f"H{i}", f"A{i}"
        # p_true varie par match (pas une constante) pour que les modèles se
        # départagent réellement et que best_individual/significance ne soient
        # pas triviaux -- sinon le bug d'ordre non déterministe (set() itéré)
        # ne se serait jamais manifesté.
        for mt, vid, p in (("elo", v_elo.id, 0.55 + 0.01 * i), ("xgboost", v_xgb.id, 0.60 - 0.01 * i), ("lightgbm", v_lgb.id, 0.5 + 0.02 * (i % 3))):
            _log_resolved(session, mt, vid, d, home, away, p_true=p, league="Ligue1")


def test_research_ensemble_never_writes_to_shared_tables(tmp_path=None):
    import tempfile

    _clean_all()
    with Session(engine) as session:
        _seed_full_fixture(session)
        before = _row_counts(session)

    with tempfile.TemporaryDirectory() as tmp_outdir:
        research_ensemble.main(n_folds=3, min_sample_size=3, outdir=tmp_outdir)

    with Session(engine) as session:
        after = _row_counts(session)

    assert before == after, f"research_ensemble.main() a modifié les tables partagées : avant={before} après={after}"
    print("  [OK] research_ensemble.main() : aucune écriture dans model_predictions/model_versions/team_ratings (avant == après)")


def test_research_ensemble_is_deterministic_across_runs():
    """§22 : deux exécutions identiques doivent produire des résultats
    numériques identiques -- régression pour un bug réel trouvé en
    exécutant ce script sur api/app.db : un `set()` de clés de match itéré
    directement (au lieu de trié) alimentait bootstrap_paired_diff dans un
    ORDRE non déterministe (hash randomisé par processus), faisant varier
    l'IC bootstrap d'un run à l'autre malgré le seed fixe. Corrigé en triant
    l'intersection avant de construire les paires -- ce test l'empêche de
    revenir silencieusement."""
    import tempfile

    _clean_all()
    with Session(engine) as session:
        _seed_full_fixture(session)

    with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
        result_a = research_ensemble.main(n_folds=3, min_sample_size=3, outdir=tmp_a)
        result_b = research_ensemble.main(n_folds=3, min_sample_size=3, outdir=tmp_b)

    ignored = {"run_id", "generated_at"}
    for key in result_a:
        if key in ignored:
            continue
        assert result_a[key] == result_b[key], f"non-déterminisme détecté sur result['{key}'] : {result_a[key]!r} != {result_b[key]!r}"
    assert result_a.get("significance"), "cette fixture doit produire une comparaison de significativité non vide pour être un test utile"
    print("  [OK] research_ensemble.main() : résultat strictement identique sur deux exécutions (même seed, mêmes données)")


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
