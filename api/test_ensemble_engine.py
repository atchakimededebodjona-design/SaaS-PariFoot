"""
test_ensemble_engine.py — Phase 7 : contrat commun (models_common.py),
Model Orchestrator (orchestrator.py), Ensemble Engine (ensemble.py).

Base isolée dédiée (jamais api/app.db) — même précaution que les suites de
tests précédentes (voir _test_support.py).

Usage : python api/test_ensemble_engine.py
"""

import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db, register_and_login, activate_subscription

DB_PATH = configure_test_env("test_ensemble_engine.db")

from fastapi.testclient import TestClient
from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion, TeamRating
from app.ai.arena.prediction_logging import PredictionRecord, log_prediction, resolve_prediction
from app.ai.arena.models_common import (
    MatchContext,
    PredictionOutcome,
    PredictionModel,
    DixonColesPredictionModel,
    EloPredictionModel,
    XGBoostPredictionModel,
    LightGBMPredictionModel,
    normalize_market,
)
from app.ai.arena.orchestrator import ModelOrchestrator
from app.ai.arena.ensemble import (
    compute_market_weights,
    combine,
    compute_confidence,
    build_live_ensemble,
    WeightResult,
    ModelWeight,
    InverseLogLossStrategy,
    SoftmaxLogLossStrategy,
    BrierStrategy,
    HybridStrategy,
)
from app.ai.arena.schemas import MarketMetrics
from main import app  # noqa: E402  (importé après configure_test_env, comme les autres suites API)

init_db()

MATCH_DATE = date(2026, 6, 1)
AUTH_HEADER: dict = {}


def _clean_all():
    with Session(engine) as session:
        for row in session.exec(select(ModelPrediction)).all():
            session.delete(row)
        for row in session.exec(select(TeamRating)).all():
            session.delete(row)
        for v in session.exec(select(ModelVersion)).all():
            session.delete(v)
        session.commit()


def _make_version(session, model_type: str, is_active: bool = False, config: str | None = None, name: str | None = None) -> ModelVersion:
    v = ModelVersion(
        name=name or f"test-{model_type}-{datetime.now(timezone.utc).timestamp()}",
        model_type=model_type, trained_at=datetime.now(timezone.utc), is_active=is_active, config=config,
    )
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


def _log_resolved(session, model_type, version_id, match_date, home_team, away_team, p_true, league="Ligue1", source="backtest"):
    """Loggue une prédiction 1X2 dont l'issue réelle est TOUJOURS 'home' avec
    probabilité p_true — permet de fixer un log_loss exact par construction
    (log_loss = -log(p_true)) plutôt que de deviner une valeur après coup."""
    other = (1.0 - p_true) / 2
    record = PredictionRecord(
        league=league, match_date=match_date, home_team=home_team, away_team=away_team,
        model_type=model_type, prob_home=p_true, prob_draw=other, prob_away=other, source=source,
    )
    row = log_prediction(session, record, version_id)
    resolve_prediction(row, 2, 0)  # domicile gagne 2-0 -> pick "home" toujours correct
    session.add(row)
    session.commit()
    return row


# ---------------------------------------------------------------------------
# 1. Interface — contrat commun
# ---------------------------------------------------------------------------

def test_all_models_share_common_interface():
    for cls in (DixonColesPredictionModel, EloPredictionModel, XGBoostPredictionModel, LightGBMPredictionModel):
        assert issubclass(cls, PredictionModel)
    print("  [OK] les 4 classes respectent le contrat commun PredictionModel")


def test_dixon_coles_success_returns_full_market_record():
    class _FakeLeagueModel:
        teams = ["Paris SG", "Marseille"]
        attack = {"Paris SG": 1.0, "Marseille": 0.5}

        def predict_1x2(self, h, a):
            return {"home_win": 0.6, "draw": 0.25, "away_win": 0.15}

        def predict_over_under(self, h, a, line=2.5):
            return {"line": line, "over": 0.55, "under": 0.45}

        def predict_btts(self, h, a):
            return {"yes": 0.5, "no": 0.5}

    _clean_all()
    with Session(engine) as session:
        model = DixonColesPredictionModel({"Ligue1": _FakeLeagueModel()})
        ctx = MatchContext("Ligue1", "Paris SG", "Marseille", MATCH_DATE)
        outcome = model.predict(session, ctx)

        assert outcome.status == "ok"
        assert outcome.model_version_id is not None
        assert outcome.record.prob_home == 0.6 and outcome.record.prob_btts_yes == 0.5
    print("  [OK] Dixon-Coles -> status=ok, 3 marchés renseignés, ModelVersion auto-créée")


def test_dixon_coles_unknown_team_is_unavailable_not_error():
    class _FakeLeagueModel:
        teams = ["Paris SG"]
        attack = {"Paris SG": 1.0}

        def predict_1x2(self, h, a):
            raise KeyError(a)

        def predict_over_under(self, h, a, line=2.5):
            raise KeyError(a)

        def predict_btts(self, h, a):
            raise KeyError(a)

    _clean_all()
    with Session(engine) as session:
        model = DixonColesPredictionModel({"Ligue1": _FakeLeagueModel()})
        ctx = MatchContext("Ligue1", "Paris SG", "Equipe Inconnue", MATCH_DATE)
        outcome = model.predict(session, ctx)
        assert outcome.status == "unavailable"
        assert outcome.record is None
    print("  [OK] équipe inconnue -> status=unavailable (jamais 'error' ni prédiction fabriquée)")


def test_elo_no_active_version_is_unavailable():
    _clean_all()
    with Session(engine) as session:
        model = EloPredictionModel()
        outcome = model.predict(session, MatchContext("Ligue1", "A", "B", MATCH_DATE))
        assert outcome.status == "unavailable"
        assert "active" in outcome.reason.lower()
    print("  [OK] aucune version Elo active -> unavailable, raison explicite")


def test_elo_active_version_without_config_is_unavailable():
    _clean_all()
    with Session(engine) as session:
        _make_version(session, "elo", is_active=True, config=None)
        model = EloPredictionModel()
        outcome = model.predict(session, MatchContext("Ligue1", "A", "B", MATCH_DATE))
        assert outcome.status == "unavailable"
        assert outcome.model_version_id is not None
    print("  [OK] version Elo active mais sans config (créée avant Phase 7) -> unavailable, jamais fabriqué")


def test_elo_active_version_with_config_predicts():
    import json
    _clean_all()
    with Session(engine) as session:
        config = json.dumps({"home_advantage": 60.0, "k": 32.0, "leagues": {"Ligue1": {"c": 0.3, "scale": 200.0}}})
        version = _make_version(session, "elo", is_active=True, config=config)
        session.add(TeamRating(team="Paris SG", league="Ligue1", attack=1600.0, defense=0.0, model_version_id=version.id))
        session.add(TeamRating(team="Marseille", league="Ligue1", attack=1500.0, defense=0.0, model_version_id=version.id))
        session.commit()

        model = EloPredictionModel()
        outcome = model.predict(session, MatchContext("Ligue1", "Paris SG", "Marseille", MATCH_DATE))
        assert outcome.status == "ok"
        r = outcome.record
        assert abs((r.prob_home + r.prob_draw + r.prob_away) - 1.0) < 1e-6
        assert r.prob_home > r.prob_away  # rating domicile plus haut + home_advantage -> favori logique
        assert r.prob_btts_yes is None, "Elo ne modélise jamais BTTS — jamais fabriqué"
    print(f"  [OK] Elo actif+config -> prédiction 1X2 cohérente (home={r.prob_home:.3f}), BTTS/O-U=None")


def test_elo_unseen_team_falls_back_to_initial_rating():
    import json
    _clean_all()
    with Session(engine) as session:
        config = json.dumps({"home_advantage": 60.0, "k": 32.0, "leagues": {"Ligue1": {"c": 0.3, "scale": 200.0}}})
        version = _make_version(session, "elo", is_active=True, config=config)
        # Aucun TeamRating pour ces deux équipes -> rating initial (1500) des deux côtés.
        model = EloPredictionModel()
        outcome = model.predict(session, MatchContext("Ligue1", "Nouvelle A", "Nouvelle B", MATCH_DATE))
        assert outcome.status == "ok"
        # Seule la home_advantage différencie les deux -> home_win > away_win, jamais une erreur.
        assert outcome.record.prob_home > outcome.record.prob_away
    print("  [OK] équipes jamais vues -> rating initial (1500), pas de crash")


def test_xgboost_and_lightgbm_without_active_version_are_unavailable():
    """Depuis la Phase 8, XGBoost/LightGBM PEUVENT être live-servables (voir
    api/test_ml_live_serving.py pour le roundtrip complet train->save->load->
    predict) — mais restent honnêtement 'unavailable' tant qu'aucune
    ModelVersion active avec artefact/config exploitable n'existe, exactement
    comme Elo (jamais un statut différent pour la même situation)."""
    _clean_all()
    for cls in (XGBoostPredictionModel, LightGBMPredictionModel):
        with Session(engine) as session:
            outcome = cls({}).predict(session, MatchContext("Ligue1", "A", "B", MATCH_DATE))
            assert outcome.status == "unavailable"
            assert "active" in outcome.reason.lower()
    print("  [OK] XGBoost/LightGBM sans version active -> unavailable, raison explicite "
          "(disponibilité LIVE réelle testée séparément dans test_ml_live_serving.py)")


# ---------------------------------------------------------------------------
# 2. Probabilités — validation/normalisation (§6)
# ---------------------------------------------------------------------------

def test_normalize_market_exact_sum_unchanged():
    probs = {"home_win": 0.5, "draw": 0.3, "away_win": 0.2}
    result = normalize_market(probs, "1X2", "test")
    assert result == probs
    print("  [OK] somme déjà = 1 -> valeurs inchangées")


def test_normalize_market_small_drift_normalized():
    probs = {"home_win": 0.501, "draw": 0.3, "away_win": 0.2}  # somme = 1.001
    result = normalize_market(probs, "1X2", "test")
    assert result is not None
    assert abs(sum(result.values()) - 1.0) < 1e-9
    print(f"  [OK] léger écart (1.001) -> normalisé, somme exacte = {sum(result.values())}")


def test_normalize_market_out_of_range_rejected():
    probs = {"home_win": 1.5, "draw": -0.3, "away_win": -0.2}
    result = normalize_market(probs, "1X2", "test")
    assert result is None
    print("  [OK] valeur hors [0,1] -> rejetée (None), jamais forcée")


def test_normalize_market_grossly_wrong_sum_rejected():
    probs = {"home_win": 0.9, "draw": 0.9, "away_win": 0.9}  # somme = 2.7
    result = normalize_market(probs, "1X2", "test")
    assert result is None
    print("  [OK] somme très éloignée de 1 -> rejetée (None), jamais renormalisée en silence")


def test_normalize_market_btts_two_way():
    probs = {"yes": 0.55, "no": 0.44}  # somme = 0.99
    result = normalize_market(probs, "BTTS", "test")
    assert result is not None and abs(sum(result.values()) - 1.0) < 1e-9
    print("  [OK] marché à 2 issues (BTTS) -> même règle, somme normalisée à 1")


# ---------------------------------------------------------------------------
# 3. Orchestrateur — isolation des échecs (§9)
# ---------------------------------------------------------------------------

class _FakeModel(PredictionModel):
    def __init__(self, model_type, outcome_or_exc):
        self.model_type = model_type
        self._outcome_or_exc = outcome_or_exc

    def predict(self, session, ctx):
        if isinstance(self._outcome_or_exc, Exception):
            raise self._outcome_or_exc
        return self._outcome_or_exc

    def check_availability(self, session):
        from app.ai.arena.models_common import AvailabilityCheck
        return AvailabilityCheck(live_available=not isinstance(self._outcome_or_exc, Exception))


def _ok_outcome(model_type, prob_home=0.5, prob_draw=0.3, prob_away=0.2):
    record = PredictionRecord(
        league="Ligue1", match_date=MATCH_DATE, home_team="A", away_team="B",
        model_type=model_type, prob_home=prob_home, prob_draw=prob_draw, prob_away=prob_away, source="live",
    )
    return PredictionOutcome(model_type, "ok", model_version_id=None, record=record)


def test_orchestrator_all_four_succeed():
    _clean_all()
    models = [_FakeModel(mt, _ok_outcome(mt)) for mt in ("dixon_coles", "elo", "xgboost", "lightgbm")]
    # model_version_id=None pour ces fakes -> log_prediction échouerait (FK) ; persist=False ici.
    with Session(engine) as session:
        results = ModelOrchestrator(models).predict_all(session, MatchContext("Ligue1", "A", "B", MATCH_DATE), persist=False)
        assert set(results.keys()) == {"dixon_coles", "elo", "xgboost", "lightgbm"}
        assert all(o.status == "ok" for o in results.values())
    print("  [OK] 4/4 modèles disponibles -> 4 résultats status=ok")


def test_orchestrator_one_model_raises_others_still_succeed():
    _clean_all()
    models = [
        _FakeModel("dixon_coles", _ok_outcome("dixon_coles")),
        _FakeModel("elo", RuntimeError("panne simulée")),
        _FakeModel("xgboost", _ok_outcome("xgboost")),
        _FakeModel("lightgbm", _ok_outcome("lightgbm")),
    ]
    with Session(engine) as session:
        results = ModelOrchestrator(models).predict_all(session, MatchContext("Ligue1", "A", "B", MATCH_DATE), persist=False)
        assert results["elo"].status == "error"
        assert results["dixon_coles"].status == "ok"
        assert results["xgboost"].status == "ok"
        assert results["lightgbm"].status == "ok"
    print("  [OK] 1 modèle plante -> les 3 autres répondent quand même (status=ok), elo -> error typé")


def test_orchestrator_zero_available_never_crashes():
    _clean_all()
    models = [_FakeModel(mt, PredictionOutcome(mt, "unavailable", reason="test")) for mt in ("dixon_coles", "elo", "xgboost", "lightgbm")]
    with Session(engine) as session:
        results = ModelOrchestrator(models).predict_all(session, MatchContext("Ligue1", "A", "B", MATCH_DATE), persist=False)
        assert all(o.status == "unavailable" for o in results.values())
    print("  [OK] 0/4 modèles disponibles -> ne plante pas, 4 entrées 'unavailable'")


def test_orchestrator_persists_successful_predictions():
    _clean_all()
    with Session(engine) as session:
        version = _make_version(session, "dixon_coles", is_active=True)
        record = PredictionRecord(
            league="Ligue1", match_date=MATCH_DATE, home_team="A", away_team="B",
            model_type="dixon_coles", prob_home=0.5, prob_draw=0.3, prob_away=0.2, source="live",
        )
        outcome = PredictionOutcome("dixon_coles", "ok", model_version_id=version.id, record=record)
        models = [_FakeModel("dixon_coles", outcome)]
        ModelOrchestrator(models).predict_all(session, MatchContext("Ligue1", "A", "B", MATCH_DATE), persist=True)

        rows = session.exec(select(ModelPrediction).where(ModelPrediction.model_type == "dixon_coles")).all()
        assert len(rows) == 1 and rows[0].status == "pending"
    print("  [OK] persist=True -> prédiction réussie écrite dans model_predictions (status=pending)")


# ---------------------------------------------------------------------------
# 4. Ensemble Engine — poids, combinaison, anti-fuite
# ---------------------------------------------------------------------------

def test_compute_market_weights_formula_and_sum_to_one():
    """log_loss_elo=0.5 (p_true=e^-0.5), log_loss_xgb=1.0 (p_true=e^-1) ->
    score_elo=2.0, score_xgb=1.0 -> poids attendus 2/3 et 1/3 exactement."""
    _clean_all()
    with Session(engine) as session:
        elo_v = _make_version(session, "elo", is_active=True)
        xgb_v = _make_version(session, "xgboost", is_active=True)
        for i in range(100):
            _log_resolved(session, "elo", elo_v.id, MATCH_DATE, f"H{i}", f"A{i}", math.exp(-0.5))
            _log_resolved(session, "xgboost", xgb_v.id, MATCH_DATE, f"H{i}", f"A{i}", math.exp(-1.0))

        result = compute_market_weights(session, "1X2", until=MATCH_DATE, min_sample_size=100)
        assert set(result.weights.keys()) == {"elo", "xgboost"}
        total = sum(w.weight for w in result.weights.values())
        assert abs(total - 1.0) < 1e-6
        assert abs(result.weights["elo"].weight - 2 / 3) < 1e-3
        assert abs(result.weights["xgboost"].weight - 1 / 3) < 1e-3
    print(f"  [OK] poids dérivés du log_loss -> elo={result.weights['elo'].weight:.4f} (attendu 0.6667), "
          f"xgboost={result.weights['xgboost'].weight:.4f} (attendu 0.3333), somme=1")


# ---------------------------------------------------------------------------
# 4bis. Weight strategies (Phase 8, §14-§18)
# ---------------------------------------------------------------------------

def _metrics(log_loss=None, brier=None, sample_size=100):
    return MarketMetrics(log_loss=log_loss, brier_score=brier, sample_size=sample_size)


def test_inverse_log_loss_strategy_baseline_formula():
    scores = InverseLogLossStrategy(epsilon=1e-4).compute_scores({
        "elo": _metrics(log_loss=0.5), "xgboost": _metrics(log_loss=1.0),
    })
    assert abs(scores["elo"] - 2.0) < 1e-9 and abs(scores["xgboost"] - 1.0) < 1e-9
    print(f"  [OK] InverseLogLossStrategy : scores={scores} (1/log_loss exact)")


def test_softmax_log_loss_strategy_temperature_effect():
    metrics = {"elo": _metrics(log_loss=0.5), "xgboost": _metrics(log_loss=1.0)}
    low_temp = SoftmaxLogLossStrategy(temperature=0.001).compute_scores(metrics)
    high_temp = SoftmaxLogLossStrategy(temperature=50.0).compute_scores(metrics)

    low_ratio = low_temp["elo"] / low_temp["xgboost"]
    high_ratio = high_temp["elo"] / high_temp["xgboost"]
    assert abs(low_ratio - 1.0) < 0.05, "température quasi nulle -> scores quasi uniformes"
    assert high_ratio > 1000, "température élevée -> le meilleur modèle rafle presque tout le score"
    print(f"  [OK] SoftmaxLogLossStrategy : ratio elo/xgboost quasi uniforme à T=0.001 ({low_ratio:.3f}), "
          f"très contrasté à T=50 ({high_ratio:.1f})")


def test_brier_strategy_excludes_model_without_brier_score():
    scores = BrierStrategy().compute_scores({
        "elo": _metrics(log_loss=0.5, brier=0.4), "xgboost": _metrics(log_loss=1.0, brier=None),
    })
    assert set(scores.keys()) == {"elo"}, "un modèle sans brier_score doit être exclu, jamais un score fabriqué"
    print("  [OK] BrierStrategy exclut nativement un modèle sans brier_score disponible")


def test_hybrid_strategy_combines_log_loss_and_brier():
    metrics = {
        "elo": _metrics(log_loss=0.5, brier=0.3),
        "xgboost": _metrics(log_loss=1.0, brier=0.3),  # même brier, log_loss différent -> hybrid doit départager
    }
    pure_log_loss = InverseLogLossStrategy().compute_scores(metrics)
    pure_brier = BrierStrategy().compute_scores(metrics)
    hybrid_50 = HybridStrategy(alpha=0.5).compute_scores(metrics)

    ll_norm = {k: v / sum(pure_log_loss.values()) for k, v in pure_log_loss.items()}
    br_norm = {k: v / sum(pure_brier.values()) for k, v in pure_brier.items()}
    expected_elo = 0.5 * ll_norm["elo"] + 0.5 * br_norm["elo"]
    assert abs(hybrid_50["elo"] - expected_elo) < 1e-9

    # alpha=1.0 doit redonner exactement la même PROPORTION que InverseLogLossStrategy seule.
    hybrid_pure_ll = HybridStrategy(alpha=1.0).compute_scores(metrics)
    ratio_hybrid = hybrid_pure_ll["elo"] / hybrid_pure_ll["xgboost"]
    ratio_ll = pure_log_loss["elo"] / pure_log_loss["xgboost"]
    assert abs(ratio_hybrid - ratio_ll) < 1e-9
    print(f"  [OK] HybridStrategy(alpha=0.5) = combinaison normalisée de InverseLogLoss+Brier ; "
          f"alpha=1.0 -> même proportion que InverseLogLoss seule")


def test_hybrid_strategy_rejects_invalid_alpha():
    try:
        HybridStrategy(alpha=1.5)
        assert False, "alpha hors [0,1] doit lever ValueError"
    except ValueError:
        pass
    print("  [OK] HybridStrategy(alpha=1.5) -> ValueError (jamais silencieusement accepté)")


def test_compute_market_weights_with_softmax_strategy_end_to_end():
    _clean_all()
    with Session(engine) as session:
        elo_v = _make_version(session, "elo", is_active=True)
        xgb_v = _make_version(session, "xgboost", is_active=True)
        for i in range(100):
            _log_resolved(session, "elo", elo_v.id, MATCH_DATE, f"H{i}", f"A{i}", math.exp(-0.5))
            _log_resolved(session, "xgboost", xgb_v.id, MATCH_DATE, f"H{i}", f"A{i}", math.exp(-1.0))

        result = compute_market_weights(session, "1X2", until=MATCH_DATE, min_sample_size=100,
                                         strategy=SoftmaxLogLossStrategy(temperature=10.0))
        assert result.strategy == "softmax_log_loss"
        assert abs(sum(w.weight for w in result.weights.values()) - 1.0) < 1e-6
        assert result.weights["elo"].weight > result.weights["xgboost"].weight
    print(f"  [OK] compute_market_weights(strategy=softmax_log_loss) -> poids={{elo: {result.weights['elo'].weight:.4f}, "
          f"xgboost: {result.weights['xgboost'].weight:.4f}}}, somme=1")


# ---------------------------------------------------------------------------
# 4ter. Pondération par ligue avec repli (Phase 8, §24)
# ---------------------------------------------------------------------------

def test_league_specific_weights_used_when_sample_sufficient():
    _clean_all()
    with Session(engine) as session:
        elo_v = _make_version(session, "elo", is_active=True)
        # 100 résolues en Ligue1 (p_true différent de la fenêtre globale) -> doit utiliser CE log_loss, pas le global.
        for i in range(100):
            _log_resolved(session, "elo", elo_v.id, MATCH_DATE, f"H{i}", f"A{i}", math.exp(-0.5), league="Ligue1")
        for i in range(100):
            _log_resolved(session, "elo", elo_v.id, MATCH_DATE, f"HG{i}", f"AG{i}", math.exp(-2.0), league="SerieA")

        result = compute_market_weights(session, "1X2", until=MATCH_DATE, min_sample_size=100, league="Ligue1")
        assert result.weights["elo"].log_loss == 0.5 or abs(result.weights["elo"].log_loss - 0.5) < 1e-6
        assert result.weights["elo"].league_fallback is False
        assert result.weights["elo"].sample_size == 100
    print(f"  [OK] échantillon Ligue1 suffisant (100) -> log_loss Ligue1 utilisé directement "
          f"({result.weights['elo'].log_loss:.4f}), pas de repli global")


def test_league_specific_weights_fall_back_to_global_when_insufficient():
    _clean_all()
    with Session(engine) as session:
        elo_v = _make_version(session, "elo", is_active=True)
        # Seulement 20 résolues en Ligue1 (< seuil 100) -> doit se replier sur le GLOBAL
        # (Ligue1 + SerieA = 120), jamais rester bloqué sur un échantillon de 20.
        for i in range(20):
            _log_resolved(session, "elo", elo_v.id, MATCH_DATE, f"H{i}", f"A{i}", math.exp(-0.5), league="Ligue1")
        for i in range(100):
            _log_resolved(session, "elo", elo_v.id, MATCH_DATE, f"HG{i}", f"AG{i}", math.exp(-2.0), league="SerieA")

        result = compute_market_weights(session, "1X2", until=MATCH_DATE, min_sample_size=100, league="Ligue1")
        assert "elo" in result.weights, "doit être éligible via le repli global (120 >= 100), pas exclu"
        assert result.weights["elo"].league_fallback is True
        assert result.weights["elo"].sample_size == 120
    print(f"  [OK] échantillon Ligue1 insuffisant (20 < 100) -> repli automatique sur le global "
          f"(120 prédictions, league_fallback=True)")


def test_compute_market_weights_excludes_insufficient_sample():
    _clean_all()
    with Session(engine) as session:
        v = _make_version(session, "lightgbm", is_active=True)
        for i in range(50):  # < seuil de 100 passé ci-dessous
            _log_resolved(session, "lightgbm", v.id, MATCH_DATE, f"H{i}", f"A{i}", 0.6)

        result = compute_market_weights(session, "1X2", until=MATCH_DATE, min_sample_size=100)
        assert "lightgbm" not in result.weights
        assert "lightgbm" in result.excluded
        assert "50" in result.excluded["lightgbm"]
    print(f"  [OK] échantillon < seuil -> exclu du calcul de poids, raison explicite : {result.excluded['lightgbm']}")


def test_compute_market_weights_excludes_no_active_version():
    _clean_all()
    with Session(engine) as session:
        result = compute_market_weights(session, "1X2", until=MATCH_DATE, min_sample_size=1)
        assert result.weights == {}
        assert all(m in result.excluded for m in ("dixon_coles", "elo", "xgboost", "lightgbm"))
    print("  [OK] aucune version active pour aucun modèle -> weights={}, tous exclus avec raison")


def test_compute_market_weights_respects_leakage_cutoff():
    """Une prédiction résolue APRÈS `until` ne doit jamais compter dans le
    calcul des poids (§19 du ticket) — vérifié en construisant explicitement
    une ligne juste après la coupure et en s'assurant qu'elle est ignorée."""
    _clean_all()
    cutoff = date(2026, 5, 1)
    with Session(engine) as session:
        v = _make_version(session, "elo", is_active=True)
        for i in range(100):
            _log_resolved(session, "elo", v.id, cutoff, f"H{i}", f"A{i}", 0.6)  # avant/à la coupure -> inclus
        _log_resolved(session, "elo", v.id, cutoff + timedelta(days=1), "Future Home", "Future Away", 0.99)  # après -> exclu

        result = compute_market_weights(session, "1X2", until=cutoff, min_sample_size=100)
        assert result.weights["elo"].sample_size == 100, "la ligne postérieure à `until` a fuité dans le calcul des poids"
    print("  [OK] prédiction résolue après `until` -> jamais comptée dans le calcul des poids (sample_size=100, pas 101)")


def test_combine_weighted_average_matches_manual_computation():
    weight_result = WeightResult(
        market="1X2", until=MATCH_DATE,
        weights={
            "elo": ModelWeight("elo", "v1", 1, weight=0.6, log_loss=0.5, sample_size=100),
            "xgboost": ModelWeight("xgboost", "v1", 2, weight=0.4, log_loss=0.7, sample_size=100),
        },
    )
    model_probs = {
        "elo": {"home_win": 0.5, "draw": 0.3, "away_win": 0.2},
        "xgboost": {"home_win": 0.4, "draw": 0.35, "away_win": 0.25},
    }
    combined = combine(model_probs, weight_result)
    expected_home = 0.6 * 0.5 + 0.4 * 0.4
    assert abs(combined.probs["home_win"] - expected_home) < 1e-6
    assert combined.models_used == 2 and combined.degraded is False
    print(f"  [OK] combinaison pondérée = {combined.probs} (home attendu {expected_home})")


def test_combine_renormalizes_when_one_eligible_model_missing_at_predict_time():
    """elo est pondéré historiquement mais n'a, pour CE match, produit aucune
    prédiction valide (ex. version active désactivée entre temps) -> le
    poids restant (xgboost) doit être renormalisé à 1.0, jamais à 0.4."""
    weight_result = WeightResult(
        market="1X2", until=MATCH_DATE,
        weights={
            "elo": ModelWeight("elo", "v1", 1, weight=0.6, log_loss=0.5, sample_size=100),
            "xgboost": ModelWeight("xgboost", "v1", 2, weight=0.4, log_loss=0.7, sample_size=100),
        },
    )
    model_probs = {"xgboost": {"home_win": 0.4, "draw": 0.35, "away_win": 0.25}}
    combined = combine(model_probs, weight_result)
    assert combined.models_used == 1 and combined.degraded is True
    assert abs(combined.probs["home_win"] - 0.4) < 1e-9, "poids doit être renormalisé à 1.0 pour le seul modèle restant"
    print(f"  [OK] 1 seul modèle dispo au moment du match -> poids renormalisé à 1.0 (degraded=True), probs={combined.probs}")


def test_combine_returns_none_when_no_eligible_model_has_probability():
    weight_result = WeightResult(market="BTTS", until=MATCH_DATE, weights={
        "elo": ModelWeight("elo", "v1", 1, weight=1.0, log_loss=0.5, sample_size=100),
    })
    combined = combine({}, weight_result)  # elo ne modélise pas BTTS -> jamais dans model_probs
    assert combined is None
    print("  [OK] aucun modèle éligible n'a de probabilité pour ce marché -> None (jamais fabriqué)")


def test_combine_never_mutates_input_probabilities():
    weight_result = WeightResult(market="1X2", until=MATCH_DATE, weights={
        "elo": ModelWeight("elo", "v1", 1, weight=1.0, log_loss=0.5, sample_size=100),
    })
    original = {"home_win": 0.5, "draw": 0.3, "away_win": 0.2}
    snapshot = dict(original)
    model_probs = {"elo": original}
    combine(model_probs, weight_result)
    assert original == snapshot, "combine() ne doit jamais modifier les probabilités originales en place"
    print("  [OK] combine() ne mute jamais les probabilités originales passées en entrée")


def test_confidence_is_margin_not_max_probability():
    probs = {"home_win": 0.5, "draw": 0.3, "away_win": 0.2}
    conf = compute_confidence(probs)
    assert abs(conf - 0.2) < 1e-9, "confiance = écart top1-top2 (0.5-0.3=0.2), pas max()=0.5"
    print(f"  [OK] confidence={conf} = écart 1re/2e probabilité (0.5-0.3), jamais max(probabilities)=0.5")


def test_weights_computed_separately_per_market_missing_market_not_available():
    """elo/xgboost/lightgbm ne modélisent jamais BTTS -> même avec un
    historique 1X2 riche, l'Ensemble BTTS doit rester NOT_AVAILABLE tant
    qu'aucun modèle (ex. dixon_coles) ne l'alimente réellement (§15)."""
    _clean_all()
    with Session(engine) as session:
        v = _make_version(session, "elo", is_active=True)
        for i in range(100):
            _log_resolved(session, "elo", v.id, MATCH_DATE, f"H{i}", f"A{i}", 0.6)

        weight_result = compute_market_weights(session, "BTTS", until=MATCH_DATE, min_sample_size=100)
        assert weight_result.weights == {}, "elo n'a jamais de log_loss BTTS -> jamais éligible sur ce marché"
    print("  [OK] BTTS jamais modélisé par elo -> exclu du calcul de poids BTTS (marché absent, pas une erreur)")


# ---------------------------------------------------------------------------
# 5. Ensemble live de bout en bout — enregistrement, non-mélange des sources (§18)
# ---------------------------------------------------------------------------

def test_build_live_ensemble_end_to_end_and_registers_itself():
    _clean_all()
    with Session(engine) as session:
        elo_v = _make_version(session, "elo", is_active=True)
        xgb_v = _make_version(session, "xgboost", is_active=True)
        history_date = MATCH_DATE - timedelta(days=30)
        for i in range(100):
            _log_resolved(session, "elo", elo_v.id, history_date, f"H{i}", f"A{i}", math.exp(-0.5))
            _log_resolved(session, "xgboost", xgb_v.id, history_date, f"HX{i}", f"AX{i}", math.exp(-1.0))

        model_records = {
            "elo": PredictionRecord(league="Ligue1", match_date=MATCH_DATE, home_team="Paris SG", away_team="Marseille",
                                     model_type="elo", prob_home=0.5, prob_draw=0.3, prob_away=0.2, source="live"),
            "xgboost": PredictionRecord(league="Ligue1", match_date=MATCH_DATE, home_team="Paris SG", away_team="Marseille",
                                         model_type="xgboost", prob_home=0.4, prob_draw=0.35, prob_away=0.25, source="live"),
        }
        result = build_live_ensemble(session, model_records, "Ligue1", "Paris SG", "Marseille", MATCH_DATE, min_sample_size=100)

        assert result.markets["1X2"].status == "ok"
        assert result.markets["BTTS"].status == "not_available"  # ni elo ni xgboost ne le modélisent
        assert result.model_version_id is not None
        assert result.confidence is not None

        ensemble_rows = session.exec(select(ModelPrediction).where(ModelPrediction.model_type == "ensemble")).all()
        assert len(ensemble_rows) == 1
        assert ensemble_rows[0].model_version_id == result.model_version_id

        # Les poids de l'Ensemble ne doivent JAMAIS être recalculés à partir
        # des prédictions individuelles ELLES-MÊMES loguées "live" ci-dessus
        # (elles datent de MATCH_DATE, exclues par la coupure anti-fuite) —
        # seul l'historique à history_date a pu compter.
        weights = result.weight_results["1X2"].weights
        assert weights["elo"].sample_size == 100
        assert weights["xgboost"].sample_size == 100
    print(f"  [OK] ensemble live bout-en-bout : 1X2 ok (conf={result.confidence}), BTTS not_available, "
          f"1 ligne 'ensemble' enregistrée séparément (model_version_id={result.model_version_id})")


def test_ensemble_prediction_never_counted_in_its_own_weights():
    """§18 : une prédiction 'ensemble' déjà loguée ne doit jamais apparaître
    comme un modèle éligible dans un calcul de poids ultérieur."""
    _clean_all()
    with Session(engine) as session:
        ens_v = _make_version(session, "ensemble", is_active=True)
        for i in range(200):
            _log_resolved(session, "ensemble", ens_v.id, MATCH_DATE - timedelta(days=1), f"H{i}", f"A{i}", 0.9)

        result = compute_market_weights(session, "1X2", until=MATCH_DATE, min_sample_size=100)
        assert "ensemble" not in result.weights and "ensemble" not in result.excluded
    print("  [OK] model_type='ensemble' jamais considéré comme un modèle pondérable (KNOWN_MODEL_TYPES l'exclut)")


# ---------------------------------------------------------------------------
# 6. API — POST /models/ensemble/predict (TestClient + base isolée)
# ---------------------------------------------------------------------------

def test_ensemble_predict_endpoint_unknown_league_returns_404(client):
    r = client.post("/models/ensemble/predict", json={"league": "Ligue Inconnue", "home_team": "A", "away_team": "B"},
                     headers=AUTH_HEADER)
    assert r.status_code == 404, r.text
    print("  [OK] POST /models/ensemble/predict, ligue inconnue -> 404")


def test_ensemble_predict_endpoint_unknown_team_returns_404(client):
    r = client.post("/models/ensemble/predict", json={"league": "Ligue1", "home_team": "Equipe Inexistante XYZ", "away_team": "Marseille"},
                     headers=AUTH_HEADER)
    assert r.status_code == 404, r.text
    print("  [OK] POST /models/ensemble/predict, équipe inconnue -> 404 avec message actionnable")


def test_ensemble_predict_endpoint_returns_all_four_models_and_ensemble_shape(client):
    _clean_all()
    r = client.post("/models/ensemble/predict", json={"league": "Ligue1", "home_team": "Paris SG", "away_team": "Marseille"},
                     headers=AUTH_HEADER)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["status"] == "ok"
    assert body["match"]["home_team"] == "Paris SG" and body["match"]["away_team"] == "Marseille"
    assert set(body["models"].keys()) == {"dixon_coles", "elo", "xgboost", "lightgbm"}

    # Dixon-Coles est le seul modèle live-servable dans cette base fraîchement isolée
    # (Elo n'a pas de version active ici, XGBoost/LightGBM jamais live) — vérifié
    # explicitement plutôt que supposé, pour ne pas masquer une régression de service.
    assert body["models"]["dixon_coles"]["status"] == "ok"
    assert body["models"]["elo"]["status"] == "unavailable"
    assert body["models"]["xgboost"]["status"] == "unavailable"
    assert body["models"]["lightgbm"]["status"] == "unavailable"

    assert "1X2" in body["ensemble"]["markets"]
    assert "weighting_metric" in body["ensemble"]

    # La prédiction Dixon-Coles doit avoir été journalisée (dual-write déjà en
    # place, réutilisé ici via l'orchestrateur) — jamais un appel "silencieux".
    dc_rows = _rows(ModelPrediction, ModelPrediction.model_type == "dixon_coles")
    assert len(dc_rows) == 1
    print(f"  [OK] réponse complète : 4 modèles présents (1 ok, 3 unavailable honnêtes), "
          f"ensemble.markets['1X2']={body['ensemble']['markets']['1X2']['status']}, prédiction DC journalisée")


def test_ensemble_predict_endpoint_rejects_unknown_strategy(client):
    r = client.post("/models/ensemble/predict",
                     json={"league": "Ligue1", "home_team": "Paris SG", "away_team": "Marseille", "strategy": "not_a_real_strategy"},
                     headers=AUTH_HEADER)
    assert r.status_code == 400, r.text
    assert "not_a_real_strategy" in r.json()["detail"]
    print("  [OK] POST /models/ensemble/predict, stratégie inconnue -> 400 avec message explicite")


def test_ensemble_predict_endpoint_accepts_valid_strategy(client):
    _clean_all()
    r = client.post("/models/ensemble/predict",
                     json={"league": "Ligue1", "home_team": "Paris SG", "away_team": "Marseille", "strategy": "softmax_log_loss"},
                     headers=AUTH_HEADER)
    assert r.status_code == 200, r.text
    assert r.json()["ensemble"]["weighting_metric"] == "softmax_log_loss"
    print("  [OK] POST /models/ensemble/predict avec strategy='softmax_log_loss' -> weighting_metric reflété dans la réponse")


def test_ensemble_strategies_endpoint_lists_all_four(client):
    r = client.get("/models/ensemble/strategies", headers=AUTH_HEADER)
    assert r.status_code == 200, r.text
    body = r.json()
    names = {s["name"] for s in body["strategies"]}
    assert names == {"inverse_log_loss", "softmax_log_loss", "brier", "hybrid"}
    assert body["default"] == "inverse_log_loss"
    print(f"  [OK] GET /models/ensemble/strategies -> {sorted(names)}, défaut={body['default']}")


def test_models_availability_endpoint_reports_all_four_model_types(client):
    _clean_all()
    r = client.get("/models/availability", headers=AUTH_HEADER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {"dixon_coles", "elo", "xgboost", "lightgbm"}
    for model_type, info in body.items():
        assert "live_available" in info and "active" in info
        assert set(info["markets"].keys()) == {"1X2", "BTTS", "OVER_UNDER_2_5"}
        for market_info in info["markets"].values():
            assert "benchmark_eligible" in market_info and "ensemble_eligible" in market_info
    # Elo n'a aucune version active dans cette base fraîchement isolée.
    assert body["elo"]["active"] is False
    assert body["elo"]["live_available"] is False
    print(f"  [OK] GET /models/availability -> 4 model_types, chacun avec active/live_available + 3 marchés "
          f"(elo: active={body['elo']['active']}, live_available={body['elo']['live_available']})")


def _rows(model_cls, *where):
    with Session(engine) as session:
        return session.exec(select(model_cls).where(*where)).all()


API_TESTS = [
    test_ensemble_predict_endpoint_unknown_league_returns_404,
    test_ensemble_predict_endpoint_unknown_team_returns_404,
    test_ensemble_predict_endpoint_returns_all_four_models_and_ensemble_shape,
    test_ensemble_predict_endpoint_rejects_unknown_strategy,
    test_ensemble_predict_endpoint_accepts_valid_strategy,
    test_ensemble_strategies_endpoint_lists_all_four,
    test_models_availability_endpoint_reports_all_four_model_types,
]


UNIT_TESTS = [
    test_all_models_share_common_interface,
    test_dixon_coles_success_returns_full_market_record,
    test_dixon_coles_unknown_team_is_unavailable_not_error,
    test_elo_no_active_version_is_unavailable,
    test_elo_active_version_without_config_is_unavailable,
    test_elo_active_version_with_config_predicts,
    test_elo_unseen_team_falls_back_to_initial_rating,
    test_xgboost_and_lightgbm_without_active_version_are_unavailable,
    test_normalize_market_exact_sum_unchanged,
    test_normalize_market_small_drift_normalized,
    test_normalize_market_out_of_range_rejected,
    test_normalize_market_grossly_wrong_sum_rejected,
    test_normalize_market_btts_two_way,
    test_orchestrator_all_four_succeed,
    test_orchestrator_one_model_raises_others_still_succeed,
    test_orchestrator_zero_available_never_crashes,
    test_orchestrator_persists_successful_predictions,
    test_compute_market_weights_formula_and_sum_to_one,
    test_inverse_log_loss_strategy_baseline_formula,
    test_softmax_log_loss_strategy_temperature_effect,
    test_brier_strategy_excludes_model_without_brier_score,
    test_hybrid_strategy_combines_log_loss_and_brier,
    test_hybrid_strategy_rejects_invalid_alpha,
    test_compute_market_weights_with_softmax_strategy_end_to_end,
    test_league_specific_weights_used_when_sample_sufficient,
    test_league_specific_weights_fall_back_to_global_when_insufficient,
    test_compute_market_weights_excludes_insufficient_sample,
    test_compute_market_weights_excludes_no_active_version,
    test_compute_market_weights_respects_leakage_cutoff,
    test_combine_weighted_average_matches_manual_computation,
    test_combine_renormalizes_when_one_eligible_model_missing_at_predict_time,
    test_combine_returns_none_when_no_eligible_model_has_probability,
    test_combine_never_mutates_input_probabilities,
    test_confidence_is_margin_not_max_probability,
    test_weights_computed_separately_per_market_missing_market_not_available,
    test_build_live_ensemble_end_to_end_and_registers_itself,
    test_ensemble_prediction_never_counted_in_its_own_weights,
]


if __name__ == "__main__":
    failures = 0

    print("\n" + "=" * 60 + "\nTESTS UNITAIRES (models_common / orchestrator / ensemble)\n" + "=" * 60)
    for t in UNIT_TESTS:
        print(f"\n=== {t.__name__} ===")
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"  [ECHEC] {e}")

    print("\n" + "=" * 60 + "\nTESTS API (TestClient + base isolée)\n" + "=" * 60)
    with TestClient(app) as client:
        user_id, token = register_and_login(client, "ensemble-tests@example.com", "correct-horse-battery-staple")
        activate_subscription(client, token, user_id, email="ensemble-tests@example.com",
                               webhook_secret="whsec_test_secret_for_signature_verification")
        AUTH_HEADER["Authorization"] = f"Bearer {token}"

        for t in API_TESTS:
            print(f"\n=== {t.__name__} ===")
            try:
                t(client)
            except AssertionError as e:
                failures += 1
                print(f"  [ECHEC] {e}")

    total = len(UNIT_TESTS) + len(API_TESTS)
    cleanup_db(DB_PATH)
    print(f"\n{'='*60}\n{total-failures}/{total} tests reussis\n{'='*60}")
    sys.exit(1 if failures else 0)
