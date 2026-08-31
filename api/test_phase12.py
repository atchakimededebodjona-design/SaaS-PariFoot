"""
test_phase12.py — Phase 12 : tests des SEULES fonctions nouvelles de cette
phase (dans scripts/shadow_observation_period.py — Phase 12 introduit
délibérément AUCUN nouveau module api/app/ai/, §2 du prompt : "minimum
absolu de nouveau code").

§39 du prompt Phase 12 : "Si toutes ces garanties sont déjà couvertes par
les phases précédentes et aucune nouvelle logique n'est introduite : NE PAS
DUPLIQUER INUTILEMENT LES TESTS. Documenter la réutilisation." — en
conséquence, ce fichier NE RÉ-EXÉCUTE PAS les 44 scénarios listés dans le
prompt : ils portent tous sur DISCOVER/CAPTURE/VERIFY/RESOLVE/TRACK/
MONITOR/READINESS/MODE_2 evaluation/evidence history/blocker evolution/
rollback/kill switch — c'est-à-dire EXACTEMENT le périmètre de
scripts/internal_shadow_operation.py (Phase 11), déjà exhaustivement
couvert par api/test_phase11.py (60 scénarios/100 assertions) et re-exécuté
en régression réelle par ce script lui-même (§40). Aucune ligne de capture/
résolution/track record/temporal/provenance/rollback n'est réimplémentée
ici — voir la docstring de scripts/shadow_observation_period.py.

Ce fichier teste UNIQUEMENT :
  - assert_mode_1_only/OPERATING_MODE (Phase 11, réutilisés tels quels —
    re-confirmés dans le contexte Phase 12, jamais réimplémentés).
  - find_latest_report() (Phase 12, nouveau — généralisation triviale de
    find_latest_phase10_report, Phase 11).
  - build_activation_matrix_status() (Phase 12, nouveau — la SEULE logique
    de calcul ajoutée par cette phase : ~5 lignes comparant
    critical_gates_required à readiness_critical_failures).
  - la réutilisation de compare_to_phase10_baseline (Phase 11) avec deux
    baselines différentes (Phase 10 et Phase 11) — AUCUNE nouvelle fonction
    de comparaison introduite.
  - sécurité structurelle du nouveau script (mode/réseau/training/
    promotion/scheduler/frontend/DB).

Usage : python api/test_phase12.py
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_phase12.db")

from app.ai.shadow.internal_operation import OPERATING_MODE, assert_mode_1_only, compare_to_phase10_baseline
from app.ai.shadow.evidence import build_activation_matrix
from app.ai.readiness.schemas import CRITICAL_GATES

import shadow_observation_period as sop

SCRIPT_SOURCE = inspect.getsource(sop)

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
# 1. mode 1 enforcement (reused from Phase 11, re-confirmed here).
# ---------------------------------------------------------------------------

def test_mode_1_enforcement():
    section("1. mode 1 enforcement (reused from Phase 11)")
    check("OPERATING_MODE is MODE_1_SHADOW_ONLY", OPERATING_MODE == "MODE_1_SHADOW_ONLY")
    assert_mode_1_only(OPERATING_MODE)  # ne doit jamais lever.
    check("accepts MODE_1_SHADOW_ONLY", True)
    check("script never registers a --mode CLI argument", 'add_argument("--mode"' not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 2. mode 2/3/4 rejected (reused from Phase 11).
# ---------------------------------------------------------------------------

def test_mode_2_3_4_rejected():
    section("2. mode 2/3/4 rejected (reused from Phase 11)")
    for bad_mode in ("MODE_2_LIMITED_INTERNAL", "MODE_3_LIMITED_PRODUCTION", "MODE_4_FULL_PRODUCTION"):
        try:
            assert_mode_1_only(bad_mode)
            check(f"{bad_mode} rejected", False)
        except ValueError:
            check(f"{bad_mode} rejected", True)


# ---------------------------------------------------------------------------
# 3. no automatic activation (activation matrix always activated=False).
# ---------------------------------------------------------------------------

def test_no_automatic_activation():
    section("3. no automatic activation (activation matrix — activated=False for every mode, always)")
    matrix_all_pass = sop.build_activation_matrix_status([])  # aucun blocker -> conditions_met=True partout
    check("4 modes present", set(matrix_all_pass.keys()) == set(build_activation_matrix().keys()))
    check("activated=False for every mode even with 0 blockers", all(v["activated"] is False for v in matrix_all_pass.values()))
    check("MODE_1 conditions always met (no required gates)", matrix_all_pass["MODE_1_SHADOW_ONLY"]["conditions_met"] is True)


# ---------------------------------------------------------------------------
# 4/5/6/7. find_latest_report.
# ---------------------------------------------------------------------------

def test_find_latest_report_missing_dir():
    section("4. find_latest_report: missing directory -> None, never fabricated")
    result = sop.find_latest_report(Path("/definitely/does/not/exist/xyz"), "xfoot_phaseX")
    check("None on missing directory", result is None)


def test_find_latest_report_empty_dir():
    section("5. find_latest_report: empty directory -> None")
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        result = sop.find_latest_report(Path(d), "xfoot_phaseX")
        check("None on empty directory", result is None)


def test_find_latest_report_picks_latest():
    section("6. find_latest_report: picks the lexicographically latest file (timestamped names sort chronologically)")
    import tempfile
    import json as json_mod
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "xfoot_phaseX_20260101_000000.json").write_text(json_mod.dumps({"run_id": "old"}), encoding="utf-8")
        (d / "xfoot_phaseX_20260831_235959.json").write_text(json_mod.dumps({"run_id": "new"}), encoding="utf-8")
        result = sop.find_latest_report(d, "xfoot_phaseX")
        check("picks the newest by filename", result["run_id"] == "new")


def test_find_latest_report_corrupted():
    section("7. find_latest_report: corrupted JSON -> None, never raises, never fabricated")
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "xfoot_phaseX_20260831_000000.json").write_text("{not valid json", encoding="utf-8")
        result = sop.find_latest_report(d, "xfoot_phaseX")
        check("None on corrupted report, no exception raised", result is None)


# ---------------------------------------------------------------------------
# 8/9/10. build_activation_matrix_status.
# ---------------------------------------------------------------------------

def test_activation_matrix_mode1_always_met():
    section("8. activation matrix: MODE_1 conditions always met (empty prerequisite list)")
    status = sop.build_activation_matrix_status(["TRACK_RECORD", "PROVENANCE", "MONITORING", "ROLLBACK"])
    check("MODE_1 conditions_met True even with 4 real blockers", status["MODE_1_SHADOW_ONLY"]["conditions_met"] is True)
    check("MODE_1 unmet_gates empty", status["MODE_1_SHADOW_ONLY"]["unmet_gates"] == [])


def test_activation_matrix_unmet_gate_detected():
    section("9. activation matrix: an unmet critical gate is detected, never silently passed")
    status = sop.build_activation_matrix_status(["TRACK_RECORD"])
    check("MODE_3 conditions_met False (TRACK_RECORD required)", status["MODE_3_LIMITED_PRODUCTION"]["conditions_met"] is False)
    check("TRACK_RECORD listed in MODE_3 unmet_gates", "TRACK_RECORD" in status["MODE_3_LIMITED_PRODUCTION"]["unmet_gates"])
    check("MODE_2 unaffected (TRACK_RECORD not a MODE_2 prerequisite)", status["MODE_2_LIMITED_INTERNAL"]["conditions_met"] is True)


def test_activation_matrix_mode4_all_gates():
    section("10. activation matrix: MODE_4 'ALL' interpreted as the full CRITICAL_GATES set, never a shortcut")
    status = sop.build_activation_matrix_status([])
    check("MODE_4 required_gates == full CRITICAL_GATES", set(status["MODE_4_FULL_PRODUCTION"]["required_gates"]) == set(CRITICAL_GATES))
    status_blocked = sop.build_activation_matrix_status(["KILL_SWITCH"])
    check("MODE_4 conditions_met False when any CRITICAL_GATES member fails", status_blocked["MODE_4_FULL_PRODUCTION"]["conditions_met"] is False)


# ---------------------------------------------------------------------------
# 11. comparison reuse (compare_to_phase10_baseline reused unchanged, twice).
# ---------------------------------------------------------------------------

def test_comparison_reuse():
    section("11. comparison reuse (compare_to_phase10_baseline, Phase 11, reused unchanged for both baselines)")
    kwargs_common = dict(current_readiness_verdict="NO_GO", current_real_prospective_count=0, current_track_record_sample_size=0,
                          current_provenance_complete=0, current_gate_statuses={"TRACK_RECORD": "FAIL"})
    vs_phase10 = compare_to_phase10_baseline(baseline_readiness_verdict="NO_GO", baseline_real_prospective_count=0,
                                              baseline_track_record_sample_size=0, baseline_provenance_complete=0,
                                              baseline_gate_statuses={"TRACK_RECORD": "FAIL"}, **kwargs_common)
    vs_phase11 = compare_to_phase10_baseline(baseline_readiness_verdict="BLOCKED", baseline_real_prospective_count=0,
                                              baseline_track_record_sample_size=0, baseline_provenance_complete=0,
                                              baseline_gate_statuses={"TRACK_RECORD": "FAIL"}, **kwargs_common)
    check("vs_phase10 well-formed", "readiness_verdict" in vs_phase10 and "critical_gates" in vs_phase10)
    check("vs_phase11 well-formed", "readiness_verdict" in vs_phase11 and "critical_gates" in vs_phase11)
    check("independent baselines produce independent deltas (NO_GO->NO_GO unchanged, BLOCKED->NO_GO improved)",
          vs_phase10["readiness_verdict"]["delta"] == "UNCHANGED" and vs_phase11["readiness_verdict"]["delta"] == "IMPROVED")


# ---------------------------------------------------------------------------
# 12. DB purity / production isolation (structural — script delegates ALL DB work to subprocesses).
# ---------------------------------------------------------------------------

def test_db_purity_and_production_isolation():
    section("12. DB purity / production isolation (script never touches the DB directly)")
    check("no Session/engine import", "from app.core.database import" not in SCRIPT_SOURCE)
    check("no ShadowDecisionStore import (delegated entirely to the Phase 11 runner subprocess)", "ShadowDecisionStore" not in SCRIPT_SOURCE)
    check("no session.add(/session.commit(", "session.add(" not in SCRIPT_SOURCE and "session.commit(" not in SCRIPT_SOURCE)
    check("no apply_promotion(/execute_rollback( call", "apply_promotion(" not in SCRIPT_SOURCE and "execute_rollback(" not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 13. no network.
# ---------------------------------------------------------------------------

def test_no_network():
    section("13. no network")
    check("no httpx/requests/urllib", all(x not in SCRIPT_SOURCE for x in ("import httpx", "import requests", "import urllib")))


# ---------------------------------------------------------------------------
# 14. no training / no promotion.
# ---------------------------------------------------------------------------

def test_no_training_no_promotion():
    section("14. no training / no promotion")
    check("no train_/.fit(", "train_" not in SCRIPT_SOURCE and ".fit(" not in SCRIPT_SOURCE)
    check("no apply_promotion(/deactivate_other_versions( call", "apply_promotion(" not in SCRIPT_SOURCE and "deactivate_other_versions(" not in SCRIPT_SOURCE)
    check("no .predict( call", ".predict(" not in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 15. no scheduler / no frontend modification.
# ---------------------------------------------------------------------------

def test_no_scheduler_no_frontend():
    section("15. no scheduler modification / no frontend modification")
    check("scheduler never imported", "arena.scheduler" not in SCRIPT_SOURCE and "import scheduler" not in SCRIPT_SOURCE)
    check("no frontend import", "import frontend" not in SCRIPT_SOURCE and "from frontend" not in SCRIPT_SOURCE)
    check("no cron/Windows Task creation (§34)", all(x not in SCRIPT_SOURCE for x in ("schtasks", "crontab", "CronCreate")))


# ---------------------------------------------------------------------------
# 16. deterministic output.
# ---------------------------------------------------------------------------

def test_deterministic_output():
    section("16. deterministic output (pure functions)")
    s1 = sop.build_activation_matrix_status(["TRACK_RECORD", "ROLLBACK"])
    s2 = sop.build_activation_matrix_status(["TRACK_RECORD", "ROLLBACK"])
    check("identical inputs -> identical activation matrix status", s1 == s2)
    check("build_activation_matrix itself deterministic (Phase 9.3, reused)", build_activation_matrix() == build_activation_matrix())


# ---------------------------------------------------------------------------
# 17. report generation.
# ---------------------------------------------------------------------------

def test_report_generation():
    section("17. report generation (verdict vocabulary — PRODUCTION_READY never a literal chosen value)")
    check("script never literally assigns PRODUCTION_READY as a final_verdict", '"final_verdict": "PRODUCTION_READY"' not in SCRIPT_SOURCE)
    check("recommended cadence documented", "RECOMMENDED_OBSERVATION_CADENCE" in SCRIPT_SOURCE)
    check("no cron/schtasks call created (§34, documentary cadence only)", all(x not in SCRIPT_SOURCE for x in ("schtasks", "crontab")))
    check("verdict is read from the Phase 11 report, never recomputed with new evidence-classification logic",
          'phase12_current.get("final_verdict"' in SCRIPT_SOURCE)


# ---------------------------------------------------------------------------
# 18. error isolation.
# ---------------------------------------------------------------------------

def test_error_isolation():
    section("18. error isolation (missing/corrupted baseline never crashes report assembly — proven at the find_latest_report unit level; "
            "full end-to-end isolation is empirically proven by the real §40 execution itself, already run this session)")
    check("missing dir handled", sop.find_latest_report(Path("/nope/nope/nope"), "x") is None)
    status = sop.build_activation_matrix_status([])  # aucun blocker connu -> ne doit jamais lever
    check("empty blocker list handled without raising", isinstance(status, dict) and len(status) == 4)


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
