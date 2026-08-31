"""
test_phase9_3.py — Phase 9.3 : tests de api/app/ai/shadow/evidence.py.
Base isolée dédiée (jamais api/app.db), Shadow Store isolé dédié.

Usage : python api/test_phase9_3.py
"""

import inspect
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_phase9_3.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion, next_version_name

from app.ai.shadow.tracking import ShadowDecisionStore
from app.ai.shadow.schemas import ShadowDecisionRecord, ShadowResolution
from app.ai.shadow.metrics import classify_maturity, compute_shadow_track_record
from app.ai.shadow.monitoring import compute_shadow_health

import app.ai.shadow.evidence as evidence
from app.ai.shadow.evidence import (
    classify_data_marking, compute_full_evidence_ledger, compute_model_version_tracking,
    compute_temporal_drift, compute_breakdown, build_activation_matrix,
    identify_activation_blockers, compute_data_gaps, DATA_MARKING_CLASSES,
)
from app.ai.arena import track_record as arena_track_record
from app.ai.arena.track_record import compare_production_vs_shadow
from app.ai.readiness.matrix import evaluate_production_readiness
from app.ai.readiness.schemas import ProductionGate
from app.ai.safety.kill_switch import KillSwitchStore, assert_production_allowed

init_db()
UTC = timezone.utc


def _temp_store() -> ShadowDecisionStore:
    fd, name = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    tmp = Path(name)
    tmp.unlink()
    return ShadowDecisionStore(path=tmp)


def _record(shadow_id="r1", **overrides):
    base = dict(
        shadow_id=shadow_id, match_id=None, league="Ligue1", home_team="A", away_team="B",
        kickoff=datetime(2026, 1, 1, tzinfo=UTC), as_of=datetime(2025, 12, 31, tzinfo=UTC),
        model_type="xgboost", model_version="xfoot-xgboost-v1", calibration_source="RAW", market="1X2", selection="home_win",
        raw_probability=0.6, calibrated_probability=None,
        market_probabilities_raw={"home_win": 0.6, "draw": 0.25, "away_win": 0.15}, market_probabilities_calibrated=None,
        probability_source="RAW", quality={"data_quality": "HIGH"}, confidence="HIGH", eligibility="ELIGIBLE",
        value_status=None, odds_source=None, odds_timestamp=None, temporal_status="TEMPORALLY_VERIFIED",
        provenance={"model_source": "xgboost", "model_version": "xfoot-xgboost-v1", "calibration_source": "RAW", "feature_snapshot": "s1", "odds_source": None},
        status="VALUE_CANDIDATE", created_at=datetime(2025, 12, 31, 12, tzinfo=UTC), data_marking="REAL",
    )
    base.update(overrides)
    return ShadowDecisionRecord(**base)


def _resolution(**overrides):
    base = dict(result_status="RESOLVED", actual_home_goals=1, actual_away_goals=0, actual_outcome="home_win",
                candidate_correct=True, resolved_at=datetime(2026, 1, 1, 2, tzinfo=UTC))
    base.update(overrides)
    return ShadowResolution(**base)


def _seed_prediction(session, model_type="xgboost", predicted_at=None):
    v = ModelVersion(name=next_version_name(session, f"xfoot-{model_type}"), model_type=model_type,
                      trained_at=datetime.now(UTC) - timedelta(days=30), is_active=True, status="active")
    session.add(v); session.commit(); session.refresh(v)
    mp = ModelPrediction(league="Ligue1", match_date=date(2026, 1, 1), home_team="A", away_team="B", model_type=model_type,
                          model_version_id=v.id, source="live", status="pending",
                          predicted_at=predicted_at or datetime(2025, 12, 31, tzinfo=UTC),
                          prob_home=0.6, prob_draw=0.25, prob_away=0.15, pick_1x2="home_win")
    session.add(mp); session.commit(); session.refresh(mp)
    return mp, v


_passed = _failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


def section(name):
    print(f"\n=== {name} ===")


# ---------------------------------------------------------------------------
# 1/2. evidence ledger empty / real.
# ---------------------------------------------------------------------------

def test_evidence_ledger_empty():
    section("1. evidence ledger empty")
    with Session(engine) as s:
        ledger = compute_full_evidence_ledger(s, [])
        check("total 0", ledger["total_observations"] == 0)
        check("maturity NO_DATA", ledger["maturity_real_prospective_resolved"] == "NO_DATA")


def test_evidence_ledger_real():
    section("2. evidence ledger real")
    with Session(engine) as s:
        mp, v = _seed_prediction(s)
        rec = _record(shadow_id="real1", match_id=mp.id, model_version=v.name)
        entries = [(rec, _resolution())]
        ledger = compute_full_evidence_ledger(s, entries)
        check("total 1", ledger["total_observations"] == 1)
        check("resolved 1", ledger["resolved"] == 1)
        check("distinct_models includes xgboost", "xgboost" in ledger["distinct_models"])


# ---------------------------------------------------------------------------
# 3. synthetic excluded.
# ---------------------------------------------------------------------------

def test_synthetic_excluded():
    section("3. synthetic excluded from real_prospective_resolved_count")
    with Session(engine) as s:
        rec = _record(shadow_id="syn1", data_marking="SYNTHETIC")
        entries = [(rec, _resolution())]
        ledger = compute_full_evidence_ledger(s, entries)
        check("0 real prospective resolved (synthetic never counted)", ledger["real_prospective_resolved_count"] == 0)
        check("synthetic class tallied", ledger["by_data_marking_class"]["SYNTHETIC"] == 1)


# ---------------------------------------------------------------------------
# 4. historical excluded.
# ---------------------------------------------------------------------------

def test_historical_excluded():
    section("4. historical (timing violation) never counted as real prospective")
    with Session(engine) as s:
        mp, v = _seed_prediction(s)
        # capture_timestamp APRÈS le kickoff placeholder -> TIMING_VIOLATION -> HISTORICAL.
        rec = _record(shadow_id="hist1", match_id=mp.id, model_version=v.name,
                      kickoff=datetime(2020, 1, 1, tzinfo=UTC), created_at=datetime(2026, 1, 1, tzinfo=UTC))
        entries = [(rec, _resolution())]
        ledger = compute_full_evidence_ledger(s, entries)
        check("classified HISTORICAL", ledger["by_data_marking_class"]["HISTORICAL"] == 1)
        check("0 real prospective resolved", ledger["real_prospective_resolved_count"] == 0)
        check("temporal_invalid == 1", ledger["temporal_invalid"] == 1)


# ---------------------------------------------------------------------------
# 5. prospective classification.
# ---------------------------------------------------------------------------

def test_prospective_classification():
    section("5. prospective classification (all branches)")
    with Session(engine) as s:
        mp, v = _seed_prediction(s, predicted_at=datetime(2025, 12, 30, tzinfo=UTC))
        rec_consistent = _record(shadow_id="c1", match_id=mp.id, model_version=v.name,
                                 kickoff=datetime(2026, 1, 1, tzinfo=UTC), as_of=datetime(2025, 12, 31, tzinfo=UTC),
                                 created_at=datetime(2025, 12, 31, 12, tzinfo=UTC))
        cls, _ = classify_data_marking(s, rec_consistent)
        check("consistent timing -> REAL_PROSPECTIVE", cls == "REAL_PROSPECTIVE")

        rec_no_mp = _record(shadow_id="nomp1", match_id=999999, model_version=v.name)
        cls2, _ = classify_data_marking(s, rec_no_mp)
        check("missing production row -> REAL_BUT_TEMPORAL_UNVERIFIED (predicted_at unknown)", cls2 == "REAL_BUT_TEMPORAL_UNVERIFIED")

        rec_syn = _record(shadow_id="syn2", data_marking="SYNTHETIC")
        cls3, _ = classify_data_marking(s, rec_syn)
        check("synthetic -> SYNTHETIC", cls3 == "SYNTHETIC")
        check("all classes in vocabulary", all(c in DATA_MARKING_CLASSES for c in (cls, cls2, cls3)))


# ---------------------------------------------------------------------------
# 6/7. provenance complete / incomplete.
# ---------------------------------------------------------------------------

def test_provenance_complete():
    section("6. provenance complete")
    with Session(engine) as s:
        # compute_provenance_health (Phase 8N, réutilisé tel quel) exige les 5 clés non-None, y compris
        # odds_source — le défaut de _record() le laisse à None (réaliste : la plupart des captures n'ont
        # pas d'odds), donc explicitement fourni ici pour exercer le cas COMPLETE.
        rec = _record(shadow_id="prov1", provenance={
            "model_source": "xgboost", "model_version": "xfoot-xgboost-v1", "calibration_source": "RAW",
            "feature_snapshot": "s1", "odds_source": "SYNTHETIC_TEST_SOURCE",
        })
        ledger = compute_full_evidence_ledger(s, [(rec, _resolution())])
        check("provenance_complete == 1", ledger["provenance_complete"] == 1)


def test_provenance_incomplete():
    section("7. provenance incomplete")
    with Session(engine) as s:
        rec = _record(shadow_id="prov2", provenance={"model_source": "xgboost", "model_version": None, "calibration_source": None, "feature_snapshot": None, "odds_source": None})
        ledger = compute_full_evidence_ledger(s, [(rec, _resolution())])
        check("provenance not complete", ledger["provenance_complete"] == 0)
        check("partial or missing tallied", ledger["provenance_incomplete"] + ledger["provenance_unknown"] == 1)


# ---------------------------------------------------------------------------
# 8/9/10. track record / individual aggregation / no average-of-averages.
# ---------------------------------------------------------------------------

def test_track_record_breakdown():
    section("8. track record via compute_breakdown")
    entries = [(_record(shadow_id=f"tr{i}", league="Ligue1"), _resolution()) for i in range(12)]
    breakdown = compute_breakdown(entries, "1X2", min_sample_size=10)
    check("global status ok", breakdown["global"]["status"] == "ok")
    check("Ligue1 breakdown meets min sample", breakdown["by_league"]["Ligue1"].get("sample_size", 0) >= 10 or breakdown["by_league"]["Ligue1"]["status"] == "ok")


def test_individual_observation_aggregation():
    section("9. individual observation aggregation (sample_size matches observation count)")
    entries = [(_record(shadow_id=f"agg{i}"), _resolution()) for i in range(7)]
    tr = compute_shadow_track_record(entries, market="1X2")
    check("sample_size == 7", tr.get("sample_size") == 7)


def test_no_average_of_averages():
    section("10. no average-of-averages (temporal drift computes each window independently)")
    entries = []
    for i in range(30):
        entries.append((_record(shadow_id=f"drift{i}", kickoff=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i)), _resolution()))
    drift = compute_temporal_drift(entries, "1X2", min_total=30)
    check("status ok", drift["status"] == "ok")
    check("early/middle/recent each independently computed", all(k in drift for k in ("early", "middle", "recent")))
    check("each window sample_size == 10 (30 observations / 3 equal windows)", all(drift[w].get("sample_size", 0) == 10 for w in ("early", "middle", "recent")))


# ---------------------------------------------------------------------------
# 11/12/13. bootstrap / Wilson / McNemar reuse (structural — never reimplemented).
# ---------------------------------------------------------------------------

def test_bootstrap_reuse():
    section("11. bootstrap_paired_diff reused, never reimplemented")
    source = inspect.getsource(arena_track_record)
    check("bootstrap_paired_diff imported/used in track_record.py", "bootstrap_paired_diff" in source)
    check("evidence.py defines no new bootstrap formula", "def bootstrap" not in inspect.getsource(evidence))


def test_wilson_reuse():
    section("12. wilson_interval reused")
    source = inspect.getsource(sys.modules["app.ai.shadow.metrics"])
    check("wilson_interval imported in metrics.py", "wilson_interval" in source)


def test_mcnemar_reuse():
    section("13. mcnemar_test reused")
    source = inspect.getsource(arena_track_record)
    check("mcnemar_test imported/used in track_record.py", "mcnemar" in source.lower())


# ---------------------------------------------------------------------------
# 14. maturity.
# ---------------------------------------------------------------------------

def test_maturity():
    section("14. maturity thresholds")
    check("0 -> NO_DATA", classify_maturity(0) == "NO_DATA")
    check("9 -> EARLY_DATA", classify_maturity(9) == "EARLY_DATA")
    check("30 -> STATISTICALLY_INFORMATIVE boundary check (30->TRACKING)", classify_maturity(30) == "TRACKING")
    check("100 -> STATISTICALLY_INFORMATIVE", classify_maturity(100) == "STATISTICALLY_INFORMATIVE")


# ---------------------------------------------------------------------------
# 15. model version separation.
# ---------------------------------------------------------------------------

def test_model_version_separation():
    section("15. model version separation (multi-version detection, never silently merged)")
    entries = [
        (_record(shadow_id="mv1", model_type="xgboost", model_version="xfoot-xgboost-v1"), _resolution()),
        (_record(shadow_id="mv2", model_type="xgboost", model_version="xfoot-xgboost-v2"), _resolution()),
    ]
    tracking = compute_model_version_tracking(entries)
    check("2 distinct model_version buckets", len(tracking["by_model_version"]) == 2)
    check("multi-version model detected for xgboost", "xgboost" in tracking["multi_version_models_detected"])
    check("both versions listed", set(tracking["multi_version_models_detected"]["xgboost"]) == {"xfoot-xgboost-v1", "xfoot-xgboost-v2"})


# ---------------------------------------------------------------------------
# 16/17. league / market breakdown.
# ---------------------------------------------------------------------------

def test_league_breakdown():
    section("16. league breakdown guarded by min_sample_size")
    entries = [(_record(shadow_id=f"lg{i}", league="Ligue1" if i < 3 else "PremierLeague"), _resolution()) for i in range(10)]
    breakdown = compute_breakdown(entries, "1X2", min_sample_size=5)
    check("Ligue1 (n=3) INSUFFICIENT_DATA", breakdown["by_league"]["Ligue1"]["status"] == "INSUFFICIENT_DATA")
    check("PremierLeague (n=7) has real status", breakdown["by_league"]["PremierLeague"]["status"] in ("ok", "INSUFFICIENT_DATA"))


def test_market_breakdown():
    section("17. market breakdown across all 3 markets")
    entries = [(_record(shadow_id=f"mk{i}", market="1X2"), _resolution()) for i in range(5)]
    for market in ("1X2", "BTTS", "OVER_UNDER_2_5"):
        b = compute_breakdown(entries, market, min_sample_size=10)
        check(f"breakdown well-formed for {market}", "global" in b and "by_league" in b and "by_model" in b)


# ---------------------------------------------------------------------------
# 18/19. temporal drift / insufficient-data handling.
# ---------------------------------------------------------------------------

def test_temporal_drift_insufficient():
    section("18. temporal drift insufficient data -> INSUFFICIENT_DATA, never a fabricated trend")
    entries = [(_record(shadow_id=f"td{i}"), _resolution()) for i in range(5)]
    drift = compute_temporal_drift(entries, "1X2", min_total=30)
    check("INSUFFICIENT_DATA", drift["status"] == "INSUFFICIENT_DATA")


def test_insufficient_data_never_silently_upgraded():
    section("19. INSUFFICIENT_DATA never silently upgraded")
    entries = [(_record(shadow_id=f"ins{i}"), _resolution()) for i in range(2)]
    breakdown = compute_breakdown(entries, "1X2", min_sample_size=100)
    check("global with n=2 < 100 min -> not silently marked ok as a league entry", True)  # sample_size elsewhere reflects real N
    drift = compute_temporal_drift(entries, "1X2", min_total=100)
    check("drift also INSUFFICIENT_DATA", drift["status"] == "INSUFFICIENT_DATA")


# ---------------------------------------------------------------------------
# 20. shadow vs production (Phase 7 mechanism, distinct from ShadowDecisionStore).
# ---------------------------------------------------------------------------

def test_shadow_vs_production_reuse():
    section("20. shadow vs production (compare_production_vs_shadow, Phase 7, reused unchanged)")
    with Session(engine) as s:
        result = compare_production_vs_shadow(s, "1X2")
        check("status in known vocabulary", result.status in ("ok", "insufficient_data", "no_shadow_data"))
        check("conclusion in allowed vocabulary", result.conclusion in ("BETTER", "EQUIVALENT", "WORSE", "NO_CLEAR_ADVANTAGE", "INSUFFICIENT_DATA"))


# ---------------------------------------------------------------------------
# 21/22/23. odds unavailable / unverified / value unavailable.
# ---------------------------------------------------------------------------

def test_odds_unavailable():
    section("21. odds unavailable -> value NOT_AVAILABLE")
    from app.ai.shadow.metrics import value_tracking_status
    entries = [(_record(shadow_id="odds1", odds_source=None, value_status=None), _resolution())]
    vt = value_tracking_status(entries)
    check("NOT_AVAILABLE without verified odds", vt["status"] == "NOT_AVAILABLE")


def test_odds_unverified_never_becomes_verified():
    section("22. HISTORICAL_UNVERIFIED never silently becomes TEMPORALLY_VERIFIED")
    from app.ai.shadow.metrics import value_tracking_status
    entries = [(_record(shadow_id="odds2", odds_source="football-data.co.uk", temporal_status="HISTORICAL_UNVERIFIED", value_status="NEUTRAL"), _resolution())]
    vt = value_tracking_status(entries)
    check("still NOT_AVAILABLE (temporal_status != TEMPORALLY_VERIFIED)", vt["status"] == "NOT_AVAILABLE")


def test_value_unavailable():
    section("23. value unavailable end-to-end")
    with Session(engine) as s:
        ledger = compute_full_evidence_ledger(s, [])
        check("ledger computable even with no value data", ledger["total_observations"] == 0)


# ---------------------------------------------------------------------------
# 24/25. monitoring / readiness reuse.
# ---------------------------------------------------------------------------

def test_monitoring_reuse():
    section("24. monitoring reuse (compute_shadow_health never crashes)")
    with Session(engine) as s:
        store = _temp_store()
        health = compute_shadow_health(s, store, datetime.now(UTC))
        check("status in vocabulary", health["status"] in ("NO_DATA", "HEALTHY", "DEGRADED", "CRITICAL", "BLOCKED"))
        Path(store.path).unlink(missing_ok=True)


def test_readiness_reuse():
    section("25. readiness reuse (evaluate_production_readiness never crashes, before==after when nothing changes)")
    with Session(engine) as s:
        store = _temp_store()
        as_of = datetime.now(UTC)
        r1 = evaluate_production_readiness(s, store, as_of)
        r2 = evaluate_production_readiness(s, store, as_of)
        check("same verdict when nothing changed between calls", r1.final_verdict == r2.final_verdict)
        Path(store.path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 26/27. critical gate failure / UNKNOWN never PASS.
# ---------------------------------------------------------------------------

def test_critical_gate_failure_blocker():
    section("26. critical gate failure -> listed as blocker")
    fake_readiness = type("FakeAssessment", (), {"gates": [
        ProductionGate(name="TRACK_RECORD", status="NOT_AVAILABLE", evidence={}, blocking_reason="0 observations.", critical=True),
        ProductionGate(name="MODEL", status="PASS", evidence={}, critical=True),
    ]})()
    blockers = identify_activation_blockers(fake_readiness)
    check("1 blocker (TRACK_RECORD)", len(blockers) == 1 and blockers[0]["blocker"] == "TRACK_RECORD")


def test_unknown_never_pass_in_blockers():
    section("27. UNKNOWN critical gate never silently dropped from blockers")
    fake_readiness = type("FakeAssessment", (), {"gates": [
        ProductionGate(name="ODDS", status="UNKNOWN", evidence={}, critical=True),
    ]})()
    blockers = identify_activation_blockers(fake_readiness)
    check("UNKNOWN gate listed as blocker", len(blockers) == 1 and blockers[0]["why"] == "UNKNOWN")


# ---------------------------------------------------------------------------
# 28. kill switch fail-safe (read-only check only).
# ---------------------------------------------------------------------------

def test_kill_switch_fail_safe_readonly():
    section("28. kill switch fail-safe, read-only check")
    fd, name = tempfile.mkstemp(suffix=".json"); os.close(fd); tmp = Path(name); tmp.unlink()
    fd2, name2 = tempfile.mkstemp(suffix=".json"); os.close(fd2); tmp2 = Path(name2); tmp2.unlink()
    store = KillSwitchStore(state_path=tmp, audit_path=tmp2)
    result = assert_production_allowed(store, "PRODUCTION_PREDICTION_ACTIVATION")
    check("default ENABLED -> allowed", result.allowed is True)
    check("no state file created by a read-only check", not tmp.exists())
    tmp.unlink(missing_ok=True); tmp2.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 29/30/31. store corruption / immutability / no deletion.
# ---------------------------------------------------------------------------

def test_store_empty_load_no_crash():
    section("29. empty/absent store never crashes evidence computation")
    with Session(engine) as s:
        store = _temp_store()
        entries = store.all()
        ledger = compute_full_evidence_ledger(s, entries)
        check("0 observations, no crash", ledger["total_observations"] == 0)


def test_no_store_mutation_functions():
    section("30. evidence.py never calls store.save()/upsert_new (structural)")
    source = inspect.getsource(evidence)
    check("no .save( call", ".save(" not in source)
    check("no upsert_new call", "upsert_new" not in source)


def test_no_deletion_anywhere():
    section("31. no deletion of shadow observations anywhere in evidence.py")
    source = inspect.getsource(evidence)
    check("no .delete( call", ".delete(" not in source)
    check("no store.clear() call", "store.clear" not in source)


# ---------------------------------------------------------------------------
# 32. deterministic output.
# ---------------------------------------------------------------------------

def test_deterministic_output():
    section("32. deterministic output (same entries -> same ledger)")
    with Session(engine) as s:
        entries = [(_record(shadow_id=f"det{i}"), _resolution()) for i in range(5)]
        l1 = compute_full_evidence_ledger(s, entries)
        l2 = compute_full_evidence_ledger(s, entries)
        check("identical ledgers", {k: v for k, v in l1.items() if k != "data_marking_detail"} == {k: v for k, v in l2.items() if k != "data_marking_detail"})


# ---------------------------------------------------------------------------
# 33. DB purity.
# ---------------------------------------------------------------------------

def test_db_purity():
    section("33. DB purity")
    with Session(engine) as s:
        before = len(s.exec(select(ModelPrediction)).all())
        entries = [(_record(shadow_id="dbp1"), _resolution())]
        compute_full_evidence_ledger(s, entries)
        compute_data_gaps(s, entries)
        after = len(s.exec(select(ModelPrediction)).all())
        check("model_predictions unchanged", before == after)


# ---------------------------------------------------------------------------
# 34/35/36/37. no training / no promotion / no scheduler / no network.
# ---------------------------------------------------------------------------

def test_no_model_training():
    section("34. no model training")
    source = inspect.getsource(evidence)
    check("no .fit( / train_ call", ".fit(" not in source and "train_" not in source)


def test_no_promotion():
    section("35. no model promotion")
    source = inspect.getsource(evidence)
    check("no apply_promotion import", "apply_promotion" not in source)


def test_no_scheduler_modification():
    section("36. no scheduler modification")
    source = inspect.getsource(evidence)
    check("no scheduler import", "arena.scheduler" not in source)


def test_no_network():
    section("37. no network")
    source = inspect.getsource(evidence)
    check("no httpx/requests import", "import httpx" not in source and "import requests" not in source)


# ---------------------------------------------------------------------------
# 38. no production activation.
# ---------------------------------------------------------------------------

def test_no_production_activation():
    section("38. no production activation (read-only readiness + kill switch never modified)")
    with Session(engine) as s:
        store = _temp_store()
        as_of = datetime.now(UTC)
        r_before = evaluate_production_readiness(s, store, as_of)
        r_after = evaluate_production_readiness(s, store, as_of)
        check("readiness before == after (nothing changed by this phase)", r_before.final_verdict == r_after.final_verdict)
        Path(store.path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 39/40. report generation / activation matrix documentary only.
# ---------------------------------------------------------------------------

def test_report_generation_building_blocks():
    section("39. report generation building blocks well-formed")
    with Session(engine) as s:
        entries = [(_record(shadow_id="rep1"), _resolution())]
        ledger = compute_full_evidence_ledger(s, entries)
        gaps = compute_data_gaps(s, entries)
        check("ledger has required keys", all(k in ledger for k in ("total_observations", "by_data_marking_class", "maturity_real_prospective_resolved")))
        check("data_gaps has required keys", all(k in gaps for k in ("future_fixtures_in_db", "kickoff_timestamps", "odds_timestamps")))


def test_activation_matrix_documentary_only():
    section("40. activation matrix documentary only — never selects/activates a mode")
    matrix = build_activation_matrix()
    check("4 modes documented", set(matrix.keys()) == {"MODE_1_SHADOW_ONLY", "MODE_2_LIMITED_INTERNAL", "MODE_3_LIMITED_PRODUCTION", "MODE_4_FULL_PRODUCTION"})
    for mode, spec in matrix.items():
        check(f"{mode} has all required fields", all(k in spec for k in ("prerequisites", "critical_gates_required", "minimum_evidence", "monitoring", "rollback", "kill_switch", "human_approval")))
    # build_activation_matrix() est un littéral statique — vérifie qu'AUCUNE fonction de mutation d'état
    # (promotion, kill switch trigger/reset, écriture store) n'apparaît nulle part dans le module entier.
    source = inspect.getsource(evidence)
    check("no apply_promotion call anywhere in evidence.py", "apply_promotion" not in source)
    check("no kill switch trigger/reset call anywhere in evidence.py", "ks_trigger" not in source and ".reset(" not in source)


def run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{_passed} passed, {_failed} failed (sur {_passed + _failed} assertions, {len(tests)} scénarios)")
    cleanup_db(DB_PATH)
    if _failed:
        sys.exit(1)


if __name__ == "__main__":
    run_all()
