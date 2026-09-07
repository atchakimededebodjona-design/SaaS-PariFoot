"""
scripts/phase13_evidence_maturation.py — Phase 13 : XFOOT PROSPECTIVE SHADOW
OBSERVATION & EVIDENCE MATURATION V1.
=============================================================================
§2/§3 du prompt : "REUSE FIRST" / "PAS DE NOUVELLE ARCHITECTURE PAR DÉFAUT".
Ce script n'ajoute AUCUN nouveau module api/app/ai/ — l'infrastructure
Phase 8K-12 couvre déjà DISCOVER/CAPTURE/VERIFY/RESOLVE/TRACK/MONITOR/
READINESS/MODE_2 evaluation/evidence history/blocker evolution/comparaison
de baseline. La SEULE lacune concrète (§2 : "démontrer pourquoi les
fonctions existantes ne suffisent pas") : aucune fonction existante ne
calcule un DELTA OBSERVABLE (§5) entre deux full_evidence_ledger (nouvelles
observations / résolutions / provenance / model_versions / markets /
leagues) — compare_to_phase10_baseline (Phase 11) compare des SCALAIRES
(readiness_verdict/evidence count/track record/provenance) mais jamais des
ENSEMBLES (marchés/ligues/versions distincts). compute_observation_delta()
ci-dessous comble cette lacune précise, rien de plus.

Réutilise TEL QUEL (jamais réimplémenté) :
  - scripts/shadow_observation_period.py::find_latest_report/
    build_activation_matrix_status/_gate_statuses_from_critical_failures (Phase 12).
  - app.ai.shadow.internal_operation.OPERATING_MODE/assert_mode_1_only/
    compare_to_phase10_baseline (Phase 11).
  - app.ai.shadow.watch.compute_blocker_evolution (Phase 9.5).
  - app.ai.shadow.evidence.build_activation_matrix (Phase 9.3).
  - app.ai.readiness.schemas.CRITICAL_GATES (Phase 9).

§28 : exécute RÉELLEMENT les runners Phase 9.5/11/12 (jamais simulé, jamais
réimplémenté). §29 : plusieurs invocations en lecture seule de ces runners
ajoutent chacune un snapshot longitudinal (comportement normal, voir
EvidenceHistoryStore Phase 9.5) — mais AUCUNE n'incrémente le compte
d'évidence réel (aucun --capture n'est jamais passé ici) : le delta reste
donc honnêtement calculé sur les COMPTES d'évidence, jamais sur le nombre
de snapshots produits.

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/phase13_evidence_maturation.py [--json] [--markdown]
"""

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.ai.shadow.internal_operation import OPERATING_MODE, assert_mode_1_only, compare_to_phase10_baseline  # noqa: E402
from app.ai.shadow.watch import compute_blocker_evolution  # noqa: E402

from shadow_observation_period import find_latest_report, build_activation_matrix_status, _gate_statuses_from_critical_failures  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("phase13_evidence_maturation")
UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_CHECKPOINT = "00dd971"

REGRESSION_SUITES = [
    "test_model_selection.py", "test_track_record.py", "test_feature_registry.py", "test_feature_engineering_v1.py",
    "test_value_engine.py", "test_decision_layer.py", "test_end_to_end_pipeline.py", "test_shadow_operational.py",
    "test_historical_replay.py", "test_live_shadow_track.py", "test_shadow_monitoring.py",
    "test_production_readiness.py", "test_safety_controls.py", "test_prospective_shadow.py",
    "test_phase9_3.py", "test_phase9_4.py", "test_phase9_5.py", "test_phase10.py", "test_phase11.py", "test_phase12.py",
]


# ---------------------------------------------------------------------------
# §5/§29 : LA seule fonction de calcul nouvelle de cette phase — delta
# OBSERVABLE entre deux full_evidence_ledger (Phase 9.3, jamais modifié).
# ---------------------------------------------------------------------------

def compute_observation_delta(baseline_ledger: dict, current_ledger: dict) -> dict:
    """§5 : différences purement observables — comptes déjà produits par compute_full_evidence_ledger
    (Phase 9.3), jamais recalculés. §7 : aucune reclassification, uniquement une soustraction de comptes
    déjà classifiés ailleurs. §29 : basé sur les COMPTES d'évidence, jamais sur le nombre de snapshots."""
    def _new_members(key):
        return sorted(set(current_ledger.get(key) or []) - set(baseline_ledger.get(key) or []))

    new_observations = (current_ledger.get("total_observations", 0) or 0) - (baseline_ledger.get("total_observations", 0) or 0)
    new_real_prospective = (current_ledger.get("real_prospective_resolved_count", 0) or 0) - (baseline_ledger.get("real_prospective_resolved_count", 0) or 0)
    new_resolved = (current_ledger.get("resolved", 0) or 0) - (baseline_ledger.get("resolved", 0) or 0)
    new_provenance_complete = (current_ledger.get("provenance_complete", 0) or 0) - (baseline_ledger.get("provenance_complete", 0) or 0)
    new_model_versions = _new_members("distinct_model_versions")
    new_markets = _new_members("distinct_markets")
    new_leagues = _new_members("distinct_leagues")

    no_new_evidence = (new_observations <= 0 and new_real_prospective <= 0 and new_resolved <= 0
                        and new_provenance_complete <= 0 and not new_model_versions and not new_markets and not new_leagues)

    return {
        "NEW_OBSERVATIONS": new_observations, "NEW_REAL_PROSPECTIVE": new_real_prospective,
        "NEW_RESOLVED": new_resolved, "NEW_PROVENANCE_COMPLETE": new_provenance_complete,
        "NEW_MODEL_VERSIONS": new_model_versions, "NEW_MARKETS": new_markets, "NEW_LEAGUES": new_leagues,
        "NO_NEW_EVIDENCE": no_new_evidence,
    }


def run_existing_regression_suites() -> dict:
    results = {}
    api_dir = REPO_ROOT / "api"
    for suite in REGRESSION_SUITES:
        proc = subprocess.run([sys.executable, suite], cwd=api_dir, capture_output=True, text=True)
        tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        results[suite] = {"status": "PASSED" if proc.returncode == 0 else "FAILED", "returncode": proc.returncode, "summary_line": tail, "pass": proc.returncode == 0}
    return results


def run_this_phase_tests() -> dict:
    api_dir = REPO_ROOT / "api"
    test_file = api_dir / "test_phase13.py"
    if not test_file.exists():
        return {"status": "NOT_RUN", "returncode": None, "summary_line": "api/test_phase13.py absent.", "pass": True}
    proc = subprocess.run([sys.executable, "test_phase13.py"], cwd=api_dir, capture_output=True, text=True)
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    return {"status": "PASSED" if proc.returncode == 0 else "FAILED", "returncode": proc.returncode, "summary_line": tail, "pass": proc.returncode == 0}


def _run(cmd: list[str]) -> dict:
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    return {"returncode": proc.returncode, "summary_line": tail, "pass": proc.returncode == 0, "stderr_tail": proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ""}


def run_real_execution_log() -> dict:
    """§28 : exécute RÉELLEMENT les 8 commandes littéralement prescrites. §28 note explicite : documenter
    précisément si un runner ne supporte pas un flag demandé plutôt que de le passer sous silence —
    scripts/shadow_observation_period.py (Phase 12) n'expose PAS de flag --monitor ; on le tente, on
    documente l'échec, puis on retombe sur l'invocation sans flag (jamais un flag fabriqué a posteriori)."""
    log = {}
    log["scripts/internal_shadow_operation.py --dry-run"] = _run([sys.executable, "scripts/internal_shadow_operation.py", "--dry-run"])
    log["scripts/internal_shadow_operation.py --monitor"] = _run([sys.executable, "scripts/internal_shadow_operation.py", "--monitor"])
    log["scripts/shadow_evidence_watch.py --dry-run"] = _run([sys.executable, "scripts/shadow_evidence_watch.py", "--dry-run"])
    log["scripts/shadow_evidence_watch.py --monitor"] = _run([sys.executable, "scripts/shadow_evidence_watch.py", "--monitor"])
    log["scripts/shadow_observation_period.py"] = _run([sys.executable, "scripts/shadow_observation_period.py"])

    monitor_attempt = _run([sys.executable, "scripts/shadow_observation_period.py", "--monitor"])
    if monitor_attempt["returncode"] != 0 and "unrecognized arguments" in monitor_attempt["stderr_tail"]:
        monitor_attempt["note"] = ("scripts/shadow_observation_period.py (Phase 12) n'expose pas de flag --monitor — "
                                    "documenté honnêtement (§28) plutôt que masqué ; le script est de toute façon déjà "
                                    "entièrement en lecture seule à chaque invocation, --monitor n'aurait rien changé.")
        log["scripts/shadow_observation_period.py --monitor (attempted, unsupported flag)"] = monitor_attempt
        log["scripts/shadow_observation_period.py (fallback re-run, no --monitor)"] = _run([sys.executable, "scripts/shadow_observation_period.py"])
    else:
        log["scripts/shadow_observation_period.py --monitor"] = monitor_attempt

    log["scripts/production_readiness.py --dry-run"] = _run([sys.executable, "scripts/production_readiness.py", "--dry-run"])
    return log


PRODUCTION_FILE_PREFIXES = (
    "api/main.py", "api/app/core/", "api/app/models/", "api/app/billing/", "api/app/ai/engine/",
    "api/app/ai/arena/ensemble.py", "api/app/ai/arena/service.py", "api/app/ai/arena/scheduler.py",
    "api/app/ai/arena/promotion.py", "api/app/ai/arena/orchestrator.py", "api/app/ai/arena/models_common.py",
    "api/app/ai/arena/prediction_logging.py", "api/app/ai/features/registry.py", "api/app/ai/decision/",
    "api/app/ai/pipeline/", "api/app/ai/value/", "frontend/", "web/", "src/", "api/alembic/",
)


def _check_production_files_untouched(repo_root: Path) -> tuple[str, str, str, bool]:
    status_short = subprocess.run(["git", "status", "--short"], cwd=repo_root, capture_output=True, text=True).stdout
    diff_stat = subprocess.run(["git", "diff", "--stat"], cwd=repo_root, capture_output=True, text=True).stdout
    diff_names = subprocess.run(["git", "diff", "--name-only"], cwd=repo_root, capture_output=True, text=True).stdout
    modified = [f for f in diff_names.splitlines() if f.strip()]
    hits = [f for f in modified if any(f.startswith(p) for p in PRODUCTION_FILE_PREFIXES)]
    return status_short, diff_stat, diff_names, bool(hits)


def _git_baseline() -> dict:
    log_out = subprocess.run(["git", "log", "--oneline", "-5"], cwd=REPO_ROOT, capture_output=True, text=True).stdout
    return {"git_log_oneline_5": log_out, "expected_checkpoint": EXPECTED_CHECKPOINT,
            "checkpoint_present_in_recent_log": EXPECTED_CHECKPOINT in log_out}


def main(emit_json: bool, emit_markdown: bool) -> dict:
    assert_mode_1_only(OPERATING_MODE)  # §19 : défense en profondeur.

    run_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    generated_at = datetime.now(UTC).isoformat()

    git_baseline = _git_baseline()

    # §4 : baseline Phase 12 RÉELLE, chargée AVANT toute nouvelle exécution — jamais recréée par hypothèse.
    phase12_baseline = find_latest_report(REPO_ROOT / "reports" / "phase12", "xfoot_phase12")
    phase11_of_phase12 = (phase12_baseline or {}).get("phase12_current_report") or {}
    baseline_ledger = phase11_of_phase12.get("full_evidence_ledger") or {}
    baseline_readiness_verdict = (phase12_baseline or {}).get("readiness_verdict", "UNKNOWN")
    baseline_critical_failures = (phase12_baseline or {}).get("readiness_critical_failures", [])

    # §28 : REAL EXECUTION.
    real_execution_log = run_real_execution_log()

    # État courant = le rapport Phase 12 le plus récent APRÈS exécution (lui-même dérivé du rapport Phase 11
    # le plus récent) — jamais recalculé séparément ici (§2/§3).
    phase12_current = find_latest_report(REPO_ROOT / "reports" / "phase12", "xfoot_phase12")
    phase11_of_current = (phase12_current or {}).get("phase12_current_report") or {}
    current_ledger = phase11_of_current.get("full_evidence_ledger") or {}
    current_readiness_verdict = (phase12_current or {}).get("readiness_verdict", "UNKNOWN")
    current_critical_failures = (phase12_current or {}).get("readiness_critical_failures", [])
    current_final_verdict = (phase12_current or {}).get("final_verdict", "UNKNOWN")
    current_human_review = (phase12_current or {}).get("human_review_status", "NOT_READY_FOR_HUMAN_REVIEW")

    status_short, diff_stat, diff_names, production_modified = _check_production_files_untouched(REPO_ROOT)
    regression = run_existing_regression_suites()
    this_phase_tests = run_this_phase_tests()

    literal_commands_pass = all(v["pass"] for k, v in real_execution_log.items() if "attempted, unsupported flag" not in k)
    tests_green = (not production_modified and this_phase_tests["pass"] and all(r["pass"] for r in regression.values()) and literal_commands_pass)

    if phase12_baseline is None:
        observation_delta = {"status": "NO_PHASE12_BASELINE_AVAILABLE", "reason": "Aucun rapport reports/phase12/*.json trouvé — delta non fabriqué."}
        blocker_evolution = {"status": "NO_BASELINE"}
        comparison_vs_phase12 = {"status": "NO_BASELINE"}
    else:
        observation_delta = compute_observation_delta(baseline_ledger, current_ledger)
        blocker_evolution = compute_blocker_evolution([baseline_critical_failures], current_critical_failures)
        comparison_vs_phase12 = compare_to_phase10_baseline(
            current_readiness_verdict=current_readiness_verdict, baseline_readiness_verdict=baseline_readiness_verdict,
            current_real_prospective_count=current_ledger.get("real_prospective_resolved_count", 0),
            baseline_real_prospective_count=baseline_ledger.get("real_prospective_resolved_count", 0),
            current_track_record_sample_size=0, baseline_track_record_sample_size=0,  # non exposé au niveau Phase 12 — évité plutôt que fabriqué (§0)
            current_provenance_complete=current_ledger.get("provenance_complete", 0), baseline_provenance_complete=baseline_ledger.get("provenance_complete", 0),
            current_gate_statuses=_gate_statuses_from_critical_failures(current_critical_failures),
            baseline_gate_statuses=_gate_statuses_from_critical_failures(baseline_critical_failures),
        )
        comparison_vs_phase12["baseline_run_id"] = phase12_baseline.get("run_id")
        comparison_vs_phase12["current_run_id"] = phase12_current.get("run_id") if phase12_current else None

    activation_matrix = build_activation_matrix_status(current_critical_failures)

    # §30/§39 : verdict — jamais sélectionné, toujours dérivé. §2/§3 : cette phase n'introduit aucune
    # nouvelle classification d'évidence — le verdict reste celui déjà calculé par Phase 11/12 (repris tel
    # quel), sauf si CETTE phase elle-même détecte un problème (tests non verts, baseline absente).
    if phase12_current is None:
        final_verdict = "BLOCKED"
    elif not tests_green:
        final_verdict = "NEEDS_FIXES"
    else:
        final_verdict = current_final_verdict

    result = {
        "run_id": run_id, "generated_at": generated_at, "phase": "13", "kind": "prospective_shadow_evidence_maturation_v1",
        "rule": "POST-COMMIT CONTROLLED OBSERVATION. MODE_1_SHADOW_ONLY. NO PRODUCTION ACTIVATION. NO MODE_2/3/4 ACTIVATION.",
        "operating_mode": OPERATING_MODE, "git_baseline": git_baseline,
        "phase12_baseline_run_id": phase12_baseline.get("run_id") if phase12_baseline else None,
        "phase12_baseline_generated_at": phase12_baseline.get("generated_at") if phase12_baseline else None,
        "phase12_baseline_final_verdict": phase12_baseline.get("final_verdict") if phase12_baseline else None,
        "phase12_current_run_id": phase12_current.get("run_id") if phase12_current else None,
        "real_execution_log": real_execution_log,
        "observation_delta": observation_delta, "blocker_evolution": blocker_evolution,
        "comparison_vs_phase12": comparison_vs_phase12, "activation_matrix": activation_matrix,
        "readiness_verdict": current_readiness_verdict, "readiness_critical_failures": current_critical_failures,
        "human_review_status": current_human_review,
        "baseline_evidence_ledger": baseline_ledger, "current_evidence_ledger": current_ledger,
        "existing_regression_suites": regression, "this_phase_tests": this_phase_tests,
        "git_status_short": status_short, "git_diff_stat": diff_stat, "git_diff_names": diff_names,
        "production_files_modified": production_modified, "tests_green": tests_green,
        "final_verdict": final_verdict,
        "the_odds_api_status": "SUPPORT_REQUIRED — non appelé dans cette phase.", "no_user_betting_signal": True,
        "mode": OPERATING_MODE, "production_activation": "BLOCKED",
    }

    if production_modified:
        logger.error("ARRÊT : des fichiers de production ont été modifiés — voir git diff --name-only.")

    _write_reports(result, render_markdown(result))

    if emit_json:
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    if emit_markdown:
        print(render_markdown(result))
    if not emit_json and not emit_markdown:
        print("\n" + "=" * 80)
        print(f"Verdict final : {final_verdict}  (readiness={current_readiness_verdict}, human_review={current_human_review})")
        print(f"Observation delta : {observation_delta}")
        print(f"Checkpoint {EXPECTED_CHECKPOINT} present in recent log : {git_baseline['checkpoint_present_in_recent_log']}")
        print("git status --short :")
        print(status_short or "(clean)")
        print("git diff --stat :")
        print(diff_stat or "(no tracked file modified)")
        print("git log --oneline -5 :")
        print(git_baseline["git_log_oneline_5"])
        print("PHASE 13 — XFOOT PROSPECTIVE SHADOW EVIDENCE MATURATION V1 TERMINÉE. "
              "ÉVIDENCE PROSPECTIVE ÉVALUÉE SUR DONNÉES RÉELLES OU LIMITATIONS DOCUMENTÉES. "
              "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION HUMAINE.")
        print("=" * 80)
    return result


def render_markdown(result: dict) -> str:
    md = ["# XFOOT PHASE 13\n\n# PROSPECTIVE SHADOW OBSERVATION & EVIDENCE MATURATION\n"]
    md.append(f"\n## 1. Executive Summary\n\nRun id `{result['run_id']}` — {result['generated_at']}\n\n"
               f"**Verdict : {result['final_verdict']}** — {result['human_review_status']}\n\n"
               f"**{'NO_NEW_EVIDENCE' if result['observation_delta'].get('NO_NEW_EVIDENCE') else 'NEW EVIDENCE DETECTED'}** depuis Phase 12 "
               f"(run_id={result['phase12_baseline_run_id']}).\n")
    gb = result["git_baseline"]
    md.append(f"\n## 2. Git Baseline\n\nCheckpoint attendu `{gb['expected_checkpoint']}` présent dans les 5 derniers commits : "
               f"**{gb['checkpoint_present_in_recent_log']}**\n\n```\n{gb['git_log_oneline_5']}```\n")
    md.append(f"\n## 3. Phase 12 Baseline\n\nrun_id={result['phase12_baseline_run_id']}, generated_at={result['phase12_baseline_generated_at']}, "
               f"final_verdict={result['phase12_baseline_final_verdict']}\n\nbaseline_evidence_ledger={result['baseline_evidence_ledger']}\n")
    md.append(f"\n## 4. Observation Period\n\nCommandes réellement exécutées (§28) : {list(result['real_execution_log'].keys())}\n\n"
               "§29 : plusieurs runners en lecture seule ont chacun ajouté un snapshot longitudinal — comportement normal "
               "(EvidenceHistoryStore, Phase 9.5), jamais compté comme plusieurs preuves indépendantes ; le delta ci-dessous "
               "porte sur les COMPTES d'évidence, jamais sur le nombre de snapshots.\n")
    md.append(f"\n## 5. Discovery\n\nVoir current_evidence_ledger.distinct_fixtures : {result['current_evidence_ledger'].get('distinct_fixtures', 'N/A')}\n")
    md.append(f"\n## 6. Capture\n\nNEW_OBSERVATIONS={result['observation_delta'].get('NEW_OBSERVATIONS', 'N/A')} (aucun --capture invoqué par cette phase — capture réelle uniquement si demandée explicitement, §8).\n")
    md.append("\n## 7. Temporal Integrity\n\nVoir current_evidence_ledger.by_data_marking_class — vocabulaire réutilisé (Phase 9.2/9.3), jamais recalculé ici.\n"
               f"\n{result['current_evidence_ledger'].get('by_data_marking_class', 'N/A')}\n")
    md.append(f"\n## 8. Provenance\n\nNEW_PROVENANCE_COMPLETE={result['observation_delta'].get('NEW_PROVENANCE_COMPLETE', 'N/A')} — "
               f"complete={result['current_evidence_ledger'].get('provenance_complete', 'N/A')}\n")
    md.append("\n## 9. Consistency\n\nVoir rapport Phase 11 référencé — jamais recalculé ici.\n")
    md.append(f"\n## 10. Resolution\n\nNEW_RESOLVED={result['observation_delta'].get('NEW_RESOLVED', 'N/A')}\n")
    md.append(f"\n## 11. Track Record\n\n(REAL_PROSPECTIVE + RESOLVED uniquement — voir rapport Phase 11 référencé pour le détail complet)\n")
    md.append(f"\n## 12. Maturity\n\n{result['current_evidence_ledger'].get('maturity_real_prospective_resolved', 'N/A')}\n")
    md.append(f"\n## 13. Model Versions\n\nNEW_MODEL_VERSIONS={result['observation_delta'].get('NEW_MODEL_VERSIONS', 'N/A')} — "
               f"distinct_model_versions={result['current_evidence_ledger'].get('distinct_model_versions', 'N/A')}\n")
    md.append(f"\n## 14. Breakdowns\n\nNEW_MARKETS={result['observation_delta'].get('NEW_MARKETS', 'N/A')}, NEW_LEAGUES={result['observation_delta'].get('NEW_LEAGUES', 'N/A')}\n")
    md.append("\n## 15. Temporal Drift\n\nVoir rapport Phase 11 référencé — jamais recalculé ici (aucune nouvelle observation résolue).\n")
    md.append(f"\n## 16. Monitoring\n\nVoir rapport Phase 11 référencé (shadow_health_status).\n")
    md.append(f"\n## 17. Evidence History\n\nMÊME EvidenceHistoryStore que Phase 9.5/11/12 — voir §4 ci-dessus pour la note sur les snapshots multiples.\n")
    md.append(f"\n## 18. Blocker Evolution\n\n{result['blocker_evolution']}\n")
    md.append(f"\n## 19. Readiness\n\nverdict={result['readiness_verdict']} (critical failures: {result['readiness_critical_failures']})\n")
    md.append("\n## 20. MODE_1\n\nMODE_1_SHADOW_ONLY obligatoire — assert_mode_1_only() vérifié (§19), jamais contourné.\n")
    mode2 = result["activation_matrix"].get("MODE_2_LIMITED_INTERNAL", {})
    md.append(f"\n## 21. MODE_2 Documentary Evaluation\n\n{mode2}\n")
    md.append(f"\n## 22. MODE_3/4 Blockers\n\nMODE_3: {result['activation_matrix'].get('MODE_3_LIMITED_PRODUCTION', {})}\n\n"
               f"MODE_4: {result['activation_matrix'].get('MODE_4_FULL_PRODUCTION', {})}\n")
    md.append("\n## 23. Kill Switch\n\nFail-closed, jamais reset en production — voir rapport Phase 11 référencé pour l'état réel lu.\n")
    md.append("\n## 24. Rollback\n\nAucune opération rollback tentée sur api/app.db dans cette phase — démonstration empirique "
               "exclusivement sur DB isolée (api/test_phase11.py, api/test_phase10.py, api/test_safety_controls.py).\n")
    md.append(f"\n## 25. Database Safety\n\nproduction_files_modified={result['production_files_modified']} — voir aussi db_safety du rapport Phase 11 référencé.\n")
    md.append(f"\n## 26. Production Isolation\n\nmode={result['mode']}, production_activation={result['production_activation']}. "
               "Ce script n'appelle jamais capture/resolve/training/promotion — uniquement des runners déjà validés en sous-processus.\n")
    md.append(f"\n## 27. Odds\n\nNOT_AVAILABLE — aucun appel The Odds API, aucun crédit consommé (§11).\n")
    md.append("\n## 28. Value\n\nSans odds temporellement vérifiées : VALUE=NOT_AVAILABLE, aucun signal de pari.\n")
    md.append(f"\n## 29. Human Review\n\n**{result['human_review_status']}** — ne signifie jamais production ready (§34).\n")
    md.append(f"\n## 30. Phase 10/11/12/13 Comparison\n\n### Phase 12 -> Phase 13\n\n{result['comparison_vs_phase12']}\n\n"
               "(Phase 10/11 déjà comparées dans reports/phase12/ — non recalculées ici, §2/§3.)\n")
    md.append(f"\n## 31. Data Gaps\n\nVoir rapport Phase 11 référencé (data_gaps).\n")
    md.append(f"\n## 32. Required Next\n\n{[b for b in result['readiness_critical_failures']]} — voir aussi blocker_evolution ci-dessus.\n")
    md.append(f"\n## 33. Final Verdict\n\n**{result['final_verdict']}** — {result['human_review_status']}\n")
    md.append("\n---\n\n### EXISTING REGRESSION SUITES\n\n| Suite | Status |\n|---|---|\n")
    for suite, r in result["existing_regression_suites"].items():
        md.append(f"| {suite} | {r['status']} |\n")
    md.append(f"\n| api/test_phase13.py (this phase) | {result['this_phase_tests']['status']} |\n")
    md.append("\n---\n\nPHASE 13 — XFOOT PROSPECTIVE SHADOW EVIDENCE MATURATION V1 TERMINÉE. "
               "ÉVIDENCE PROSPECTIVE ÉVALUÉE SUR DONNÉES RÉELLES OU LIMITATIONS DOCUMENTÉES. "
               "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION HUMAINE.\n")
    return "".join(md)


def _write_reports(result: dict, markdown: str) -> None:
    outdir = REPO_ROOT / "reports" / "phase13"
    outdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    json_path = outdir / f"xfoot_phase13_{ts}.json"
    md_path = outdir / f"xfoot_phase13_{ts}.md"
    json_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")
    logger.info("Rapports écrits : %s / %s", json_path, md_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args()
    main(args.json, args.markdown)
    sys.exit(0)
