"""
test_live_shadow_track.py — Phase 8M : tests de api/app/ai/shadow/live.py
et des améliorations §40 (écriture atomique, détection de corruption) sur
ShadowDecisionStore.

Base isolée dédiée (jamais api/app.db pour les tests DB-purity) — certains
tests lisent DÉLIBÉRÉMENT api/app.db en lecture seule (§55).

Usage : python api/test_live_shadow_track.py
"""

import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_live_shadow_track.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match, MatchStats
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion, TeamRating, next_version_name

from app.ai.pipeline.orchestrator import run_pipeline
from app.ai.shadow.schemas import pending_resolution, ShadowResolution
from app.ai.shadow.tracking import ShadowDecisionStore, capture_shadow_decision, dedup_key
from app.ai.shadow.resolution import resolve_record
from app.ai.shadow.metrics import compute_shadow_track_record
from app.ai.shadow.live import discover_live_candidates, assess_capture_eligibility, build_pipeline_input_for_live, check_production_consistency

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


def _seed_pending_prediction(session, league, match_date, home, away, model_type="xgboost", predicted_at=None, prob_home=0.5, prob_draw=0.3, prob_away=0.2):
    # Convention RÉELLE de production (voir scripts/train_ml_stacking_from_db.py::persist_model_version) : "xfoot-{model_type}".
    version = ModelVersion(name=next_version_name(session, f"xfoot-{model_type}"), model_type=model_type, trained_at=datetime.now(timezone.utc) - timedelta(days=30), is_active=True)
    session.add(version); session.commit(); session.refresh(version)
    mp = ModelPrediction(
        league=league, match_date=match_date, home_team=home, away_team=away, model_type=model_type,
        model_version_id=version.id, source="live", status="pending",
        # Par défaut, bien AVANT tout as_of de test raisonnable (jamais "now" — évite qu'un as_of de test
        # tombe accidentellement avant predicted_at et déclenche PREDICTION_TIMESTAMP_AFTER_AS_OF pour la
        # mauvaise raison).
        predicted_at=predicted_at or (datetime.now(timezone.utc) - timedelta(days=1)),
        prob_home=prob_home, prob_draw=prob_draw, prob_away=prob_away, pick_1x2="home_win",
    )
    session.add(mp); session.commit(); session.refresh(mp)
    return mp, version


# ---------------------------------------------------------------------------
# 1. Future fixture capture / kickoff cutoff / late capture (§1/§5/§6/§34)
# ---------------------------------------------------------------------------

def test_future_fixture_is_capturable():
    with Session(engine) as session:
        future_date = date.today() + timedelta(days=2)
        mp, _ = _seed_pending_prediction(session, "Ligue1", future_date, "Future A", "Future B")
        as_of = datetime.now(timezone.utc)
        capturable, reason = assess_capture_eligibility(mp, as_of)
    assert capturable is True and reason is None
    print(f"  [OK] §1 : fixture future (kickoff dans 2 jours) capturable à as_of=now")


def test_kickoff_cutoff_respected():
    with Session(engine) as session:
        d = date.today() + timedelta(days=1)
        mp, _ = _seed_pending_prediction(session, "Ligue1", d, "Cut A", "Cut B")
        as_of_before = datetime.combine(d, datetime.min.time(), tzinfo=UTC) - timedelta(hours=6)
        as_of_after = datetime.combine(d, datetime.min.time(), tzinfo=UTC) + timedelta(minutes=1)
        cap_before, _ = assess_capture_eligibility(mp, as_of_before)
        cap_after, reason_after = assess_capture_eligibility(mp, as_of_after)
    assert cap_before is True and cap_after is False and reason_after == "TOO_LATE"
    print(f"  [OK] §6/§34 : as_of avant kickoff -> capturable ; as_of après kickoff -> TOO_LATE")


def test_late_capture_rejection_even_with_existing_production_prediction():
    with Session(engine) as session:
        past_date = date.today() - timedelta(days=5)
        mp, _ = _seed_pending_prediction(session, "Ligue1", past_date, "Late A", "Late B")
        pi, diagnostics = build_pipeline_input_for_live(session, mp, "1X2", "home_win", datetime.now(timezone.utc))
    assert pi is None and diagnostics["reason"] == "TOO_LATE"
    print(f"  [OK] §34 : match déjà passé, prédiction production existante -> {diagnostics['reason']} (jamais capturé)")


def test_prediction_timestamp_leakage_section_35():
    with Session(engine) as session:
        future_date = date.today() + timedelta(days=3)
        future_predicted_at = datetime.now(timezone.utc) + timedelta(days=1)  # prédiction "créée" après as_of
        mp, _ = _seed_pending_prediction(session, "Ligue1", future_date, "Leak A", "Leak B", predicted_at=future_predicted_at)
        pi, diagnostics = build_pipeline_input_for_live(session, mp, "1X2", "home_win", datetime.now(timezone.utc))
    assert pi is None and diagnostics["reason"] == "PREDICTION_TIMESTAMP_AFTER_AS_OF"
    print(f"  [OK] §35 : predicted_at postérieur à as_of -> {diagnostics['reason']} (jamais prétendu disponible)")


# ---------------------------------------------------------------------------
# 2. Consistency checks (§36/§37/§38)
# ---------------------------------------------------------------------------

def test_model_version_and_probability_consistency():
    with Session(engine) as session:
        future_date = date.today() + timedelta(days=2)
        mp, version = _seed_pending_prediction(session, "Ligue1", future_date, "Cons A", "Cons B", prob_home=0.6, prob_draw=0.25, prob_away=0.15)
        pi, diagnostics = build_pipeline_input_for_live(session, mp, "1X2", "home_win", datetime.now(timezone.utc))
        assessment = run_pipeline(pi)
        mismatches = check_production_consistency(mp, pi, assessment)
    assert mismatches == []
    assert pi.probabilities == {"home_win": 0.6, "draw": 0.25, "away_win": 0.15}
    print(f"  [OK] §36/§37/§38 : model_version/probability/decision cohérents avec le snapshot production -> {mismatches}")


def test_probability_mismatch_never_silently_corrected():
    with Session(engine) as session:
        future_date = date.today() + timedelta(days=2)
        mp, _ = _seed_pending_prediction(session, "Ligue1", future_date, "Mism A", "Mism B", prob_home=0.6, prob_draw=0.25, prob_away=0.15)
        pi, _ = build_pipeline_input_for_live(session, mp, "1X2", "home_win", datetime.now(timezone.utc))
        # simule un écart (PipelineInput altéré artificiellement, comme le ferait un bug) :
        import dataclasses
        tampered = dataclasses.replace(pi, probabilities={"home_win": 0.99, "draw": 0.005, "away_win": 0.005})
        mismatches = check_production_consistency(mp, tampered, None)
    assert "PROBABILITY_MISMATCH" in mismatches
    print(f"  [OK] §37 : écart de probabilité détecté, jamais corrigé silencieusement -> {mismatches}")


# ---------------------------------------------------------------------------
# 3. Duplicate prevention / multiple as_of (§15/§16)
# ---------------------------------------------------------------------------

def test_duplicate_prevention_same_key():
    with Session(engine) as session:
        future_date = date.today() + timedelta(days=2)
        mp, _ = _seed_pending_prediction(session, "Ligue1", future_date, "Dup A", "Dup B")
        as_of = datetime.now(timezone.utc)
        store = _temp_store()
        pi1, _ = build_pipeline_input_for_live(session, mp, "1X2", "home_win", as_of)
        a1 = run_pipeline(pi1)
        r1, c1 = capture_shadow_decision(store, pi1, a1, home_team=mp.home_team, away_team=mp.away_team)
        pi2, _ = build_pipeline_input_for_live(session, mp, "1X2", "home_win", as_of)  # même as_of exact
        a2 = run_pipeline(pi2)
        r2, c2 = capture_shadow_decision(store, pi2, a2, home_team=mp.home_team, away_team=mp.away_team)
    assert c1 is True and c2 is False and len(store.all()) == 1
    print("  [OK] §15 : même (match_id, market, model_version, as_of) -> 1 seul record")


def test_multiple_as_of_produce_distinct_captures():
    with Session(engine) as session:
        future_date = date.today() + timedelta(days=2)
        mp, _ = _seed_pending_prediction(session, "Ligue1", future_date, "Multi A", "Multi B")
        store = _temp_store()
        as_of_1 = datetime.now(timezone.utc)
        as_of_2 = as_of_1 + timedelta(hours=6)
        for as_of in (as_of_1, as_of_2):
            pi, _ = build_pipeline_input_for_live(session, mp, "1X2", "home_win", as_of)
            a = run_pipeline(pi)
            capture_shadow_decision(store, pi, a, home_team=mp.home_team, away_team=mp.away_team)
    assert len(store.all()) == 2
    print("  [OK] §16 : deux as_of différents pour le même match -> deux captures distinctes (jamais dédupliqué abusivement)")


# ---------------------------------------------------------------------------
# 4. Resolution / conflict / pending exclusion / track record / filters (réutilisés de 8K, revérifiés ici)
# ---------------------------------------------------------------------------

def test_resolution_reused_from_phase8k():
    with Session(engine) as session:
        league, match_date, home, away = "Ligue1", date(2020, 1, 1), "Res A", "Res B"
        session.add(Match(league=league, date=datetime.combine(match_date, datetime.min.time()), home_team=home, away_team=away, home_goals=2, away_goals=0))
        session.commit()
        from app.ai.shadow.schemas import ShadowDecisionRecord
        record = ShadowDecisionRecord(
            shadow_id="live-k1", match_id=None, league=league, home_team=home, away_team=away,
            kickoff=datetime(2020, 1, 1, tzinfo=UTC), as_of=datetime(2020, 1, 1, tzinfo=UTC),
            model_type="xgboost", model_version=None, calibration_source=None, market="1X2", selection="home_win",
            raw_probability=0.6, calibrated_probability=None, market_probabilities_raw={"home_win": 0.6, "draw": 0.25, "away_win": 0.15},
            market_probabilities_calibrated=None, probability_source="RAW", quality={}, confidence="UNKNOWN",
            eligibility="ELIGIBLE", value_status=None, odds_source=None, odds_timestamp=None, temporal_status="TEMPORALLY_VERIFIED",
            provenance={}, status="NOT_AVAILABLE", created_at=datetime.now(timezone.utc),
        )
        resolution = resolve_record(session, record, pending_resolution())
    assert resolution.result_status == "RESOLVED" and resolution.candidate_correct is True
    print(f"  [OK] résolution réutilisée telle quelle de Phase 8K -> {resolution.result_status}")


def test_pending_excluded_and_track_record():
    store = _temp_store()
    with Session(engine) as session:
        future_date = date.today() + timedelta(days=2)
        mp, _ = _seed_pending_prediction(session, "Ligue1", future_date, "TR A", "TR B", prob_home=0.6, prob_draw=0.2, prob_away=0.2)
        pi, _ = build_pipeline_input_for_live(session, mp, "1X2", "home_win", datetime.now(timezone.utc))
        a = run_pipeline(pi)
        record, _ = capture_shadow_decision(store, pi, a, home_team=mp.home_team, away_team=mp.away_team)
    tr = compute_shadow_track_record(store.all(), market="1X2")
    assert tr["status"] == "INSUFFICIENT_DATA" and tr.get("pending", 0) == 1  # aucun resolved -> exclu
    print(f"  [OK] §25 : record PENDING exclu du track record -> {tr['status']} (pending={tr.get('pending')})")


def test_league_market_model_window_filters_reused():
    store = _temp_store()
    with Session(engine) as session:
        d = date.today() + timedelta(days=2)
        for i, (league, market, sel, model) in enumerate([("Ligue1", "1X2", "home_win", "xgboost"), ("PremierLeague", "BTTS", "yes", "lightgbm")]):
            mp, _ = _seed_pending_prediction(session, league, d + timedelta(days=i), f"F{i}A", f"F{i}B", model_type=model,
                                              prob_home=0.5 if market == "1X2" else 0.5, prob_draw=0.3, prob_away=0.2)
            if market == "BTTS":
                mp.prob_btts_yes, mp.prob_btts_no = 0.55, 0.45
                session.add(mp); session.commit()
            pi, _ = build_pipeline_input_for_live(session, mp, market, sel, datetime.now(timezone.utc))
            a = run_pipeline(pi)
            capture_shadow_decision(store, pi, a, home_team=mp.home_team, away_team=mp.away_team)
    entries = store.all()
    tr_ligue1 = compute_shadow_track_record(entries, market="1X2", league="Ligue1")
    tr_btts = compute_shadow_track_record(entries, market="BTTS")
    assert tr_ligue1["market"] == "1X2" and tr_btts["market"] == "BTTS"
    print(f"  [OK] §47 : filtres league/market réutilisés de Phase 7/8K sans modification")


# ---------------------------------------------------------------------------
# 5. No odds / temporal unknown (§11)
# ---------------------------------------------------------------------------

def test_no_odds_stays_not_available():
    with Session(engine) as session:
        future_date = date.today() + timedelta(days=2)
        mp, _ = _seed_pending_prediction(session, "Ligue1", future_date, "Odds A", "Odds B")
        pi, _ = build_pipeline_input_for_live(session, mp, "1X2", "home_win", datetime.now(timezone.utc))
    assert pi.odds_input is None
    a = run_pipeline(pi)
    assert a.value is None
    print("  [OK] §11 : aucune odds fabriquée, value jamais calculé sans odds réelles")


def test_temporal_unknown_never_verified_in_live_capture():
    # Le snapshot live n'a pas d'odds -> temporal_quality concerne ici la donnée modèle elle-même ;
    # vérifie que sans odds, aucune promesse TEMPORALLY_VERIFIED n'est faite sur la partie odds/value.
    with Session(engine) as session:
        future_date = date.today() + timedelta(days=2)
        mp, _ = _seed_pending_prediction(session, "Ligue1", future_date, "TU A", "TU B")
        pi, _ = build_pipeline_input_for_live(session, mp, "1X2", "home_win", datetime.now(timezone.utc))
        a = run_pipeline(pi)
    assert a.value_stage_status == "SKIPPED_NO_ODDS"
    print(f"  [OK] §11 : value_stage_status={a.value_stage_status} — jamais TEMPORALLY_VERIFIED sans preuve")


# ---------------------------------------------------------------------------
# 6. Error isolation / determinism (§31/§53)
# ---------------------------------------------------------------------------

def test_error_isolation_reused_from_phase8j():
    from app.ai.pipeline.shadow import run_shadow_batch
    from app.ai.pipeline.schemas import PipelineInput, CalibrationInput, FeatureSnapshotInput, TemporalMetadataInput
    good = PipelineInput(match_id=1, league="Ligue1", kickoff=datetime.now(timezone.utc) + timedelta(days=1), as_of=datetime.now(timezone.utc),
                          model="xgboost", model_version="v1", market="1X2", selection="home_win", probabilities={"home_win": 0.5, "draw": 0.3, "away_win": 0.2},
                          calibration=CalibrationInput(source="RAW"), feature_snapshot=FeatureSnapshotInput(coverage={"coverage_ratio": 0.9}),
                          temporal_metadata=TemporalMetadataInput())
    bad = PipelineInput(match_id=2, league="Ligue1", kickoff=None, as_of=None, model="xgboost", model_version="v1", market="1X2",
                         selection="home_win", probabilities={"home_win": 0.5, "draw": 0.3, "away_win": 0.2},
                         calibration=CalibrationInput(source="RAW"), feature_snapshot=FeatureSnapshotInput(), temporal_metadata=TemporalMetadataInput())
    results = run_shadow_batch([good, bad])
    assert results[0].error is None and results[1].error is not None
    print("  [OK] §31 : une fixture défaillante n'interrompt pas les autres (réutilise run_shadow_batch, Phase 8J)")


def test_determinism_live_capture():
    with Session(engine) as session:
        future_date = date.today() + timedelta(days=2)
        mp, _ = _seed_pending_prediction(session, "Ligue1", future_date, "Det A", "Det B")
        as_of = datetime.now(timezone.utc)
        pi1, _ = build_pipeline_input_for_live(session, mp, "1X2", "home_win", as_of)
        pi2, _ = build_pipeline_input_for_live(session, mp, "1X2", "home_win", as_of)
    a1, a2 = run_pipeline(pi1), run_pipeline(pi2)
    assert a1.final_status == a2.final_status and a1.decision.eligibility == a2.decision.eligibility
    print("  [OK] §53 : même snapshot -> même résultat (déterminisme)")


# ---------------------------------------------------------------------------
# 7. JSON atomic write / corrupted store safety (§40)
# ---------------------------------------------------------------------------

def test_json_store_atomic_write_leaves_no_partial_file():
    store = _temp_store()
    store.load()
    store._data = {"k": {"record": {"a": 1}, "resolution": {"result_status": "PENDING"}}}
    store.save()
    assert store.path.exists()
    # aucun fichier temporaire résiduel dans le même répertoire
    leftovers = list(store.path.parent.glob(".shadow_store_*.tmp"))
    assert leftovers == []
    print("  [OK] §40 : écriture atomique — aucun fichier temporaire résiduel après succès")


def test_corrupted_store_stops_never_overwrites_silently():
    store = _temp_store()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not valid json!!", encoding="utf-8")
    try:
        store.load()
        raise AssertionError("devait lever ValueError sur un store corrompu")
    except ValueError as e:
        assert "corrompu" in str(e)
    print("  [OK] §40 : store JSON corrompu -> ValueError explicite, STOP, jamais écrasé silencieusement")


# ---------------------------------------------------------------------------
# 8. DB purity (§42)
# ---------------------------------------------------------------------------

def test_live_capture_never_touches_production_tables():
    with Session(engine) as session:
        future_date = date.today() + timedelta(days=2)
        mp, _ = _seed_pending_prediction(session, "Ligue1", future_date, "Pure A", "Pure B")
        session.commit()
        before = _row_counts(session)

        store = _temp_store()
        candidates = discover_live_candidates(session, datetime.now(timezone.utc))
        for c in candidates:
            pi, _ = build_pipeline_input_for_live(session, c, "1X2", "home_win", datetime.now(timezone.utc))
            if pi is not None:
                a = run_pipeline(pi)
                capture_shadow_decision(store, pi, a, home_team=c.home_team, away_team=c.away_team)
        resolve_record(session, list(store.all())[0][0], list(store.all())[0][1]) if store.all() else None

        after = _row_counts(session)
    assert before == after, f"la capture live a modifié une table : avant={before} après={after}"
    print("  [OK] §42 : découverte + capture + tentative de résolution -> aucune table production modifiée")


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
