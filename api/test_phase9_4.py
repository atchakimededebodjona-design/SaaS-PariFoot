"""
test_phase9_4.py — Phase 9.4 : tests de api/app/ai/shadow/operations.py
(run_preflight_safety/summarize_multi_as_of_runs/derive_final_verdict, les
SEULES fonctions nouvelles de cette phase) et vérification structurelle de
scripts/shadow_operations.py (le runner opérationnel, jamais importé/exécuté
ici pour éviter tout appel récursif à ses propres suites de régression —
uniquement inspecté comme texte).

Toutes les briques DÉJÀ testées ailleurs (discover/capture/resolve/
monitoring/track record/évidence) sont réutilisées TELLES QUELLES via les
mêmes fonctions que scripts/shadow_operations.py appelle réellement — ce
fichier vérifie que la composition opérationnelle (pré-vol, multi-as_of,
dérivation de verdict, pureté DB, immutabilité, kill switch jamais écrit)
tient, jamais une réécriture des garanties déjà prouvées en Phase 8K-9.3.

Base isolée dédiée (jamais api/app.db), Shadow Store / lock / Kill Switch
store isolés dédiés (jamais les fichiers réels sous reports/).

Usage : python api/test_phase9_4.py
"""

import inspect
import os
import sys
import tempfile
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_phase9_4.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match
from app.models.model_prediction import ModelPrediction
from app.models.prediction_log import PredictionLog
from app.models.team_rating import ModelVersion, next_version_name

from app.ai.pipeline.schemas import PipelineInput, CalibrationInput, FeatureSnapshotInput, TemporalMetadataInput

from app.ai.shadow.tracking import ShadowDecisionStore
from app.ai.shadow.schemas import ShadowDecisionRecord, ShadowResolution, pending_resolution
from app.ai.shadow.live import check_production_consistency
from app.ai.shadow.resolution import resolve_record
from app.ai.shadow.metrics import compute_shadow_track_record, classify_maturity, value_tracking_status
from app.ai.shadow.monitoring import compute_shadow_health, classify_prediction_timing
import app.ai.shadow.prospective as prospective
from app.ai.shadow.prospective import (
    run_prospective_capture, compute_evidence_ledger, classify_capture_quality, is_real_prospective,
    acquire_capture_lock, backup_store, restore_and_validate, CAPTURE_QUALITY_CATEGORIES,
)
from app.ai.readiness.matrix import evaluate_production_readiness
from app.ai.safety.kill_switch import KillSwitchStore

import app.ai.shadow.operations as operations
from app.ai.shadow.operations import (
    run_preflight_safety, summarize_multi_as_of_runs, derive_final_verdict,
    PREFLIGHT_STATUSES, FINAL_VERDICTS,
)

init_db()
UTC = timezone.utc
SCRIPT_SOURCE = (Path(__file__).parent.parent / "scripts" / "shadow_operations.py").read_text(encoding="utf-8")


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
# 1. preflight safety.
# ---------------------------------------------------------------------------

def test_preflight_safety():
    section("1. preflight safety (healthy state -> PASS, all checks reported)")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        ks = _temp_switch()
        try:
            result = run_preflight_safety(s, store, ks, datetime.now(UTC))
            check("status in vocabulary", result["status"] in PREFLIGHT_STATUSES)
            check("status PASS on healthy state", result["status"] == "PASS")
            check("no blocking codes", result["blocking"] == [])
            check("store_integrity checked", "store_integrity" in result["checks"])
            check("db_accessibility checked", "db_accessibility" in result["checks"])
            check("kill_switch checked", "kill_switch" in result["checks"])
            check("production_readiness checked", "production_readiness" in result["checks"])
            check("mode checked", result["checks"]["mode"]["value"] == "MODE_1_SHADOW_ONLY")
            check("readiness_assessment attached", result["readiness_assessment"] is not None)
        finally:
            _cleanup_switch(ks)


# ---------------------------------------------------------------------------
# 2. mode 1 enforced.
# ---------------------------------------------------------------------------

def test_mode_1_enforced():
    section("2. MODE_1_SHADOW_ONLY enforced structurally")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        ks = _temp_switch()
        try:
            result = run_preflight_safety(s, store, ks, datetime.now(UTC))
            check("mode is MODE_1_SHADOW_ONLY", result["checks"]["mode"]["value"] == "MODE_1_SHADOW_ONLY")
        finally:
            _cleanup_switch(ks)
    source = inspect.getsource(operations)
    check("no other mode ever assigned in operations.py", "MODE_2" not in source and "MODE_3" not in source and "MODE_4" not in source)
    check("script never claims MODE_2/3/4 as operating mode", '"mode": "MODE_2' not in SCRIPT_SOURCE and '"mode": "MODE_3' not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 3. future discovery.
# ---------------------------------------------------------------------------

def test_future_discovery():
    section("3. future discovery (DISCOVER stage, reused from Phase 8M/9.2)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        outcome = run_prospective_capture(s, store, datetime.now(UTC), dry_run=True, lock_path=lock)
        check("candidate discovered", outcome["candidates"] == 1)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 4. zero future fixtures.
# ---------------------------------------------------------------------------

def test_zero_future_fixtures():
    section("4. zero future fixtures -> NO_DATA verdict")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        lock = _temp_lock()
        outcome = run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        ledger = compute_evidence_ledger(store.all())
        verdict = derive_final_verdict(
            preflight_status="PASS", tests_green=True, capture_blocked=False, candidates=outcome["candidates"],
            total_real_observations=ledger["total_real_observations"], maturity=ledger["maturity"],
            blockers_after=[], readiness_after_verdict="NO_GO",
        )
        check("0 candidates", outcome["candidates"] == 0)
        check("verdict NO_DATA", verdict == "NO_DATA")
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 5. pending-only.
# ---------------------------------------------------------------------------

def test_pending_only():
    section("5. pending-only (resolved production predictions never re-discovered)")
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
# 6. no network.
# ---------------------------------------------------------------------------

def test_no_network():
    section("6. no network")
    source = inspect.getsource(operations)
    check("operations.py: no httpx/requests", "import httpx" not in source and "import requests" not in source)
    check("shadow_operations.py: no httpx/requests", "import httpx" not in SCRIPT_SOURCE and "import requests" not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 7. no model call.
# ---------------------------------------------------------------------------

def test_no_model_call():
    section("7. no model call (operations.py never imports a model class)")
    source = inspect.getsource(operations)
    check("no xgboost/lightgbm/dixon_coles import", all(x not in source for x in ("import xgboost", "import lightgbm", "dixon_coles")))
    check("script never calls .predict(", ".predict(" not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 8. no training.
# ---------------------------------------------------------------------------

def test_no_training():
    section("8. no training")
    check("operations.py: no train_/fit", "train_" not in inspect.getsource(operations) and ".fit(" not in inspect.getsource(operations))
    check("script: no train_/fit", "train_" not in SCRIPT_SOURCE and ".fit(" not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 9. no promotion.
# ---------------------------------------------------------------------------

def test_no_promotion():
    section("9. no promotion")
    check("operations.py: no apply_promotion/deactivate_other_versions", all(x not in inspect.getsource(operations) for x in ("apply_promotion", "deactivate_other_versions")))
    check("script: no apply_promotion/deactivate_other_versions", all(x not in SCRIPT_SOURCE for x in ("apply_promotion", "deactivate_other_versions")))


# ---------------------------------------------------------------------------
# 10. no scheduler.
# ---------------------------------------------------------------------------

def test_no_scheduler():
    section("10. no scheduler modification")
    check("operations.py: scheduler never imported", "arena.scheduler" not in inspect.getsource(operations) and "import scheduler" not in inspect.getsource(operations))
    check("script: scheduler never imported", "arena.scheduler" not in SCRIPT_SOURCE and "import scheduler" not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 11. no frontend.
# ---------------------------------------------------------------------------

def test_no_frontend():
    section("11. no frontend modification")
    check("operations.py: no frontend import", "import frontend" not in inspect.getsource(operations) and "from frontend" not in inspect.getsource(operations))
    check("script: no frontend import", "import frontend" not in SCRIPT_SOURCE and "from frontend" not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 12. cutoff.
# ---------------------------------------------------------------------------

def test_cutoff():
    section("12. cutoff (prediction_timestamp <= as_of < kickoff strictly enforced)")
    with Session(engine) as s:
        _clear_all(s)
        match_date = date.today() + timedelta(days=2)
        as_of = datetime.now(UTC)
        _seed_pending_prediction(s, match_date=match_date, predicted_at=as_of + timedelta(hours=1))  # prediction AFTER as_of
        store = _temp_store()
        lock = _temp_lock()
        outcome = run_prospective_capture(s, store, as_of, lock_path=lock)
        check("discovered but rejected at validation (prediction after as_of)", outcome["candidates"] == 1 and outcome["captured"] == 0 and len(outcome["rejected"]) == 1)
        check("rejection reason correct", outcome["rejected"][0]["reason"] == "PREDICTION_TIMESTAMP_AFTER_AS_OF")
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 13. unknown kickoff.
# ---------------------------------------------------------------------------

def test_unknown_kickoff():
    section("13. unknown kickoff -> UNKNOWN, never a positive claim")
    status, _ = is_real_prospective(capture_timestamp=datetime.now(UTC), match_date=None, prediction_timestamp=datetime.now(UTC), as_of=datetime.now(UTC))
    check("missing match_date -> UNKNOWN", status == "UNKNOWN")


# ---------------------------------------------------------------------------
# 14. late prediction.
# ---------------------------------------------------------------------------

def test_late_prediction():
    section("14. late prediction (kickoff time structurally unknown -> KICKOFF_TIME_UNKNOWN, never fabricated LATE)")
    with Session(engine) as s:
        _clear_all(s)
        mp, _ = _seed_pending_prediction(s)
        timing = classify_prediction_timing(mp, datetime.now(UTC), kickoff_time_known=False)
        check("late_status is KICKOFF_TIME_UNKNOWN", timing["late_status"] == "KICKOFF_TIME_UNKNOWN")


# ---------------------------------------------------------------------------
# 15. future information.
# ---------------------------------------------------------------------------

def test_future_information():
    section("15. future information (as_of at/after kickoff placeholder -> TIMING_VIOLATION)")
    match_date = date.today()
    kickoff_placeholder = datetime.combine(match_date, datetime.min.time(), tzinfo=UTC)
    status, _ = is_real_prospective(
        capture_timestamp=kickoff_placeholder - timedelta(hours=1), match_date=match_date,
        prediction_timestamp=kickoff_placeholder - timedelta(hours=2), as_of=kickoff_placeholder + timedelta(hours=1),
    )
    check("as_of after kickoff placeholder -> TIMING_VIOLATION", status == "TIMING_VIOLATION")


# ---------------------------------------------------------------------------
# 16. model mismatch.
# ---------------------------------------------------------------------------

def _fake_pi(model="xgboost", model_version="xfoot-xgboost-v1", market="1X2", probabilities=None):
    return PipelineInput(
        match_id=1, league="Ligue1", kickoff=datetime.now(UTC), as_of=datetime.now(UTC), model=model,
        market=market, selection="home_win", probabilities=probabilities or {"home_win": 0.5, "draw": 0.3, "away_win": 0.2},
        calibration=CalibrationInput(), feature_snapshot=FeatureSnapshotInput(), temporal_metadata=TemporalMetadataInput(),
        model_version=model_version,
    )


def test_model_mismatch():
    section("16. model mismatch (VERIFY stage — never auto-corrected)")
    with Session(engine) as s:
        _clear_all(s)
        mp, _ = _seed_pending_prediction(s, model_type="xgboost")
        pi = _fake_pi(model="lightgbm")  # différent de mp.model_type
        mismatches = check_production_consistency(mp, pi, None)
        check("MODEL_TYPE_MISMATCH detected", "MODEL_TYPE_MISMATCH" in mismatches)


# ---------------------------------------------------------------------------
# 17. probability mismatch.
# ---------------------------------------------------------------------------

def test_probability_mismatch():
    section("17. probability mismatch (VERIFY stage — never auto-corrected)")
    with Session(engine) as s:
        _clear_all(s)
        mp, _ = _seed_pending_prediction(s, model_type="xgboost")  # prob_home=0.5
        pi = _fake_pi(model="xgboost", probabilities={"home_win": 0.99, "draw": 0.005, "away_win": 0.005})
        mismatches = check_production_consistency(mp, pi, None)
        check("PROBABILITY_MISMATCH detected", "PROBABILITY_MISMATCH" in mismatches)


# ---------------------------------------------------------------------------
# 18. decision mismatch.
# ---------------------------------------------------------------------------

def test_decision_mismatch():
    section("18. decision mismatch (VERIFY stage — never auto-corrected)")
    with Session(engine) as s:
        _clear_all(s)
        mp, _ = _seed_pending_prediction(s, model_type="xgboost")
        pi = _fake_pi(model="xgboost", model_version="xfoot-xgboost-x", probabilities={"home_win": 0.5, "draw": 0.3, "away_win": 0.2})
        fake_assessment = SimpleNamespace(decision=SimpleNamespace(), prediction={"probabilities": {"home_win": 0.1, "draw": 0.1, "away_win": 0.8}})
        mismatches = check_production_consistency(mp, pi, fake_assessment)
        check("DECISION_INPUT_MISMATCH detected", "DECISION_INPUT_MISMATCH" in mismatches)


# ---------------------------------------------------------------------------
# 19. provenance.
# ---------------------------------------------------------------------------

def test_provenance():
    section("19. provenance (captured, never fabricated)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        entries = store.all()
        check("at least 1 captured", len(entries) >= 1)
        for r, _ in entries:
            check(f"provenance dict present for {r.shadow_id}", isinstance(r.provenance, dict) and len(r.provenance) > 0)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 20. immutability.
# ---------------------------------------------------------------------------

def test_immutability():
    section("20. immutability (ShadowDecisionRecord frozen — no field ever reassigned)")
    record = ShadowDecisionRecord(
        shadow_id="x", match_id=1, league="L", home_team="A", away_team="B", kickoff=datetime.now(UTC),
        as_of=datetime.now(UTC), model_type="xgboost", model_version="v1", calibration_source=None,
        market="1X2", selection="home_win", raw_probability=0.5, calibrated_probability=None,
        market_probabilities_raw={"home_win": 0.5}, market_probabilities_calibrated=None, probability_source="RAW",
        quality={}, confidence="UNKNOWN", eligibility="UNKNOWN", value_status=None, odds_source=None,
        odds_timestamp=None, temporal_status="UNKNOWN", provenance={}, status="UNKNOWN", created_at=datetime.now(UTC),
    )
    try:
        record.model_type = "lightgbm"
        check("mutation raises FrozenInstanceError", False)
    except FrozenInstanceError:
        check("mutation raises FrozenInstanceError", True)


# ---------------------------------------------------------------------------
# 21. duplicate prevention.
# ---------------------------------------------------------------------------

def test_duplicate_prevention():
    section("21. duplicate prevention (same fixture/market/model/as_of -> 0 new observation)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        as_of = datetime.now(UTC)
        first = run_prospective_capture(s, store, as_of, lock_path=lock)
        second = run_prospective_capture(s, store, as_of, lock_path=lock)
        check("first run captures 1", first["captured"] == 1)
        check("second run captures 0", second["captured"] == 0)
        check("second run reports duplicate prevented", second["duplicates_prevented"] == 1)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 22. multi-as_of.
# ---------------------------------------------------------------------------

def test_multi_as_of():
    section("22. multi-as_of (distinct as_of values across separate runs -> distinct observations, grouped/labeled)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s, predicted_at=datetime.now(UTC) - timedelta(days=1))
        store = _temp_store()
        lock = _temp_lock()
        as_of_1 = datetime.now(UTC) - timedelta(hours=20)
        as_of_2 = datetime.now(UTC) - timedelta(hours=8)
        run_prospective_capture(s, store, as_of_1, lock_path=lock)
        run_prospective_capture(s, store, as_of_2, lock_path=lock)
        entries = store.all()
        check("2 distinct observations captured (distinct as_of)", len(entries) == 2)
        summary = summarize_multi_as_of_runs(entries)
        check("1 combination with multiple as_of", summary["combinations_with_multiple_as_of"] == 1)
        check("detail non-empty and window-labeled", all("window_label" in o for group in summary["detail"].values() for o in group))
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 23. capture.
# ---------------------------------------------------------------------------

def test_capture():
    section("23. capture (real write to Shadow Store)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        outcome = run_prospective_capture(s, store, datetime.now(UTC), dry_run=False, lock_path=lock)
        check("1 captured", outcome["captured"] == 1)
        check("store file written", store.path.exists())
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 24. resolution.
# ---------------------------------------------------------------------------

def test_resolution():
    section("24. resolution (PENDING -> RESOLVED from a real match result)")
    with Session(engine) as s:
        _clear_all(s)
        # Capture exige kickoff > as_of (match FUTUR au moment de la capture, §5/§34 Phase 8M) — le résultat
        # (Match) n'est ajouté qu'APRÈS, représentant le match désormais joué (resolve_record ne dépend, lui,
        # jamais de l'heure courante — seulement de record.kickoff déjà figé à la capture).
        match_date = date.today() + timedelta(days=1)
        _seed_pending_prediction(s, match_date=match_date, predicted_at=datetime.now(UTC) - timedelta(hours=2))
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC) - timedelta(hours=1), lock_path=lock)
        m = Match(league="Ligue1", date=datetime.combine(match_date, datetime.min.time()), home_team="A", away_team="B", home_goals=2, away_goals=0)
        s.add(m); s.commit()
        record, resolution = store.all()[0]
        new_resolution = resolve_record(s, record, resolution)
        check("resolved", new_resolution.result_status == "RESOLVED")
        check("correct outcome derived", new_resolution.actual_outcome == "home_win")
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 25. conflict.
# ---------------------------------------------------------------------------

def test_conflict():
    section("25. conflict (two disagreeing sources -> CONFLICT, never an arbitrary pick)")
    with Session(engine) as s:
        _clear_all(s)
        league, match_date, home, away = "Ligue1", date.today() - timedelta(days=1), "A", "B"
        version = ModelVersion(name=next_version_name(s, "xfoot-xgboost"), model_type="xgboost",
                                trained_at=datetime.now(UTC) - timedelta(days=30), is_active=True, status="active")
        s.add(version); s.commit(); s.refresh(version)
        mp_resolved = ModelPrediction(league=league, match_date=match_date, home_team=home, away_team=away, model_type="xgboost",
                                       model_version_id=version.id, source="live", status="resolved",
                                       predicted_at=datetime.now(UTC) - timedelta(days=2),
                                       prob_home=0.5, prob_draw=0.3, prob_away=0.2, pick_1x2="home_win",
                                       result_home_goals=2, result_away_goals=0)
        s.add(mp_resolved)
        m = Match(league=league, date=datetime.combine(match_date, datetime.min.time()), home_team=home, away_team=away, home_goals=0, away_goals=0)
        s.add(m)
        s.commit()
        record = ShadowDecisionRecord(
            shadow_id="conflict-test", match_id=None, league=league, home_team=home, away_team=away,
            kickoff=datetime.combine(match_date, datetime.min.time(), tzinfo=UTC), as_of=datetime.now(UTC) - timedelta(hours=1),
            model_type="xgboost", model_version=version.name, calibration_source=None, market="1X2", selection="home_win",
            raw_probability=0.5, calibrated_probability=None, market_probabilities_raw={"home_win": 0.5, "draw": 0.3, "away_win": 0.2},
            market_probabilities_calibrated=None, probability_source="RAW", quality={}, confidence="UNKNOWN",
            eligibility="UNKNOWN", value_status=None, odds_source=None, odds_timestamp=None, temporal_status="UNKNOWN",
            provenance={}, status="UNKNOWN", created_at=datetime.now(UTC),
        )
        result = resolve_record(s, record, pending_resolution())
        check("CONFLICT detected", result.result_status == "CONFLICT")
        check("conflict_sources carries all values, no arbitrary pick", result.conflict_sources is not None and len(result.conflict_sources) >= 2)


# ---------------------------------------------------------------------------
# 26. resolved immutability.
# ---------------------------------------------------------------------------

def test_resolved_immutability():
    section("26. resolved immutability (store never overwrites an already-non-PENDING resolution)")
    with Session(engine) as s:
        _clear_all(s)
        match_date = date.today() + timedelta(days=1)  # capture exige kickoff > as_of — résultat ajouté APRÈS.
        _seed_pending_prediction(s, match_date=match_date, predicted_at=datetime.now(UTC) - timedelta(hours=2))
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC) - timedelta(hours=1), lock_path=lock)
        m = Match(league="Ligue1", date=datetime.combine(match_date, datetime.min.time()), home_team="A", away_team="B", home_goals=1, away_goals=1)
        s.add(m); s.commit()
        record, resolution = store.all()[0]
        first_resolution = resolve_record(s, record, resolution)
        applied = store.update_resolution(record.shadow_id, first_resolution)
        check("first resolution applied", applied is True)
        # Deuxième tentative avec un résultat DIFFÉRENT -> refusée, jamais écrasée.
        bogus = ShadowResolution(result_status="RESOLVED", actual_home_goals=9, actual_away_goals=9, actual_outcome="away_win")
        applied_again = store.update_resolution(record.shadow_id, bogus)
        check("second resolution rejected (already non-PENDING)", applied_again is False)
        _, stored_resolution = store.get(record.shadow_id)
        check("stored resolution unchanged", stored_resolution.actual_home_goals != 9)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 27. evidence ledger.
# ---------------------------------------------------------------------------

def test_evidence_ledger():
    section("27. evidence ledger (derived from real observations only)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        ledger = compute_evidence_ledger(store.all())
        check("total_real_observations == 1", ledger["total_real_observations"] == 1)
        check("maturity present", ledger["maturity"] in ("NO_DATA", "EARLY_DATA", "TRACKING", "STATISTICALLY_INFORMATIVE"))
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 28. track record.
# ---------------------------------------------------------------------------

def test_track_record():
    section("28. track record (resolved observation feeds compute_shadow_track_record)")
    with Session(engine) as s:
        _clear_all(s)
        match_date = date.today() + timedelta(days=1)  # capture exige kickoff > as_of — résultat ajouté APRÈS.
        _seed_pending_prediction(s, match_date=match_date, predicted_at=datetime.now(UTC) - timedelta(hours=2))
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC) - timedelta(hours=1), lock_path=lock)
        m = Match(league="Ligue1", date=datetime.combine(match_date, datetime.min.time()), home_team="A", away_team="B", home_goals=2, away_goals=0)
        s.add(m); s.commit()
        record, resolution = store.all()[0]
        new_resolution = resolve_record(s, record, resolution)
        store.update_resolution(record.shadow_id, new_resolution)
        tr = compute_shadow_track_record(store.all(), market="1X2")
        check("sample_size == 1", tr.get("sample_size") == 1)
        check("accuracy computed", tr.get("accuracy") is not None)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 29. maturity.
# ---------------------------------------------------------------------------

def test_maturity():
    section("29. maturity (reused thresholds, never an artificial transition)")
    check("0 -> NO_DATA", classify_maturity(0) == "NO_DATA")
    check("5 -> EARLY_DATA", classify_maturity(5) == "EARLY_DATA")
    check("15 -> TRACKING", classify_maturity(15) == "TRACKING")
    check("150 -> STATISTICALLY_INFORMATIVE", classify_maturity(150) == "STATISTICALLY_INFORMATIVE")


# ---------------------------------------------------------------------------
# 30. monitoring.
# ---------------------------------------------------------------------------

def test_monitoring():
    section("30. monitoring (compute_shadow_health reused, read-only)")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        health = compute_shadow_health(s, store, datetime.now(UTC))
        check("status present", health["status"] in ("NO_DATA", "HEALTHY", "DEGRADED", "CRITICAL", "BLOCKED"))
        check("NO_DATA on empty DB", health["status"] == "NO_DATA")


# ---------------------------------------------------------------------------
# 31. readiness reassessment.
# ---------------------------------------------------------------------------

def test_readiness_reassessment():
    section("31. readiness reassessment (before/after via evaluate_production_readiness, impact computed)")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        before = evaluate_production_readiness(s, store, datetime.now(UTC))
        after = evaluate_production_readiness(s, store, datetime.now(UTC))
        from app.ai.shadow.prospective import compute_readiness_impact
        impact = compute_readiness_impact(before.gates, after.gates)
        check("impact covers all tracked gates", set(impact.keys()) == set(prospective.READINESS_IMPACT_GATES))
        check("never PRODUCTION_READY reported as this-phase verdict vocabulary", "PRODUCTION_READY" not in FINAL_VERDICTS)


# ---------------------------------------------------------------------------
# 32. odds unavailable.
# ---------------------------------------------------------------------------

def test_odds_unavailable():
    section("32. odds unavailable (never fabricated)")
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
# 33. value unavailable.
# ---------------------------------------------------------------------------

def test_value_unavailable():
    section("33. value unavailable (NOT_AVAILABLE without verified odds)")
    status = value_tracking_status([])
    check("NOT_AVAILABLE on empty entries", status["status"] == "NOT_AVAILABLE")


# ---------------------------------------------------------------------------
# 34. store corruption.
# ---------------------------------------------------------------------------

def test_store_corruption():
    section("34. store corruption -> preflight FAIL, never silently accepted")
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
# 35. store recovery.
# ---------------------------------------------------------------------------

def test_store_recovery():
    section("35. store recovery (backup + restore round trip)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            backup_path = backup_store(store, tmpdir)
            restored = restore_and_validate(backup_path, tmpdir / "restored.json")
            check("restored store has same record count", len(restored.all()) == len(store.all()))
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 36. atomicity.
# ---------------------------------------------------------------------------

def test_atomicity():
    section("36. atomicity (save produces valid, reloadable JSON)")
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


# ---------------------------------------------------------------------------
# 37. concurrency.
# ---------------------------------------------------------------------------

def test_concurrency():
    section("37. concurrency (two simultaneous captures -> second BLOCKED, no duplicate/corruption)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        with acquire_capture_lock(lock) as held:
            check("first acquisition succeeds", held is True)
            outcome = run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
            check("concurrent run blocked", outcome["blocked"] is True and outcome["reason"] == "LOCK_HELD")
            check("no write happened", not store.path.exists())
        outcome2 = run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        check("run succeeds after lock released", outcome2["blocked"] is False)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 38. deterministic output.
# ---------------------------------------------------------------------------

def test_deterministic_output():
    section("38. deterministic output (same DB + same store + same as_of -> same evidence ledger)")
    results = []
    fixed_as_of = datetime.now(UTC) - timedelta(hours=1)
    fixed_predicted_at = fixed_as_of - timedelta(days=1)
    for _ in range(2):
        with Session(engine) as s:
            _clear_all(s)
            match_date = date.today() + timedelta(days=1)
            _seed_pending_prediction(s, match_date=match_date, predicted_at=fixed_predicted_at)
            store = _temp_store()
            lock = _temp_lock()
            run_prospective_capture(s, store, fixed_as_of, lock_path=lock)
            entries = store.all()
            ledger = compute_evidence_ledger(entries)
            results.append({k: v for k, v in ledger.items() if k != "last_observation_captured_at"})
            lock.unlink(missing_ok=True)
    check("ledgers structurally identical across independent runs", results[0] == results[1])


# ---------------------------------------------------------------------------
# 39. dry-run purity.
# ---------------------------------------------------------------------------

def test_dry_run_purity():
    section("39. dry-run purity (0 write, 0 resolve, 0 DB mutation)")
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
# 40. capture DB purity.
# ---------------------------------------------------------------------------

def test_capture_db_purity():
    section("40. capture DB purity (real capture writes ONLY to the Shadow Store)")
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
# 41. resolve DB purity.
# ---------------------------------------------------------------------------

def test_resolve_db_purity():
    section("41. resolve DB purity (resolve updates ONLY the Shadow Store, never model_predictions/match/prediction_log)")
    with Session(engine) as s:
        _clear_all(s)
        match_date = date.today() + timedelta(days=1)  # capture exige kickoff > as_of — résultat ajouté APRÈS.
        _seed_pending_prediction(s, match_date=match_date, predicted_at=datetime.now(UTC) - timedelta(hours=2))
        before_mp = len(s.exec(select(ModelPrediction)).all())
        before_pl = len(s.exec(select(PredictionLog)).all())
    store = _temp_store()
    lock = _temp_lock()
    with Session(engine) as s:
        run_prospective_capture(s, store, datetime.now(UTC) - timedelta(hours=1), lock_path=lock)
    with Session(engine) as s:
        # Le résultat du match (déjà en base, produit par la production) est une donnée d'ENTRÉE de la
        # résolution — sa création n'est pas l'opération testée ici, seul resolve_record/update_resolution l'est.
        m = Match(league="Ligue1", date=datetime.combine(match_date, datetime.min.time()), home_team="A", away_team="B", home_goals=2, away_goals=0)
        s.add(m); s.commit()
        before_match = len(s.exec(select(Match)).all())
        record, resolution = store.all()[0]
        new_resolution = resolve_record(s, record, resolution)
        store.update_resolution(record.shadow_id, new_resolution)
    with Session(engine) as s:
        after_mp = len(s.exec(select(ModelPrediction)).all())
        after_match = len(s.exec(select(Match)).all())
        after_pl = len(s.exec(select(PredictionLog)).all())
    check("model_predictions row count unchanged", before_mp == after_mp)
    check("match row count unchanged", before_match == after_match)
    check("prediction_log row count unchanged", before_pl == after_pl)
    check("resolution actually applied in store", store.get(record.shadow_id)[1].result_status == "RESOLVED")
    lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 42. report generation.
# ---------------------------------------------------------------------------

def test_report_generation_building_blocks():
    section("42. report generation (building blocks well-formed)")
    with Session(engine) as s:
        _clear_all(s)
        _seed_pending_prediction(s)
        store = _temp_store()
        lock = _temp_lock()
        outcome = run_prospective_capture(s, store, datetime.now(UTC), lock_path=lock)
        entries = store.all()
        ledger = compute_evidence_ledger(entries)
        quality = classify_capture_quality(outcome, entries)
        summary = summarize_multi_as_of_runs(entries)
        check("ledger has expected keys", all(k in ledger for k in ("total_real_observations", "maturity", "distinct_models")))
        check("quality categories match vocabulary", set(quality.keys()) == set(CAPTURE_QUALITY_CATEGORIES))
        check("multi_as_of_summary well-formed", all(k in summary for k in ("distinct_match_market_model_combinations", "combinations_with_multiple_as_of", "detail")))
        verdict = derive_final_verdict(preflight_status="PASS", tests_green=True, capture_blocked=False, candidates=outcome["candidates"],
                                        total_real_observations=ledger["total_real_observations"], maturity=ledger["maturity"],
                                        blockers_after=[], readiness_after_verdict="NO_GO")
        check("verdict in allowed vocabulary", verdict in FINAL_VERDICTS)
        check("PRODUCTION_READY structurally absent from vocabulary", "PRODUCTION_READY" not in FINAL_VERDICTS)
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 43. no automatic activation.
# ---------------------------------------------------------------------------

def test_no_automatic_activation():
    section("43. no automatic activation")
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
    source = inspect.getsource(operations)
    check("operations.py never triggers/resets the Kill Switch", ".trigger(" not in source and ".reset(" not in source)
    check("script never triggers/resets the Kill Switch", ".trigger(" not in SCRIPT_SOURCE and ".reset(" not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 44. kill switch unchanged.
# ---------------------------------------------------------------------------

def test_kill_switch_unchanged():
    section("44. kill switch unchanged (preflight is READ-ONLY on the Kill Switch)")
    with Session(engine) as s:
        _clear_all(s)
        store = _temp_store()
        ks = _temp_switch()
        try:
            check("state file absent before preflight", not ks.state_path.exists())
            run_preflight_safety(s, store, ks, datetime.now(UTC))
            check("state file still absent after preflight (never created/written)", not ks.state_path.exists())
        finally:
            _cleanup_switch(ks)


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
