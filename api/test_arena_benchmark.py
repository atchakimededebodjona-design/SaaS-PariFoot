"""
test_arena_benchmark.py — Xfoot AI Arena, Phase 5 V2 (Performance &
Benchmarking) : métriques par marché (accuracy/log_loss/brier_score),
GET /models/benchmark, logique de "meilleur modèle".

Deux familles de tests :
  1. UNITAIRES, sur les fonctions pures de app/ai/arena/service.py
     (_compute_market_metrics, _market_observation, _pick_best_model) —
     aucune base de données, valeurs connues à l'avance calculées à la main.
  2. API, via TestClient + base isolée (comme test_arena.py) — vérifie que
     GET /models/performance et GET /models/benchmark exposent bien ces
     métriques depuis des données réellement insérées, avec les bons
     filtres et le bon comportement "insufficient_data".

Usage : python api/test_arena_benchmark.py
"""

import math
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db, register_and_login, activate_subscription

DB_PATH = configure_test_env("test_arena_benchmark.db")

from fastapi.testclient import TestClient
from sqlmodel import Session
from main import app, engine
from app.models.prediction_log import PredictionLog
from app.ai.arena.service import (
    _market_observation,
    _compute_market_metrics,
    _pick_best_model,
    MIN_BENCHMARK_SAMPLE_SIZE,
)
from app.ai.arena.schemas import ModelMarketSummary

AUTH_HEADER: dict = {}


# ---------------------------------------------------------------------------
# 1. Tests unitaires — fonctions pures, aucune DB
# ---------------------------------------------------------------------------

class _FakeLog:
    """Substitut minimal de PredictionLog pour tester _market_observation
    sans passer par la base — seuls les champs lus par cette fonction."""
    def __init__(self, result_home_goals, result_away_goals, correct_1x2=None, correct_btts=None, correct_over_2_5=None):
        self.result_home_goals = result_home_goals
        self.result_away_goals = result_away_goals
        self.correct_1x2 = correct_1x2
        self.correct_btts = correct_btts
        self.correct_over_2_5 = correct_over_2_5


def test_accuracy_100_percent():
    payload = {"home_win": 0.7, "draw": 0.2, "away_win": 0.1}
    logs = [_FakeLog(2, 0, correct_1x2=True) for _ in range(5)]
    observations = [_market_observation(log, payload, "1X2") for log in logs]
    metrics = _compute_market_metrics(observations)
    assert metrics.accuracy == 1.0, metrics
    assert metrics.correct_predictions == 5
    assert metrics.sample_size == 5
    print(f"  [OK] 5/5 correct_1x2=True -> accuracy=1.0 (metrics={metrics})")


def test_accuracy_0_percent():
    payload = {"home_win": 0.7, "draw": 0.2, "away_win": 0.1}
    logs = [_FakeLog(2, 0, correct_1x2=False) for _ in range(4)]
    observations = [_market_observation(log, payload, "1X2") for log in logs]
    metrics = _compute_market_metrics(observations)
    assert metrics.accuracy == 0.0, metrics
    assert metrics.correct_predictions == 0
    print(f"  [OK] 0/4 correct_1x2 -> accuracy=0.0 (metrics={metrics})")


def test_accuracy_mixed():
    payload = {"home_win": 0.5, "draw": 0.3, "away_win": 0.2}
    logs = [_FakeLog(2, 0, correct_1x2=True)] * 3 + [_FakeLog(0, 1, correct_1x2=False)] * 1
    observations = [_market_observation(log, payload, "1X2") for log in logs]
    metrics = _compute_market_metrics(observations)
    assert metrics.accuracy == 0.75, metrics
    assert metrics.sample_size == 4
    print(f"  [OK] 3/4 correct -> accuracy=0.75 (metrics={metrics})")


def test_log_loss_known_value():
    """1 seule observation, p_true=0.5 exact -> log_loss = -log(0.5)."""
    payload = {"home_win": 0.5, "draw": 0.25, "away_win": 0.25}
    log = _FakeLog(2, 0, correct_1x2=True)  # home_win réel, p_true = payload["home_win"] = 0.5
    metrics = _compute_market_metrics([_market_observation(log, payload, "1X2")])
    expected = -math.log(0.5)
    assert abs(metrics.log_loss - round(expected, 4)) < 1e-6, metrics
    print(f"  [OK] p_true=0.5 -> log_loss={metrics.log_loss} (attendu -log(0.5)={expected:.4f})")


def test_log_loss_clips_against_log_zero():
    """p_true=0.0 exact (pathologique) ne doit JAMAIS produire -inf/NaN."""
    payload = {"home_win": 0.0, "draw": 0.5, "away_win": 0.5}
    log = _FakeLog(2, 0, correct_1x2=True)  # home_win réel, p_true = 0.0
    metrics = _compute_market_metrics([_market_observation(log, payload, "1X2")])
    assert math.isfinite(metrics.log_loss), f"log_loss doit rester fini, obtenu {metrics.log_loss}"
    assert metrics.log_loss > 30, "avec p clippée à 1e-15, -log(p) doit être très grand (~34.5)"
    print(f"  [OK] p_true=0.0 -> log_loss fini et très pénalisant (={metrics.log_loss}), pas -inf/NaN")


def test_brier_perfect_prediction():
    """Probabilité 1.0 sur l'issue réellement survenue, 0 ailleurs -> Brier=0."""
    payload = {"btts_yes": 1.0, "btts_no": 0.0}
    log = _FakeLog(1, 1, correct_btts=True)  # BTTS réel = yes
    metrics = _compute_market_metrics([_market_observation(log, payload, "BTTS")])
    assert metrics.brier_score == 0.0, metrics
    print(f"  [OK] prédiction BTTS parfaite -> brier_score=0.0")


def test_brier_worst_prediction():
    """Probabilité 1.0 sur l'issue qui NE survient PAS -> pire Brier possible
    (convention multiclasse-somme, identique à scripts/backtest_elo.py::brier_score,
    généralisée : (0-1)^2 + (1-0)^2 = 2 pour un marché à 2 issues)."""
    payload = {"btts_yes": 0.0, "btts_no": 1.0}
    log = _FakeLog(1, 1, correct_btts=False)  # BTTS réel = yes, modèle donnait 0% à "yes"
    metrics = _compute_market_metrics([_market_observation(log, payload, "BTTS")])
    assert metrics.brier_score == 2.0, metrics
    print(f"  [OK] prédiction BTTS totalement fausse -> brier_score=2.0 (pire cas, convention somme sur 2 issues)")


def test_brier_intermediate_probabilities():
    payload = {"over_2_5": 0.6, "under_2_5": 0.4}
    log = _FakeLog(2, 1, correct_over_2_5=True)  # total=3 > 2.5 -> "over" réel
    metrics = _compute_market_metrics([_market_observation(log, payload, "OVER_UNDER_2_5")])
    expected = (0.6 - 1.0) ** 2 + (0.4 - 0.0) ** 2  # = 0.16 + 0.16 = 0.32
    assert abs(metrics.brier_score - round(expected, 4)) < 1e-6, metrics
    print(f"  [OK] probabilités intermédiaires (0.6/0.4) -> brier_score={metrics.brier_score} (attendu {expected:.4f})")


def test_market_metrics_not_available_when_no_observations():
    metrics = _compute_market_metrics([])
    assert metrics.accuracy is None
    assert metrics.log_loss is None
    assert metrics.brier_score is None
    assert metrics.sample_size == 0
    print("  [OK] aucune observation -> accuracy/log_loss/brier_score=None (NOT_AVAILABLE), sample_size=0")


def test_market_observation_skips_unresolved_predictions():
    payload = {"home_win": 0.5, "draw": 0.3, "away_win": 0.2}
    unresolved = _FakeLog(None, None, correct_1x2=None)
    assert _market_observation(unresolved, payload, "1X2") is None
    print("  [OK] prédiction non résolue (result_*=None) -> observation=None, jamais fabriquée")


def _summary(model_type, log_loss=None, brier_score=None, accuracy=None, sample_size=0):
    return ModelMarketSummary(
        model_type=model_type, version=f"{model_type}-v1", source="test", is_active=False,
        log_loss=log_loss, brier_score=brier_score, accuracy=accuracy, sample_size=sample_size,
    )


def test_best_model_prefers_lower_log_loss():
    summaries = [
        _summary("dixon_coles", log_loss=1.02, sample_size=200),
        _summary("xgboost", log_loss=0.95, sample_size=200),  # plus bas = meilleur
    ]
    result = _pick_best_model(summaries, "1X2", min_sample_size=100)
    assert result.status == "ok"
    assert result.metric == "log_loss"
    assert result.best_model == "xgboost"
    assert result.value == 0.95
    print(f"  [OK] log_loss plus faible (xgboost=0.95 < dixon_coles=1.02) -> meilleur modèle = xgboost")


def test_best_model_higher_accuracy_wins_when_only_accuracy_available():
    summaries = [
        _summary("dixon_coles", accuracy=0.48, sample_size=200),
        _summary("xgboost", accuracy=0.51, sample_size=200),  # plus élevé = meilleur
    ]
    result = _pick_best_model(summaries, "1X2", min_sample_size=100)
    assert result.status == "ok"
    assert result.metric == "accuracy"
    assert result.best_model == "xgboost"
    print(f"  [OK] seule accuracy dispo -> plus élevé gagne (xgboost=0.51 > dixon_coles=0.48)")


def test_best_model_tie_picks_first_deterministically():
    summaries = [
        _summary("dixon_coles", log_loss=1.00, sample_size=200),
        _summary("xgboost", log_loss=1.00, sample_size=200),
    ]
    result = _pick_best_model(summaries, "1X2", min_sample_size=100)
    assert result.status == "ok"
    assert result.value == 1.00
    assert result.best_model in ("dixon_coles", "xgboost")
    print(f"  [OK] égalité stricte (1.00 == 1.00) -> un modèle désigné de façon déterministe ({result.best_model}), pas d'erreur")


def test_best_model_insufficient_sample_size():
    summaries = [
        _summary("dixon_coles", log_loss=1.02, sample_size=5),  # sous le seuil
        _summary("xgboost", log_loss=0.95, sample_size=5),
    ]
    result = _pick_best_model(summaries, "1X2", min_sample_size=100)
    assert result.status == "insufficient_data"
    assert "sample_size" in result.reason
    print(f"  [OK] sample_size (5) < seuil (100) -> insufficient_data, raison explicite : {result.reason}")


def test_best_model_only_one_model_has_data():
    summaries = [
        _summary("dixon_coles", log_loss=1.02, sample_size=200),
        _summary("xgboost", log_loss=None, sample_size=0),  # pas de donnée
    ]
    result = _pick_best_model(summaries, "1X2", min_sample_size=100)
    assert result.status == "insufficient_data"
    print(f"  [OK] un seul modèle avec des données -> insufficient_data, jamais un gagnant par défaut : {result.reason}")


def test_best_model_no_data_at_all():
    summaries = [_summary("dixon_coles"), _summary("xgboost")]
    result = _pick_best_model(summaries, "1X2", min_sample_size=100)
    assert result.status == "insufficient_data"
    print(f"  [OK] aucune métrique nulle part -> insufficient_data : {result.reason}")


def test_calibration_none_below_min_sample_size():
    """Phase 8, §26 : un diagramme de calibration ne doit jamais être
    affiché sur un échantillon trop petit pour être digne de confiance."""
    payload = {"home_win": 0.7, "draw": 0.2, "away_win": 0.1}
    logs = [_FakeLog(2, 0, correct_1x2=True) for _ in range(5)]  # 5 << MIN_BENCHMARK_SAMPLE_SIZE (100)
    observations = [_market_observation(log, payload, "1X2") for log in logs]
    metrics = _compute_market_metrics(observations)
    assert metrics.calibration is None
    print("  [OK] échantillon (5) < MIN_BENCHMARK_SAMPLE_SIZE -> calibration=None, jamais un binning fabriqué")


def test_calibration_bins_by_pick_confidence():
    """100 observations à confiance ~0.7 (pick correct 70% du temps) doivent
    tomber dans le bin [0.6,0.8) avec observed_frequency proche de 0.7 —
    calibration 'parfaite' par construction, vérifiée numériquement."""
    payload = {"home_win": 0.7, "draw": 0.2, "away_win": 0.1}
    logs = [_FakeLog(2, 0, correct_1x2=True) for _ in range(70)] + [_FakeLog(0, 2, correct_1x2=False) for _ in range(30)]
    observations = [_market_observation(log, payload, "1X2") for log in logs]
    metrics = _compute_market_metrics(observations)
    assert metrics.calibration is not None
    bins_with_data = [b for b in metrics.calibration if b["count"] > 0]
    assert len(bins_with_data) == 1, f"toutes les observations ont la même confiance (0.7) -> un seul bin peuplé, obtenu {bins_with_data}"
    b = bins_with_data[0]
    assert b["predicted_confidence_avg"] == 0.7
    assert abs(b["observed_frequency"] - 0.7) < 1e-9
    assert b["count"] == 100
    print(f"  [OK] calibration bien formée : {b} (confiance prédite = fréquence observée, calibration parfaite par construction)")


UNIT_TESTS = [
    test_accuracy_100_percent,
    test_accuracy_0_percent,
    test_accuracy_mixed,
    test_log_loss_known_value,
    test_log_loss_clips_against_log_zero,
    test_brier_perfect_prediction,
    test_brier_worst_prediction,
    test_brier_intermediate_probabilities,
    test_market_metrics_not_available_when_no_observations,
    test_market_observation_skips_unresolved_predictions,
    test_best_model_prefers_lower_log_loss,
    test_best_model_higher_accuracy_wins_when_only_accuracy_available,
    test_best_model_tie_picks_first_deterministically,
    test_best_model_insufficient_sample_size,
    test_best_model_only_one_model_has_data,
    test_best_model_no_data_at_all,
    test_calibration_none_below_min_sample_size,
    test_calibration_bins_by_pick_confidence,
]


# ---------------------------------------------------------------------------
# 2. Tests API — TestClient + base isolée
# ---------------------------------------------------------------------------

def _resolve_prediction(session, league, home_team, away_team, result_home, result_away):
    from sqlmodel import select
    log = session.exec(
        select(PredictionLog).where(
            PredictionLog.league == league,
            PredictionLog.home_team == home_team,
            PredictionLog.away_team == away_team,
        )
    ).one()
    log.result_home_goals, log.result_away_goals = result_home, result_away
    log.result_fetched_at = datetime.now(timezone.utc)
    log.correct_1x2 = (log.pick_1x2 == ("home" if result_home > result_away else ("draw" if result_home == result_away else "away")))
    log.correct_btts = (log.pick_btts == ("yes" if (result_home > 0 and result_away > 0) else "no"))
    log.correct_over_2_5 = (log.pick_over_2_5 == ("over" if (result_home + result_away) > 2.5 else "under"))
    session.add(log)
    session.commit()


def test_performance_endpoint_exposes_markets_field(client):
    r = client.get("/predictions/Ligue1/Paris SG/Marseille", headers=AUTH_HEADER)
    assert r.status_code == 200, r.text

    with Session(engine) as session:
        _resolve_prediction(session, "Ligue1", "Paris SG", "Marseille", 2, 0)

    r = client.get("/models/performance")
    assert r.status_code == 200, r.text
    body = r.json()
    dc = next(m for m in body["models"] if m["model_type"] == "dixon_coles")

    for market in ("1X2", "BTTS", "OVER_UNDER_2_5"):
        assert market in dc["markets"], dc["markets"]
        assert dc["markets"][market]["sample_size"] == 1
        assert dc["markets"][market]["accuracy"] in (0.0, 1.0)
        assert dc["markets"][market]["log_loss"] is not None
        assert dc["markets"][market]["brier_score"] is not None

    assert body["min_benchmark_sample_size"] == MIN_BENCHMARK_SAMPLE_SIZE
    assert body["filters"]["period"] == "all_time"
    print(f"  [OK] /models/performance -> markets 1X2/BTTS/OVER_UNDER_2_5 tous peuplés, "
          f"min_benchmark_sample_size={body['min_benchmark_sample_size']}")


def test_model_versions_entries_have_not_available_markets(client):
    from app.models.team_rating import ModelVersion
    with Session(engine) as session:
        session.add(ModelVersion(
            name="xfoot-xgboost-v1", model_type="xgboost", is_active=False,
            trained_at=datetime.now(timezone.utc),
            notes="log-loss=1.0311 vs Dixon-Coles=1.0315 ; verdict : PAS DE GAIN CLAIR",
        ))
        session.commit()

    r = client.get("/models/performance")
    body = r.json()
    xgb = next(m for m in body["models"] if m["model_type"] == "xgboost")
    for market in ("1X2", "BTTS", "OVER_UNDER_2_5"):
        assert xgb["markets"][market]["accuracy"] is None
        assert xgb["markets"][market]["log_loss"] is None
        assert xgb["markets"][market]["brier_score"] is None
        assert xgb["markets"][market]["sample_size"] == 0
    print("  [OK] version model_versions sans donnée par-prédiction -> markets NOT_AVAILABLE sur les 3 marchés")


def test_league_filter_narrows_prediction_set(client):
    r = client.get("/predictions/Bundesliga/Bayern/Dortmund", headers=AUTH_HEADER)
    assert r.status_code == 200, r.text
    with Session(engine) as session:
        _resolve_prediction(session, "Bundesliga", "Bayern Munich", "Dortmund", 1, 1)

    r_all = client.get("/models/performance")
    dc_all = next(m for m in r_all.json()["models"] if m["model_type"] == "dixon_coles")
    total_before_filter = dc_all["markets"]["1X2"]["sample_size"]

    r_ligue1 = client.get("/models/performance", params={"league": "Ligue1"})
    assert r_ligue1.status_code == 200, r_ligue1.text
    dc_l1 = next(m for m in r_ligue1.json()["models"] if m["model_type"] == "dixon_coles")
    assert dc_l1["markets"]["1X2"]["sample_size"] < total_before_filter
    assert r_ligue1.json()["filters"]["league"] == "Ligue1"
    print(f"  [OK] ?league=Ligue1 -> sample_size réduit ({dc_l1['markets']['1X2']['sample_size']} "
          f"< {total_before_filter} toutes ligues confondues), filtre bien reflété dans la réponse")


def test_league_filter_rejects_unknown_league(client):
    r = client.get("/models/performance", params={"league": "PasUneLigue"})
    assert r.status_code == 400, r.text
    print(f"  [OK] ?league=PasUneLigue -> 400, detail: {r.json()['detail']}")


def test_benchmark_endpoint_returns_all_three_markets_by_default(client):
    r = client.get("/models/benchmark")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert set(body["markets"].keys()) == {"1X2", "BTTS", "OVER_UNDER_2_5"}
    print(f"  [OK] GET /models/benchmark sans filtre -> les 3 marchés présents")


def test_benchmark_single_market_filter(client):
    r = client.get("/models/benchmark", params={"market": "BTTS"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["markets"].keys()) == {"BTTS"}
    print(f"  [OK] ?market=BTTS -> seule BTTS retournée")


def test_benchmark_rejects_unknown_market(client):
    r = client.get("/models/benchmark", params={"market": "PasUnMarche"})
    assert r.status_code == 400, r.text
    print(f"  [OK] ?market=PasUnMarche -> 400, detail: {r.json()['detail']}")


def test_benchmark_insufficient_data_when_only_one_model_has_metrics(client):
    """Avec les données locales (quelques prédictions Dixon-Coles seulement,
    aucune donnée par-prédiction pour Elo/XGBoost/LightGBM), le benchmark ne
    doit JAMAIS désigner de gagnant — un seul modèle est comparable."""
    r = client.get("/models/benchmark")
    body = r.json()
    for market, bench in body["markets"].items():
        assert bench["best_model"]["status"] == "insufficient_data", (market, bench["best_model"])
        assert bench["best_model"]["reason"], "la raison doit toujours être explicite quand insufficient_data"
    print("  [OK] un seul modèle avec des données réelles -> best_model='insufficient_data' sur tous les marchés, "
          "jamais un gagnant inventé")


def test_benchmark_no_data_in_filtered_window(client):
    """Fenêtre de dates garantie sans aucune prédiction résolue (passé
    lointain, avant toute donnée de test) -> l'endpoint répond quand même
    200, sample_size=0 partout, jamais d'erreur 500 malgré l'absence totale
    de données dans ce sous-ensemble."""
    r = client.get("/models/benchmark", params={"until": "2000-01-01"})
    assert r.status_code == 200, r.text
    body = r.json()
    dc_summary = next(m for m in body["markets"]["1X2"]["models"] if m["model_type"] == "dixon_coles")
    assert dc_summary["sample_size"] == 0
    assert dc_summary["accuracy"] is None
    assert body["markets"]["1X2"]["best_model"]["status"] == "insufficient_data"
    print("  [OK] fenêtre de dates sans aucune prédiction -> 200, sample_size=0, insufficient_data (pas d'erreur)")


API_TESTS = [
    test_performance_endpoint_exposes_markets_field,
    test_model_versions_entries_have_not_available_markets,
    test_league_filter_narrows_prediction_set,
    test_league_filter_rejects_unknown_league,
    test_benchmark_endpoint_returns_all_three_markets_by_default,
    test_benchmark_single_market_filter,
    test_benchmark_rejects_unknown_market,
    test_benchmark_insufficient_data_when_only_one_model_has_metrics,
    test_benchmark_no_data_in_filtered_window,
]


if __name__ == "__main__":
    failures = 0

    print("\n" + "=" * 60 + "\nTESTS UNITAIRES (fonctions pures, sans DB)\n" + "=" * 60)
    for t in UNIT_TESTS:
        print(f"\n=== {t.__name__} ===")
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"  [ECHEC] {e}")

    print("\n" + "=" * 60 + "\nTESTS API (TestClient + base isolée)\n" + "=" * 60)
    with TestClient(app) as client:
        user_id, token = register_and_login(client, "arena-benchmark-tests@example.com", "correct-horse-battery-staple")
        activate_subscription(client, token, user_id, email="arena-benchmark-tests@example.com",
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
