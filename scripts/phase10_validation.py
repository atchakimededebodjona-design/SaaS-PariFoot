"""
scripts/phase10_validation.py — Phase 10 : XFOOT CONTROLLED PRODUCTION
READINESS & EMPIRICAL VALIDATION V1.
=============================================================================
NO AUTOMATIC ACTIVATION. Consolide, sur données réelles, ce que Phase 8A-9.5
ont déjà construit et validé — REUSE > REIMPLEMENT (§1/§2) : ce script
n'évalue RIEN qu'une fonction existante ne calcule déjà, il orchestre et
synthétise.

Réutilise TEL QUEL :
  - api/app/ai/readiness/matrix.py::evaluate_production_readiness (Phase 9) — BASELINE (§3).
  - api/app/ai/shadow/monitoring.py::compute_shadow_health (Phase 8N) — SHADOW BASELINE (§4).
  - api/app/ai/shadow/evidence.py (Phase 9.3) — evidence ledger étendu / breakdown / drift / model-version / data gaps / blockers.
  - api/app/ai/shadow/watch.py::filter_real_prospective_entries/readiness_blockers (Phase 9.5).
  - api/app/ai/readiness/human_review.py (Phase 10, nouveau) — checklist / evidence status / entry-exit criteria / verdict.
  - api/app/ai/safety/guards.py::can_activate_production (Phase 9.1, LECTURE SEULE).

§32 : EXÉCUTE RÉELLEMENT (jamais simulées) les commandes littérales
prescrites, PUIS toutes les régressions — jamais un autre mode.
STRICTEMENT LECTURE SEULE sur api/app.db : ce script n'appelle jamais
execute_rollback/capture/resolve — la démonstration empirique du rollback
(§19, isolée, jamais api/app.db) vit exclusivement dans api/test_phase10.py.

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/phase10_validation.py [--as-of ISO] [--json] [--markdown]
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
from app.ai.shadow.monitoring import compute_shadow_health  # noqa: E402
from app.ai.shadow.metrics import compute_shadow_track_record, value_tracking_status  # noqa: E402
from app.ai.shadow.evidence import (  # noqa: E402
    compute_full_evidence_ledger, compute_model_version_tracking, compute_breakdown,
    compute_temporal_drift, compute_data_gaps,
)
from app.ai.shadow.watch import filter_real_prospective_entries, readiness_blockers  # noqa: E402
from app.ai.shadow.operations import run_preflight_safety  # noqa: E402
from app.ai.readiness.matrix import evaluate_production_readiness  # noqa: E402
from app.ai.readiness.human_review import (  # noqa: E402
    build_phase10_checklist, classify_evidence_status, human_review_gate,
    build_entry_criteria, build_exit_criteria, derive_phase10_verdict, PHASE10_VERDICTS,
)
from app.ai.safety.kill_switch import KillSwitchStore  # noqa: E402
from app.ai.safety.guards import can_activate_production  # noqa: E402

import feature_engineering_walkforward as fewf  # noqa: E402 (réutilise snapshot_db_counts, jamais réimplémenté)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("phase10_validation")
UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parent.parent
MARKETS = ("1X2", "BTTS", "OVER_UNDER_2_5")

REGRESSION_SUITES = [
    "test_model_selection.py", "test_track_record.py", "test_feature_registry.py", "test_feature_engineering_v1.py",
    "test_value_engine.py", "test_decision_layer.py", "test_end_to_end_pipeline.py", "test_shadow_operational.py",
    "test_historical_replay.py", "test_live_shadow_track.py", "test_shadow_monitoring.py",
    "test_production_readiness.py", "test_safety_controls.py", "test_prospective_shadow.py",
    "test_phase9_3.py", "test_phase9_4.py", "test_phase9_5.py",
]


def run_existing_regression_suites() -> dict:
    """§31 : PASSED/FAILED/NOT_RUN explicite — jamais une suite non exécutée comptée comme PASS."""
    results = {}
    api_dir = REPO_ROOT / "api"
    for suite in REGRESSION_SUITES:
        proc = subprocess.run([sys.executable, suite], cwd=api_dir, capture_output=True, text=True)
        tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        results[suite] = {"status": "PASSED" if proc.returncode == 0 else "FAILED", "returncode": proc.returncode, "summary_line": tail, "pass": proc.returncode == 0}
    return results


def run_this_phase_tests() -> dict:
    api_dir = REPO_ROOT / "api"
    proc = subprocess.run([sys.executable, "test_phase10.py"], cwd=api_dir, capture_output=True, text=True)
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    return {"status": "PASSED" if proc.returncode == 0 else "FAILED", "returncode": proc.returncode, "summary_line": tail, "pass": proc.returncode == 0}


def run_real_execution_log() -> dict:
    """§32 : exécute RÉELLEMENT les 3 commandes CLI littéralement prescrites (jamais simulées) — le 4e
    (api/test_phase10.py) est capturé séparément par run_this_phase_tests() pour éviter une double exécution."""
    log = {}
    commands = [
        ("scripts/shadow_evidence_watch.py --dry-run", [sys.executable, "scripts/shadow_evidence_watch.py", "--dry-run"]),
        ("scripts/shadow_evidence_watch.py --monitor", [sys.executable, "scripts/shadow_evidence_watch.py", "--monitor"]),
        ("scripts/production_readiness.py --dry-run", [sys.executable, "scripts/production_readiness.py", "--dry-run"]),
    ]
    for label, cmd in commands:
        proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
        tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        log[label] = {"returncode": proc.returncode, "summary_line": tail, "pass": proc.returncode == 0}
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


def main(as_of_arg: str, emit_json: bool, emit_markdown: bool) -> dict:
    run_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    generated_at = datetime.now(UTC).isoformat()
    as_of = datetime.fromisoformat(as_of_arg).replace(tzinfo=UTC) if as_of_arg else datetime.now(UTC)

    init_db()
    store = ShadowDecisionStore()  # LECTURE SEULE — ce script n'appelle jamais .save()/capture/resolve.
    kill_switch_store = KillSwitchStore()  # LECTURE SEULE.

    with Session(engine) as session:
        db_before = fewf.snapshot_db_counts(session)

    # ---- §18 : SAFETY CONTROLS — préflight (réutilise Phase 9.4 tel quel). ----
    with Session(engine) as session:
        preflight = run_preflight_safety(session, store, kill_switch_store, as_of)

    # ---- §32 : REAL EXECUTION — les 3 commandes littéralement prescrites, exécutées pour de vrai. ----
    real_execution_log = run_real_execution_log()

    if preflight["status"] == "FAIL":
        logger.error("PRE-FLIGHT SAFETY FAIL — STOP (%s).", preflight["blocking"])
        with Session(engine) as session:
            db_after = fewf.snapshot_db_counts(session)
        status_short, diff_stat, diff_names, production_modified = _check_production_files_untouched(REPO_ROOT)
        result = {
            "run_id": run_id, "generated_at": generated_at, "phase": "10", "kind": "controlled_production_readiness_empirical_validation_v1",
            "rule": "NO AUTOMATIC ACTIVATION. EMPIRICAL VALIDATION ON REAL EVIDENCE ONLY.",
            "as_of": as_of.isoformat(), "preflight": preflight, "real_execution_log": real_execution_log,
            "final_verdict": "BLOCKED", "human_review_status": "NOT_READY_FOR_HUMAN_REVIEW",
            "db_safety": {"before": db_before, "after": db_after, "unchanged": db_before == db_after},
            "git_status_short": status_short, "git_diff_stat": diff_stat, "git_diff_names": diff_names,
            "production_files_modified": production_modified,
            "mode": "MODE_1_SHADOW_ONLY", "production_activation": "BLOCKED", "no_user_betting_signal": True,
        }
        _write_reports(result, render_markdown_blocked(result))
        if emit_json:
            print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        print("PHASE 10 — XFOOT CONTROLLED PRODUCTION READINESS & EMPIRICAL VALIDATION V1 TERMINÉE. "
              "READINESS PRODUCTION ÉVALUÉE SUR PREUVES RÉELLES OU LIMITATIONS DOCUMENTÉES. "
              "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION HUMAINE.")
        return result

    # ---- §3 : PRODUCTION READINESS BASELINE. ----
    with Session(engine) as session:
        readiness = evaluate_production_readiness(session, store, as_of)
    current_blockers = readiness_blockers(readiness)
    activation_guard = can_activate_production(kill_switch_store, readiness.gates, scope="PRODUCTION_PREDICTION_ACTIVATION")

    # ---- §4 : SHADOW BASELINE. ----
    with Session(engine) as session:
        health = compute_shadow_health(session, store, as_of)

    entries = store.all()

    # ---- §5/§6/§7/§8/§9/§10 : evidence + track record — REAL_PROSPECTIVE + RESOLVED uniquement. ----
    with Session(engine) as session:
        full_ledger = compute_full_evidence_ledger(session, entries)
        prospective_entries = filter_real_prospective_entries(session, entries)
        model_version_tracking = compute_model_version_tracking(entries)
        data_gaps = compute_data_gaps(session, entries)

    track_record = {m: compute_shadow_track_record(prospective_entries, market=m) for m in MARKETS}
    track_record["value_tracking"] = value_tracking_status(entries)

    # ---- §12 : temporal drift — EARLY/MIDDLE/RECENT, jamais average-of-averages. ----
    temporal_drift = {m: compute_temporal_drift(prospective_entries, m) for m in MARKETS}

    # ---- §13/§14 : model-version / league / market breakdown. ----
    breakdown = {m: compute_breakdown(prospective_entries, market=m) for m in MARKETS}

    maturity = full_ledger["maturity_real_prospective_resolved"]

    # ---- §23/§24/§21/§22/§40 : couche de décision Phase 10 (human_review.py, nouveau). ----
    checklist = build_phase10_checklist(readiness)
    evidence_status = classify_evidence_status(readiness)
    human_review_status = human_review_gate(maturity=maturity, blockers=current_blockers, readiness_verdict=readiness.final_verdict)
    entry_criteria = build_entry_criteria(readiness)
    exit_criteria = build_exit_criteria()

    with Session(engine) as session:
        db_after = fewf.snapshot_db_counts(session)
    db_safety = {"before": db_before, "after": db_after, "unchanged": db_before == db_after}

    regression = run_existing_regression_suites()
    this_phase_tests = run_this_phase_tests()
    real_execution_log["api/test_phase10.py"] = {"returncode": this_phase_tests["returncode"], "summary_line": this_phase_tests["summary_line"], "pass": this_phase_tests["pass"]}
    status_short, diff_stat, diff_names, production_modified = _check_production_files_untouched(REPO_ROOT)

    tests_green = (db_safety["unchanged"] and not production_modified and this_phase_tests["pass"]
                   and all(r["pass"] for r in regression.values()) and all(r["pass"] for r in real_execution_log.values()))

    final_verdict = derive_phase10_verdict(
        preflight_status=preflight["status"], tests_green=tests_green, readiness_verdict=readiness.final_verdict,
        future_fixtures=health["reality"]["future_fixtures"], real_prospective_resolved=full_ledger["real_prospective_resolved_count"],
        maturity=maturity, blockers=current_blockers,
    )
    assert final_verdict in PHASE10_VERDICTS and final_verdict != "PRODUCTION_READY"  # §35/§39 : garde-fou structurel, jamais contournable.

    result = {
        "run_id": run_id, "generated_at": generated_at, "phase": "10", "kind": "controlled_production_readiness_empirical_validation_v1",
        "rule": "NO AUTOMATIC ACTIVATION. EMPIRICAL VALIDATION ON REAL EVIDENCE ONLY.",
        "as_of": as_of.isoformat(), "preflight": preflight, "real_execution_log": real_execution_log,
        "readiness_verdict": readiness.final_verdict, "readiness_critical_failures": readiness.critical_gate_failures,
        "readiness_recommended_mode": readiness.recommended_mode,
        "current_blockers": current_blockers, "activation_guard_allowed": activation_guard.allowed,
        "activation_guard_blocking_reasons": activation_guard.blocking_reasons,
        "shadow_health_status": health["status"], "shadow_health": health,
        "full_evidence_ledger": full_ledger, "track_record": track_record, "temporal_drift": temporal_drift,
        "breakdown": breakdown, "model_version_tracking": model_version_tracking, "data_gaps": data_gaps,
        "maturity": maturity,
        "checklist": checklist, "evidence_status": evidence_status, "human_review_status": human_review_status,
        "entry_criteria": entry_criteria, "exit_criteria": exit_criteria,
        "db_safety": db_safety, "existing_regression_suites": regression, "this_phase_tests": this_phase_tests,
        "git_status_short": status_short, "git_diff_stat": diff_stat, "git_diff_names": diff_names,
        "production_files_modified": production_modified, "tests_green": tests_green,
        "final_verdict": final_verdict,
        "the_odds_api_status": "SUPPORT_REQUIRED — non appelé dans cette phase, hors scope tant que non explicitement validé/autorisé (§15).",
        "no_user_betting_signal": True, "mode": "MODE_1_SHADOW_ONLY", "production_activation": "BLOCKED",
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
        print(f"Verdict final : {final_verdict}  (readiness={readiness.final_verdict}, maturity={maturity}, human_review={human_review_status})")
        print(f"Blockers critiques : {current_blockers}")
        print("git status --short :")
        print(status_short or "(clean)")
        print("git diff --stat :")
        print(diff_stat or "(no tracked file modified)")
        print("PHASE 10 — XFOOT CONTROLLED PRODUCTION READINESS & EMPIRICAL VALIDATION V1 TERMINÉE. "
              "READINESS PRODUCTION ÉVALUÉE SUR PREUVES RÉELLES OU LIMITATIONS DOCUMENTÉES. "
              "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION HUMAINE.")
        print("=" * 80)
    return result


def render_markdown_blocked(result: dict) -> str:
    md = ["# XFOOT PHASE 10\n\n# CONTROLLED PRODUCTION READINESS & EMPIRICAL VALIDATION\n"]
    md.append(f"\n## 1. Executive Summary\n\nRun id `{result['run_id']}` — {result['generated_at']}.\n\n"
               f"**PRE-FLIGHT SAFETY FAIL — STOP.** {result['preflight']}\n")
    md.append(f"\n## 22. Regression\n\n{result.get('real_execution_log')}\n")
    md.append("\n## 24. Final Verdict\n\n**BLOCKED**\n")
    md.append("\n---\n\nPHASE 10 — XFOOT CONTROLLED PRODUCTION READINESS & EMPIRICAL VALIDATION V1 TERMINÉE. "
               "READINESS PRODUCTION ÉVALUÉE SUR PREUVES RÉELLES OU LIMITATIONS DOCUMENTÉES. "
               "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION HUMAINE.\n")
    return "".join(md)


def render_markdown(result: dict) -> str:
    md = ["# XFOOT PHASE 10\n\n# CONTROLLED PRODUCTION READINESS & EMPIRICAL VALIDATION\n"]
    md.append(f"\n## 1. Executive Summary\n\nRun id `{result['run_id']}` — {result['generated_at']}. as_of={result['as_of']}\n\n"
               f"**Verdict : {result['final_verdict']}** — readiness={result['readiness_verdict']}, human_review={result['human_review_status']}\n")
    md.append(f"\n## 2. Baseline\n\nreadiness_verdict={result['readiness_verdict']} (critical failures: {result['readiness_critical_failures']}), "
               f"recommended_mode={result['readiness_recommended_mode']}\n\ncan_activate_production (information uniquement) : "
               f"allowed={result['activation_guard_allowed']}, reasons={result['activation_guard_blocking_reasons']}\n")
    md.append(f"\n## 3. Shadow Evidence\n\n{result['shadow_health']['reality']}\n\nfull_evidence_ledger={result['full_evidence_ledger']}\n")
    md.append(f"\n## 4. Temporal Integrity\n\nby_data_marking_class={result['full_evidence_ledger']['by_data_marking_class']}\n\ndrift={result['temporal_drift']}\n")
    md.append(f"\n## 5. Model / Version\n\n{result['model_version_tracking']}\n\nbreakdown.by_model={ {m: b.get('by_model') for m, b in result['breakdown'].items()} }\n")
    md.append(f"\n## 6. Feature Provenance\n\ncomplete={result['full_evidence_ledger']['provenance_complete']}, "
               f"incomplete={result['full_evidence_ledger']['provenance_incomplete']}, unknown={result['full_evidence_ledger']['provenance_unknown']}\n")
    md.append(f"\n## 7. Track Record\n\n(REAL_PROSPECTIVE + RESOLVED uniquement, §10)\n\n{result['track_record']}\n")
    md.append(f"\n## 8. Maturity\n\n{result['maturity']}\n")
    md.append(f"\n## 9. Drift\n\n{result['temporal_drift']}\n")
    md.append(f"\n## 10. Monitoring\n\nstatus={result['shadow_health_status']}\n\nalerts={result['shadow_health'].get('alerts')}\n")
    md.append(f"\n## 11. Safety\n\npreflight={result['preflight']['status']}, kill_switch={result['preflight']['checks'].get('kill_switch')}\n")
    md.append("\n## 12. Rollback\n\nDémonstration empirique (§19) : mécanisme exercé sur une DB ISOLÉE dédiée UNIQUEMENT "
               f"(api/test_phase10.py, jamais api/app.db — voir aussi api/test_safety_controls.py, Phase 9.1). "
               f"Gate ROLLBACK actuelle (readiness) : voir checklist ci-dessous. this_phase_tests.pass={result['this_phase_tests']['pass']}\n")
    md.append("\n## 13. Readiness\n\n" + str({"verdict": result["readiness_verdict"], "critical_failures": result["readiness_critical_failures"]}) + "\n")
    md.append("\n## 14. Activation Modes\n\n" + str(result["exit_criteria"]["activation_mode_ladder"]) + "\n")
    md.append("\n## 15. Entry Criteria\n\n" + str(result["entry_criteria"]) + "\n")
    md.append("\n## 16. Exit Criteria\n\n" + str(result["exit_criteria"]["maturity_ladder"]) + "\n")
    md.append(f"\n## 17. Human Review\n\n**{result['human_review_status']}**\n")
    md.append(f"\n## 18. Odds\n\n{result['track_record']['value_tracking']}\n")
    md.append(f"\n## 19. Value\n\nSans odds temporellement vérifiées : VALUE=NOT_AVAILABLE, aucun signal de pari (§16).\n")
    db = result["db_safety"]
    md.append(f"\n## 20. DB Safety\n\nBefore: {db['before']}\n\nAfter: {db['after']}\n\nUnchanged: **{db['unchanged']}**\n")
    md.append(f"\n## 21. Production Isolation\n\nmode={result['mode']}, production_activation={result['production_activation']}. "
               "Ce script n'appelle jamais capture/resolve/execute_rollback/apply_promotion — lecture seule sur api/app.db.\n")
    md.append("\n## 22. Regression\n\n### Real execution (§32, littéralement exécuté)\n\n| Command | Pass |\n|---|---|\n")
    for label, r in result["real_execution_log"].items():
        md.append(f"| {label} | {r['pass']} |\n")
    md.append("\n### Regression suites\n\n| Suite | Status |\n|---|---|\n")
    for suite, r in result["existing_regression_suites"].items():
        md.append(f"| {suite} | {r['status']} |\n")
    md.append(f"\n| api/test_phase10.py (this phase) | {result['this_phase_tests']['status']} |\n")
    md.append(
        "\n## 23. Limitations\n\n"
        "**WHAT IS PROVEN** : " + str([e["gate"] for e in result["evidence_status"]["proven"]]) + "\n\n"
        "**WHAT IS OBSERVED** : " + str([e["gate"] for e in result["evidence_status"]["observed"]]) + "\n\n"
        "**WHAT IS UNKNOWN** : " + str([e["gate"] for e in result["evidence_status"]["unknown"]]) + "\n\n"
        "**WHAT IS BLOCKED** : " + str([e["gate"] for e in result["evidence_status"]["blocked"]]) + "\n\n"
        "**WHAT IS REQUIRED NEXT** : " + str(result["evidence_status"]["required_next"]) + "\n\n"
        "- Aucune heure de coup d'envoi réelle n'est persistée — toute classification prospective reste qualifiée.\n"
        "- The Odds API reste hors scope (§15) tant que son accès historique n'est pas explicitement validé/autorisé.\n"
        "- Un bon score modèle ne compense jamais un échec temporal/provenance/rollback/safety/monitoring (§36).\n"
    )
    md.append(f"\n## 24. Final Verdict\n\n**{result['final_verdict']}** — {result['human_review_status']}\n\n"
               "Jamais PRODUCTION_READY sans gates critiques réellement PASS ET autorisation humaine explicite — et même "
               "alors, cette phase n'active jamais la production (§35/§39).\n")
    md.append("\n---\n\nPHASE 10 — XFOOT CONTROLLED PRODUCTION READINESS & EMPIRICAL VALIDATION V1 TERMINÉE. "
               "READINESS PRODUCTION ÉVALUÉE SUR PREUVES RÉELLES OU LIMITATIONS DOCUMENTÉES. "
               "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION HUMAINE.\n")
    return "".join(md)


def _write_reports(result: dict, markdown: str) -> None:
    outdir = REPO_ROOT / "reports" / "phase10"
    outdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    json_path = outdir / f"xfoot_phase10_{ts}.json"
    md_path = outdir / f"xfoot_phase10_{ts}.md"
    json_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")
    logger.info("Rapports écrits : %s / %s", json_path, md_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as-of", type=str, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args()
    main(args.as_of, args.json, args.markdown)
    assert PHASE10_VERDICTS  # référencé pour la cohérence du vocabulaire — jamais PRODUCTION_READY dans cette liste.
    sys.exit(0)
