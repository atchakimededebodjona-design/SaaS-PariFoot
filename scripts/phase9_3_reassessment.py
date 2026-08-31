"""
scripts/phase9_3_reassessment.py — Phase 9.3 : XFOOT SHADOW EVIDENCE
ACCUMULATION & ACTIVATION GATE REASSESSMENT V1.
=============================================================================
STRICTEMENT LECTURE SEULE. Aucune écriture DB, aucune écriture du Shadow
Store, aucun trigger/reset du Kill Switch, aucune activation. Réutilise TEL
QUEL :
  - api/app/ai/shadow/evidence.py (Phase 9.3).
  - api/app/ai/shadow/prospective.py::compute_evidence_ledger/classify_capture_quality (Phase 9.2).
  - api/app/ai/shadow/monitoring.py::compute_shadow_health (Phase 8N).
  - api/app/ai/shadow/metrics.py::value_tracking_status (Phase 8K/8M/8N).
  - api/app/ai/arena/track_record.py::compare_production_vs_shadow (Phase 7).
  - api/app/ai/readiness/matrix.py::evaluate_production_readiness (Phase 9).
  - api/app/ai/safety/kill_switch.py::KillSwitchStore/assert_production_allowed (Phase 9.1, LECTURE SEULE).

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/phase9_3_reassessment.py [--as-of ISO] [--json] [--markdown]
"""

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from sqlmodel import Session  # noqa: E402
from app.core.database import engine, init_db  # noqa: E402

from app.ai.shadow.tracking import ShadowDecisionStore  # noqa: E402
from app.ai.shadow.prospective import compute_evidence_ledger, classify_capture_quality  # noqa: E402
from app.ai.shadow.monitoring import compute_shadow_health  # noqa: E402
from app.ai.shadow.metrics import value_tracking_status, MATURITY_THRESHOLDS  # noqa: E402
from app.ai.shadow.evidence import (  # noqa: E402
    compute_full_evidence_ledger, compute_model_version_tracking, compute_temporal_drift,
    compute_breakdown, build_activation_matrix, identify_activation_blockers, compute_data_gaps,
)
from app.ai.arena.track_record import compare_production_vs_shadow  # noqa: E402
from app.ai.readiness.matrix import evaluate_production_readiness  # noqa: E402
from app.ai.safety.kill_switch import KillSwitchStore, assert_production_allowed  # noqa: E402

import feature_engineering_walkforward as fewf  # noqa: E402 (réutilise snapshot_db_counts, jamais réimplémenté)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("phase9_3_reassessment")
UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parent.parent

MARKETS = ("1X2", "BTTS", "OVER_UNDER_2_5")

REGRESSION_SUITES = [
    "test_model_selection.py", "test_track_record.py", "test_feature_registry.py", "test_feature_engineering_v1.py",
    "test_value_engine.py", "test_decision_layer.py", "test_end_to_end_pipeline.py", "test_shadow_operational.py",
    "test_historical_replay.py", "test_live_shadow_track.py", "test_shadow_monitoring.py",
    "test_production_readiness.py", "test_safety_controls.py", "test_prospective_shadow.py",
]


def run_existing_regression_suites() -> dict:
    """§25 : jamais une suite non exécutée comptée comme PASS — `pass` défaut False."""
    results = {}
    api_dir = REPO_ROOT / "api"
    for suite in REGRESSION_SUITES:
        proc = subprocess.run([sys.executable, suite], cwd=api_dir, capture_output=True, text=True)
        tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        results[suite] = {"returncode": proc.returncode, "summary_line": tail, "pass": proc.returncode == 0}
    return results


def run_this_phase_tests() -> dict:
    api_dir = REPO_ROOT / "api"
    proc = subprocess.run([sys.executable, "test_phase9_3.py"], cwd=api_dir, capture_output=True, text=True)
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    return {"returncode": proc.returncode, "summary_line": tail, "pass": proc.returncode == 0}


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


def build_manual_runbook() -> list[dict]:
    """§22 : documentaire uniquement — aucune étape n'active la production."""
    return [
        {"step": 1, "action": "Vérifier la sécurité", "command": "python scripts/safety_control.py status"},
        {"step": 2, "action": "Lancer un dry-run", "command": "python scripts/prospective_shadow.py --dry-run"},
        {"step": 3, "action": "Inspecter les candidates", "command": "voir capture_outcome.candidates/rejected du dry-run précédent"},
        {"step": 4, "action": "Lancer la capture Shadow réelle", "command": "python scripts/prospective_shadow.py"},
        {"step": 5, "action": "Vérifier le store", "command": "python scripts/phase9_3_reassessment.py (section Store Integrity)"},
        {"step": 6, "action": "Résoudre les observations", "command": "python scripts/prospective_shadow.py --resolve"},
        {"step": 7, "action": "Recalculer le track record", "command": "inclus dans le rapport prospective_shadow / phase9_3_reassessment"},
        {"step": 8, "action": "Lancer le monitoring", "command": "python scripts/shadow_monitor.py"},
        {"step": 9, "action": "Recalculer la readiness", "command": "python scripts/production_readiness.py"},
        {"step": 10, "action": "Documenter les blockers", "command": "python scripts/phase9_3_reassessment.py (section Blockers)"},
    ]


def main(as_of_arg: str, emit_json: bool, emit_markdown: bool) -> dict:
    run_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    generated_at = datetime.now(UTC).isoformat()
    as_of = datetime.fromisoformat(as_of_arg).replace(tzinfo=UTC) if as_of_arg else datetime.now(UTC)

    init_db()
    with Session(engine) as session:
        db_before = fewf.snapshot_db_counts(session)

    store = ShadowDecisionStore()  # LECTURE SEULE — jamais .save() dans cette phase.

    with Session(engine) as session:
        # §15 : readiness AVANT (état actuel, pour comparaison — cette phase ne modifie rien entre les deux
        # appels, donc "before"/"after" sont identiques par construction, ce qui EST le résultat honnête
        # attendu d'une phase strictement read-only, §29 : aucune activation, aucune amélioration inventée).
        readiness_before = evaluate_production_readiness(session, store, as_of)

    entries = store.all()

    with Session(engine) as session:
        full_ledger = compute_full_evidence_ledger(session, entries)
        basic_ledger = compute_evidence_ledger(entries)
        model_version_tracking = compute_model_version_tracking(entries)
        data_gaps = compute_data_gaps(session, entries)

    temporal_drift = {m: compute_temporal_drift(entries, m) for m in MARKETS}
    breakdown = {m: compute_breakdown(entries, m) for m in MARKETS}
    value_tracking = value_tracking_status(entries)

    # §12 : compare_production_vs_shadow (Phase 7) — mécanisme DISTINCT (shadow_selection_predictions), jamais confondu.
    with Session(engine) as session:
        model_selection_shadow_comparison = {m: compare_production_vs_shadow(session, m).__dict__ for m in MARKETS}

    with Session(engine) as session:
        health = compute_shadow_health(session, store, as_of)

    with Session(engine) as session:
        readiness_after = evaluate_production_readiness(session, store, as_of)

    blockers = identify_activation_blockers(readiness_after)
    activation_matrix = build_activation_matrix()

    # §18 : Kill Switch — LECTURE SEULE (jamais trigger()/reset() ici).
    kill_switch_store = KillSwitchStore()
    kill_switch_check = assert_production_allowed(kill_switch_store, "PRODUCTION_PREDICTION_ACTIVATION")

    with Session(engine) as session:
        db_after = fewf.snapshot_db_counts(session)
    db_safety = {"before": db_before, "after": db_after, "unchanged": db_before == db_after}

    regression = run_existing_regression_suites()
    this_phase_tests = run_this_phase_tests()
    status_short, diff_stat, diff_names, production_modified = _check_production_files_untouched(REPO_ROOT)

    tests_green = db_safety["unchanged"] and not production_modified and this_phase_tests["pass"] and all(r["pass"] for r in regression.values())

    maturity = full_ledger["maturity_real_prospective_resolved"]

    # §32 : verdict — jamais PRODUCTION_READY.
    if not tests_green:
        final_verdict = "NEEDS_FIXES"
    elif not kill_switch_check.allowed or readiness_after.final_verdict == "BLOCKED":
        final_verdict = "BLOCKED"
    elif maturity == "NO_DATA":
        final_verdict = "INSUFFICIENT_REAL_DATA"
    elif maturity == "EARLY_DATA":
        final_verdict = "EARLY_EVIDENCE"
    elif maturity == "TRACKING":
        final_verdict = "TRACKING"
    else:  # STATISTICALLY_INFORMATIVE
        if not blockers and readiness_after.final_verdict in ("CONDITIONALLY_READY", "PRODUCTION_READY"):
            final_verdict = "READY_FOR_HUMAN_REVIEW"
        else:
            final_verdict = "STATISTICALLY_INFORMATIVE"

    result = {
        "run_id": run_id, "generated_at": generated_at, "phase": "9.3", "kind": "shadow_evidence_activation_gate_reassessment_v1",
        "rule": "READ-ONLY REASSESSMENT. MODE_1_SHADOW_ONLY. NO PRODUCTION ACTIVATION. NO AUTOMATIC GATE OVERRIDE.",
        "as_of": as_of.isoformat(),
        "evidence_ledger": full_ledger, "basic_evidence_ledger": basic_ledger,
        "model_version_tracking": model_version_tracking, "temporal_drift": temporal_drift, "breakdown": breakdown,
        "value_tracking": value_tracking, "model_selection_shadow_comparison": model_selection_shadow_comparison,
        "shadow_health": health, "shadow_health_status": health["status"],
        "readiness_before_verdict": readiness_before.final_verdict, "readiness_after_verdict": readiness_after.final_verdict,
        "readiness_before_critical_failures": readiness_before.critical_gate_failures,
        "readiness_after_critical_failures": readiness_after.critical_gate_failures,
        "activation_matrix": activation_matrix, "blockers": blockers, "data_gaps": data_gaps,
        "manual_runbook": build_manual_runbook(),
        "kill_switch_status": kill_switch_check.allowed, "kill_switch_code": kill_switch_check.code,
        "maturity": maturity, "maturity_thresholds": MATURITY_THRESHOLDS,
        "db_safety": db_safety, "existing_regression_suites": regression, "this_phase_tests": this_phase_tests,
        "git_status_short": status_short, "git_diff_stat": diff_stat, "git_diff_names": diff_names,
        "production_files_modified": production_modified, "tests_green": tests_green, "final_verdict": final_verdict,
        "the_odds_api_status": "SUPPORT_REQUIRED — non appelé dans cette phase.", "no_user_betting_signal": True,
        "mode": "MODE_1_SHADOW_ONLY", "production_activation": "BLOCKED",
    }

    if production_modified:
        logger.error("ARRÊT : des fichiers de production ont été modifiés — voir git diff --name-only.")

    _write_reports(result, run_id)

    if emit_json:
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    if emit_markdown:
        print(render_markdown(result))
    if not emit_json and not emit_markdown:
        print("\n" + "=" * 80)
        print(f"Verdict final : {final_verdict}  (maturity={maturity})")
        print(f"Readiness : before={readiness_before.final_verdict} -> after={readiness_after.final_verdict}")
        print(f"Blockers critiques : {[b['blocker'] for b in blockers]}")
        print("git status --short :")
        print(status_short or "(clean)")
        print("git diff --stat :")
        print(diff_stat or "(no tracked file modified)")
        print("PHASE 9.3 — XFOOT SHADOW EVIDENCE ACCUMULATION & ACTIVATION GATE REASSESSMENT V1 TERMINÉE. "
              "ÉVIDENCE SHADOW ÉVALUÉE, READINESS RÉÉVALUÉE ET CONDITIONS D'ACTIVATION DOCUMENTÉES. "
              "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION.")
        print("=" * 80)
    return result


def render_markdown(result: dict) -> str:
    md = ["# XFOOT PHASE 9.3\n\n# SHADOW EVIDENCE & ACTIVATION GATE REASSESSMENT\n"]
    md.append(f"\n## 1. Executive Summary\n\nRun id `{result['run_id']}` — {result['generated_at']}. as_of={result['as_of']}\n\n**Verdict : {result['final_verdict']}** — maturity={result['maturity']}\n")
    md.append(f"\n## 2. Current Operating Mode\n\n{result['mode']} — production_activation={result['production_activation']}\n")
    md.append(f"\n## 3. Evidence Ledger\n\n{result['evidence_ledger']}\n")
    md.append(f"\n## 4. Evidence Quality\n\nby_data_marking_class={result['evidence_ledger']['by_data_marking_class']}\n")
    md.append(f"\n## 5. Prospective Integrity\n\nVoir data_marking_detail (par observation) dans evidence_ledger.\n")
    md.append(f"\n## 6. Provenance\n\ncomplete={result['evidence_ledger']['provenance_complete']}, incomplete={result['evidence_ledger']['provenance_incomplete']}, unknown={result['evidence_ledger']['provenance_unknown']}\n")
    md.append(f"\n## 7. Track Record\n\n{result['breakdown']}\n")
    md.append(f"\n## 8. Statistical Evidence\n\nWilson CI déjà intégré dans compute_shadow_track_record (Phase 5.7/8K) ; bootstrap/McNemar déjà intégrés dans compare_production_vs_shadow (Phase 7) — aucune nouvelle formule (§7).\n\n{result['model_selection_shadow_comparison']}\n")
    md.append(f"\n## 9. Maturity\n\n{result['maturity']} (seuils : {result['maturity_thresholds']})\n")
    md.append(f"\n## 10. Model / Version Tracking\n\n{result['model_version_tracking']}\n")
    md.append(f"\n## 11. Temporal Drift\n\n{result['temporal_drift']}\n")
    md.append(f"\n## 12. League Analysis\n\nVoir breakdown.by_league par marché.\n")
    md.append(f"\n## 13. Market Analysis\n\n{result['breakdown']}\n")
    md.append(f"\n## 14. Shadow vs Production\n\n(Mécanisme Phase 7 distinct — shadow_selection_predictions, PAS le ShadowDecisionStore)\n\n{result['model_selection_shadow_comparison']}\n")
    md.append(f"\n## 15. Value Evidence\n\n{result['value_tracking']}\n")
    md.append(f"\n## 16. Monitoring\n\nstatus={result['shadow_health_status']}\n")
    md.append(f"\n## 17. Readiness Gates\n\nBEFORE={result['readiness_before_verdict']} (failures: {result['readiness_before_critical_failures']}) -> AFTER={result['readiness_after_verdict']} (failures: {result['readiness_after_critical_failures']})\n")
    md.append("\n## 18. Activation Matrix (DOCUMENTAIRE — jamais sélectionnée/activée)\n\n")
    for mode, spec in result["activation_matrix"].items():
        md.append(f"\n**{mode}**\n\n{spec}\n")
    md.append("\n## 19. Blockers\n\n| Blocker | Why | Required to clear |\n|---|---|---|\n")
    for b in result["blockers"]:
        md.append(f"| {b['blocker']} | {b['why']} | {str(b['required_to_clear'])[:100]} |\n")
    md.append(f"\n## 20. Data Gaps\n\n{result['data_gaps']}\n")
    md.append("\n## 21. Operational Runbook\n\n")
    for step in result["manual_runbook"]:
        md.append(f"{step['step']}. {step['action']} — `{step['command']}`\n")
    md.append(f"\n## 22. Production Impact\n\nAucun — production_activation={result['production_activation']}, kill_switch_status allowed={result['kill_switch_status']}\n")
    md.append(
        "\n## 23. Limitations\n\n"
        "- Kickoff réel jamais disponible (ModelPrediction.match_date typé `date`) — toute classification prospective reste qualifiée (CONSISTENT_WITH_PLACEHOLDER_KICKOFF), jamais une preuve à heure exacte.\n"
        "- value_tracking reste NOT_AVAILABLE tant qu'aucune odds TEMPORALLY_VERIFIED n'existe.\n"
        "- compare_production_vs_shadow (§14) lit shadow_selection_predictions (Phase 6/7), PAS le ShadowDecisionStore (Phase 8K-9.2) — deux mécanismes distincts, jamais fusionnés.\n"
        "- readiness_before == readiness_after par construction (cette phase n'écrit rien entre les deux appels) — résultat honnête d'une reassessment strictement read-only, pas un bug.\n"
    )
    md.append(f"\n## 24. Verdict\n\n**{result['final_verdict']}**\n")
    md.append("\n---\n\n### EXISTING REGRESSION SUITES\n\n| Suite | Pass |\n|---|---|\n")
    for suite, r in result["existing_regression_suites"].items():
        md.append(f"| {suite} | {r['pass']} |\n")
    md.append(f"\n| test_phase9_3.py (this phase) | {result['this_phase_tests']['pass']} |\n")
    md.append("\n---\n\nPHASE 9.3 — XFOOT SHADOW EVIDENCE ACCUMULATION & ACTIVATION GATE REASSESSMENT V1 TERMINÉE. "
               "ÉVIDENCE SHADOW ÉVALUÉE, READINESS RÉÉVALUÉE ET CONDITIONS D'ACTIVATION DOCUMENTÉES. "
               "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION.\n")
    return "".join(md)


def _write_reports(result: dict, run_id: str) -> None:
    outdir = REPO_ROOT / "reports" / "phase9_3"
    outdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    json_path = outdir / f"xfoot_phase9_3_{ts}.json"
    md_path = outdir / f"xfoot_phase9_3_{ts}.md"
    json_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    logger.info("Rapports écrits : %s / %s", json_path, md_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as-of", type=str, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args()
    main(args.as_of, args.json, args.markdown)
    sys.exit(0)
