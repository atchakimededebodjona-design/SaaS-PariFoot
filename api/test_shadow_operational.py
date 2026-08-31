"""
test_shadow_operational.py — Phase 8K : tests de api/app/ai/shadow/.

Base isolée dédiée (jamais api/app.db). Utilise un ShadowDecisionStore
TEMPORAIRE (jamais reports/shadow/shadow_decision_store.json, jamais
partagé entre tests) — voir _test_support.py pour la précaution DB
équivalente.

Usage : python api/test_shadow_operational.py
"""

import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_shadow_operational.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match, MatchStats
from app.models.model_prediction import ModelPrediction
from app.models.prediction_log import PredictionLog
from app.models.team_rating import ModelVersion, TeamRating, next_version_name

from app.ai.pipeline.schemas import PipelineInput, CalibrationInput, FeatureSnapshotInput, TemporalMetadataInput
from app.ai.pipeline.orchestrator import run_pipeline
from app.ai.pipeline.shadow import run_shadow_batch

from app.ai.shadow.schemas import ShadowDecisionRecord, ShadowResolution, pending_resolution, RESOLUTION_STATES
from app.ai.shadow.tracking import ShadowDecisionStore, capture_shadow_decision, dedup_key
from app.ai.shadow.resolution import find_candidate_results, resolve_record
from app.ai.shadow.metrics import compute_shadow_track_record, value_tracking_status
from app.ai.shadow.replay import measure_data_reality, find_replay_candidates, build_pipeline_input_for_replay

init_db()
UTC = timezone.utc
CUTOFF = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
ODDS_TS = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
KICKOFF = datetime(2026, 1, 1, 20, 0, tzinfo=UTC)


def _row_counts(session):
    return {
        "match": len(session.exec(select(Match)).all()),
        "match_stats": len(session.exec(select(MatchStats)).all()),
        "model_predictions": len(session.exec(select(ModelPrediction)).all()),
        "model_versions": len(session.exec(select(ModelVersion)).all()),
        "team_ratings": len(session.exec(select(TeamRating)).all()),
    }


def _temp_store() -> ShadowDecisionStore:
    fd, name = tempfile.mkstemp(suffix=".json")
    import os
    os.close(fd)  # Windows : le descripteur doit être fermé avant de pouvoir supprimer/réutiliser le chemin
    tmp = Path(name)
    tmp.unlink()  # n'existe pas encore -> store vide au premier load()
    return ShadowDecisionStore(path=tmp)


def _pi_1x2(**overrides):
    base = dict(
        match_id=1, league="Ligue1", kickoff=KICKOFF, as_of=CUTOFF, model="xgboost", model_version="xfoot-xgboost-v1",
        market="1X2", selection="home_win", probabilities={"home_win": 0.60, "draw": 0.20, "away_win": 0.20},
        calibration=CalibrationInput(source="RAW"),
        feature_snapshot=FeatureSnapshotInput(coverage={"total_features": 25, "missing": 1, "present": 24, "coverage_ratio": 0.96}, team_mapping_confident=True),
        temporal_metadata=TemporalMetadataInput(cutoff_timestamp=CUTOFF, match_kickoff=KICKOFF),
        odds_input=None, selection_decision=None, sample_size=None,
    )
    base.update(overrides)
    return PipelineInput(**base)


def _capture(store, **pi_overrides):
    pi = _pi_1x2(**pi_overrides)
    assessment = run_pipeline(pi)
    record, created = capture_shadow_decision(store, pi, assessment, home_team="Team A", away_team="Team B")
    return record, created, assessment


# ---------------------------------------------------------------------------
# 1. Snapshot creation / immutability (§1/§4/§5)
# ---------------------------------------------------------------------------

def test_snapshot_creation_captures_pipeline_output():
    store = _temp_store()
    record, created, assessment = _capture(store)
    assert created is True
    assert record.eligibility == assessment.decision.eligibility
    assert record.status == assessment.final_status
    assert record.market_probabilities_raw == {"home_win": 0.60, "draw": 0.20, "away_win": 0.20}
    print(f"  [OK] capture -> shadow_id={record.shadow_id}, status={record.status}, eligibility={record.eligibility}")


def test_snapshot_is_frozen_dataclass_structurally_immutable():
    store = _temp_store()
    record, _, _ = _capture(store)
    try:
        record.status = "TAMPERED"
        raise AssertionError("devait lever FrozenInstanceError")
    except Exception as e:
        assert "frozen" in str(type(e).__name__).lower() or "FrozenInstance" in str(type(e))
    print("  [OK] §5 : ShadowDecisionRecord est un dataclass frozen — toute réassignation lève une exception")


def test_second_capture_never_overwrites_first_even_with_different_pipeline_result():
    store = _temp_store()
    record1, created1, _ = _capture(store)
    # même clé logique (match_id/market/model_version/as_of identiques) mais probabilités "différentes" simulées :
    record2, created2, _ = _capture(store, probabilities={"home_win": 0.99, "draw": 0.005, "away_win": 0.005})
    assert created1 is True and created2 is False
    stored_record, _ = store.get(record1.shadow_id)
    assert stored_record.market_probabilities_raw == {"home_win": 0.60, "draw": 0.20, "away_win": 0.20}
    print("  [OK] §5/§6 : le premier snapshot capturé n'est jamais écrasé par une seconde capture de même clé")


# ---------------------------------------------------------------------------
# 2. Deduplication (§6/§7)
# ---------------------------------------------------------------------------

def test_dedup_same_key_prevented():
    store = _temp_store()
    _, created1, _ = _capture(store)
    _, created2, _ = _capture(store)
    assert created1 is True and created2 is False
    assert len(store.all()) == 1
    print("  [OK] §6/§7 : (match_id, market, model_version, as_of) identiques -> un seul record, run 2 = 0 nouveau")


def test_dedup_different_market_allowed():
    store = _temp_store()
    _, c1, _ = _capture(store, market="1X2", selection="home_win")
    _, c2, _ = _capture(store, market="BTTS", selection="yes", probabilities={"yes": 0.55, "no": 0.45})
    assert c1 and c2 and len(store.all()) == 2
    print("  [OK] §7 : deux marchés différents -> deux records autorisés")


def test_dedup_different_model_allowed():
    store = _temp_store()
    _, c1, _ = _capture(store, model="xgboost", model_version="xfoot-xgboost-v1")
    _, c2, _ = _capture(store, model="lightgbm", model_version="xfoot-lightgbm-v1")
    assert c1 and c2 and len(store.all()) == 2
    print("  [OK] §7 : deux modèles différents -> deux records autorisés")


def test_dedup_different_cutoff_allowed():
    store = _temp_store()
    _, c1, _ = _capture(store, as_of=CUTOFF)
    _, c2, _ = _capture(store, as_of=CUTOFF - timedelta(hours=6))
    assert c1 and c2 and len(store.all()) == 2
    print("  [OK] §7 : deux cutoffs différents -> deux records autorisés")


# ---------------------------------------------------------------------------
# 3. Resolution / conflict / pending exclusion (§12-§14/§36-§38)
# ---------------------------------------------------------------------------

def _seed_match_and_prediction(session, league, match_date, home, away, hg, ag, model_type="xgboost"):
    m = session.exec(select(Match).where(Match.league == league, Match.date == datetime.combine(match_date, datetime.min.time()), Match.home_team == home, Match.away_team == away)).first()
    if m is None:
        session.add(Match(league=league, date=datetime.combine(match_date, datetime.min.time()), home_team=home, away_team=away, home_goals=hg, away_goals=ag))
        session.commit()
    version = ModelVersion(name=next_version_name(session, "test-shadow"), model_type=model_type, trained_at=datetime.now(timezone.utc), is_active=False)
    session.add(version); session.commit(); session.refresh(version)
    mp = ModelPrediction(league=league, match_date=match_date, home_team=home, away_team=away, model_type=model_type,
                          model_version_id=version.id, source="backtest", status="resolved",
                          result_home_goals=hg, result_away_goals=ag,
                          prob_home=0.5, prob_draw=0.3, prob_away=0.2, pick_1x2="home_win")
    session.add(mp); session.commit()
    return version


def test_resolution_pending_to_resolved():
    with Session(engine) as session:
        _seed_match_and_prediction(session, "Ligue1", date(2026, 2, 1), "Real Team A", "Real Team B", 2, 1, model_type="dixon_coles")
        record = ShadowDecisionRecord(
            shadow_id="k1", match_id=None, league="Ligue1", home_team="Real Team A", away_team="Real Team B",
            kickoff=datetime(2026, 2, 1, tzinfo=UTC), as_of=datetime(2026, 2, 1, tzinfo=UTC),
            model_type="dixon_coles", model_version=None, calibration_source=None, market="1X2", selection="home_win",
            raw_probability=0.5, calibrated_probability=None, market_probabilities_raw={"home_win": 0.5, "draw": 0.3, "away_win": 0.2},
            market_probabilities_calibrated=None, probability_source="RAW", quality={}, confidence="UNKNOWN",
            eligibility="ELIGIBLE", value_status=None, odds_source=None, odds_timestamp=None, temporal_status="TEMPORALLY_VERIFIED",
            provenance={}, status="NOT_AVAILABLE", created_at=datetime.now(timezone.utc),
        )
        resolution = resolve_record(session, record, pending_resolution())
    assert resolution.result_status == "RESOLVED" and resolution.actual_outcome == "home_win" and resolution.candidate_correct is True
    print(f"  [OK] §36 : PENDING -> RESOLVED, actual={resolution.actual_outcome}, correct={resolution.candidate_correct}")


def test_resolution_idempotent_second_call_no_change():
    with Session(engine) as session:
        _seed_match_and_prediction(session, "Ligue1", date(2026, 2, 2), "Idem A", "Idem B", 1, 1, model_type="dixon_coles")
        record = ShadowDecisionRecord(
            shadow_id="k2", match_id=None, league="Ligue1", home_team="Idem A", away_team="Idem B",
            kickoff=datetime(2026, 2, 2, tzinfo=UTC), as_of=datetime(2026, 2, 2, tzinfo=UTC),
            model_type="dixon_coles", model_version=None, calibration_source=None, market="1X2", selection="draw",
            raw_probability=0.3, calibrated_probability=None, market_probabilities_raw={"home_win": 0.5, "draw": 0.3, "away_win": 0.2},
            market_probabilities_calibrated=None, probability_source="RAW", quality={}, confidence="UNKNOWN",
            eligibility="ELIGIBLE", value_status=None, odds_source=None, odds_timestamp=None, temporal_status="TEMPORALLY_VERIFIED",
            provenance={}, status="NOT_AVAILABLE", created_at=datetime.now(timezone.utc),
        )
        r1 = resolve_record(session, record, pending_resolution())
        r2 = resolve_record(session, record, r1)  # deuxième appel avec la résolution DÉJÀ résolue
    assert r1.result_status == "RESOLVED" and r2 == r1
    print("  [OK] §36 : deuxième résolution -> aucun changement (r2 == r1, jamais remplacée)")


def test_conflict_detected_never_arbitrary():
    with Session(engine) as session:
        league, match_date, home, away = "Ligue1", date(2026, 2, 3), "Conf A", "Conf B"
        session.add(Match(league=league, date=datetime.combine(match_date, datetime.min.time()), home_team=home, away_team=away, home_goals=1, away_goals=0))  # match=HOME
        session.commit()
        version = ModelVersion(name=next_version_name(session, "test-conflict"), model_type="xgboost", trained_at=datetime.now(timezone.utc), is_active=False)
        session.add(version); session.commit(); session.refresh(version)
        session.add(ModelPrediction(league=league, match_date=match_date, home_team=home, away_team=away, model_type="xgboost",
                                     model_version_id=version.id, source="backtest", status="resolved",
                                     result_home_goals=0, result_away_goals=1,  # model_predictions=AWAY -> désaccord avec match=HOME
                                     prob_home=0.5, prob_draw=0.3, prob_away=0.2, pick_1x2="home_win"))
        session.commit()

        record = ShadowDecisionRecord(
            shadow_id="k3", match_id=None, league=league, home_team=home, away_team=away,
            kickoff=datetime(2026, 2, 3, tzinfo=UTC), as_of=datetime(2026, 2, 3, tzinfo=UTC),
            model_type="xgboost", model_version=None, calibration_source=None, market="1X2", selection="home_win",
            raw_probability=0.5, calibrated_probability=None, market_probabilities_raw={"home_win": 0.5, "draw": 0.3, "away_win": 0.2},
            market_probabilities_calibrated=None, probability_source="RAW", quality={}, confidence="UNKNOWN",
            eligibility="ELIGIBLE", value_status=None, odds_source=None, odds_timestamp=None, temporal_status="TEMPORALLY_VERIFIED",
            provenance={}, status="NOT_AVAILABLE", created_at=datetime.now(timezone.utc),
        )
        resolution = resolve_record(session, record, pending_resolution())
    assert resolution.result_status == "CONFLICT"
    assert resolution.conflict_sources["match"] == [1, 0] and resolution.conflict_sources["model_predictions"] == [0, 1]
    print(f"  [OK] §37 : sources en désaccord -> CONFLICT, jamais choisi arbitrairement : {resolution.conflict_sources}")


def test_pending_excluded_from_track_record():
    store = _temp_store()
    resolved_record, _, _ = _capture(store)
    resolved_resolution = ShadowResolution(result_status="RESOLVED", actual_outcome="home_win", candidate_correct=True, resolved_at=datetime.now(timezone.utc))
    store.update_resolution(resolved_record.shadow_id, resolved_resolution)

    pending_record, _, _ = _capture(store, match_id=2)
    # reste PENDING (jamais résolu)

    entries = store.all()
    tr = compute_shadow_track_record(entries, market="1X2")
    assert tr["status"] == "ok" and tr["sample_size"] == 1 and tr["pending"] == 1
    print(f"  [OK] §15/§38 : sample_size={tr['sample_size']} (1 resolved), pending={tr['pending']} exclu des métriques")


# ---------------------------------------------------------------------------
# 4. Track record filters (§16/§39/§40/§41/§42)
# ---------------------------------------------------------------------------

def _resolved_entry(league, market, model_type, match_id, kickoff, correct=True):
    store = _temp_store()
    probs = {"home_win": 0.6, "draw": 0.2, "away_win": 0.2} if market == "1X2" else {"yes": 0.6, "no": 0.4} if market == "BTTS" else {"over": 0.6, "under": 0.4}
    selection = "home_win" if market == "1X2" else ("yes" if market == "BTTS" else "over")
    record, _, _ = _capture(store, league=league, market=market, selection=selection, probabilities=probs, model=model_type, match_id=match_id, kickoff=kickoff, as_of=kickoff)
    actual = selection if correct else ("away_win" if market == "1X2" else ("no" if market == "BTTS" else "under"))
    resolution = ShadowResolution(result_status="RESOLVED", actual_outcome=actual, candidate_correct=correct, resolved_at=datetime.now(timezone.utc))
    return record, resolution


def test_league_filter_matches_phase7_behavior():
    e1 = _resolved_entry("Ligue1", "1X2", "xgboost", 1, KICKOFF)
    e2 = _resolved_entry("PremierLeague", "1X2", "xgboost", 2, KICKOFF)
    entries = [e1, e2]
    tr_global = compute_shadow_track_record(entries, market="1X2")
    tr_ligue1 = compute_shadow_track_record(entries, market="1X2", league="Ligue1")
    assert tr_global["sample_size"] == 2 and tr_ligue1["sample_size"] == 1
    print(f"  [OK] §39 : global sample_size={tr_global['sample_size']}, Ligue1={tr_ligue1['sample_size']}")


def test_market_filter_no_cross_contamination():
    e1 = _resolved_entry("Ligue1", "1X2", "xgboost", 1, KICKOFF)
    e2 = _resolved_entry("Ligue1", "BTTS", "xgboost", 2, KICKOFF)
    entries = [e1, e2]
    tr_1x2 = compute_shadow_track_record(entries, market="1X2")
    tr_btts = compute_shadow_track_record(entries, market="BTTS")
    assert tr_1x2["sample_size"] == 1 and tr_btts["sample_size"] == 1
    print("  [OK] §40 : 1X2 et BTTS ne se contaminent jamais")


def test_model_filter_insufficient_data_when_no_match():
    e1 = _resolved_entry("Ligue1", "1X2", "xgboost", 1, KICKOFF)
    tr = compute_shadow_track_record([e1], market="1X2", model_type="lightgbm")
    assert tr["status"] == "INSUFFICIENT_DATA"
    print("  [OK] §41 : filtre modèle sans donnée -> INSUFFICIENT_DATA")


def test_window_filter_last_n_and_since_until():
    entries = [_resolved_entry("Ligue1", "1X2", "xgboost", i, KICKOFF + timedelta(days=i)) for i in range(5)]
    tr_last2 = compute_shadow_track_record(entries, market="1X2", last_n=2)
    tr_since = compute_shadow_track_record(entries, market="1X2", since=(KICKOFF + timedelta(days=3)).date())
    assert tr_last2["sample_size"] == 2 and tr_since["sample_size"] == 2
    print(f"  [OK] §42 : last_n=2 -> {tr_last2['sample_size']}, since day3 -> {tr_since['sample_size']}")


def test_value_tracking_not_available_without_verified_odds():
    e1 = _resolved_entry("Ligue1", "1X2", "xgboost", 1, KICKOFF)
    status = value_tracking_status([e1])
    assert status["status"] == "NOT_AVAILABLE"
    print(f"  [OK] §18 : value_tracking_status -> {status['status']} (aucune odds TEMPORALLY_VERIFIED)")


# ---------------------------------------------------------------------------
# 5. Leakage (§27-§32)
# ---------------------------------------------------------------------------

def test_result_leakage_score_absent_from_pre_resolution_record():
    store = _temp_store()
    record, _, _ = _capture(store)
    # §29 : le score final ne doit JAMAIS apparaître dans le record lui-même (seulement dans une ShadowResolution séparée, créée APRÈS coup).
    record_fields = record.__dict__
    assert "result_home_goals" not in record_fields and "result_away_goals" not in record_fields
    print("  [OK] §29 : ShadowDecisionRecord ne porte structurellement aucun champ de score — le résultat ne peut être ajouté qu'via une ShadowResolution séparée")


def test_model_version_leakage_rejected_in_replay():
    with Session(engine) as session:
        league, match_date, home, away = "Ligue1", date(2020, 1, 1), "Old A", "Old B"
        session.add(Match(league=league, date=datetime.combine(match_date, datetime.min.time()), home_team=home, away_team=away, home_goals=1, away_goals=0))
        session.commit()
        # ModelVersion entraînée BIEN APRÈS le match (2026) -> doit être rejetée pour un replay "as_of=kickoff du match (2020)".
        future_version = ModelVersion(name=next_version_name(session, "future-leak"), model_type="xgboost", trained_at=datetime(2026, 1, 1, tzinfo=timezone.utc), is_active=False)
        session.add(future_version); session.commit(); session.refresh(future_version)
        mp = ModelPrediction(league=league, match_date=match_date, home_team=home, away_team=away, model_type="xgboost",
                              model_version_id=future_version.id, source="backtest", status="resolved",
                              result_home_goals=1, result_away_goals=0,
                              prob_home=0.6, prob_draw=0.2, prob_away=0.2, pick_1x2="home_win")
        session.add(mp); session.commit()

        pi, diagnostics = build_pipeline_input_for_replay(session, mp, "1X2", "home_win")
    assert pi is None and diagnostics["reason"] == "MODEL_VERSION_TRAINED_AFTER_AS_OF"
    print(f"  [OK] §30 : ModelVersion.trained_at postérieur à as_of -> replay rejeté ({diagnostics['reason']})")


def test_calibration_never_fabricated_in_replay():
    # §31 : aucune CalibrationResult n'est jamais fabriquée en mode replay (aucune calibration par match n'est persistée nulle part).
    with Session(engine) as session:
        league, match_date, home, away = "Ligue1", date(2020, 1, 2), "Cal A", "Cal B"
        session.add(Match(league=league, date=datetime.combine(match_date, datetime.min.time()), home_team=home, away_team=away, home_goals=2, away_goals=2))
        session.commit()
        version = ModelVersion(name=next_version_name(session, "cal-test"), model_type="xgboost", trained_at=datetime(2019, 1, 1, tzinfo=timezone.utc), is_active=False)
        session.add(version); session.commit(); session.refresh(version)
        mp = ModelPrediction(league=league, match_date=match_date, home_team=home, away_team=away, model_type="xgboost",
                              model_version_id=version.id, source="backtest", status="resolved",
                              result_home_goals=2, result_away_goals=2,
                              prob_home=0.4, prob_draw=0.3, prob_away=0.3, pick_1x2="home_win")
        session.add(mp); session.commit()
        pi, diagnostics = build_pipeline_input_for_replay(session, mp, "1X2", "draw")
    assert pi is not None and pi.calibration.calibration_result is None and pi.calibration.source == "RAW"
    print("  [OK] §31 : replay -> calibration_result=None, source=RAW (jamais une calibration fabriquée)")


def test_odds_leakage_still_enforced_via_phase8i_reused():
    # §32 : réutilise l'invariant déjà prouvé en Phase 8I/8J — odds postérieures au cutoff -> rejetées, jamais utilisées même si "meilleure value".
    from app.ai.pipeline.schemas import OddsInput
    future_ts = KICKOFF + timedelta(hours=1)
    pi = _pi_1x2(odds_input=OddsInput(odds_by_selection={"home_win": 5.0, "draw": 4.5, "away_win": 5.0}, odds_timestamp=future_ts, has_measured_timestamp=True, source_label="SYNTHETIC"))
    assessment = run_pipeline(pi)
    assert assessment.value is None and assessment.final_status == "INELIGIBLE"
    print("  [OK] §32 : odds postérieures au cutoff -> rejetées (réutilise Phase 8I/8J, jamais une seconde implémentation)")


def test_temporal_leakage_as_of_invariant():
    # §27/§28 : feature snapshot réel (Phase 8A) — vérifie qu'aucun match POSTÉRIEUR à as_of n'entre dans le calcul de couverture.
    with Session(engine) as session:
        league, home, away = "Ligue1", "Asof A", "Asof B"
        session.add(Match(league=league, date=datetime(2025, 1, 1), home_team=home, away_team="Other X", home_goals=1, away_goals=0))
        session.add(Match(league=league, date=datetime(2025, 6, 1), home_team=home, away_team="Other Y", home_goals=0, away_goals=0))  # APRÈS as_of
        session.commit()
        as_of = date(2025, 3, 1)
        from app.ai.features.snapshot import build_feature_snapshot
        snap_before = build_feature_snapshot(session, league, home, away, cutoff=as_of)
        # home_form_points_avg ne doit refléter QUE le match de janvier (1 victoire), jamais celui de juin (postérieur à as_of).
        assert snap_before.features.get("home_form_points_avg") == 3.0
    print("  [OK] §27/§28 : as-of invariant vérifié sur données réelles — aucun match postérieur à as_of n'entre dans le snapshot")


# ---------------------------------------------------------------------------
# 6. Determinism / error isolation (§25/§26/§43)
# ---------------------------------------------------------------------------

def test_determinism_same_dataset_same_track_record():
    entries = [_resolved_entry("Ligue1", "1X2", "xgboost", i, KICKOFF + timedelta(days=i)) for i in range(3)]
    tr1 = compute_shadow_track_record(list(entries), market="1X2")
    tr2 = compute_shadow_track_record(list(reversed(entries)), market="1X2")  # ordre d'entrée inversé
    assert tr1["sample_size"] == tr2["sample_size"] and tr1["accuracy"] == tr2["accuracy"] and tr1["log_loss"] == tr2["log_loss"]
    print("  [OK] §43 : même dataset (ordre différent en entrée) -> même track record")


def test_batch_error_isolation_reused_from_phase8j():
    a = _pi_1x2(match_id=100)
    b = _pi_1x2(match_id=101, as_of=None, kickoff=None)  # invalide -> exception dans run_pipeline
    c = _pi_1x2(match_id=102)
    results = run_shadow_batch([a, b, c])
    assert results[0].error is None and results[1].error is not None and results[2].error is None
    print("  [OK] §25 : une erreur sur un match n'arrête pas le batch (réutilise run_shadow_batch, Phase 8J)")


# ---------------------------------------------------------------------------
# 7. Real data reality check (§9/§52) + DB safety (§33/§38)
# ---------------------------------------------------------------------------

def test_measure_data_reality_never_assumes():
    with Session(engine) as session:
        reality = measure_data_reality(session)
    assert "future_fixtures" in reality and reality["shadow_live_data"] in ("NONE_AVAILABLE", "AVAILABLE")
    print(f"  [OK] §9 : mesure réelle -> {reality}")


def test_shadow_operations_never_touch_production_tables():
    with Session(engine) as session:
        session.add(Match(league="Ligue1", date=datetime.combine(date(2026, 1, 1), datetime.min.time()), home_team="Z1", away_team="Z2", home_goals=1, away_goals=0))
        session.commit()
        before = _row_counts(session)

        store = _temp_store()
        _capture(store)
        find_candidate_results(session, "Ligue1", date(2026, 1, 1), "Z1", "Z2")
        measure_data_reality(session)
        find_replay_candidates(session, limit=5)

        after = _row_counts(session)
    assert before == after, f"une opération shadow a modifié une table : avant={before} après={after}"
    print("  [OK] §33/§38 : aucune opération shadow ne modifie match/match_stats/model_predictions/model_versions/team_ratings")


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
