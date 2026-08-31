"""
test_end_to_end_pipeline.py — Phase 8J : tests de api/app/ai/pipeline/.

Base isolée dédiée (jamais api/app.db). Le pipeline est purement
fonctionnel (aucun accès DB, aucun réseau) ; ce fichier vérifie
explicitement qu'aucune table n'est modifiée (§38 du prompt Phase 8J).

Usage : python api/test_end_to_end_pipeline.py
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_end_to_end_pipeline.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match, MatchStats
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion, TeamRating

from app.ai.arena.model_selection import SelectionDecision
from app.ai.arena.calibration_engine import CalibrationResult
from app.ai.arena.schemas import MarketMetrics
from app.ai.arena.service import _compute_market_metrics

from app.ai.pipeline.schemas import (
    PipelineInput, OddsInput, CalibrationInput, FeatureSnapshotInput, TemporalMetadataInput,
    PIPELINE_FINAL_STATUSES, VALUE_STAGE_STATUSES,
)
from app.ai.pipeline.orchestrator import run_pipeline
from app.ai.pipeline.shadow import run_shadow_batch, pipeline_assessment_to_observation

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
    return CalibrationResult(choice="isotonic", verdict="HELPFUL", raw_metrics=m, platt_metrics=None, isotonic_metrics=m,
                              raw_ece=0.05, platt_ece=None, isotonic_ece=0.02, train_sample_size=300, test_sample_size=200)


def _good_coverage():
    return {"total_features": 25, "missing": 1, "present": 24, "coverage_ratio": 0.96}


CUTOFF = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
ODDS_TS = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
KICKOFF = datetime(2026, 1, 1, 20, 0, tzinfo=UTC)


def _pi_1x2(**overrides):
    """§18 : cas synthétique 1X2 — Home=0.60/Draw=0.20/Away=0.20, odds Home=2.00/Draw=4.50/Away=5.00, marqué SYNTHETIC."""
    base = dict(
        match_id=1, league="Ligue1", kickoff=KICKOFF, as_of=CUTOFF, model="xgboost", model_version="xfoot-xgboost-v1",
        market="1X2", selection="home_win", probabilities={"home_win": 0.60, "draw": 0.20, "away_win": 0.20},
        calibration=CalibrationInput(probabilities=None, source="RAW", calibration_result=_good_calibration_result(), calibration_method_label="isotonic"),
        feature_snapshot=FeatureSnapshotInput(coverage=_good_coverage(), generated_at=ODDS_TS, snapshot_id="snapshot-id", team_mapping_confident=True),
        temporal_metadata=TemporalMetadataInput(cutoff_timestamp=CUTOFF, match_kickoff=KICKOFF),
        odds_input=OddsInput(
            odds_by_selection={"home_win": 2.00, "draw": 4.50, "away_win": 5.00},
            odds_timestamp=ODDS_TS, has_measured_timestamp=True, bookmaker="SYNTHETIC_BOOK", source_label="SYNTHETIC",
        ),
        selection_decision=_good_selection_decision(), sample_size=200, prediction_id=42,
    )
    base.update(overrides)
    return PipelineInput(**base)


def _pi_btts(**overrides):
    """§19 : Yes=0.60/No=0.40 + odds synthétiques."""
    base = dict(
        match_id=2, league="Ligue1", kickoff=KICKOFF, as_of=CUTOFF, model="xgboost", model_version="xfoot-xgboost-v1",
        market="BTTS", selection="yes", probabilities={"yes": 0.60, "no": 0.40},
        calibration=CalibrationInput(source="RAW", calibration_result=_good_calibration_result()),
        feature_snapshot=FeatureSnapshotInput(coverage=_good_coverage(), team_mapping_confident=True),
        temporal_metadata=TemporalMetadataInput(cutoff_timestamp=CUTOFF, match_kickoff=KICKOFF),
        odds_input=OddsInput(odds_by_selection={"yes": 1.80, "no": 2.10}, odds_timestamp=ODDS_TS, has_measured_timestamp=True, source_label="SYNTHETIC"),
        selection_decision=_good_selection_decision(), sample_size=150,
    )
    base.update(overrides)
    return PipelineInput(**base)


def _pi_ou(**overrides):
    """§20 : Over=0.55/Under=0.45 + odds synthétiques."""
    base = dict(
        match_id=3, league="Ligue1", kickoff=KICKOFF, as_of=CUTOFF, model="lightgbm", model_version="xfoot-lightgbm-v1",
        market="OVER_UNDER_2_5", selection="over", probabilities={"over": 0.55, "under": 0.45},
        calibration=CalibrationInput(source="RAW", calibration_result=_good_calibration_result()),
        feature_snapshot=FeatureSnapshotInput(coverage=_good_coverage(), team_mapping_confident=True),
        temporal_metadata=TemporalMetadataInput(cutoff_timestamp=CUTOFF, match_kickoff=KICKOFF),
        odds_input=OddsInput(odds_by_selection={"over": 1.90, "under": 1.95}, odds_timestamp=ODDS_TS, has_measured_timestamp=True, source_label="SYNTHETIC"),
        selection_decision=_good_selection_decision(), sample_size=150,
    )
    base.update(overrides)
    return PipelineInput(**base)


# ---------------------------------------------------------------------------
# 1. Construction / marchés (§17/§18/§19/§20)
# ---------------------------------------------------------------------------

def test_pipeline_construction_basic():
    result = run_pipeline(_pi_1x2())
    assert result.match_id == 1 and result.market == "1X2"
    assert result.final_status in PIPELINE_FINAL_STATUSES
    print(f"  [OK] construction basique -> final_status={result.final_status}")


def test_market_1x2_full_chain():
    result = run_pipeline(_pi_1x2())
    assert result.decision.eligibility == "ELIGIBLE"
    assert result.value is not None and result.value_stage_status == "EVALUATED"
    assert abs(result.value.expected_value - (0.60 * 2.00 - 1.0)) < 1e-9
    assert result.final_status == "VALUE_CANDIDATE"
    print(f"  [OK] 1X2 (§18) : EV={result.value.expected_value:+.3f}, edge={result.value.edge:+.3f}, final={result.final_status}")


def test_market_btts_full_chain():
    result = run_pipeline(_pi_btts())
    assert result.market == "BTTS" and result.value is not None
    assert result.value.market_probability_raw is not None and result.value.market_probability_normalized is not None
    print(f"  [OK] BTTS (§19) : market_prob_raw={result.value.market_probability_raw:.4f}, final={result.final_status}")


def test_market_over_under_full_chain():
    result = run_pipeline(_pi_ou())
    assert result.market == "OVER_UNDER_2_5" and result.value is not None
    print(f"  [OK] O/U 2.5 (§20) : EV={result.value.expected_value:+.3f}, final={result.final_status}")


# ---------------------------------------------------------------------------
# 2. Odds absentes / invalides (§10/§21/§22)
# ---------------------------------------------------------------------------

def test_no_odds_never_raises_never_fabricates_value():
    """
    §10/§21 : odds=None -> aucune exception, aucune valeur fabriquée. Constat
    architectural honnête (§1 : documenter, jamais contourner) : sans
    odds_timestamp, quality.classify_temporal_status (Phase 8H, réutilisée
    telle quelle par Phase 8I) retourne UNKNOWN (aucune référence temporelle
    à évaluer) -> la règle STRICTE, déjà testée en Phase 8I ("UNKNOWN ->
    INELIGIBLE"), s'applique alors MÊME en l'absence totale d'odds — ce
    pipeline ne la contourne PAS (§8 : ne jamais reconstruire les hard
    gates). Le critère réellement exigé par §21 ("Value=NOT_AVAILABLE/
    INSUFFICIENT_DATA", "Final : aucun signal de pari") reste garanti.
    """
    result = run_pipeline(_pi_1x2(odds_input=None))
    assert result.value is None and result.value_stage_status == "SKIPPED_NO_ODDS"
    assert result.final_status not in ("VALUE_CANDIDATE", "NO_VALUE")
    assert result.decision.quality_dimensions.temporal_quality == "UNKNOWN"
    print(f"  [OK] §10/§21 : odds=None -> aucune exception, value=None, final={result.final_status} "
          f"(eligibility={result.decision.eligibility}, jamais un signal de pari — voir docstring pour le constat architectural)")


def test_invalid_odds_never_positive_value():
    """
    §22 : odds=1.0/0.0/-1.5 -> jamais un Value Signal positif. Constat
    architectural (§1) : Phase 8I évalue AUSSI market_quality à partir des
    mêmes odds (app.ai.decision.quality.assess_market_quality, qui réutilise
    compute_market_probabilities, Phase 8H) — une cote invalide y échoue
    DÉJÀ le GATE_MARKET (Phase 8I) avant même que Phase 8H::build_value_signal
    ne soit appelé, faisant remonter la Decision à INSUFFICIENT_DATA (le
    Value stage est alors SKIPPED, jamais atteint). Ce n'est PAS un défaut :
    les deux couches indépendantes rejettent la donnée invalide, la première
    (Decision) gagnant simplement la course — le critère réellement exigé
    (aucun VALUE_CANDIDATE) reste garanti dans tous les cas.
    """
    for bad_odds in (1.0, 0.0, -1.5):
        pi = _pi_1x2(odds_input=OddsInput(
            odds_by_selection={"home_win": bad_odds, "draw": 4.50, "away_win": 5.00},
            odds_timestamp=ODDS_TS, has_measured_timestamp=True, source_label="SYNTHETIC",
        ))
        result = run_pipeline(pi)
        assert result.final_status not in ("VALUE_CANDIDATE", "NO_VALUE")
        assert result.value is None or result.value.status == "INVALID_ODDS"
        print(f"  [OK] §22 : odds={bad_odds} -> value={result.value.status if result.value else None}, "
              f"decision.eligibility={result.decision.eligibility}, final={result.final_status}")


# ---------------------------------------------------------------------------
# 3. Probabilité invalide (§23)
# ---------------------------------------------------------------------------

def test_invalid_probability_rejects_whole_pipeline():
    # §23 : Home=0.80, Draw=0.40, Away=0.20 -> somme=1.40, jamais corrigée.
    pi = _pi_1x2(probabilities={"home_win": 0.80, "draw": 0.40, "away_win": 0.20})
    result = run_pipeline(pi)
    assert result.final_status == "INELIGIBLE" and "INVALID_PROBABILITY" in result.reasons
    assert result.value is None  # jamais évalué sur une probabilité invalide
    print(f"  [OK] §23 : somme=1.40 -> {result.final_status} / {result.reasons}, value jamais calculé")


# ---------------------------------------------------------------------------
# 4. Temporal safety (§11/§12/§24/§25/§26)
# ---------------------------------------------------------------------------

def test_future_information_rejected_even_with_favorable_ev():
    # §24 : kickoff=20:00, odds_timestamp=20:10 (après kickoff) -> rejeté MÊME SI l'EV serait très favorable.
    future_ts = KICKOFF + timedelta(minutes=10)
    pi = _pi_1x2(
        probabilities={"home_win": 0.90, "draw": 0.05, "away_win": 0.05},  # EV très favorable si accepté
        odds_input=OddsInput(odds_by_selection={"home_win": 5.00, "draw": 4.50, "away_win": 5.00}, odds_timestamp=future_ts, has_measured_timestamp=True, source_label="SYNTHETIC"),
    )
    result = run_pipeline(pi)
    assert result.final_status == "INELIGIBLE"
    assert result.value is None  # jamais calculé, même si p=0.90/odds=5.00 serait un EV extrême
    print(f"  [OK] §24 : odds postérieures au kickoff -> {result.final_status}, value jamais calculé malgré EV favorable")


def test_temporal_unknown_never_verified():
    pi = _pi_1x2(odds_input=OddsInput(odds_by_selection={"home_win": 2.00, "draw": 4.50, "away_win": 5.00}, odds_timestamp=None, has_measured_timestamp=False, source_label="SYNTHETIC"))
    result = run_pipeline(pi)
    assert result.decision.quality_dimensions.temporal_quality == "UNKNOWN"
    assert result.final_status == "INELIGIBLE"
    print(f"  [OK] §25 : timestamp absent -> temporal=UNKNOWN (jamais TEMPORALLY_VERIFIED), final={result.final_status}")


def test_historical_unverified_stays_research_only():
    pi = _pi_1x2(odds_input=OddsInput(odds_by_selection={"home_win": 2.00, "draw": 4.50, "away_win": 5.00}, odds_timestamp=ODDS_TS, has_measured_timestamp=False, source_label="SYNTHETIC"))
    result = run_pipeline(pi)
    assert result.decision.quality_dimensions.temporal_quality == "HISTORICAL_UNVERIFIED"
    assert result.final_status == "RESEARCH_ONLY"
    assert result.value is not None  # calculable en recherche (Phase 8H l'autorise), mais jamais promu
    print(f"  [OK] §26 : historical_unverified -> final={result.final_status} (jamais production eligible, même si value={result.value.status})")


def test_quality_high_cannot_bypass_unknown_temporal():
    # §12 : Quality=HIGH mais temporal=UNKNOWN -> Decision != ELIGIBLE.
    pi = _pi_1x2(odds_input=OddsInput(odds_by_selection={"home_win": 2.00, "draw": 4.50, "away_win": 5.00}, odds_timestamp=None, has_measured_timestamp=False, source_label="SYNTHETIC"))
    result = run_pipeline(pi)
    assert result.decision.eligibility != "ELIGIBLE"
    print(f"  [OK] §12 : bonne qualité + temporal UNKNOWN -> decision.eligibility={result.decision.eligibility} (jamais ELIGIBLE)")


def test_decision_ineligible_never_produces_positive_value():
    # §13/§15 : Decision=INELIGIBLE -> Value jamais POSITIVE_VALUE (jamais même calculé ici).
    pi = _pi_1x2(odds_input=OddsInput(odds_by_selection={"home_win": 2.00, "draw": 4.50, "away_win": 5.00}, odds_timestamp=None, has_measured_timestamp=False, source_label="SYNTHETIC"))
    result = run_pipeline(pi)
    assert result.decision.eligibility == "INELIGIBLE"
    assert result.value is None
    assert result.final_status != "VALUE_CANDIDATE"
    print(f"  [OK] §13/§15 : Decision=INELIGIBLE -> value={result.value}, final={result.final_status}")


# ---------------------------------------------------------------------------
# 5. Provenance (§5/§27)
# ---------------------------------------------------------------------------

def test_provenance_preserved_intact():
    pi = _pi_1x2()
    result = run_pipeline(pi)
    p = result.provenance
    assert p.model_source == "xgboost" and p.model_version == "xfoot-xgboost-v1"
    assert p.calibration_source == "isotonic"
    assert p.feature_snapshot == "snapshot-id"
    assert p.odds_source == "SYNTHETIC"
    assert p.odds_timestamp == ODDS_TS and p.cutoff_timestamp == CUTOFF
    print(f"  [OK] §27 : provenance intacte -> {p}")


def test_provenance_never_fabricated_when_absent():
    pi = _pi_1x2(odds_input=None, model_version=None)
    result = run_pipeline(pi)
    assert result.provenance.odds_source is None and result.provenance.odds_timestamp is None
    assert result.provenance.model_version is None
    print("  [OK] §5 : provenance absente reste None/UNKNOWN, jamais fabriquée")


# ---------------------------------------------------------------------------
# 6. Déterminisme (§28/§29)
# ---------------------------------------------------------------------------

def test_deterministic_same_input_same_output():
    pi = _pi_1x2()
    r1, r2 = run_pipeline(pi), run_pipeline(pi)
    assert r1.final_status == r2.final_status and r1.decision.eligibility == r2.decision.eligibility
    assert (r1.value.expected_value if r1.value else None) == (r2.value.expected_value if r2.value else None)
    print("  [OK] §28 : même input -> même output (final_status/eligibility/EV)")


def test_input_immutability_across_evaluations():
    pi = _pi_1x2()
    before = (dict(pi.probabilities), dict(pi.odds_input.odds_by_selection))
    run_pipeline(pi)
    run_pipeline(pi)
    after = (dict(pi.probabilities), dict(pi.odds_input.odds_by_selection))
    assert before == after
    print("  [OK] §29 : PipelineInput jamais muté par deux évaluations successives")


# ---------------------------------------------------------------------------
# 7. Batch / isolation d'erreur (§30/§31)
# ---------------------------------------------------------------------------

def test_batch_continues_after_one_failure():
    valid_a = _pi_1x2(match_id=10)
    invalid_b = _pi_1x2(match_id=11, as_of=None, kickoff=None)  # -> evaluated_at manquant -> exception dans run_pipeline
    valid_c = _pi_1x2(match_id=12)

    results = run_shadow_batch([valid_a, invalid_b, valid_c])
    assert len(results) == 3
    assert results[0].final_status != "REJECTED" and results[0].error is None
    assert results[1].final_status == "REJECTED" and results[1].error is not None
    assert results[2].final_status != "REJECTED" and results[2].error is None
    print(f"  [OK] §30/§31 : A={results[0].final_status} B={results[1].final_status}(erreur isolée) C={results[2].final_status} — le batch continue")


def test_batch_never_writes_to_database():
    with Session(engine) as session:
        before = _row_counts(session)
        run_shadow_batch([_pi_1x2(), _pi_btts(), _pi_ou()])
        after = _row_counts(session)
    assert before == after
    print("  [OK] run_shadow_batch : aucune écriture DB")


# ---------------------------------------------------------------------------
# 8. Track Record adapter (§32)
# ---------------------------------------------------------------------------

def test_track_record_adapter_shape_compatible_with_arena_stats():
    result = run_pipeline(_pi_1x2())
    obs = pipeline_assessment_to_observation(result, actual_outcome="home_win")
    assert obs is not None and set(obs) == {"p_true", "probs", "actual", "correct"}
    metrics = _compute_market_metrics([obs])  # réutilise service.py, Phase 5 — jamais réimplémenté
    assert metrics.sample_size == 1
    print(f"  [OK] §32 : observation compatible avec service._compute_market_metrics -> {metrics}")


def test_track_record_adapter_none_when_ineligible():
    pi = _pi_1x2(odds_input=OddsInput(odds_by_selection={"home_win": 2.00, "draw": 4.50, "away_win": 5.00}, odds_timestamp=None, has_measured_timestamp=False, source_label="SYNTHETIC"))
    result = run_pipeline(pi)
    assert result.decision.eligibility == "INELIGIBLE"
    assert pipeline_assessment_to_observation(result, actual_outcome="home_win") is None
    print("  [OK] §32 : aucune observation produite pour une Decision INELIGIBLE (jamais fabriquée)")


# ---------------------------------------------------------------------------
# 9. Sécurité DB (§38) — le pipeline n'accède jamais à la DB.
# ---------------------------------------------------------------------------

def test_pipeline_never_touches_the_database():
    with Session(engine) as session:
        session.add(Match(league="Ligue1", date=datetime.combine(date(2026, 1, 1), datetime.min.time()),
                           home_team="A", away_team="B", home_goals=1, away_goals=0))
        session.commit()
        before = _row_counts(session)
        run_pipeline(_pi_1x2())
        run_pipeline(_pi_btts())
        run_pipeline(_pi_ou())
        after = _row_counts(session)
    assert before == after, f"le pipeline a modifié une table : avant={before} après={after}"
    print("  [OK] pipeline strictement pur : aucune table modifiée")


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
