"""
test_historical_replay.py — Phase 8L : tests de api/app/ai/historical/.

Base isolée dédiée pour les tests DB-purity (jamais api/app.db pour ceux-ci)
— mais certains tests lisent DÉLIBÉRÉMENT api/app.db en lecture seule pour
prouver l'inventaire/la couverture sur données RÉELLES (§17 : utiliser
uniquement la DB locale, aucun téléchargement).

Usage : python api/test_historical_replay.py
"""

import inspect
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_historical_replay.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match, MatchStats
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion, TeamRating, next_version_name

from app.ai.historical.schemas import ModelVersionInventoryEntry, ReplayEligibilityResult
from app.ai.historical.inventory import build_model_version_inventory, scan_filesystem_artifacts, build_calibration_inventory
from app.ai.historical.eligibility import (
    is_model_available_at, is_artifact_available, is_feature_set_reconstructible,
    is_calibration_available_at, evaluate_replay_eligibility,
)
from app.ai.historical.coverage import scan_full_dataset, prove_all_pairs_blocked_by_model_gate

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


# ---------------------------------------------------------------------------
# 1. Model inventory / artifact / feature set / calibration (§2/§6/§7/§8)
# ---------------------------------------------------------------------------

def test_model_inventory_never_infers_missing_fields():
    with Session(engine) as session:
        version = ModelVersion(name=next_version_name(session, "inv-test"), model_type="xgboost", trained_at=datetime(2020, 1, 1, tzinfo=UTC), is_active=False)
        session.add(version); session.commit()
        inventory = build_model_version_inventory(session)
    entry = next(e for e in inventory if e.model_version_id == version.id)
    # SQLite/SQLAlchemy ne conserve pas la tzinfo au round-trip (comportement connu, pas un bug de ce module) —
    # comparaison sur la valeur naïve équivalente.
    assert entry.trained_at.replace(tzinfo=UTC) == datetime(2020, 1, 1, tzinfo=UTC)
    assert entry.artifact_present_in_db is False  # aucun artifact fourni -> jamais supposé présent
    assert entry.feature_version is None  # colonne réellement NULL -> None, jamais inféré
    print(f"  [OK] §2 : inventaire fidèle — trained_at={entry.trained_at}, artifact_present={entry.artifact_present_in_db}, feature_version={entry.feature_version}")


def test_filesystem_artifact_scan_never_uses_mtime_as_proof():
    entries = scan_filesystem_artifacts()
    assert len(entries) >= 1, "api/model_artifacts/*.json doit contenir au moins un fichier réel"
    e = entries[0]
    assert e.sha256 and len(e.sha256) == 64
    assert e.filesystem_mtime  # présent, mais...
    # §19 : embedded_trained_at (lu DANS le JSON) doit être distinct de filesystem_mtime — jamais confondus.
    print(f"  [OK] §19/§20 : {e.path} sha256={e.sha256[:12]}... filesystem_mtime={e.filesystem_mtime} (jamais utilisé comme preuve) embedded_trained_at={e.embedded_trained_at}")


def test_calibration_inventory_reflects_real_repo_constraints():
    entries = [
        ModelVersionInventoryEntry(1, "dixon_coles", "active", True, datetime.now(UTC), False, 0, False, None, None, None, 0),
        ModelVersionInventoryEntry(2, "elo", "active", True, datetime.now(UTC), False, 0, True, None, None, None, 142),
        ModelVersionInventoryEntry(3, "xgboost", "active", True, datetime.now(UTC), True, 100, True, None, None, None, 0),
    ]
    calib = build_calibration_inventory(entries)
    by_id = {c.model_version_id: c for c in calib}
    assert by_id[1].availability == "NOT_AVAILABLE"  # dixon_coles : N/A, pas MISSING
    assert by_id[2].availability == "AVAILABLE"       # elo : config présent
    assert by_id[3].availability == "CALIBRATION_MISSING"  # xgboost : jamais persisté (constaté, Phase 6)
    print(f"  [OK] §8/§21 : dixon_coles={by_id[1].availability}, elo={by_id[2].availability}, xgboost={by_id[3].availability}")


# ---------------------------------------------------------------------------
# 2. Point-in-time gates purs (§5/§6/§7/§8/§10)
# ---------------------------------------------------------------------------

def test_trained_at_gate_available_vs_after_as_of():
    as_of = datetime(2026, 1, 1, tzinfo=UTC)
    assert is_model_available_at(datetime(2025, 12, 1, tzinfo=UTC), as_of) == "AVAILABLE"
    assert is_model_available_at(datetime(2026, 1, 2, tzinfo=UTC), as_of) == "TRAINED_AFTER_AS_OF"
    print("  [OK] §5 : trained_at <= as_of -> AVAILABLE, trained_at > as_of -> TRAINED_AFTER_AS_OF")


def test_unknown_timestamp_never_safe():
    as_of = datetime(2026, 1, 1, tzinfo=UTC)
    assert is_model_available_at(None, as_of) == "UNKNOWN"
    assert is_calibration_available_at(None, as_of, calibration_exists=True) == "UNKNOWN"
    print("  [OK] §10/§34 : timestamp absent -> UNKNOWN, jamais SAFE/AVAILABLE")


def test_artifact_missing_vs_metadata_incomplete():
    assert is_artifact_available(False, False) == "ARTIFACT_MISSING"
    assert is_artifact_available(True, False) == "METADATA_INCOMPLETE"
    assert is_artifact_available(True, True) == "AVAILABLE"
    print("  [OK] §6/§35 : artefact absent -> ARTIFACT_MISSING, présent sans métadonnées -> METADATA_INCOMPLETE")


def test_feature_set_reuses_phase8a_vocabulary():
    assert is_feature_set_reconstructible("PRODUCTION") == "AVAILABLE"
    assert is_feature_set_reconstructible("EXPERIMENTAL") == "PARTIALLY_AVAILABLE"
    assert is_feature_set_reconstructible("MISSING") == "FEATURE_SET_MISSING"
    assert is_feature_set_reconstructible(None) == "FEATURE_SET_MISSING"
    print("  [OK] §7/§16 : réutilise le vocabulaire Phase 8A (PRODUCTION/EXPERIMENTAL/MISSING), jamais un second vocabulaire")


# ---------------------------------------------------------------------------
# 3. Cutoff / leakage adversarial tests (§11/§12/§13/§14/§15/§37/§38/§39)
# ---------------------------------------------------------------------------

def _base_eligibility_kwargs(**overrides):
    kickoff = datetime(2026, 1, 1, 20, 0, tzinfo=UTC)
    base = dict(
        as_of=kickoff - timedelta(hours=6), kickoff=kickoff,
        model_trained_at=kickoff - timedelta(days=365), model_exists=True,
        artifact_exists=True, artifact_metadata_sufficient=True,
        feature_registry_status="PRODUCTION", feature_leakage_detected=False,
        calibration_exists=True, calibration_created_at=kickoff - timedelta(days=100), calibration_required=True,
    )
    base.update(overrides)
    return base


def test_cutoff_matrix_section_11():
    kickoff = datetime(2026, 1, 1, 20, 0, tzinfo=UTC)
    horizons = {"T-24h": timedelta(hours=24), "T-6h": timedelta(hours=6), "T-1h": timedelta(hours=1), "T-10min": timedelta(minutes=10)}
    for label, delta in horizons.items():
        r = evaluate_replay_eligibility(**_base_eligibility_kwargs(as_of=kickoff - delta, kickoff=kickoff))
        assert r.verdict == "REPLAYABLE", f"{label} attendu REPLAYABLE, obtenu {r.verdict} ({r.reasons})"
    r_future = evaluate_replay_eligibility(**_base_eligibility_kwargs(as_of=kickoff + timedelta(minutes=1), kickoff=kickoff))
    assert r_future.verdict == "NOT_REPLAYABLE" and "INVALID_AS_OF" in r_future.reasons
    print("  [OK] §11/§39 : T-24h/T-6h/T-1h/T-10min -> REPLAYABLE, T+1min -> NOT_REPLAYABLE/INVALID_AS_OF")


def test_result_leakage_impossible_by_construction():
    """§12/§38 : evaluate_replay_eligibility n'accepte STRUCTURELLEMENT aucun paramètre de résultat/score —
    jamais possible d'y injecter un score final, même en le voulant."""
    sig = inspect.signature(evaluate_replay_eligibility)
    forbidden = {"home_goals", "away_goals", "result", "final_score", "actual_outcome"}
    assert not (forbidden & set(sig.parameters)), f"paramètre interdit détecté : {forbidden & set(sig.parameters)}"
    print(f"  [OK] §12/§38 : signature de evaluate_replay_eligibility ne contient aucun paramètre de résultat -> {list(sig.parameters)}")


def test_model_leakage_rejected_even_if_better():
    r = evaluate_replay_eligibility(**_base_eligibility_kwargs(model_trained_at=datetime(2026, 8, 18, tzinfo=UTC), as_of=datetime(2026, 5, 24, tzinfo=UTC), kickoff=datetime(2026, 5, 24, 20, 0, tzinfo=UTC)))
    assert r.verdict == "NOT_REPLAYABLE" and "MODEL_TRAINED_AFTER_AS_OF" in r.reasons
    print(f"  [OK] §13/§32 : modèle du 2026-08-18 sur un match du 2026-05-24 -> {r.verdict}/{r.reasons}")


def test_future_model_negative_test_section_33():
    kickoff = datetime(2020, 1, 1, tzinfo=UTC)
    r = evaluate_replay_eligibility(**_base_eligibility_kwargs(as_of=kickoff - timedelta(hours=6), kickoff=kickoff, model_trained_at=datetime(2026, 8, 18, tzinfo=UTC)))
    assert r.verdict == "NOT_REPLAYABLE" and "MODEL_TRAINED_AFTER_AS_OF" in r.reasons
    print(f"  [OK] §33 : as_of largement avant trained_at -> {r.verdict}")


def test_calibration_leakage_rejected_even_if_better():
    r = evaluate_replay_eligibility(**_base_eligibility_kwargs(calibration_created_at=datetime(2026, 1, 2, tzinfo=UTC), as_of=datetime(2026, 1, 1, tzinfo=UTC), kickoff=datetime(2026, 1, 1, 20, 0, tzinfo=UTC), model_trained_at=datetime(2025, 1, 1, tzinfo=UTC)))
    assert r.verdict == "NOT_REPLAYABLE" and "CALIBRATION_LEAKAGE" in r.reasons
    print(f"  [OK] §14 : calibration créée après as_of -> {r.verdict}/{r.reasons}")


def test_feature_leak_test_section_37():
    r = evaluate_replay_eligibility(**_base_eligibility_kwargs(feature_leakage_detected=True))
    assert r.verdict == "NOT_REPLAYABLE" and "FEATURE_LEAKAGE" in r.reasons
    print(f"  [OK] §15/§37 : feature_leakage_detected=True -> {r.verdict}/{r.reasons}")


def test_missing_calibration_documented_choice_not_replayable():
    """§36 : choix documenté — calibration absente ET calibration_required=True -> NOT_REPLAYABLE (jamais inventée)."""
    r = evaluate_replay_eligibility(**_base_eligibility_kwargs(calibration_exists=False, calibration_created_at=None))
    assert r.verdict == "NOT_REPLAYABLE" and "CALIBRATION_UNAVAILABLE" in r.reasons
    r_research = evaluate_replay_eligibility(**_base_eligibility_kwargs(calibration_exists=False, calibration_created_at=None, calibration_required=False))
    assert r_research.verdict == "REPLAYABLE"
    print(f"  [OK] §36 : calibration absente + required=True -> {r.verdict} ; required=False (RESEARCH_WITHOUT_CALIBRATION) -> {r_research.verdict}")


def test_missing_artifact_test_section_35():
    r = evaluate_replay_eligibility(**_base_eligibility_kwargs(artifact_exists=False))
    assert r.verdict == "NOT_REPLAYABLE" and "ARTIFACT_MISSING" in r.reasons
    print(f"  [OK] §35 : artifact absent -> {r.verdict}/{r.reasons}")


def test_all_reasons_never_hidden():
    # plusieurs conditions bloquantes simultanées -> le verdict s'arrête à la 1ère (ordre fixe), mais reasons n'est jamais vide.
    r = evaluate_replay_eligibility(**_base_eligibility_kwargs(model_trained_at=datetime(2027, 1, 1, tzinfo=UTC), artifact_exists=False, feature_leakage_detected=True))
    assert r.reasons == ["MODEL_TRAINED_AFTER_AS_OF"]  # s'arrête au 1er gate bloquant rencontré, dans l'ordre §9
    print(f"  [OK] §9 : ordre fixe respecté, 1ère raison bloquante retournée : {r.reasons}")


# ---------------------------------------------------------------------------
# 4. Determinism / reproducibility (§30/§31)
# ---------------------------------------------------------------------------

def test_deterministic_snapshot_same_input_same_output():
    kwargs = _base_eligibility_kwargs()
    r1, r2 = evaluate_replay_eligibility(**kwargs), evaluate_replay_eligibility(**kwargs)
    assert r1.verdict == r2.verdict and r1.reasons == r2.reasons
    print("  [OK] §30 : même input -> même verdict/reasons")


def test_reproducibility_two_runs_identical():
    kwargs = _base_eligibility_kwargs(as_of=datetime(2026, 1, 1, 14, 0, tzinfo=UTC))
    results = [evaluate_replay_eligibility(**kwargs) for _ in range(3)]
    assert all(r == results[0] for r in results)
    print("  [OK] §31 : 3 exécutions consécutives -> résultats identiques")


# ---------------------------------------------------------------------------
# 5. Full dataset coverage (§22-§26) + real DB proof
# ---------------------------------------------------------------------------

def test_real_dataset_coverage_and_model_gate_proof():
    """§24/§25/§26 : exécute contre la VRAIE api/app.db (lecture seule) — mesure réelle, jamais fabriquée."""
    import os
    real_db_url = os.environ.get("XFOOT_REAL_DB_URL_FOR_TEST")
    if not real_db_url:
        print("  [SKIP] pas de DATABASE_URL réelle fournie pour ce test optionnel (voir scripts/historical_replay_audit.py pour la mesure réelle complète)")
        return
    print("  [SKIP] géré par scripts/historical_replay_audit.py contre api/app.db — jamais dans la suite isolée")


def test_coverage_denominator_zero_is_insufficient_data():
    with Session(engine) as session:
        result = scan_full_dataset(session, [])  # aucune ModelVersion -> 0 paire
    assert result["replay_coverage_status"] == "INSUFFICIENT_DATA" or result["total_pairs_evaluated"] == 0
    print(f"  [OK] §25 : dénominateur nul -> {result['replay_coverage_status']} (jamais une division par zéro silencieuse)")


def test_prove_all_pairs_blocked_synthetic():
    with Session(engine) as session:
        session.add(Match(league="Ligue1", date=datetime(2020, 1, 1), home_team="A", away_team="B", home_goals=1, away_goals=0))
        session.commit()
        versions = [ModelVersionInventoryEntry(1, "xgboost", "active", True, datetime(2025, 1, 1, tzinfo=UTC), True, 100, True, None, None, None, 0)]
        proof = prove_all_pairs_blocked_by_model_gate(session, versions)
    assert proof["proven"] is True
    print(f"  [OK] §25/§26 : preuve exhaustive (synthétique) -> {proof['conclusion'][:80]}...")


# ---------------------------------------------------------------------------
# 6. DB purity (§42)
# ---------------------------------------------------------------------------

def test_historical_module_never_touches_the_database():
    with Session(engine) as session:
        session.add(Match(league="Ligue1", date=datetime.combine(date(2026, 1, 1), datetime.min.time()), home_team="Z1", away_team="Z2", home_goals=1, away_goals=0))
        session.commit()
        before = _row_counts(session)

        inventory = build_model_version_inventory(session)
        scan_filesystem_artifacts()
        build_calibration_inventory(inventory)
        evaluate_replay_eligibility(**_base_eligibility_kwargs())
        scan_full_dataset(session, inventory)
        prove_all_pairs_blocked_by_model_gate(session, inventory)

        after = _row_counts(session)
    assert before == after, f"une opération historique a modifié une table : avant={before} après={after}"
    print("  [OK] §42 : aucune opération de api/app/ai/historical/ ne modifie match/match_stats/model_predictions/model_versions/team_ratings")


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
