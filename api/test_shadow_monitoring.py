"""
test_shadow_monitoring.py — Phase 8N : tests de api/app/ai/shadow/monitoring.py.

Base isolée dédiée (jamais api/app.db pour ceux-ci). Le monitoring est
STRICTEMENT READ-ONLY — ce fichier le vérifie explicitement (§32/§52).

Usage : python api/test_shadow_monitoring.py
"""

import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_shadow_monitoring.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match, MatchStats
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion, TeamRating, next_version_name

from app.ai.pipeline.orchestrator import run_pipeline
from app.ai.shadow.schemas import ShadowDecisionRecord, ShadowResolution, pending_resolution
from app.ai.shadow.tracking import ShadowDecisionStore, capture_shadow_decision, dedup_key
from app.ai.shadow.live import build_pipeline_input_for_live
from app.ai.shadow.monitoring import (
    HEALTH_STATUSES, ALERT_CATEGORIES, ALERT_SEVERITIES, Alert,
    compute_fixture_coverage, compute_capture_coverage, classify_missed_capture_reason,
    classify_prediction_timing, compute_temporal_health, compute_provenance_health,
    compute_consistency_health, compute_duplicate_health, compute_store_integrity,
    compute_resolution_health, find_matches_played_but_pending, compute_track_record_health,
    check_invalid_probabilities, compute_odds_and_value_health, compute_feature_health,
    build_alerts, derive_health_status, compute_shadow_health, _dedup_alerts,
    OPERATIONAL_UNRESOLVED_AGE_HOURS,
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


def _temp_store() -> ShadowDecisionStore:
    import os
    fd, name = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    tmp = Path(name)
    tmp.unlink()
    return ShadowDecisionStore(path=tmp)


def _record(shadow_id="r1", **overrides):
    base = dict(
        shadow_id=shadow_id, match_id=None, league="Ligue1", home_team="A", away_team="B",
        kickoff=datetime(2026, 1, 1, tzinfo=UTC), as_of=datetime(2026, 1, 1, tzinfo=UTC),
        model_type="xgboost", model_version="xfoot-xgboost-v1", calibration_source="RAW", market="1X2", selection="home_win",
        raw_probability=0.6, calibrated_probability=None,
        market_probabilities_raw={"home_win": 0.6, "draw": 0.25, "away_win": 0.15}, market_probabilities_calibrated=None,
        probability_source="RAW", quality={"data_quality": "HIGH"}, confidence="HIGH", eligibility="ELIGIBLE",
        value_status=None, odds_source=None, odds_timestamp=None, temporal_status="TEMPORALLY_VERIFIED",
        provenance={"model_source": "xgboost", "model_version": "xfoot-xgboost-v1", "calibration_source": "RAW", "feature_snapshot": "s1", "odds_source": None},
        status="VALUE_CANDIDATE", created_at=datetime.now(UTC),
    )
    base.update(overrides)
    return ShadowDecisionRecord(**base)


def _seed_pending_prediction(session, league, match_date, home, away, model_type="xgboost", predicted_at=None):
    version = ModelVersion(name=next_version_name(session, f"xfoot-{model_type}"), model_type=model_type, trained_at=datetime.now(timezone.utc) - timedelta(days=30), is_active=True)
    session.add(version); session.commit(); session.refresh(version)
    mp = ModelPrediction(league=league, match_date=match_date, home_team=home, away_team=away, model_type=model_type,
                          model_version_id=version.id, source="live", status="pending",
                          predicted_at=predicted_at or (datetime.now(timezone.utc) - timedelta(days=1)),
                          prob_home=0.5, prob_draw=0.3, prob_away=0.2, pick_1x2="home_win")
    session.add(mp); session.commit(); session.refresh(mp)
    return mp, version


# ---------------------------------------------------------------------------
# 1. Health states (§4/§61) : no-data / healthy / degraded / critical
# ---------------------------------------------------------------------------

def test_no_data_state_never_healthy():
    reality = {"future_fixtures": 0, "pending_model_predictions": 0}
    status = derive_health_status(reality=reality, alerts=[], store_integrity={"valid_json": True})
    assert status == "NO_DATA"
    print("  [OK] §5/§61 : 0 fixture future -> NO_DATA, jamais HEALTHY même avec 0 alerte")


def test_healthy_state_requires_data_and_no_alerts():
    reality = {"future_fixtures": 5, "pending_model_predictions": 5}
    status = derive_health_status(reality=reality, alerts=[], store_integrity={"valid_json": True})
    assert status == "HEALTHY"
    print("  [OK] données présentes + 0 alerte -> HEALTHY")


def test_degraded_state_on_warning_or_error():
    reality = {"future_fixtures": 5, "pending_model_predictions": 5}
    alerts = [Alert("LOW_COVERAGE", "WARNING", "k1", "msg")]
    assert derive_health_status(reality=reality, alerts=alerts, store_integrity={"valid_json": True}) == "DEGRADED"
    print("  [OK] alerte WARNING -> DEGRADED")


def test_critical_state_on_critical_alert():
    reality = {"future_fixtures": 5, "pending_model_predictions": 5}
    alerts = [Alert("MODEL_MISMATCH", "CRITICAL", "k1", "msg")]
    assert derive_health_status(reality=reality, alerts=alerts, store_integrity={"valid_json": True}) == "CRITICAL"
    print("  [OK] alerte CRITICAL -> CRITICAL")


def test_blocked_state_on_unreadable_store():
    status = derive_health_status(reality={"future_fixtures": 5, "pending_model_predictions": 5}, alerts=[], store_integrity={"valid_json": False})
    assert status == "BLOCKED"
    print("  [OK] store illisible -> BLOCKED")


# ---------------------------------------------------------------------------
# 2. Fixture / prediction coverage (§5/§6/§7)
# ---------------------------------------------------------------------------

def test_fixture_coverage_zero_future_is_no_data():
    cov = compute_fixture_coverage({"future_fixtures": 0, "pending_model_predictions": 3})
    assert cov["status"] == "NO_DATA" and cov["coverage"] is None
    print(f"  [OK] §6 : future=0 -> {cov}")


def test_prediction_coverage_computed_when_available():
    cov = compute_fixture_coverage({"future_fixtures": 100, "pending_model_predictions": 80})
    assert cov["coverage"] == 0.8
    print(f"  [OK] §6 : 80/100 -> coverage={cov['coverage']}")


def test_capture_coverage_insufficient_when_zero_eligible():
    cov = compute_capture_coverage(0, 0)
    assert cov["status"] == "INSUFFICIENT_DATA" and cov["coverage"] is None
    print(f"  [OK] §7 : eligible=0 -> {cov}")


# ---------------------------------------------------------------------------
# 3. Missed capture (§8)
# ---------------------------------------------------------------------------

def test_missed_capture_categorization():
    with Session(engine) as session:
        future_date = date.today() + timedelta(days=2)
        mp, _ = _seed_pending_prediction(session, "Ligue1", future_date, "Miss A", "Miss B")
        as_of = datetime.now(timezone.utc)
        category = classify_missed_capture_reason(session, mp, as_of, already_captured=False)
    assert category in ("NO_PRODUCTION_PREDICTION", "LATE_PREDICTION", "TEMPORAL_UNKNOWN", "PIPELINE_ERROR", "PROVENANCE_MISSING", "OTHER")
    print(f"  [OK] §8 : catégorie de capture manquée -> {category}")


def test_already_captured_never_flagged_missed():
    assert classify_missed_capture_reason(None, None, None, already_captured=True) is None
    print("  [OK] §8 : déjà capturé -> None (jamais un faux 'missed')")


# ---------------------------------------------------------------------------
# 4. Late / stale prediction (§9/§10/§35)
# ---------------------------------------------------------------------------

def test_late_prediction_requires_known_kickoff_time():
    with Session(engine) as session:
        d = date.today() + timedelta(days=1)
        mp, _ = _seed_pending_prediction(session, "Ligue1", d, "L A", "L B")
        as_of = datetime.now(timezone.utc)
        result = classify_prediction_timing(mp, as_of, kickoff_time_known=False)
    assert result["late_status"] == "KICKOFF_TIME_UNKNOWN"
    print(f"  [OK] §9/§35 : kickoff réel inconnu -> {result['late_status']} (jamais un LATE fabriqué sur un proxy minuit)")


def test_stale_prediction_threshold():
    with Session(engine) as session:
        d = date.today() + timedelta(days=1)
        old_predicted_at = datetime.now(timezone.utc) - timedelta(hours=48)
        mp, _ = _seed_pending_prediction(session, "Ligue1", d, "S A", "S B", predicted_at=old_predicted_at)
        as_of = datetime.now(timezone.utc)
        result = classify_prediction_timing(mp, as_of, stale_threshold_hours=24.0)
    assert result["stale"] is True and result["age_hours"] > 24.0
    print(f"  [OK] §10 : predicted_at vieux de {result['age_hours']}h > seuil OPÉRATIONNEL 24h -> stale={result['stale']}")


# ---------------------------------------------------------------------------
# 5. Temporal unknown / provenance missing (§11/§12)
# ---------------------------------------------------------------------------

def test_temporal_health_counts():
    entries = [(_record("t1", temporal_status="TEMPORALLY_VERIFIED"), pending_resolution()),
               (_record("t2", temporal_status="UNKNOWN"), pending_resolution())]
    health = compute_temporal_health(entries)
    assert health["temporal_safe"] == 1 and health["temporal_unknown"] == 1
    print(f"  [OK] §11 : {health}")


def test_provenance_missing_never_fabricated():
    entries = [(_record("p1", provenance={}), pending_resolution())]
    health = compute_provenance_health(entries)
    assert health["missing"] == 1
    print(f"  [OK] §12 : provenance vide -> {health}")


# ---------------------------------------------------------------------------
# 6. Model / probability / decision consistency (§13/§14/§15)
# ---------------------------------------------------------------------------

def test_model_mismatch_detected():
    with Session(engine) as session:
        future_date = date.today() + timedelta(days=2)
        mp, version = _seed_pending_prediction(session, "Ligue1", future_date, "M A", "M B")
        # record prétend un autre model_type que celui réellement en base pour ce match_id=mp.id
        rec = _record("mm1", match_id=mp.id, model_type="lightgbm", market="1X2",
                       market_probabilities_raw={"home_win": 0.5, "draw": 0.3, "away_win": 0.2})
        health = compute_consistency_health(session, [(rec, pending_resolution())])
    assert health["model_mismatches"] == 1
    print(f"  [OK] §13 : {health}")


def test_probability_mismatch_detected():
    with Session(engine) as session:
        future_date = date.today() + timedelta(days=2)
        mp, version = _seed_pending_prediction(session, "Ligue1", future_date, "P A", "P B")
        rec = _record("pm1", match_id=mp.id, model_type="xgboost", market="1X2",
                       market_probabilities_raw={"home_win": 0.99, "draw": 0.005, "away_win": 0.005})  # ne correspond pas à mp.prob_home=0.5
        health = compute_consistency_health(session, [(rec, pending_resolution())])
    assert health["probability_mismatches"] == 1
    print(f"  [OK] §14 : {health}")


# ---------------------------------------------------------------------------
# 7. Duplicate health / store corruption (§16/§17)
# ---------------------------------------------------------------------------

def test_duplicate_health_ratio():
    health = compute_duplicate_health(attempted=10, created=6)
    assert health["duplicates_prevented"] == 4 and health["duplicate_ratio"] == 0.4
    print(f"  [OK] §16 : {health} (un doublon bloqué n'est pas une erreur)")


def test_store_corruption_detected_never_overwritten():
    store = _temp_store()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ not json", encoding="utf-8")
    health = compute_store_integrity(store)
    assert health["status"] == "CRITICAL" and health["valid_json"] is False
    print(f"  [OK] §17 : store corrompu -> {health}")


# ---------------------------------------------------------------------------
# 8. Resolution latency / played-but-pending / unresolved age (§20/§21/§39/§40)
# ---------------------------------------------------------------------------

def test_resolution_latency_percentiles():
    entries = []
    for i, hours in enumerate([2, 4, 6, 8, 100]):
        kickoff = datetime(2026, 1, 1, tzinfo=UTC)
        resolved_at = kickoff + timedelta(hours=hours)
        entries.append((_record(f"lat{i}", kickoff=kickoff), ShadowResolution(result_status="RESOLVED", resolved_at=resolved_at)))
    health = compute_resolution_health(entries)
    lat = health["resolution_latency_hours"]
    assert lat["status"] == "ok" and lat["min"] == 2 and lat["max"] == 100
    print(f"  [OK] §21 : {lat}")


def test_played_but_pending_excludes_future_matches():
    with Session(engine) as session:
        as_of = datetime.now(timezone.utc)
        past_record = _record("pp1", kickoff=as_of - timedelta(hours=72))
        future_record = _record("pp2", kickoff=as_of + timedelta(hours=72))
        result = find_matches_played_but_pending(session, [(past_record, pending_resolution()), (future_record, pending_resolution())], as_of)
    assert len(result) == 1 and result[0]["shadow_id"] == "pp1"
    assert result[0]["unresolved_age_exceeded"] is True  # 72h > seuil opérationnel 48h
    print(f"  [OK] §39/§40 : {result}")


# ---------------------------------------------------------------------------
# 9. Track record health / invalid probability / value health / feature health (§22/§23/§42/§43/§44/§46/§47)
# ---------------------------------------------------------------------------

def test_track_record_health_reuses_phase7():
    entries = [(_record("tr1"), ShadowResolution(result_status="RESOLVED", actual_outcome="home_win", candidate_correct=True))]
    health = compute_track_record_health(entries)
    assert health["maturity"] in ("NO_DATA", "EARLY_DATA", "TRACKING", "STATISTICALLY_INFORMATIVE")
    print(f"  [OK] §22/§23 : {health['maturity']} (seuils Phase 8M inchangés : {health['maturity_thresholds']})")


def test_invalid_probability_flagged_never_corrected():
    entries = [(_record("ip1", market_probabilities_raw={"home_win": 0.9, "draw": 0.9, "away_win": 0.9}), pending_resolution())]
    invalid = check_invalid_probabilities(entries)
    assert len(invalid) == 1 and invalid[0]["reason"] == "INVALID_PROBABILITY"
    print(f"  [OK] §42 : {invalid}")


def test_value_health_not_available_without_verified_odds():
    entries = [(_record("vh1"), pending_resolution())]
    health = compute_odds_and_value_health(entries)
    assert health["value_tracking"]["status"] == "NOT_AVAILABLE"
    print(f"  [OK] §43/§44 : {health['value_tracking']}")


def test_feature_health_reuses_phase8a_registry():
    entries = [(_record("fh1", quality={"data_quality": "LOW"}), pending_resolution())]
    health = compute_feature_health(entries)
    assert health["feature_registry"]["production"] > 0  # Phase 8A registry réellement peuplé
    assert health["shadow_records_with_low_data_quality"] == 1
    print(f"  [OK] §47 : registry={health['feature_registry']}, low_quality={health['shadow_records_with_low_data_quality']}")


# ---------------------------------------------------------------------------
# 10. Determinism / filters / alert dedup / error isolation (§37/§38/§28)
# ---------------------------------------------------------------------------

def test_deterministic_report_same_input():
    entries = [(_record("d1"), ShadowResolution(result_status="RESOLVED", actual_outcome="home_win", candidate_correct=True))]
    h1 = compute_track_record_health(list(entries))
    h2 = compute_track_record_health(list(reversed(entries)))
    assert h1["maturity"] == h2["maturity"] and h1["per_market"] == h2["per_market"]
    print("  [OK] §37 : ordre d'entrée différent -> même résultat")


def test_alert_deduplication_within_run():
    alerts = [Alert("MISSED_CAPTURE", "ERROR", "same_key", "msg1"), Alert("MISSED_CAPTURE", "ERROR", "same_key", "msg2")]
    deduped = _dedup_alerts(alerts)
    assert len(deduped) == 1
    print(f"  [OK] §28 : 2 détections même clé -> {len(deduped)} alerte (jamais un bruit répété)")


# ---------------------------------------------------------------------------
# 11. Full orchestrator + DB purity (§3/§32/§52)
# ---------------------------------------------------------------------------

def test_compute_shadow_health_end_to_end_synthetic():
    store = _temp_store()
    with Session(engine) as session:
        future_date = date.today() + timedelta(days=2)
        mp, _ = _seed_pending_prediction(session, "Ligue1", future_date, "E2E A", "E2E B")
        as_of = datetime.now(timezone.utc)
        pi, _ = build_pipeline_input_for_live(session, mp, "1X2", "home_win", as_of)
        a = run_pipeline(pi)
        capture_shadow_decision(store, pi, a, home_team=mp.home_team, away_team=mp.away_team)
        store.save()  # compute_shadow_health lit le store PERSISTÉ (processus indépendant en usage réel, §31/§32) — jamais l'état en mémoire non sauvegardé
        health = compute_shadow_health(session, store, as_of)
    assert health["status"] in HEALTH_STATUSES
    assert health["captured"] == 1
    print(f"  [OK] §3 : compute_shadow_health -> status={health['status']}, captured={health['captured']}, alerts={len(health['alerts'])}")


def test_monitoring_never_touches_the_database_or_store_writes():
    store = _temp_store()
    with Session(engine) as session:
        session.add(Match(league="Ligue1", date=datetime.combine(date(2026, 1, 1), datetime.min.time()), home_team="Z1", away_team="Z2", home_goals=1, away_goals=0))
        session.commit()
        before = _row_counts(session)
        health = compute_shadow_health(session, store, datetime.now(timezone.utc))
        after = _row_counts(session)
    assert before == after, f"le monitoring a modifié une table : avant={before} après={after}"
    assert not store.path.exists()  # aucune écriture du store non plus (§31 : strictement lecture seule)
    print(f"  [OK] §32/§52 : compute_shadow_health n'écrit ni en DB ni dans le store ({health['status']})")


def test_error_isolation_missing_league_never_crashes():
    # kickoff=2026-01-01 (passé) mais league/home/away absents -> jamais un crash ; le record reste
    # signalé (kickoff passé + PENDING est un fait vérifiable même sans pouvoir chercher un résultat).
    entries = [(_record("err1", league=None, home_team=None, away_team=None), pending_resolution())]
    with Session(engine) as session:
        result = find_matches_played_but_pending(session, entries, datetime.now(timezone.utc))
    assert len(result) == 1 and result[0]["result_already_available_pending_resolve_run"] is False
    print(f"  [OK] isolation : record sans league/home/away ne fait jamais planter le monitoring -> {result}")


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
