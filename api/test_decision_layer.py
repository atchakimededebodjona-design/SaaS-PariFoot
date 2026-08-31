"""
test_decision_layer.py — Phase 8I : tests de api/app/ai/decision/.

Base isolée dédiée (jamais api/app.db). Le Decision Layer est purement
fonctionnel (aucun accès DB) ; ce fichier vérifie explicitement qu'aucune
table n'est modifiée (§37 du prompt Phase 8I).

Usage : python api/test_decision_layer.py
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_decision_layer.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match, MatchStats
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion, TeamRating

from app.ai.arena.model_selection import SelectionDecision
from app.ai.arena.calibration_engine import CalibrationResult
from app.ai.arena.schemas import MarketMetrics

from app.ai.decision.quality import (
    assess_model_quality, assess_calibration_quality, assess_data_quality,
    assess_sample_quality, assess_market_quality,
)
from app.ai.decision.confidence import (
    compute_overall_confidence, assess_prediction_quality, compute_model_disagreement, compute_research_score,
)
from app.ai.decision.eligibility import evaluate_eligibility
from app.ai.decision.schemas import QualityDimensions
from app.ai.decision.decision import (
    assess_decision, evaluate_shadow_prediction, validate_market_probabilities,
    validate_probability_value, to_value_engine_input, RESEARCH_DEFAULT_THRESHOLDS,
)

init_db()
UTC = timezone.utc


def _row_counts(session):
    return {
        "match": len(session.exec(select(Match)).all()),
        "match_stats": len(session.exec(select(MatchStats)).all()),
        "model_predictions": len(session.exec(select(ModelPrediction)).all()),
        "model_versions": len(session.exec(select(ModelVersion)).all()),
        "team_ratings": len(session.exec(select(TeamRating)).all()),
    }


def _good_selection_decision():
    return SelectionDecision(status="selected", market="1X2", selected_model_type="xgboost", runner_up_model_type="elo", windows_evaluated=5)


def _good_calibration_result():
    m = MarketMetrics(sample_size=200, accuracy=0.55, log_loss=0.95, brier_score=0.6)
    return CalibrationResult(choice="platt", verdict="HELPFUL", raw_metrics=m, platt_metrics=m, isotonic_metrics=None,
                              raw_ece=0.05, platt_ece=0.02, isotonic_ece=None, train_sample_size=300, test_sample_size=200)


def _good_coverage():
    return {"total_features": 25, "missing": 1, "present": 24, "coverage_ratio": 0.96}


def _assess_decision_kwargs(**overrides):
    cutoff = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    ts = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    base = dict(
        prediction_id=1, model="xgboost", market="1X2",
        probabilities={"home_win": 0.55, "draw": 0.25, "away_win": 0.20}, selection="home_win", probability_source="CALIBRATED",
        selection_decision=_good_selection_decision(), calibration_result=_good_calibration_result(),
        feature_coverage=_good_coverage(), team_mapping_confident=True,
        odds_timestamp=ts, cutoff_timestamp=cutoff, has_measured_odds_timestamp=True,
        sample_size=200, evaluated_at=cutoff,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. Dimensions individuelles (§4-§9)
# ---------------------------------------------------------------------------

def test_model_quality_never_high_without_decision():
    assert assess_model_quality(None) == "UNKNOWN"
    assert assess_model_quality(SelectionDecision(status="selected", market="1X2")) == "HIGH"
    assert assess_model_quality(SelectionDecision(status="not_significant", market="1X2")) == "MEDIUM"
    assert assess_model_quality(SelectionDecision(status="unstable", market="1X2")) == "LOW"
    assert assess_model_quality(SelectionDecision(status="insufficient_data", market="1X2")) == "UNKNOWN"
    print("  [OK] assess_model_quality : UNKNOWN par défaut, jamais HIGH sans SelectionDecision réelle")


def test_calibration_quality_requires_helpful_verdict():
    assert assess_calibration_quality(None) == "UNKNOWN"
    assert assess_calibration_quality(_good_calibration_result()) == "CALIBRATED"
    neutral = CalibrationResult(choice="none", verdict="NEUTRAL", raw_metrics=MarketMetrics(sample_size=50), platt_metrics=None, isotonic_metrics=None, raw_ece=None, platt_ece=None, isotonic_ece=None, train_sample_size=50, test_sample_size=50)
    assert assess_calibration_quality(neutral) == "UNCALIBRATED"
    insufficient = CalibrationResult(choice="none", verdict="INSUFFICIENT_DATA", raw_metrics=MarketMetrics(sample_size=5), platt_metrics=None, isotonic_metrics=None, raw_ece=None, platt_ece=None, isotonic_ece=None, train_sample_size=5, test_sample_size=5)
    assert assess_calibration_quality(insufficient) == "INSUFFICIENT_DATA"
    print("  [OK] assess_calibration_quality : CALIBRATED seulement si verdict=HELPFUL et méthode réelle appliquée")


def test_data_quality_reuses_feature_registry_coverage():
    assert assess_data_quality(None) == "UNKNOWN"
    assert assess_data_quality({"coverage_ratio": 0.95}) == "HIGH"
    assert assess_data_quality({"coverage_ratio": 0.95}, team_mapping_confident=False) == "MEDIUM"
    assert assess_data_quality({"coverage_ratio": 0.6}) == "MEDIUM"
    assert assess_data_quality({"coverage_ratio": 0.2}) == "LOW"
    print("  [OK] assess_data_quality : dérivé de snapshot_coverage (Phase 8A), dégradé si mapping équipe douteux")


def test_sample_quality_never_confuses_size_with_accuracy():
    assert assess_sample_quality(None, 100, 30) == "UNKNOWN"
    assert assess_sample_quality(150, 100, 30) == "SUFFICIENT"
    assert assess_sample_quality(50, 100, 30) == "LIMITED"
    assert assess_sample_quality(5, 100, 30) == "INSUFFICIENT"
    print("  [OK] assess_sample_quality : uniquement basé sur la taille, jamais sur une métrique de performance")


def test_market_quality_not_available_without_odds():
    assert assess_market_quality(None) == "NOT_AVAILABLE"
    assert assess_market_quality({}) == "NOT_AVAILABLE"
    assert assess_market_quality({"home_win": 2.0, "draw": 4.0, "away_win": 5.0}, bookmaker_count=3) == "HIGH"
    assert assess_market_quality({"home_win": 2.0, "draw": 4.0, "away_win": 5.0}) == "MEDIUM"
    assert assess_market_quality({"home_win": 0.5, "draw": 4.0, "away_win": 5.0}) == "LOW"
    print("  [OK] assess_market_quality : NOT_AVAILABLE sans odds (jamais inventé), jamais fabriqué")


# ---------------------------------------------------------------------------
# 2. Confidence framework — no compensation (§10-§12)
# ---------------------------------------------------------------------------

def _dims(**overrides):
    base = dict(model_quality="HIGH", calibration_quality="CALIBRATED", data_quality="HIGH",
                temporal_quality="TEMPORALLY_VERIFIED", sample_quality="SUFFICIENT", market_quality="NOT_AVAILABLE")
    base.update(overrides)
    return QualityDimensions(**base)


def test_confidence_never_equals_edge_or_probability_by_construction():
    # Le Confidence Framework n'accepte NULLE PART un paramètre "edge" ou "probability" — vérifié par signature.
    import inspect
    sig = inspect.signature(compute_overall_confidence)
    assert list(sig.parameters) == ["dims"]
    print("  [OK] compute_overall_confidence ne peut structurellement pas recevoir edge/probability/model_score (§2)")


def test_overall_confidence_high_requires_all_strong_dimensions():
    assert compute_overall_confidence(_dims()) == "HIGH"
    print("  [OK] Case A : toutes dimensions fortes -> HIGH")


def test_overall_confidence_unknown_dimension_never_upgraded():
    assert compute_overall_confidence(_dims(calibration_quality="UNKNOWN")) == "UNKNOWN"
    print("  [OK] Case B : calibration inconnue -> UNKNOWN, jamais HIGH automatiquement")


def test_overall_confidence_low_model_cannot_be_compensated():
    result = compute_overall_confidence(_dims(model_quality="LOW", calibration_quality="CALIBRATED", data_quality="HIGH", sample_quality="SUFFICIENT"))
    assert result == "LOW"
    print("  [OK] §12 : model_quality=LOW -> LOW, jamais rattrapé par calibration/data/sample excellents (no compensation)")


def test_overall_confidence_temporal_future_is_ineligible():
    assert compute_overall_confidence(_dims(temporal_quality="FUTURE_INFORMATION")) == "INELIGIBLE"
    print("  [OK] Case C : FUTURE_INFORMATION -> INELIGIBLE")


def test_overall_confidence_temporal_unknown_is_ineligible():
    assert compute_overall_confidence(_dims(temporal_quality="UNKNOWN")) == "INELIGIBLE"
    print("  [OK] Case D : temporal UNKNOWN -> INELIGIBLE")


def test_overall_confidence_historical_unverified_never_high():
    assert compute_overall_confidence(_dims(temporal_quality="HISTORICAL_UNVERIFIED")) in ("MEDIUM", "LOW")
    print("  [OK] Case E : historical_unverified -> jamais HIGH, plafonné (§41 : != temporally_verified)")


def test_model_disagreement_is_informative_only():
    assert compute_model_disagreement({"xgboost": 0.60}) is None
    d = compute_model_disagreement({"xgboost": 0.60, "elo": 0.55, "dixon_coles": 0.50})
    assert abs(d - 0.10) < 1e-9
    print(f"  [OK] §20 : disagreement={d:.2f} calculé, jamais transformé automatiquement en confiance")


def test_research_score_never_called_confidence_and_stays_experimental():
    score = compute_research_score(_dims())
    assert score is not None and 0.0 <= score <= 1.0
    assert compute_research_score(_dims(market_quality="NOT_AVAILABLE")) is not None  # market exclu du calcul, jamais bloquant
    print(f"  [OK] §26 : research_score={score:.2f}, expérimental, jamais nommé 'confidence', jamais requis")


# ---------------------------------------------------------------------------
# 3. Hard gates / eligibility (§13-§16)
# ---------------------------------------------------------------------------

def test_gate_order_and_all_gates_always_returned():
    elig = evaluate_eligibility(_dims())
    names = [g.name for g in elig.gates]
    assert names == ["GATE_DATA", "GATE_MODEL", "GATE_CALIBRATION", "GATE_TEMPORAL", "GATE_SAMPLE", "GATE_MARKET"]
    print(f"  [OK] §15 : 6 gates toujours retournés, dans l'ordre documenté : {names}")


def test_eligible_when_all_gates_pass():
    elig = evaluate_eligibility(_dims())
    assert elig.status == "ELIGIBLE"
    print("  [OK] tous gates PASS/NOT_APPLICABLE -> ELIGIBLE")


def test_market_not_available_is_not_applicable_never_blocking():
    elig = evaluate_eligibility(_dims(market_quality="NOT_AVAILABLE"))
    market_gate = next(g for g in elig.gates if g.name == "GATE_MARKET")
    assert market_gate.status == "NOT_APPLICABLE"
    assert elig.status == "ELIGIBLE"
    print("  [OK] §9 : MARKET_QUALITY=NOT_AVAILABLE -> gate NOT_APPLICABLE, jamais bloquant")


def test_case_F_insufficient_sample():
    elig = evaluate_eligibility(_dims(sample_quality="INSUFFICIENT"))
    assert elig.status == "INSUFFICIENT_DATA" and "INSUFFICIENT_SAMPLE" in elig.reasons
    print("  [OK] Case F : sample insuffisant -> INSUFFICIENT_DATA / INSUFFICIENT_SAMPLE")


def test_case_H_missing_critical_feature_documented_rule():
    elig = evaluate_eligibility(_dims(data_quality="LOW"))
    assert elig.status == "INSUFFICIENT_DATA" and "DATA_INCOMPLETE" in elig.reasons
    print("  [OK] Case H : data_quality=LOW (feature critique manquante) -> INSUFFICIENT_DATA / DATA_INCOMPLETE (règle documentée)")


def test_case_E_historical_unverified_is_research_only():
    elig = evaluate_eligibility(_dims(temporal_quality="HISTORICAL_UNVERIFIED"))
    assert elig.status == "RESEARCH_ONLY"
    print("  [OK] Case E : historical_unverified -> RESEARCH_ONLY")


def test_model_unstable_is_ineligible_not_insufficient_data():
    elig = evaluate_eligibility(_dims(model_quality="LOW"))
    assert elig.status == "INELIGIBLE" and "MODEL_UNSTABLE" in elig.reasons
    print("  [OK] model instable -> INELIGIBLE (signal négatif réel, distinct d'un simple manque de données)")


def test_all_reasons_returned_never_hidden():
    elig = evaluate_eligibility(_dims(data_quality="LOW", sample_quality="INSUFFICIENT", calibration_quality="INSUFFICIENT_DATA"))
    assert "DATA_INCOMPLETE" in elig.reasons and "INSUFFICIENT_SAMPLE" in elig.reasons and "CALIBRATION_UNAVAILABLE" in elig.reasons
    print(f"  [OK] §15 : toutes les raisons retournées simultanément : {elig.reasons}")


# ---------------------------------------------------------------------------
# 4. Adversarial tests (§33/§34) — un edge/probabilité élevés ne contournent JAMAIS un hard gate.
# ---------------------------------------------------------------------------

def test_adversarial_high_probability_cannot_bypass_unknown_temporal():
    result = assess_decision(**_assess_decision_kwargs(
        probabilities={"home_win": 0.70, "draw": 0.20, "away_win": 0.10},
        odds_timestamp=None, cutoff_timestamp=None,  # -> TEMPORAL UNKNOWN
    ))
    assert result.eligibility == "INELIGIBLE"
    print(f"  [OK] §33 : probability=0.70 (haute) + temporal UNKNOWN -> {result.eligibility} (jamais contourné)")


def test_adversarial_all_high_but_future_information_is_ineligible():
    kickoff = datetime(2026, 1, 1, 20, 0, tzinfo=UTC)
    cutoff = kickoff - timedelta(hours=6)
    future_ts = kickoff - timedelta(hours=1)
    result = assess_decision(**_assess_decision_kwargs(
        odds_timestamp=future_ts, cutoff_timestamp=cutoff, match_kickoff=kickoff, has_measured_odds_timestamp=True,
        evaluated_at=cutoff,
    ))
    assert result.eligibility == "INELIGIBLE" and "FUTURE_INFORMATION" in result.reasons
    print(f"  [OK] §34 : tout HIGH (model/calib/data/sample) + future_information -> {result.eligibility}")


# ---------------------------------------------------------------------------
# 5. Probability validation (§18/§19)
# ---------------------------------------------------------------------------

def test_probability_value_bounds():
    assert validate_probability_value(0.5) and validate_probability_value(0.0) and validate_probability_value(1.0)
    assert not validate_probability_value(-0.1) and not validate_probability_value(1.1) and not validate_probability_value(None)
    print("  [OK] validate_probability_value : [0,1] strict")


def test_market_probabilities_must_sum_to_one_within_tolerance():
    assert validate_market_probabilities("1X2", {"home_win": 0.5, "draw": 0.3, "away_win": 0.2}) is None
    assert validate_market_probabilities("1X2", {"home_win": 0.5, "draw": 0.3, "away_win": 0.4}) == "INVALID_PROBABILITY"
    assert validate_market_probabilities("BTTS", {"yes": 0.5, "no": 0.5}) is None
    assert validate_market_probabilities("1X2", {"home_win": 0.5, "draw": 0.5}) == "INVALID_PROBABILITY"  # issue manquante
    assert validate_market_probabilities("NOT_A_MARKET", {"a": 1.0}) == "INVALID_PROBABILITY"
    print("  [OK] §18/§19 : marché/issues/somme validés, jamais corrigés silencieusement")


def test_case_G_invalid_probability_is_ineligible():
    result = assess_decision(**_assess_decision_kwargs(probabilities={"home_win": 0.9, "draw": 0.9, "away_win": 0.9}))
    assert result.eligibility == "INELIGIBLE" and "INVALID_PROBABILITY" in result.reasons
    print("  [OK] Case G : probabilité invalide -> INELIGIBLE, prime sur tout le reste")


def test_missing_probability_is_ineligible():
    result = assess_decision(**_assess_decision_kwargs(probabilities=None))
    assert result.eligibility == "INELIGIBLE" and "MISSING_PROBABILITY" in result.reasons
    print("  [OK] probabilité absente -> INELIGIBLE / MISSING_PROBABILITY")


# ---------------------------------------------------------------------------
# 6. Cases A/B/C/D/E complets via assess_decision, calibration source (§22), shadow (§31), value interface (§30)
# ---------------------------------------------------------------------------

def test_case_A_full_pipeline_eligible_high():
    result = assess_decision(**_assess_decision_kwargs())
    assert result.eligibility == "ELIGIBLE" and result.confidence.overall_status == "HIGH"
    print(f"  [OK] Case A (pipeline complet) : eligibility={result.eligibility}, confidence={result.confidence.overall_status}")


def test_probability_source_never_silently_swapped():
    kwargs_raw = _assess_decision_kwargs(probability_source="RAW")
    kwargs_bad = _assess_decision_kwargs(probability_source="MAYBE")
    r = assess_decision(**kwargs_raw)
    assert r.probability == 0.55  # même valeur quel que soit le label — le label documente juste la provenance, jamais recalculé
    try:
        assess_decision(**kwargs_bad)
        raise AssertionError("devait lever ValueError")
    except ValueError:
        pass
    print("  [OK] §22 : probability_source documente RAW/CALIBRATED sans jamais recalculer/échanger la valeur")


def test_shadow_prediction_is_pure_evaluation():
    with Session(engine) as session:
        before = _row_counts(session)
        result = evaluate_shadow_prediction(**_assess_decision_kwargs())
        after = _row_counts(session)
    assert before == after
    assert result.eligibility in ("ELIGIBLE", "RESEARCH_ONLY", "INELIGIBLE", "INSUFFICIENT_DATA", "UNKNOWN")
    print(f"  [OK] §31 : evaluate_shadow_prediction -> {result.eligibility}, aucune écriture DB, aucun pari généré")


def test_value_engine_interface_is_a_pure_contract_never_connected():
    result = assess_decision(**_assess_decision_kwargs())
    payload = to_value_engine_input(result)
    assert set(payload) == {"prediction_id", "market", "probability", "eligibility", "confidence_overall_status", "temporal_status", "research_only", "timestamp"}
    import app.ai.decision.decision as decision_module
    assert "app.ai.value" not in decision_module.__file__  # sanity : le module lui-même
    import inspect
    src = inspect.getsource(decision_module)
    assert "app.ai.value" not in src  # §30 : aucune connexion réelle, jamais un import de api/app/ai/value/
    print("  [OK] §30 : to_value_engine_input() documente un contrat pur, api/app/ai/value/ n'est jamais importé")


# ---------------------------------------------------------------------------
# 7. Déterminisme (§35)
# ---------------------------------------------------------------------------

def test_deterministic_same_input_same_output():
    kwargs = _assess_decision_kwargs()
    r1, r2 = assess_decision(**kwargs), assess_decision(**kwargs)
    assert r1.eligibility == r2.eligibility and r1.confidence.overall_status == r2.confidence.overall_status and r1.reasons == r2.reasons
    print("  [OK] §35 : même input -> même eligibility/confidence/reasons")


# ---------------------------------------------------------------------------
# 8. Sécurité DB (§37) — le Decision Layer n'accède jamais à la DB.
# ---------------------------------------------------------------------------

def test_decision_layer_never_touches_the_database():
    with Session(engine) as session:
        session.add(Match(league="Ligue1", date=datetime.combine(date(2026, 1, 1), datetime.min.time()),
                           home_team="A", away_team="B", home_goals=1, away_goals=0))
        session.commit()
        before = _row_counts(session)
        assess_decision(**_assess_decision_kwargs())
        evaluate_eligibility(_dims())
        compute_overall_confidence(_dims())
        after = _row_counts(session)
    assert before == after, f"le Decision Layer a modifié une table : avant={before} après={after}"
    print("  [OK] Decision Layer strictement pur : aucune table modifiée")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    failures = 0
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  [FAIL] {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests OK")
    cleanup_db(DB_PATH)
    sys.exit(1 if failures else 0)
