"""
test_phase9_5.py — Phase 9.5 : tests de api/app/ai/shadow/watch.py (les
SEULES fonctions nouvelles de cette phase : EvidenceHistoryStore,
compute_evidence_snapshot, filter_real_prospective_entries,
compute_evidence_trend, compute_blocker_evolution, derive_watch_verdict) et
vérification structurelle de scripts/shadow_evidence_watch.py (le watch
runner — jamais importé/exécuté ici pour éviter tout appel récursif à ses
propres suites de régression, uniquement inspecté comme texte, même
discipline que test_phase9_4.py).

Toutes les briques DÉJÀ testées ailleurs (discover/capture/resolve/
monitoring/track record/évidence/readiness/preflight) sont réutilisées
TELLES QUELLES via les mêmes fonctions que scripts/shadow_evidence_watch.py
appelle réellement — ce fichier vérifie que la couche d'observation
longitudinale (snapshot, historique, tendance, évolution des blockers)
tient, jamais une réécriture des garanties déjà prouvées en Phase 8K-9.4.

Base isolée dédiée (jamais api/app.db), Shadow Store / lock / Kill Switch
store / Evidence History store isolés dédiés (jamais les fichiers réels sous
reports/).

Usage : python api/test_phase9_5.py
"""

import inspect
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_phase9_5.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match
from app.models.model_prediction import ModelPrediction
from app.models.prediction_log import PredictionLog
from app.models.team_rating import ModelVersion, next_version_name

from app.ai.shadow.tracking import ShadowDecisionStore
from app.ai.shadow.schemas import ShadowDecisionRecord, ShadowResolution
from app.ai.shadow.resolution import resolve_record
from app.ai.shadow.metrics import compute_shadow_track_record, value_tracking_status
from app.ai.shadow.monitoring import compute_shadow_health
from app.ai.shadow.evidence import compute_full_evidence_ledger, compute_model_version_tracking, compute_breakdown, identify_activation_blockers
from app.ai.shadow.prospective import run_prospective_capture, acquire_capture_lock
from app.ai.readiness.matrix import evaluate_production_readiness
from app.ai.safety.kill_switch import KillSwitchStore

import app.ai.shadow.watch as watch
from app.ai.shadow.watch import (
    EvidenceHistoryStore, compute_evidence_snapshot, filter_real_prospective_entries,
    compute_evidence_trend, readiness_blockers, compute_blocker_evolution, derive_watch_verdict,
    SNAPSHOT_FIELDS, WATCH_VERDICTS,
)
from app.ai.shadow.operations import run_preflight_safety

init_db()
UTC = timezone.utc
SCRIPT_SOURCE = (Path(__file__).parent.parent / "scripts" / "shadow_evidence_watch.py").read_text(encoding="utf-8")


def _temp_store() -> ShadowDecisionStore:
    fd, name = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    tmp = Path(name)
    tmp.unlink()
    return ShadowDecisionStore(path=tmp)


def _temp_lock() -> Path:
    fd, name = tempfile.mkstemp(suffix=".lock")
    os.close(fd)
    p = Path(name)
    p.unlink()
    return p


def _temp_switch() -> KillSwitchStore:
    fd1, p1 = tempfile.mkstemp(suffix=".json"); os.close(fd1); Path(p1).unlink()
    fd2, p2 = tempfile.mkstemp(suffix=".json"); os.close(fd2); Path(p2).unlink()
    return KillSwitchStore(state_path=Path(p1), audit_path=Path(p2))


def _cleanup_switch(store: KillSwitchStore) -> None:
    Path(store.state_path).unlink(missing_ok=True)
    Path(store.audit_path).unlink(missing_ok=True)


def _temp_history() -> EvidenceHistoryStore:
    fd, name = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    tmp = Path(name)
    tmp.unlink()
    return EvidenceHistoryStore(path=tmp)


def _cleanup_history(store: EvidenceHistoryStore) -> None:
    Path(store.path).unlink(missing_ok=True)


def _seed_pending_prediction(session, league="Ligue1", match_date=None, home="A", away="B",
                              model_type="xgboost", predicted_at=None, is_active=True):
    match_date = match_date or (date.today() + timedelta(days=2))
    version = ModelVersion(name=next_version_name(session, f"xfoot-{model_type}"), model_type=model_type,
                            trained_at=datetime.now(UTC) - timedelta(days=30), is_active=is_active, status="active" if is_active else "candidate")
    session.add(version); session.commit(); session.refresh(version)
    mp = ModelPrediction(league=league, match_date=match_date, home_team=home, away_team=away, model_type=model_type,
                          model_version_id=version.id, source="live", status="pending",
                          predicted_at=predicted_at or (datetime.now(UTC) - timedelta(hours=1)),
                          prob_home=0.5, prob_draw=0.3, prob_away=0.2, pick_1x2="home_win")
    session.add(mp); session.commit(); session.refresh(mp)
    return mp, version


def _clear_all(session):
    for model in (ModelPrediction, ModelVersion, PredictionLog, Match):
        for row in session.exec(select(model)).all():
            session.delete(row)
    session.commit()


def _fake_snapshot(**overrides) -> dict:
    base = {
        "timestamp": datetime.now(UTC).isoformat(), "future_fixtures": 0, "pending_predictions": 0,
        "eligible_candidates": 0, "captured": 0, "resolved": 0, "blocked": 0, "rejected": 0, "conflicts": 0,
        "real_prospective": 0, "temporal_unverified": 0, "historical": 0, "synthetic": 0,
        "provenance_complete": 0, "provenance_incomplete": 0, "provenance_unknown": 0,
        "maturity": "NO_DATA", "track_record_sample_size": 0, "readiness_verdict": "NO_GO", "blockers": [],
    }
    base.update(overrides)
    return base


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
# 1. empty evidence state.
# ---------------------------------------------------------------------------

def test_empty_evidence_state():
    section("1. empty evidence state")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        health = compute_shadow_health(s, store, datetime.now(UTC))
        full_ledger = compute_full_evidence_ledger(s, store.all())
        snapshot = compute_evidence_snapshot(as_of=datetime.now(UTC), health=health, full_ledger=full_ledger,
                                              capture_outcome={"candidates": 0, "captured": 0, "blocked": False, "rejected": [], "mismatches": []},
                                              track_record_sample_size=0, readiness_verdict="NO_GO")
        check("future_fixtures 0", snapshot["future_fixtures"] == 0)
        check("captured 0", snapshot["captured"] == 0)
        check("real_prospective 0", snapshot["real_prospective"] == 0)
        check("maturity NO_DATA", snapshot["maturity"] == "NO_DATA")
        check("all SNAPSHOT_FIELDS present", all(k in snapshot for k in SNAPSHOT_FIELDS))


# ---------------------------------------------------------------------------
# 2. real evidence.
# ---------------------------------------------------------------------------

def test_real_evidence():
    section("2. real evidence (real capture reflected in the snapshot)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        outcome = run_prospective_capture(s, store, datetime.now(UTC), dry_run=False, lock_path=lock)
        health = compute_shadow_health(s, store, datetime.now(UTC))
        full_ledger = compute_full_evidence_ledger(s, store.all())
        snapshot = compute_evidence_snapshot(as_of=datetime.now(UTC), health=health, full_ledger=full_ledger,
                                              capture_outcome=outcome, track_record_sample_size=0, readiness_verdict="NO_GO")
        check("captured 1", snapshot["captured"] == 1)
        check("real_prospective >= 1", snapshot["real_prospective"] >= 1)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 3. synthetic exclusion.
# ---------------------------------------------------------------------------

def _make_record(**overrides) -> ShadowDecisionRecord:
    base = dict(
        shadow_id="x", match_id=None, league="Ligue1", home_team="A", away_team="B",
        kickoff=datetime.now(UTC) + timedelta(days=1), as_of=datetime.now(UTC), model_type="xgboost", model_version="v1",
        calibration_source=None, market="1X2", selection="home_win", raw_probability=0.5, calibrated_probability=None,
        market_probabilities_raw={"home_win": 0.5, "draw": 0.3, "away_win": 0.2}, market_probabilities_calibrated=None,
        probability_source="RAW", quality={}, confidence="UNKNOWN", eligibility="UNKNOWN", value_status=None,
        odds_source=None, odds_timestamp=None, temporal_status="UNKNOWN", provenance={}, status="UNKNOWN",
        created_at=datetime.now(UTC), data_marking="REAL",
    )
    base.update(overrides)
    return ShadowDecisionRecord(**base)


def test_synthetic_exclusion():
    section("3. synthetic exclusion (never counted as REAL_PROSPECTIVE)")
    with Session(engine) as s:
        _clear_all(s)
        record = _make_record(shadow_id="synth-1", data_marking="SYNTHETIC")
        entries = [(record, ShadowResolution(result_status="PENDING"))]
        filtered = filter_real_prospective_entries(s, entries)
        check("SYNTHETIC never in REAL_PROSPECTIVE subset", len(filtered) == 0)


# ---------------------------------------------------------------------------
# 4. historical exclusion.
# ---------------------------------------------------------------------------

def test_historical_exclusion():
    section("4. historical exclusion (timing violation never counted as REAL_PROSPECTIVE)")
    with Session(engine) as s:
        _clear_all(s)
        past_kickoff = datetime.now(UTC) - timedelta(days=5)
        record = _make_record(shadow_id="hist-1", kickoff=past_kickoff, as_of=past_kickoff + timedelta(days=1), created_at=past_kickoff + timedelta(days=1))
        entries = [(record, ShadowResolution(result_status="PENDING"))]
        filtered = filter_real_prospective_entries(s, entries)
        check("HISTORICAL never in REAL_PROSPECTIVE subset", len(filtered) == 0)


# ---------------------------------------------------------------------------
# 5. prospective classification.
# ---------------------------------------------------------------------------

def test_prospective_classification():
    section("5. prospective classification (only REAL_PROSPECTIVE kept)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC), dry_run=False, lock_path=lock)
        entries = store.all()
        filtered = filter_real_prospective_entries(s, entries)
        check("real capture classified REAL_PROSPECTIVE", len(filtered) == len(entries) and len(entries) == 1)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 6. provenance.
# ---------------------------------------------------------------------------

def test_provenance():
    section("6. provenance (never inferred COMPLETE)")
    with Session(engine) as s:
        _clear_all(s)
        record = _make_record(shadow_id="prov-1", provenance={})  # vide -> jamais COMPLETE par inférence
        entries = [(record, ShadowResolution(result_status="PENDING"))]
        full_ledger = compute_full_evidence_ledger(s, entries)
        check("empty provenance never COMPLETE", full_ledger["provenance_complete"] == 0)
        check("empty provenance counted as missing/unknown", full_ledger["provenance_unknown"] == 1)


# ---------------------------------------------------------------------------
# 7. temporal integrity.
# ---------------------------------------------------------------------------

def test_temporal_integrity():
    section("7. temporal integrity (reused vocabulary, never fabricated)")
    with Session(engine) as s:
        _clear_all(s)
        record = _make_record(shadow_id="unk-1", kickoff=None)
        entries = [(record, ShadowResolution(result_status="PENDING"))]
        filtered = filter_real_prospective_entries(s, entries)
        check("missing kickoff never REAL_PROSPECTIVE", len(filtered) == 0)


# ---------------------------------------------------------------------------
# 8. track record.
# ---------------------------------------------------------------------------

def test_track_record():
    section("8. track record (REAL_PROSPECTIVE + RESOLVED only feeds compute_shadow_track_record)")
    with Session(engine) as s:
        _clear_all(s)
        match_date = date.today() + timedelta(days=1)
        _seed_pending_prediction(s, match_date=match_date, predicted_at=datetime.now(UTC) - timedelta(hours=2))
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC) - timedelta(hours=1), dry_run=False, lock_path=lock)
        m = Match(league="Ligue1", date=datetime.combine(match_date, datetime.min.time()), home_team="A", away_team="B", home_goals=2, away_goals=0)
        s.add(m); s.commit()
        record, resolution = store.all()[0]
        new_resolution = resolve_record(s, record, resolution)
        store.update_resolution(record.shadow_id, new_resolution)
        entries = store.all()
        filtered = filter_real_prospective_entries(s, entries)
        tr = compute_shadow_track_record(filtered, market="1X2")
        check("sample_size == 1", tr.get("sample_size") == 1)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 9. maturity.
# ---------------------------------------------------------------------------

def test_maturity():
    section("9. maturity (reused thresholds via evidence ledger, no artificial promotion)")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        full_ledger = compute_full_evidence_ledger(s, store.all())
        check("NO_DATA on empty store", full_ledger["maturity_real_prospective_resolved"] == "NO_DATA")


# ---------------------------------------------------------------------------
# 10. model version tracking.
# ---------------------------------------------------------------------------

def test_model_version_tracking():
    section("10. model version tracking (multi-version never silently merged)")
    with Session(engine) as s:
        _clear_all(s)
        r1 = _make_record(shadow_id="v1", model_type="xgboost", model_version="xfoot-xgboost-v1")
        r2 = _make_record(shadow_id="v2", model_type="xgboost", model_version="xfoot-xgboost-v2")
        entries = [(r1, ShadowResolution(result_status="PENDING")), (r2, ShadowResolution(result_status="PENDING"))]
        tracking = compute_model_version_tracking(entries)
        check("multi-version detected for xgboost", "xgboost" in tracking["multi_version_models_detected"])


# ---------------------------------------------------------------------------
# 11. league breakdown.
# ---------------------------------------------------------------------------

def test_league_breakdown():
    section("11. league breakdown (insufficient sample -> INSUFFICIENT_DATA, never a fabricated stat)")
    with Session(engine) as s:
        _clear_all(s)
        r1 = _make_record(shadow_id="l1", league="Ligue1")
        entries = [(r1, ShadowResolution(result_status="RESOLVED", actual_outcome="home_win", candidate_correct=True))]
        breakdown = compute_breakdown(entries, market="1X2", min_sample_size=10)
        check("league under threshold -> INSUFFICIENT_DATA", breakdown["by_league"]["Ligue1"]["status"] == "INSUFFICIENT_DATA")


# ---------------------------------------------------------------------------
# 12. market breakdown.
# ---------------------------------------------------------------------------

def test_market_breakdown():
    section("12. market breakdown (computed independently per market)")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        breakdown_1x2 = compute_breakdown(store.all(), market="1X2")
        breakdown_btts = compute_breakdown(store.all(), market="BTTS")
        check("1X2 breakdown well-formed", "global" in breakdown_1x2)
        check("BTTS breakdown well-formed", "global" in breakdown_btts)


# ---------------------------------------------------------------------------
# 13. monitoring reuse.
# ---------------------------------------------------------------------------

def test_monitoring_reuse():
    section("13. monitoring reuse (compute_shadow_health, read-only)")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        health = compute_shadow_health(s, store, datetime.now(UTC))
        check("status present", health["status"] in ("NO_DATA", "HEALTHY", "DEGRADED", "CRITICAL", "BLOCKED"))


# ---------------------------------------------------------------------------
# 14. readiness reuse.
# ---------------------------------------------------------------------------

def test_readiness_reuse():
    section("14. readiness reuse (evaluate_production_readiness + identify_activation_blockers wrapped)")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        readiness = evaluate_production_readiness(s, store, datetime.now(UTC))
        blockers_via_watch = readiness_blockers(readiness)
        blockers_direct = sorted({b["blocker"] for b in identify_activation_blockers(readiness)})
        check("readiness_blockers matches identify_activation_blockers exactly", blockers_via_watch == blockers_direct)


# ---------------------------------------------------------------------------
# 15. blocker comparison.
# ---------------------------------------------------------------------------

def test_blocker_comparison():
    section("15. blocker comparison (NEW/PERSISTING/CLEARED/REGRESSED)")
    history = [["A", "B"], ["B", "C"]]  # A vu puis disparu (cleared avant le dernier), B persiste, C apparu au dernier snapshot passé
    current = ["B", "D", "A"]  # B persiste, D est NEW, A est REGRESSED (vu avant, absent du dernier passé, de retour)
    result = compute_blocker_evolution(history, current)
    check("status ok", result["status"] == "ok")
    check("D is NEW", "D" in result["new"])
    check("B is PERSISTING", "B" in result["persisting"])
    check("C is CLEARED", "C" in result["cleared"])
    check("A is REGRESSED", "A" in result["regressed"])
    check("A not counted as NEW", "A" not in result["new"])


# ---------------------------------------------------------------------------
# 16. baseline trend.
# ---------------------------------------------------------------------------

def test_baseline_trend():
    section("16. baseline trend (first run -> BASELINE_ONLY)")
    result = compute_blocker_evolution([], ["X", "Y"])
    check("BASELINE_ONLY on first run", result["status"] == "BASELINE_ONLY")
    check("current_blockers reported", result["current_blockers"] == ["X", "Y"])


# ---------------------------------------------------------------------------
# 17. multi-snapshot trend.
# ---------------------------------------------------------------------------

def test_multi_snapshot_trend():
    section("17. multi-snapshot trend (real delta computation between last two real snapshots)")
    s1 = _fake_snapshot(future_fixtures=0, captured=0, resolved=0, real_prospective=0, maturity="NO_DATA")
    s2 = _fake_snapshot(future_fixtures=1, captured=1, resolved=1, real_prospective=1, maturity="EARLY_DATA")
    trend = compute_evidence_trend([s1, s2])
    check("status ok", trend["status"] == "ok")
    check("captured delta == 1", trend["deltas"]["captured"] == 1)
    check("maturity_changed True", trend["maturity_changed"] is True)


# ---------------------------------------------------------------------------
# 18. no fabricated trend.
# ---------------------------------------------------------------------------

def test_no_fabricated_trend():
    section("18. no fabricated trend (< 2 snapshots -> INSUFFICIENT_DATA, never a guessed delta)")
    check("0 snapshots -> INSUFFICIENT_DATA", compute_evidence_trend([])["status"] == "INSUFFICIENT_DATA")
    check("1 snapshot -> INSUFFICIENT_DATA", compute_evidence_trend([_fake_snapshot()])["status"] == "INSUFFICIENT_DATA")


# ---------------------------------------------------------------------------
# 19. dry-run purity.
# ---------------------------------------------------------------------------

def test_dry_run_purity():
    section("19. dry-run purity (0 Shadow Store write; evidence history append is NOT a DB/production write)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        before_mp = len(s.exec(select(ModelPrediction)).all())
    store = _temp_store()
    lock = _temp_lock()
    with Session(engine) as s:
        outcome = run_prospective_capture(s, store, datetime.now(UTC), dry_run=True, lock_path=lock)
    check("dry-run reports simulated capture", outcome["captured"] == 1)
    check("store file never created (0 write)", not store.path.exists())
    with Session(engine) as s:
        after_mp = len(s.exec(select(ModelPrediction)).all())
    check("model_predictions row count unchanged", before_mp == after_mp)
    lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 20. capture purity.
# ---------------------------------------------------------------------------

def test_capture_purity():
    section("20. capture purity (real capture writes ONLY the Shadow Store)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        before_mp = len(s.exec(select(ModelPrediction)).all())
        before_match = len(s.exec(select(Match)).all())
    store = _temp_store()
    lock = _temp_lock()
    with Session(engine) as s:
        run_prospective_capture(s, store, datetime.now(UTC), dry_run=False, lock_path=lock)
    check("store file written", store.path.exists())
    with Session(engine) as s:
        after_mp = len(s.exec(select(ModelPrediction)).all())
        after_match = len(s.exec(select(Match)).all())
    check("model_predictions row count unchanged", before_mp == after_mp)
    check("match row count unchanged", before_match == after_match)
    lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 21. resolve purity.
# ---------------------------------------------------------------------------

def test_resolve_purity():
    section("21. resolve purity (resolve updates ONLY the Shadow Store)")
    with Session(engine) as s:
        _clear_all(s)
        match_date = date.today() + timedelta(days=1)
        _seed_pending_prediction(s, match_date=match_date, predicted_at=datetime.now(UTC) - timedelta(hours=2))
        before_pl = len(s.exec(select(PredictionLog)).all())
    store = _temp_store()
    lock = _temp_lock()
    with Session(engine) as s:
        run_prospective_capture(s, store, datetime.now(UTC) - timedelta(hours=1), dry_run=False, lock_path=lock)
    with Session(engine) as s:
        m = Match(league="Ligue1", date=datetime.combine(match_date, datetime.min.time()), home_team="A", away_team="B", home_goals=1, away_goals=1)
        s.add(m); s.commit()
        before_match = len(s.exec(select(Match)).all())
        record, resolution = store.all()[0]
        new_resolution = resolve_record(s, record, resolution)
        store.update_resolution(record.shadow_id, new_resolution)
    with Session(engine) as s:
        after_match = len(s.exec(select(Match)).all())
        after_pl = len(s.exec(select(PredictionLog)).all())
    check("match row count unchanged", before_match == after_match)
    check("prediction_log row count unchanged", before_pl == after_pl)
    check("resolution applied in store", store.get(record.shadow_id)[1].result_status == "RESOLVED")
    lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 22. DB purity.
# ---------------------------------------------------------------------------

def test_db_purity():
    section("22. DB purity (watch.py never writes to the DB — structural check)")
    source = inspect.getsource(watch)
    check("no session.add(", "session.add(" not in source)
    check("no session.commit(", "session.commit(" not in source)
    check("no .execute(update/insert/delete", all(x not in source for x in ("execute(update", "execute(insert", "execute(delete")))


# ---------------------------------------------------------------------------
# 23. store atomicity.
# ---------------------------------------------------------------------------

def test_store_atomicity():
    section("23. store atomicity (EvidenceHistoryStore.append produces valid, reloadable JSON)")
    history = _temp_history()
    try:
        history.append(_fake_snapshot(future_fixtures=1))
        history.append(_fake_snapshot(future_fixtures=2))
        reloaded = EvidenceHistoryStore(path=history.path)
        data = reloaded.read()
        check("2 snapshots persisted", len(data) == 2)
        check("order preserved", data[0]["future_fixtures"] == 1 and data[1]["future_fixtures"] == 2)
    finally:
        _cleanup_history(history)


# ---------------------------------------------------------------------------
# 24. store corruption.
# ---------------------------------------------------------------------------

def test_store_corruption():
    section("24. store corruption (EvidenceHistoryStore never silently accepts invalid JSON)")
    fd, name = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    Path(name).write_text("{not valid json", encoding="utf-8")
    history = EvidenceHistoryStore(path=Path(name))
    try:
        history.read()
        check("corrupted history raises ValueError", False)
    except ValueError:
        check("corrupted history raises ValueError", True)
    finally:
        Path(name).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 25. store recovery.
# ---------------------------------------------------------------------------

def test_store_recovery():
    section("25. store recovery (read-back after append survives a fresh store instance)")
    history = _temp_history()
    try:
        history.append(_fake_snapshot(future_fixtures=1))
        history.append(_fake_snapshot(future_fixtures=2))
        history.append(_fake_snapshot(future_fixtures=3))
        recovered = EvidenceHistoryStore(path=history.path).read()
        check("all 3 snapshots recovered", len(recovered) == 3)
        check("no snapshot ever lost/reordered", [s["future_fixtures"] for s in recovered] == [1, 2, 3])
    finally:
        _cleanup_history(history)


# ---------------------------------------------------------------------------
# 26. concurrency.
# ---------------------------------------------------------------------------

def test_concurrency():
    section("26. concurrency (capture lock still exclusive — reused mechanism, never weakened by this phase)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        with acquire_capture_lock(lock) as held:
            check("first acquisition succeeds", held is True)
            outcome = run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
            check("concurrent run blocked", outcome["blocked"] is True and outcome["reason"] == "LOCK_HELD")
        outcome2 = run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        check("run succeeds after lock released", outcome2["blocked"] is False)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 27. deterministic output.
# ---------------------------------------------------------------------------

def test_deterministic_output():
    section("27. deterministic output (same inputs -> same snapshot/trend, pure functions)")
    fixed_as_of = datetime.now(UTC) - timedelta(hours=1)
    health = {"reality": {"future_fixtures": 2, "pending_model_predictions": 3}, "capturable": 1, "captured": 1, "resolved_shadow": 0, "conflicts": 0}
    full_ledger = {"by_data_marking_class": {"REAL_PROSPECTIVE": 1, "REAL_BUT_TEMPORAL_UNVERIFIED": 0, "HISTORICAL": 0, "SYNTHETIC": 0},
                   "provenance_complete": 0, "provenance_incomplete": 1, "provenance_unknown": 0, "maturity_real_prospective_resolved": "NO_DATA"}
    capture_outcome = {"blocked": False, "rejected": [], "mismatches": []}
    snap1 = compute_evidence_snapshot(as_of=fixed_as_of, health=health, full_ledger=full_ledger, capture_outcome=capture_outcome, track_record_sample_size=0, readiness_verdict="NO_GO")
    snap2 = compute_evidence_snapshot(as_of=fixed_as_of, health=health, full_ledger=full_ledger, capture_outcome=capture_outcome, track_record_sample_size=0, readiness_verdict="NO_GO")
    check("identical inputs -> identical snapshot", snap1 == snap2)


# ---------------------------------------------------------------------------
# 28. no network.
# ---------------------------------------------------------------------------

def test_no_network():
    section("28. no network")
    source = inspect.getsource(watch)
    check("watch.py: no httpx/requests", "import httpx" not in source and "import requests" not in source)
    check("script: no httpx/requests", "import httpx" not in SCRIPT_SOURCE and "import requests" not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 29. no model call.
# ---------------------------------------------------------------------------

def test_no_model_call():
    section("29. no model call")
    source = inspect.getsource(watch)
    check("no xgboost/lightgbm/dixon_coles import", all(x not in source for x in ("import xgboost", "import lightgbm", "dixon_coles")))
    check("script never calls .predict(", ".predict(" not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 30. no training.
# ---------------------------------------------------------------------------

def test_no_training():
    section("30. no training")
    check("watch.py: no train_/fit", "train_" not in inspect.getsource(watch) and ".fit(" not in inspect.getsource(watch))
    check("script: no train_/fit", "train_" not in SCRIPT_SOURCE and ".fit(" not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 31. no promotion.
# ---------------------------------------------------------------------------

def test_no_promotion():
    section("31. no promotion")
    check("watch.py: no apply_promotion/deactivate_other_versions", all(x not in inspect.getsource(watch) for x in ("apply_promotion", "deactivate_other_versions")))
    check("script: no apply_promotion/deactivate_other_versions", all(x not in SCRIPT_SOURCE for x in ("apply_promotion", "deactivate_other_versions")))


# ---------------------------------------------------------------------------
# 32. no scheduler modification.
# ---------------------------------------------------------------------------

def test_no_scheduler():
    section("32. no scheduler modification")
    check("watch.py: scheduler never imported", "arena.scheduler" not in inspect.getsource(watch) and "import scheduler" not in inspect.getsource(watch))
    check("script: scheduler never imported", "arena.scheduler" not in SCRIPT_SOURCE and "import scheduler" not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 33. no frontend modification.
# ---------------------------------------------------------------------------

def test_no_frontend():
    section("33. no frontend modification")
    check("watch.py: no frontend import", "import frontend" not in inspect.getsource(watch) and "from frontend" not in inspect.getsource(watch))
    check("script: no frontend import", "import frontend" not in SCRIPT_SOURCE and "from frontend" not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 34. kill switch unchanged.
# ---------------------------------------------------------------------------

def test_kill_switch_unchanged():
    section("34. kill switch unchanged (preflight, reused from Phase 9.4, is READ-ONLY)")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        ks = _temp_switch()
        try:
            check("state file absent before preflight", not ks.state_path.exists())
            run_preflight_safety(s, store, ks, datetime.now(UTC))
            check("state file still absent after preflight", not ks.state_path.exists())
        finally:
            _cleanup_switch(ks)
    check("watch.py never triggers/resets the Kill Switch", ".trigger(" not in inspect.getsource(watch) and ".reset(" not in inspect.getsource(watch))
    check("script never triggers/resets the Kill Switch", ".trigger(" not in SCRIPT_SOURCE and ".reset(" not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 35. no production activation.
# ---------------------------------------------------------------------------

def test_no_production_activation():
    section("35. no production activation")
    with Session(engine) as s:
        _clear_all(s)
        mp, version = _seed_pending_prediction(s)
        version_id = version.id
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        s.expire_all()
        refreshed = s.get(ModelVersion, version_id)
        check("ModelVersion.is_active unchanged", refreshed.is_active is True)
        check("ModelVersion.status unchanged", refreshed.status == "active")
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 36. odds unavailable.
# ---------------------------------------------------------------------------

def test_odds_unavailable():
    section("36. odds unavailable (never fabricated)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        for r, _ in store.all():
            check(f"odds_source None for {r.shadow_id}", r.odds_source is None)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 37. value unavailable.
# ---------------------------------------------------------------------------

def test_value_unavailable():
    section("37. value unavailable (NOT_AVAILABLE without verified odds)")
    status = value_tracking_status([])
    check("NOT_AVAILABLE on empty entries", status["status"] == "NOT_AVAILABLE")


# ---------------------------------------------------------------------------
# 38. report generation.
# ---------------------------------------------------------------------------

def test_report_generation_building_blocks():
    section("38. report generation (building blocks well-formed)")
    snapshot = _fake_snapshot(future_fixtures=0, real_prospective=0, maturity="NO_DATA")
    check("snapshot has all SNAPSHOT_FIELDS", all(k in snapshot for k in SNAPSHOT_FIELDS))
    trend = compute_evidence_trend([snapshot])
    check("trend well-formed on 1 snapshot", trend["status"] == "INSUFFICIENT_DATA")
    evolution = compute_blocker_evolution([], [])
    check("blocker evolution well-formed on baseline", evolution["status"] == "BASELINE_ONLY")
    verdict = derive_watch_verdict(preflight_status="PASS", tests_green=True, future_fixtures=0, real_prospective_resolved=0,
                                    maturity="NO_DATA", blockers=[], readiness_verdict="NO_GO")
    check("verdict in allowed vocabulary", verdict in WATCH_VERDICTS)
    check("PRODUCTION_READY structurally absent from vocabulary", "PRODUCTION_READY" not in WATCH_VERDICTS)
    check("SHADOW_OPERATIONAL absent from this phase's vocabulary (own vocabulary, never Phase 9.4's)", "SHADOW_OPERATIONAL" not in WATCH_VERDICTS)


# ---------------------------------------------------------------------------
# 39. preflight blocking.
# ---------------------------------------------------------------------------

def test_preflight_blocking():
    section("39. preflight blocking (corrupted store -> FAIL, STOP)")
    with Session(engine) as s:
        _clear_all(s)
        fd, name = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        Path(name).write_text("{not valid json", encoding="utf-8")
        store = ShadowDecisionStore(path=Path(name))
        ks = _temp_switch()
        try:
            result = run_preflight_safety(s, store, ks, datetime.now(UTC))
            check("status FAIL", result["status"] == "FAIL")
            check("STORE_CORRUPTION in blocking", "STORE_CORRUPTION" in result["blocking"])
        finally:
            Path(name).unlink(missing_ok=True)
            _cleanup_switch(ks)


# ---------------------------------------------------------------------------
# 40. MODE_1 enforcement.
# ---------------------------------------------------------------------------

def test_mode_1_enforcement():
    section("40. MODE_1_SHADOW_ONLY enforced structurally")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        ks = _temp_switch()
        try:
            result = run_preflight_safety(s, store, ks, datetime.now(UTC))
            check("mode is MODE_1_SHADOW_ONLY", result["checks"]["mode"]["value"] == "MODE_1_SHADOW_ONLY")
        finally:
            _cleanup_switch(ks)
    check("no other mode ever assigned in watch.py", all(x not in inspect.getsource(watch) for x in ("MODE_2", "MODE_3", "MODE_4")))
    check("script never claims MODE_2/3/4 as operating mode", '"mode": "MODE_2' not in SCRIPT_SOURCE and '"mode": "MODE_3' not in SCRIPT_SOURCE)


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
