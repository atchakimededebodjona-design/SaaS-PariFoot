"""
test_prospective_shadow.py — Phase 9.2 : tests de api/app/ai/shadow/
prospective.py. Base isolée dédiée (jamais api/app.db), Shadow Store et
lock isolés dédiés (jamais reports/shadow/shadow_decision_store.json /
prospective_capture.lock réels).

Usage : python api/test_prospective_shadow.py
"""

import inspect
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_prospective_shadow.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match
from app.models.model_prediction import ModelPrediction
from app.models.prediction_log import PredictionLog
from app.models.team_rating import ModelVersion, next_version_name

from app.ai.shadow.tracking import ShadowDecisionStore
from app.ai.shadow.resolution import resolve_record
from app.ai.shadow.metrics import compute_shadow_track_record, classify_maturity
from app.ai.shadow.monitoring import compute_shadow_health

import app.ai.shadow.prospective as prospective
from app.ai.shadow.prospective import (
    is_real_prospective, compute_as_of_window_label, acquire_capture_lock,
    run_prospective_capture, compute_evidence_ledger, classify_capture_quality,
    backup_store, restore_and_validate, compute_readiness_impact,
    PROSPECTIVE_TIMING_STATUSES, WINDOW_LABELS, CAPTURE_QUALITY_CATEGORIES,
)

init_db()
UTC = timezone.utc


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
    for model in (ModelPrediction, ModelVersion, PredictionLog):
        for row in session.exec(select(model)).all():
            session.delete(row)
    session.commit()


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
# 1. future candidate discovery.
# ---------------------------------------------------------------------------

def test_future_candidate_discovery():
    section("1. future candidate discovery")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        as_of = datetime.now(UTC)
        outcome = run_prospective_capture(s, store, as_of, dry_run=True, lock_path=lock)
        check("candidate discovered", outcome["candidates"] == 1)
        check("captured (dry-run)", outcome["captured"] == 1)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 2. no future fixtures.
# ---------------------------------------------------------------------------

def test_no_future_fixtures():
    section("2. no future fixtures -> NO_DATA")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        lock = _temp_lock()
        outcome = run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        check("0 candidates", outcome["candidates"] == 0)
        ledger = compute_evidence_ledger(store.all())
        check("evidence maturity NO_DATA", ledger["maturity"] == "NO_DATA")
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 3. pending-only.
# ---------------------------------------------------------------------------

def test_pending_only():
    section("3. pending-only (resolved predictions never re-captured as candidates)")
    with Session(engine) as s:
        _clear_all(s)
        mp, _ = _seed_pending_prediction(s)
        mp.status = "resolved"
        s.add(mp); s.commit()
        store = _temp_store()
        lock = _temp_lock()
        outcome = run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        check("resolved prediction never discovered as candidate", outcome["candidates"] == 0)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 4. no network.
# ---------------------------------------------------------------------------

def test_no_network():
    section("4. no network")
    source = inspect.getsource(prospective)
    check("no httpx/requests import", "import httpx" not in source and "import requests" not in source)


# ---------------------------------------------------------------------------
# 5. no new prediction.
# ---------------------------------------------------------------------------

def test_no_new_prediction():
    section("5. no new prediction (Shadow never calls a model)")
    source = inspect.getsource(prospective)
    check("no dixon_coles/elo/xgboost/lightgbm engine import", "app.ai.engine" not in source)
    check("no ModelOrchestrator import", "ModelOrchestrator" not in source)


# ---------------------------------------------------------------------------
# 6-9. cutoff windows.
# ---------------------------------------------------------------------------

def test_cutoff_windows():
    section("6-9. cutoff windows T-24h/T-12h/T-6h/T-1h")
    match_date = date(2026, 9, 5)
    kickoff_placeholder = datetime(2026, 9, 5, 0, 0, tzinfo=UTC)
    cases = [
        (kickoff_placeholder - timedelta(hours=30), "T_MINUS_24H_OR_MORE"),
        (kickoff_placeholder - timedelta(hours=18), "T_MINUS_12H_TO_24H"),
        (kickoff_placeholder - timedelta(hours=9), "T_MINUS_6H_TO_12H"),
        (kickoff_placeholder - timedelta(hours=3), "T_MINUS_1H_TO_6H"),
        (kickoff_placeholder - timedelta(minutes=30), "UNDER_T_MINUS_1H"),
        (kickoff_placeholder + timedelta(hours=1), "AT_OR_AFTER_KICKOFF_PLACEHOLDER"),
    ]
    for as_of, expected in cases:
        label = compute_as_of_window_label(as_of, match_date)
        check(f"window label for as_of={as_of.isoformat()} -> {expected}", label == expected)
    check("all labels in WINDOW_LABELS vocabulary", all(lbl in WINDOW_LABELS for _, lbl in cases))


# ---------------------------------------------------------------------------
# 10/11. prediction after cutoff / future information.
# ---------------------------------------------------------------------------

def test_prediction_after_cutoff_rejected():
    section("10. prediction generated after cutoff -> REJECTED")
    with Session(engine) as s:
        _clear_all(s)
        as_of = datetime.now(UTC)
        _seed_pending_prediction(s, predicted_at=as_of + timedelta(hours=1))  # prédite APRÈS as_of
        store = _temp_store()
        lock = _temp_lock()
        outcome = run_prospective_capture(s, store, as_of, dry_run=True, lock_path=lock)
        check("rejected (prediction after cutoff)", outcome["captured"] == 0 and len(outcome["rejected"]) == 1)
        check("reason PREDICTION_TIMESTAMP_AFTER_AS_OF", outcome["rejected"][0]["reason"] == "PREDICTION_TIMESTAMP_AFTER_AS_OF")
        lock.unlink(missing_ok=True)


def test_future_information_rejected():
    section("11. future information (kickoff already passed relative to as_of) -> REJECTED")
    with Session(engine) as s:
        _clear_all(s)
        past_date = date.today() - timedelta(days=1)
        _seed_pending_prediction(s, match_date=past_date)
        store = _temp_store()
        lock = _temp_lock()
        outcome = run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        check("no candidate for a past match_date", outcome["candidates"] == 0)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 12. unknown timestamp.
# ---------------------------------------------------------------------------

def test_unknown_timestamp():
    section("12. unknown timestamp -> UNKNOWN, never a default proof")
    status, reason = is_real_prospective(capture_timestamp=None, match_date=date(2026, 9, 1), prediction_timestamp=datetime.now(UTC), as_of=datetime.now(UTC))
    check("status UNKNOWN when capture_timestamp missing", status == "UNKNOWN")
    status2, _ = is_real_prospective(capture_timestamp=datetime.now(UTC), match_date=None, prediction_timestamp=datetime.now(UTC), as_of=datetime.now(UTC))
    check("status UNKNOWN when match_date missing", status2 == "UNKNOWN")
    check("UNKNOWN in vocabulary", "UNKNOWN" in PROSPECTIVE_TIMING_STATUSES)


# ---------------------------------------------------------------------------
# 13/14/15. model/probability/decision mismatch -> reject, never auto-corrected.
# ---------------------------------------------------------------------------

def test_model_mismatch_rejected():
    section("13. model mismatch -> rejected, never auto-corrected")
    with Session(engine) as s:
        _clear_all(s)
        mp, version = _seed_pending_prediction(s)
        # Corrompt le nom de version APRÈS capture du mp (simule une divergence détectée à la relecture) :
        # jamais un scénario fabriqué au-delà de ce que check_production_consistency sait déjà détecter.
        version.name = "xfoot-completely-different-name-v1"
        s.add(version); s.commit()
        store = _temp_store()
        lock = _temp_lock()
        outcome = run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        check("captured 0, mismatch detected", outcome["captured"] == 0 and len(outcome["mismatches"]) == 1)
        check("MODEL_VERSION_MISMATCH reported", "MODEL_VERSION_MISMATCH" in outcome["mismatches"][0]["mismatches"])
        lock.unlink(missing_ok=True)


def test_probability_mismatch_rejected():
    section("14. probability mismatch -> rejected")
    # check_production_consistency compare pi.probabilities (snapshot au moment du build) à mp.prob_* actuels —
    # simulé en modifiant mp APRÈS la construction du PipelineInput mais avant l'appel à check (même mécanisme
    # que Phase 8M) : ici, on vérifie directement la fonction pure avec un désaccord construit.
    from app.ai.shadow.live import check_production_consistency
    from app.ai.pipeline.schemas import PipelineInput
    with Session(engine) as s:
        _clear_all(s)
        mp, version = _seed_pending_prediction(s)
        pi = PipelineInput(match_id=mp.id, league=mp.league, kickoff=None, as_of=datetime.now(UTC), model=mp.model_type,
                            market="1X2", selection="home_win", probabilities={"home_win": 0.99, "draw": 0.005, "away_win": 0.005},
                            model_version=version.name)
        mismatches = check_production_consistency(mp, pi, None)
        check("PROBABILITY_MISMATCH detected", "PROBABILITY_MISMATCH" in mismatches)


def test_decision_mismatch_rejected():
    section("15. decision mismatch -> rejected")
    from app.ai.shadow.live import check_production_consistency
    from app.ai.pipeline.schemas import PipelineInput
    with Session(engine) as s:
        _clear_all(s)
        mp, version = _seed_pending_prediction(s)
        pi = PipelineInput(match_id=mp.id, league=mp.league, kickoff=None, as_of=datetime.now(UTC), model=mp.model_type,
                            market="1X2", selection="home_win", probabilities={"home_win": mp.prob_home, "draw": mp.prob_draw, "away_win": mp.prob_away},
                            model_version=version.name)

        class _FakeAssessment:
            def __init__(self):
                self.prediction = {"probabilities": {"home_win": 0.11, "draw": 0.11, "away_win": 0.78}}  # divergent du PipelineInput
                self.decision = object()  # non-None : active la comparaison DECISION_INPUT_MISMATCH
        fa = _FakeAssessment()
        mismatches = check_production_consistency(mp, pi, fa)
        check("DECISION_INPUT_MISMATCH detected", "DECISION_INPUT_MISMATCH" in mismatches)


# ---------------------------------------------------------------------------
# 16. provenance missing.
# ---------------------------------------------------------------------------

def test_provenance_always_populated_on_real_capture():
    section("16. provenance missing -> never on a real successful capture")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        entries = store.all()
        check("at least 1 real capture", len(entries) >= 1)
        for r, _ in entries:
            check(f"provenance non-empty for {r.shadow_id}", bool(r.provenance) and any(v is not None for v in r.provenance.values()))
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 17. capture immutability.
# ---------------------------------------------------------------------------

def test_capture_immutability():
    section("17. capture immutability (frozen dataclass, fields survive a resolution update)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        record, resolution = store.all()[0]
        before = (record.model_type, record.probability_source, record.as_of, record.provenance)
        # tente une mutation directe -> doit lever (frozen dataclass)
        try:
            record.model_type = "tampered"
            check("frozen dataclass raises on mutation attempt", False)
        except Exception:
            check("frozen dataclass raises on mutation attempt", True)
        record2, _ = store.get(record.shadow_id)
        after = (record2.model_type, record2.probability_source, record2.as_of, record2.provenance)
        check("fields unchanged after reload", before == after)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 18. duplicate prevention.
# ---------------------------------------------------------------------------

def test_duplicate_prevention():
    section("18. duplicate prevention (same as_of -> 0 new)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        as_of = datetime.now(UTC)
        outcome1 = run_prospective_capture(s, store, as_of, lock_path=lock)
        outcome2 = run_prospective_capture(s, store, as_of, lock_path=lock)
        check("first run captures 1", outcome1["captured"] == 1)
        check("second identical run captures 0", outcome2["captured"] == 0)
        check("second run reports 1 duplicate prevented", outcome2["duplicates_prevented"] == 1)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 19. multiple as_of.
# ---------------------------------------------------------------------------

def test_multiple_as_of_produces_distinct_observations():
    section("19. multiple as_of -> distinct observations for the same fixture")
    with Session(engine) as s:
        _clear_all(s)
        match_date = date.today() + timedelta(days=5)
        _seed_pending_prediction(s, match_date=match_date, predicted_at=datetime.now(UTC) - timedelta(days=2))
        store = _temp_store()
        lock = _temp_lock()
        as_of_1 = datetime.now(UTC)
        as_of_2 = datetime.now(UTC) + timedelta(hours=6)
        o1 = run_prospective_capture(s, store, as_of_1, lock_path=lock)
        o2 = run_prospective_capture(s, store, as_of_2, lock_path=lock)
        check("first as_of captured", o1["captured"] == 1)
        check("second (different) as_of also captured", o2["captured"] == 1)
        check("store now holds 2 distinct records for the same fixture", len(store.all()) == 2)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 20/21/22. resolution / conflict / resolved immutability.
# ---------------------------------------------------------------------------

def test_resolution():
    section("20. resolution")
    with Session(engine) as s:
        _clear_all(s)
        for m in s.exec(select(Match)).all():
            s.delete(m)
        s.commit()
        match_date = date.today() + timedelta(days=1)
        mp, _ = _seed_pending_prediction(s, match_date=match_date)
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        record, resolution = store.all()[0]

        mp.status = "resolved"; mp.result_home_goals = 2; mp.result_away_goals = 0
        s.add(mp); s.commit()
        new_res = resolve_record(s, record, resolution)
        check("resolved", new_res.result_status == "RESOLVED")
        check("actual_outcome home_win", new_res.actual_outcome == "home_win")
        lock.unlink(missing_ok=True)


def test_conflict_detection():
    section("21. conflict detection between disagreeing sources")
    with Session(engine) as s:
        _clear_all(s)
        for m in s.exec(select(Match)).all():
            s.delete(m)
        s.commit()
        match_date = date.today() + timedelta(days=1)
        mp, _ = _seed_pending_prediction(s, match_date=match_date, home="ConflictHome", away="ConflictAway")
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        record, resolution = store.all()[0]

        mp.status = "resolved"; mp.result_home_goals = 2; mp.result_away_goals = 0
        s.add(mp)
        pl = PredictionLog(league=mp.league, match_date=match_date, home_team="ConflictHome", away_team="ConflictAway",
                            payload="{}", pick_1x2="home_win", pick_btts="no", pick_over_2_5="under",
                            result_home_goals=0, result_away_goals=0, result_fetched_at=datetime.now(UTC))
        s.add(pl); s.commit()

        new_res = resolve_record(s, record, resolution)
        check("CONFLICT detected", new_res.result_status == "CONFLICT")
        check("conflict_sources lists both sources", len(new_res.conflict_sources) == 2)
        lock.unlink(missing_ok=True)


def test_resolved_immutability():
    section("22. resolved immutability (never re-resolved)")
    with Session(engine) as s:
        _clear_all(s)
        for m in s.exec(select(Match)).all():
            s.delete(m)
        s.commit()
        match_date = date.today() + timedelta(days=1)
        mp, _ = _seed_pending_prediction(s, match_date=match_date)
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        record, resolution = store.all()[0]

        mp.status = "resolved"; mp.result_home_goals = 1; mp.result_away_goals = 1
        s.add(mp); s.commit()
        first = resolve_record(s, record, resolution)
        store.update_resolution(record.shadow_id, first)

        mp.result_home_goals = 5; mp.result_away_goals = 0  # changement ultérieur (ex. correction erronée) — jamais répercuté
        s.add(mp); s.commit()
        _, current = store.get(record.shadow_id)
        second = resolve_record(s, record, current)
        check("already-RESOLVED never re-evaluated", second.result_status == "RESOLVED" and second.actual_home_goals == 1)
        applied = store.update_resolution(record.shadow_id, second)
        check("update_resolution refuses to overwrite an existing RESOLVED", applied is False)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 23/24. maturity / track record.
# ---------------------------------------------------------------------------

def test_maturity():
    section("23. maturity classification")
    check("0 -> NO_DATA", classify_maturity(0) == "NO_DATA")
    check("5 -> EARLY_DATA", classify_maturity(5) == "EARLY_DATA")
    check("50 -> TRACKING", classify_maturity(50) == "TRACKING")
    check("150 -> STATISTICALLY_INFORMATIVE", classify_maturity(150) == "STATISTICALLY_INFORMATIVE")


def test_track_record_recomputed_from_observations():
    section("24. track record recomputed from individual observations (never average-of-averages)")
    with Session(engine) as s:
        _clear_all(s)
        for m in s.exec(select(Match)).all():
            s.delete(m)
        s.commit()
        match_date = date.today() + timedelta(days=1)
        mp, _ = _seed_pending_prediction(s, match_date=match_date)
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        record, resolution = store.all()[0]
        mp.status = "resolved"; mp.result_home_goals = 3; mp.result_away_goals = 0
        s.add(mp); s.commit()
        new_res = resolve_record(s, record, resolution)
        store.update_resolution(record.shadow_id, new_res)
        entries = store.all()
        tr = compute_shadow_track_record(entries, market="1X2")
        check("track record status ok with 1 resolved observation", tr["status"] == "ok")
        check("sample_size == 1", tr.get("sample_size") == 1)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 25. monitoring.
# ---------------------------------------------------------------------------

def test_monitoring_integration():
    section("25. monitoring (compute_shadow_health never crashes on new prospective records)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        as_of = datetime.now(UTC)
        run_prospective_capture(s, store, as_of, lock_path=lock)
        health = compute_shadow_health(s, store, as_of)
        check("health status in vocabulary", health["status"] in ("NO_DATA", "HEALTHY", "DEGRADED", "CRITICAL", "BLOCKED"))
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 26. dry-run purity.
# ---------------------------------------------------------------------------

def test_dry_run_purity():
    section("26. dry-run purity (0 write)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC), dry_run=True, lock_path=lock)
        check("store file never created in dry-run", not store.path.exists())
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 27. DB purity.
# ---------------------------------------------------------------------------

def test_db_purity():
    section("27. DB purity (match/match_stats/model_predictions counts unchanged by capture)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        before_mp = len(s.exec(select(ModelPrediction)).all())
        before_match = len(s.exec(select(Match)).all())
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        after_mp = len(s.exec(select(ModelPrediction)).all())
        after_match = len(s.exec(select(Match)).all())
        check("model_predictions row count unchanged", before_mp == after_mp)
        check("match row count unchanged", before_match == after_match)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 28/29/30. store atomicity / corruption / recovery.
# ---------------------------------------------------------------------------

def test_store_atomicity():
    section("28. store atomicity (save produces valid JSON, reloadable)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        reloaded = ShadowDecisionStore(path=store.path)
        reloaded.load()
        check("reload succeeds with same record count", len(reloaded.all()) == len(store.all()))
        lock.unlink(missing_ok=True)


def test_store_corruption_detected_on_backup_validation():
    section("29. store corruption detected (never silently accepted)")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        corrupt_backup = tmpdir / "corrupt_backup.json"
        corrupt_backup.write_text("{not valid json at all", encoding="utf-8")
        restore_to = tmpdir / "restored.json"
        try:
            restore_and_validate(corrupt_backup, restore_to)
            check("restore_and_validate raises on corrupted backup", False)
        except ValueError:
            check("restore_and_validate raises on corrupted backup", True)


def test_store_recovery_round_trip():
    section("30. store recovery (backup + restore round trip)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            backup_path = backup_store(store, tmpdir)
            check("backup file created", backup_path.exists())
            restored = restore_and_validate(backup_path, tmpdir / "restored_store.json")
            check("restored store has same record count", len(restored.all()) == len(store.all()))
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 31. concurrent runs.
# ---------------------------------------------------------------------------

def test_concurrent_runs_blocked():
    section("31. concurrent runs -> second BLOCKED, no duplicate/corruption")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        with acquire_capture_lock(lock) as held:
            check("first acquisition succeeds", held is True)
            # Une deuxième tentative PENDANT que le verrou est tenu -> refusée.
            outcome = run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
            check("concurrent run reports blocked", outcome["blocked"] is True and outcome["reason"] == "LOCK_HELD")
            check("no write happened", not store.path.exists())
        # Après relâchement, un nouveau run réussit normalement.
        outcome2 = run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        check("run succeeds after lock released", outcome2["blocked"] is False)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 32. deterministic output.
# ---------------------------------------------------------------------------

def test_deterministic_output():
    section("32. deterministic output (same DB + same store + same as_of -> same evidence ledger, §46)")
    results = []
    fixed_as_of = datetime.now(UTC) - timedelta(hours=1)  # fixe pour les 2 itérations — jamais datetime.now() ré-évalué (§46)
    fixed_predicted_at = fixed_as_of - timedelta(days=1)
    for _ in range(2):
        with Session(engine) as s:
            _clear_all(s)
            for m in s.exec(select(Match)).all():
                s.delete(m)
            s.commit()
            match_date = date.today() + timedelta(days=1)
            mp, _ = _seed_pending_prediction(s, match_date=match_date, predicted_at=fixed_predicted_at)
            store = _temp_store()
            lock = _temp_lock()
            run_prospective_capture(s, store, fixed_as_of, lock_path=lock)
            entries = store.all()
            ledger = compute_evidence_ledger(entries)
            # last_observation_captured_at reste le SEUL champ dérivé de l'heure système réelle (record.created_at,
            # jamais un paramètre contrôlable par l'appelant) — exclu ici pour la même raison que Phase 8N/8M
            # excluent déjà "generated_at"/"measured_at" de leurs propres comparaisons de déterminisme.
            results.append({k: v for k, v in ledger.items() if k != "last_observation_captured_at"})
            lock.unlink(missing_ok=True)
    check("ledgers structurally identical across independent runs (same fixed as_of/predicted_at)", results[0] == results[1])


# ---------------------------------------------------------------------------
# 33/34. no model training / no model promotion.
# ---------------------------------------------------------------------------

def test_no_model_training():
    section("33. no model training")
    source = inspect.getsource(prospective)
    check("no training function call", "train_" not in source and ".fit(" not in source)


def test_no_model_promotion():
    section("34. no model promotion")
    source = inspect.getsource(prospective)
    check("no apply_promotion/deactivate_other_versions import", "apply_promotion" not in source and "deactivate_other_versions" not in source)


# ---------------------------------------------------------------------------
# 35. no scheduler modification.
# ---------------------------------------------------------------------------

def test_no_scheduler_modification():
    section("35. no scheduler modification")
    source = inspect.getsource(prospective)
    check("scheduler.py never imported by prospective.py", "arena.scheduler" not in source and "import scheduler" not in source)


# ---------------------------------------------------------------------------
# 36/37. no odds fabrication / no value signal without verified odds.
# ---------------------------------------------------------------------------

def test_no_odds_fabrication():
    section("36. no odds fabrication (odds_input always None from build_pipeline_input_for_live)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        entries = store.all()
        check("at least 1 captured", len(entries) >= 1)
        for r, _ in entries:
            check(f"odds_source None for {r.shadow_id} (never fabricated)", r.odds_source is None)
        lock.unlink(missing_ok=True)


def test_no_value_signal_without_verified_odds():
    section("37. no value signal without verified odds")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        entries = store.all()
        for r, _ in entries:
            check(f"value_status never a positive value without odds for {r.shadow_id}", r.value_status not in ("POSITIVE_VALUE",))
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 38. fail-safe.
# ---------------------------------------------------------------------------

def test_fail_safe():
    section("38. fail-safe (is_real_prospective never defaults to a positive claim on missing data)")
    status, _ = is_real_prospective(capture_timestamp=None, match_date=None, prediction_timestamp=None, as_of=None)
    check("all-None inputs -> UNKNOWN, never CONSISTENT", status == "UNKNOWN")


# ---------------------------------------------------------------------------
# 39. report generation.
# ---------------------------------------------------------------------------

def test_report_generation_building_blocks():
    section("39. report generation (building blocks well-formed)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        outcome = run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        entries = store.all()
        ledger = compute_evidence_ledger(entries)
        quality = classify_capture_quality(outcome, entries)
        check("ledger has all expected keys", all(k in ledger for k in ("total_real_observations", "maturity", "distinct_models")))
        check("quality categories match vocabulary", set(quality.keys()) == set(CAPTURE_QUALITY_CATEGORIES))
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 40. no accidental production activation.
# ---------------------------------------------------------------------------

def test_no_accidental_production_activation():
    section("40. no accidental production activation")
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
