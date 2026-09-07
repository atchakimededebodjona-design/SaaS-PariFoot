"""
test_phase13.py — Phase 13 : tests de la SEULE fonction nouvelle de cette
phase (compute_observation_delta, dans scripts/phase13_evidence_maturation.py)
et vérification structurelle du script.

§27 du prompt Phase 13 : "Ne pas recopier les 100 assertions Phase 11, les
86 Phase 9.5, les 42 Phase 12, etc." — DISCOVER/CAPTURE/VERIFY/RESOLVE/
TRACK/MONITOR/READINESS/MODE_2 evaluation/evidence history/blocker
evolution/rollback/kill switch sont exhaustivement couverts par
api/test_phase9_5.py, api/test_phase10.py, api/test_phase11.py,
api/test_phase12.py — tous re-exécutés en régression réelle par ce script
(§28). Phase 13 n'introduit AUCUNE nouvelle logique de capture/résolution/
classification temporelle : compute_observation_delta() ne fait que
soustraire des comptes DÉJÀ produits par compute_full_evidence_ledger
(Phase 9.3, inchangé).

Ce fichier teste UNIQUEMENT :
  - compute_observation_delta() (Phase 13, nouveau) : delta correctness,
    no-new-evidence, aucune fabrication sur ensembles vides/absents.
  - la réutilisation de compare_to_phase10_baseline (Phase 11) et
    compute_blocker_evolution (Phase 9.5) inchangés.
  - sécurité structurelle du nouveau script (mode/réseau/training/
    promotion/scheduler/frontend/DB/rollback production).

Usage : python api/test_phase13.py
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_phase13.db")

from app.ai.shadow.internal_operation import OPERATING_MODE, assert_mode_1_only, compare_to_phase10_baseline
from app.ai.shadow.watch import compute_blocker_evolution

import phase13_evidence_maturation as p13
from phase13_evidence_maturation import compute_observation_delta, EXPECTED_CHECKPOINT

SCRIPT_SOURCE = inspect.getsource(p13)

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


def _ledger(**overrides):
    base = {
        "total_observations": 0, "real_prospective_resolved_count": 0, "resolved": 0, "provenance_complete": 0,
        "distinct_model_versions": [], "distinct_markets": [], "distinct_leagues": [],
        "maturity_real_prospective_resolved": "NO_DATA",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. delta correctness — identical ledgers.
# ---------------------------------------------------------------------------

def test_delta_correctness_identical():
    section("1. delta correctness (identical ledgers -> all zero, NO_NEW_EVIDENCE)")
    baseline = _ledger(total_observations=5, real_prospective_resolved_count=2, resolved=2, provenance_complete=1,
                        distinct_model_versions=["v1"], distinct_markets=["1X2"], distinct_leagues=["Ligue1"])
    current = _ledger(**baseline)
    delta = compute_observation_delta(baseline, current)
    check("NEW_OBSERVATIONS == 0", delta["NEW_OBSERVATIONS"] == 0)
    check("NEW_REAL_PROSPECTIVE == 0", delta["NEW_REAL_PROSPECTIVE"] == 0)
    check("NEW_RESOLVED == 0", delta["NEW_RESOLVED"] == 0)
    check("NEW_MODEL_VERSIONS empty", delta["NEW_MODEL_VERSIONS"] == [])
    check("NO_NEW_EVIDENCE True", delta["NO_NEW_EVIDENCE"] is True)


# ---------------------------------------------------------------------------
# 2. delta correctness — real growth.
# ---------------------------------------------------------------------------

def test_delta_correctness_growth():
    section("2. delta correctness (real growth correctly detected)")
    baseline = _ledger(total_observations=5, real_prospective_resolved_count=2, resolved=2, provenance_complete=1,
                        distinct_model_versions=["v1"], distinct_markets=["1X2"], distinct_leagues=["Ligue1"])
    current = _ledger(total_observations=8, real_prospective_resolved_count=4, resolved=4, provenance_complete=3,
                       distinct_model_versions=["v1", "v2"], distinct_markets=["1X2", "BTTS"], distinct_leagues=["Ligue1", "PremierLeague"])
    delta = compute_observation_delta(baseline, current)
    check("NEW_OBSERVATIONS == 3", delta["NEW_OBSERVATIONS"] == 3)
    check("NEW_REAL_PROSPECTIVE == 2", delta["NEW_REAL_PROSPECTIVE"] == 2)
    check("NEW_RESOLVED == 2", delta["NEW_RESOLVED"] == 2)
    check("NEW_PROVENANCE_COMPLETE == 2", delta["NEW_PROVENANCE_COMPLETE"] == 2)
    check("NEW_MODEL_VERSIONS == ['v2']", delta["NEW_MODEL_VERSIONS"] == ["v2"])
    check("NEW_MARKETS == ['BTTS']", delta["NEW_MARKETS"] == ["BTTS"])
    check("NEW_LEAGUES == ['PremierLeague']", delta["NEW_LEAGUES"] == ["PremierLeague"])
    check("NO_NEW_EVIDENCE False", delta["NO_NEW_EVIDENCE"] is False)


# ---------------------------------------------------------------------------
# 3. no-new-evidence (empty baseline and current — the real, current state).
# ---------------------------------------------------------------------------

def test_no_new_evidence_empty():
    section("3. no-new-evidence (both empty -> honest NO_DATA-consistent delta, never fabricated)")
    delta = compute_observation_delta(_ledger(), _ledger())
    check("all counts zero", all(delta[k] == 0 for k in ("NEW_OBSERVATIONS", "NEW_REAL_PROSPECTIVE", "NEW_RESOLVED", "NEW_PROVENANCE_COMPLETE")))
    check("NO_NEW_EVIDENCE True", delta["NO_NEW_EVIDENCE"] is True)


# ---------------------------------------------------------------------------
# 4. baseline integrity (missing/empty baseline never fabricated into fake growth).
# ---------------------------------------------------------------------------

def test_baseline_integrity_missing_keys():
    section("4. baseline integrity (missing keys in baseline dict never treated as negative infinity / crash)")
    delta = compute_observation_delta({}, _ledger(total_observations=1))
    check("missing baseline keys treated as 0, not a crash", delta["NEW_OBSERVATIONS"] == 1)
    check("missing distinct_* sets treated as empty, not a crash", isinstance(delta["NEW_MODEL_VERSIONS"], list))


# ---------------------------------------------------------------------------
# 5. real prospective classification (reused, never reclassified).
# ---------------------------------------------------------------------------

def test_real_prospective_classification_never_reclassified():
    section("5. real prospective classification (delta never reclassifies HISTORICAL/SYNTHETIC as REAL_PROSPECTIVE)")
    check("compute_observation_delta only reads real_prospective_resolved_count, never recomputes classify_data_marking",
          "classify_data_marking" not in inspect.getsource(compute_observation_delta))


# ---------------------------------------------------------------------------
# 6. no historical promotion (structural — delta never touches HISTORICAL/SYNTHETIC counters as prospective).
# ---------------------------------------------------------------------------

def test_no_historical_promotion():
    section("6. no historical promotion")
    source = inspect.getsource(compute_observation_delta)
    check("delta function never reads 'historical' or 'synthetic' counts as prospective evidence", "historical" not in source.lower() and "synthetic" not in source.lower())


# ---------------------------------------------------------------------------
# 7. blocker evolution (reused from Phase 9.5, unchanged).
# ---------------------------------------------------------------------------

def test_blocker_evolution_reused():
    section("7. blocker evolution (compute_blocker_evolution, Phase 9.5, reused unchanged)")
    result = compute_blocker_evolution([["TRACK_RECORD", "PROVENANCE"]], ["TRACK_RECORD", "MONITORING"])
    check("MONITORING is NEW", "MONITORING" in result["new"])
    check("TRACK_RECORD is PERSISTING", "TRACK_RECORD" in result["persisting"])
    check("PROVENANCE is CLEARED", "PROVENANCE" in result["cleared"])


# ---------------------------------------------------------------------------
# 8. readiness comparison (compare_to_phase10_baseline reused, generic).
# ---------------------------------------------------------------------------

def test_readiness_comparison_reused():
    section("8. readiness comparison (compare_to_phase10_baseline, Phase 11, reused unchanged)")
    result = compare_to_phase10_baseline(
        current_readiness_verdict="NO_GO", baseline_readiness_verdict="BLOCKED", current_real_prospective_count=0,
        baseline_real_prospective_count=0, current_track_record_sample_size=0, baseline_track_record_sample_size=0,
        current_provenance_complete=0, baseline_provenance_complete=0, current_gate_statuses={}, baseline_gate_statuses={},
    )
    check("BLOCKED -> NO_GO is IMPROVED", result["readiness_verdict"]["delta"] == "IMPROVED")


# ---------------------------------------------------------------------------
# 9. deterministic output.
# ---------------------------------------------------------------------------

def test_deterministic_output():
    section("9. deterministic output (pure function)")
    baseline = _ledger(total_observations=3, distinct_markets=["1X2"])
    current = _ledger(total_observations=5, distinct_markets=["1X2", "BTTS"])
    d1 = compute_observation_delta(baseline, current)
    d2 = compute_observation_delta(baseline, current)
    check("identical inputs -> identical delta", d1 == d2)


# ---------------------------------------------------------------------------
# 10. DB purity (structural — script never touches the DB directly).
# ---------------------------------------------------------------------------

def test_db_purity():
    section("10. DB purity (script delegates all DB work to subprocess runners)")
    check("no Session/engine import", "from app.core.database import" not in SCRIPT_SOURCE)
    check("no ShadowDecisionStore import", "ShadowDecisionStore" not in SCRIPT_SOURCE)
    check("no session.add(/session.commit(", "session.add(" not in SCRIPT_SOURCE and "session.commit(" not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 11. production isolation / no activation / no rollback production.
# ---------------------------------------------------------------------------

def test_production_isolation_no_activation():
    section("11. production isolation / no activation / no production rollback")
    check("no apply_promotion(/execute_rollback( call", "apply_promotion(" not in SCRIPT_SOURCE and "execute_rollback(" not in SCRIPT_SOURCE)
    check("no ModelPromotionEvent creation", "ModelPromotionEvent(" not in SCRIPT_SOURCE)
    check("no .trigger(/.reset( on the Kill Switch", ".trigger(" not in SCRIPT_SOURCE and ".reset(" not in SCRIPT_SOURCE)
    check("script never registers a --mode CLI argument", 'add_argument("--mode"' not in SCRIPT_SOURCE)
    check("no .predict( / train_ / .fit( call", all(x not in SCRIPT_SOURCE for x in (".predict(", "train_", ".fit(")))
    check("no network (httpx/requests/urllib)", all(x not in SCRIPT_SOURCE for x in ("import httpx", "import requests", "import urllib")))
    check("no scheduler/frontend import", "arena.scheduler" not in SCRIPT_SOURCE and "import frontend" not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 12. mode enforcement (reused from Phase 11).
# ---------------------------------------------------------------------------

def test_mode_enforcement_reused():
    section("12. mode enforcement (assert_mode_1_only, Phase 11, reused unchanged)")
    check("OPERATING_MODE is MODE_1_SHADOW_ONLY", OPERATING_MODE == "MODE_1_SHADOW_ONLY")
    assert_mode_1_only(OPERATING_MODE)
    check("accepts MODE_1_SHADOW_ONLY", True)
    try:
        assert_mode_1_only("MODE_2_LIMITED_INTERNAL")
        check("MODE_2 rejected", False)
    except ValueError:
        check("MODE_2 rejected", True)


# ---------------------------------------------------------------------------
# 13. git checkpoint verification.
# ---------------------------------------------------------------------------

def test_git_checkpoint_constant():
    section("13. git checkpoint (expected checkpoint constant matches the prompt)")
    check("EXPECTED_CHECKPOINT == '00dd971'", EXPECTED_CHECKPOINT == "00dd971")


# ---------------------------------------------------------------------------
# 14. no duplicate evidence (structural — delta based on counts, never on snapshot volume).
# ---------------------------------------------------------------------------

def test_no_duplicate_evidence():
    section("14. no duplicate evidence (repeated identical read-only observation -> still NO_NEW_EVIDENCE)")
    ledger = _ledger(total_observations=2, real_prospective_resolved_count=1, resolved=1)
    # Simule 3 runs en lecture seule successifs qui n'ajoutent rien au ledger (aucune capture) — le delta
    # comparé à la MÊME baseline doit rester NO_NEW_EVIDENCE, quel que soit le nombre de runs.
    for _ in range(3):
        delta = compute_observation_delta(ledger, ledger)
        check("repeated identical observation never inflates the delta", delta["NO_NEW_EVIDENCE"] is True)


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
