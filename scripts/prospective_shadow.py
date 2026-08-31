"""
scripts/prospective_shadow.py — Phase 9.2 : XFOOT PROSPECTIVE SHADOW
ACTIVATION & EVIDENCE ACCUMULATION V1.
=============================================================================
REAL FUTURE DATA + SHADOW ONLY. Aucun appel réseau, aucune clé API, aucune
écriture dans une table de production, aucune modification de scheduler.py.
Réutilise TEL QUEL :
  - api/app/ai/shadow/prospective.py (Phase 9.2) : capture verrouillée,
    evidence ledger, capture quality, backup/recovery, readiness impact.
  - api/app/ai/shadow/resolution.py::resolve_record (Phase 8K).
  - api/app/ai/shadow/metrics.py::compute_shadow_track_record (Phase 5/7).
  - api/app/ai/shadow/monitoring.py::compute_shadow_health (Phase 8N).
  - api/app/ai/readiness/matrix.py::evaluate_production_readiness (Phase 9).

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/prospective_shadow.py [--as-of ISO] [--dry-run] [--resolve] [--market M]
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
from app.ai.shadow.resolution import resolve_record  # noqa: E402
from app.ai.shadow.metrics import compute_shadow_track_record, value_tracking_status  # noqa: E402
from app.ai.shadow.monitoring import compute_shadow_health  # noqa: E402
from app.ai.shadow.prospective import (  # noqa: E402
    run_prospective_capture, compute_evidence_ledger, classify_capture_quality,
    backup_store, restore_and_validate, compute_readiness_impact,
)
from app.ai.readiness.matrix import evaluate_production_readiness  # noqa: E402

import feature_engineering_walkforward as fewf  # noqa: E402 (réutilise snapshot_db_counts, jamais réimplémenté)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("prospective_shadow")
UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parent.parent

EVIDENCE_STATUSES = ("NO_DATA", "PARTIAL", "EARLY_EVIDENCE", "TRACKING", "STATISTICALLY_INFORMATIVE")
_MATURITY_TO_EVIDENCE = {"NO_DATA": "NO_DATA", "EARLY_DATA": "EARLY_EVIDENCE", "TRACKING": "TRACKING", "STATISTICALLY_INFORMATIVE": "STATISTICALLY_INFORMATIVE"}

REGRESSION_SUITES = [
    "test_model_selection.py", "test_track_record.py", "test_feature_registry.py", "test_feature_engineering_v1.py",
    "test_value_engine.py", "test_decision_layer.py", "test_end_to_end_pipeline.py", "test_shadow_operational.py",
    "test_historical_replay.py", "test_live_shadow_track.py", "test_shadow_monitoring.py",
    "test_production_readiness.py", "test_safety_controls.py",
]


def run_existing_regression_suites() -> dict:
    results = {}
    api_dir = REPO_ROOT / "api"
    for suite in REGRESSION_SUITES:
        proc = subprocess.run([sys.executable, suite], cwd=api_dir, capture_output=True, text=True)
        tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        results[suite] = {"returncode": proc.returncode, "summary_line": tail, "pass": proc.returncode == 0}
    return results


def run_this_phase_tests() -> dict:
    api_dir = REPO_ROOT / "api"
    proc = subprocess.run([sys.executable, "test_prospective_shadow.py"], cwd=api_dir, capture_output=True, text=True)
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


def run_resolution(session, store: ShadowDecisionStore) -> dict:
    summary = {"resolved": 0, "conflicts": 0, "unresolved": 0, "invalid": 0, "still_pending": 0, "skipped_already_resolved": 0}
    for record, resolution in store.all():
        if resolution.result_status != "PENDING":
            summary["skipped_already_resolved"] += 1
            continue
        new_resolution = resolve_record(session, record, resolution)
        if new_resolution.result_status == "PENDING":
            summary["still_pending"] += 1
            continue
        if store.update_resolution(record.shadow_id, new_resolution):
            key = {"RESOLVED": "resolved", "CONFLICT": "conflicts", "UNRESOLVED": "unresolved", "INVALID": "invalid"}[new_resolution.result_status]
            summary[key] += 1
    store.save()
    return summary


def main(as_of_arg: str, dry_run: bool, do_resolve: bool, market: str, emit_json: bool, emit_markdown: bool) -> dict:
    run_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    generated_at = datetime.now(UTC).isoformat()
    as_of = datetime.fromisoformat(as_of_arg).replace(tzinfo=UTC) if as_of_arg else datetime.now(UTC)

    init_db()
    with Session(engine) as session:
        db_before = fewf.snapshot_db_counts(session)

    store = ShadowDecisionStore()  # reports/shadow/shadow_decision_store.json — MÊME store que Phase 8K/8M.

    # §27 : backup AVANT toute écriture réelle (jamais après un incident).
    backup_path = None
    if not dry_run:
        backup_dir = REPO_ROOT / "reports" / "shadow" / "prospective" / "backups"
        backup_path = backup_store(store, backup_dir)
        # §27 : valide immédiatement que la sauvegarde est restaurable, sur une copie temporaire distincte.
        tmp_restore_path = backup_dir / f"_validate_{run_id}.json"
        try:
            restore_and_validate(backup_path, tmp_restore_path)
            backup_validated = True
        except ValueError as e:
            backup_validated = False
            logger.error("Sauvegarde non restaurable : %s", e)
        finally:
            tmp_restore_path.unlink(missing_ok=True)
    else:
        backup_validated = None

    # §49 : readiness AVANT capture (lecture seule, sur l'état actuel du store/DB).
    with Session(engine) as session:
        readiness_before = evaluate_production_readiness(session, store, as_of)

    with Session(engine) as session:
        capture_outcome = run_prospective_capture(session, store, as_of, dry_run=dry_run, market=market)
        resolution_summary = run_resolution(session, store) if (do_resolve and not dry_run) else {"skipped": "NOT_REQUESTED_OR_DRY_RUN"}

    with Session(engine) as session:
        readiness_after = evaluate_production_readiness(session, store, as_of)

    readiness_impact = compute_readiness_impact(readiness_before.gates, readiness_after.gates)

    entries = store.all()
    evidence_ledger = compute_evidence_ledger(entries)
    capture_quality = classify_capture_quality(capture_outcome, entries)
    track_record = {
        "1X2": compute_shadow_track_record(entries, market="1X2"),
        "BTTS": compute_shadow_track_record(entries, market="BTTS"),
        "OVER_UNDER_2_5": compute_shadow_track_record(entries, market="OVER_UNDER_2_5"),
        "value_tracking": value_tracking_status(entries),
    }

    with Session(engine) as session:
        health = compute_shadow_health(session, store, as_of)

    with Session(engine) as session:
        db_after = fewf.snapshot_db_counts(session)
    db_safety = {"before": db_before, "after": db_after, "unchanged": db_before == db_after}

    regression = run_existing_regression_suites()
    this_phase_tests = run_this_phase_tests()
    status_short, diff_stat, diff_names, production_modified = _check_production_files_untouched(REPO_ROOT)

    tests_green = db_safety["unchanged"] and not production_modified and this_phase_tests["pass"] and all(r["pass"] for r in regression.values())

    evidence_status = _MATURITY_TO_EVIDENCE.get(evidence_ledger["maturity"], "NO_DATA")
    if evidence_ledger["total_real_observations"] == 0:
        evidence_status = "NO_DATA"

    if not tests_green:
        final_verdict = "SHADOW_NEEDS_FIXES"
    elif capture_outcome.get("blocked"):
        final_verdict = "BLOCKED"
    elif capture_outcome["candidates"] == 0 and evidence_ledger["total_real_observations"] == 0:
        final_verdict = "INSUFFICIENT_REAL_DATA"
    elif evidence_status in ("TRACKING", "STATISTICALLY_INFORMATIVE"):
        final_verdict = "SHADOW_OPERATIONAL"
    elif evidence_status == "EARLY_EVIDENCE" or capture_outcome["captured"] > 0:
        final_verdict = "EARLY_EVIDENCE"
    else:
        final_verdict = "INSUFFICIENT_REAL_DATA"

    result = {
        "run_id": run_id, "generated_at": generated_at, "phase": "9.2", "kind": "prospective_shadow_evidence_accumulation_v1",
        "rule": "REAL-WORLD SHADOW ONLY. MODE_1_SHADOW_ONLY. NO PRODUCTION ACTIVATION. NO NEW PREDICTION. NO NETWORK.",
        "as_of": as_of.isoformat(), "dry_run": dry_run, "market": market, "resolve_requested": do_resolve,
        "capture_outcome": capture_outcome, "resolution_summary": resolution_summary,
        "evidence_ledger": evidence_ledger, "evidence_status": evidence_status, "capture_quality": capture_quality,
        "track_record": track_record, "shadow_health_status": health["status"], "shadow_health": health,
        "readiness_impact": readiness_impact, "readiness_before_verdict": readiness_before.final_verdict,
        "readiness_after_verdict": readiness_after.final_verdict,
        "backup_path": str(backup_path) if backup_path else None, "backup_validated": backup_validated,
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
        print(f"Verdict final : {final_verdict}  (evidence_status={evidence_status})")
        print(f"Capture : {capture_outcome.get('captured', 0)} capturé(s), {capture_outcome.get('candidates', 0)} candidat(s), "
              f"blocked={capture_outcome.get('blocked')}")
        print("git status --short :")
        print(status_short or "(clean)")
        print("git diff --stat :")
        print(diff_stat or "(no tracked file modified)")
        print("PHASE 9.2 — XFOOT PROSPECTIVE SHADOW ACTIVATION & EVIDENCE ACCUMULATION V1 TERMINÉE. "
              "SHADOW PROSPECTIF CONFIGURÉ ET ÉVALUÉ AVEC DONNÉES RÉELLES OU LIMITATION DOCUMENTÉE. "
              "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION.")
        print("=" * 80)
    return result


def render_markdown(result: dict) -> str:
    md = ["# XFOOT PROSPECTIVE SHADOW EVIDENCE V1\n"]
    md.append(f"\n## 1. Executive Summary\n\nRun id `{result['run_id']}` — {result['generated_at']}. as_of={result['as_of']}, dry_run={result['dry_run']}\n\n**Verdict : {result['final_verdict']}** — evidence_status={result['evidence_status']}\n")
    md.append(f"\n## 2. Current Mode\n\n{result['mode']} — production_activation={result['production_activation']}\n")
    md.append(f"\n## 3. Real Candidate Fixtures\n\ncandidates={result['capture_outcome'].get('candidates')}\n")
    md.append(f"\n## 4. Capture Results\n\n{result['capture_outcome']}\n")
    md.append(f"\n## 5. Rejection Reasons\n\n{result['capture_outcome'].get('rejected')}\n\nmismatches={result['capture_outcome'].get('mismatches')}\n")
    md.append(f"\n## 6. Temporal Integrity\n\nVoir prospective_status par record capturé dans capture_outcome.captured_records.\n")
    md.append(f"\n## 7. Production Consistency\n\nmismatches détectés (jamais corrigés silencieusement) : {result['capture_outcome'].get('mismatches')}\n")
    md.append(f"\n## 8. Provenance\n\nVoir evidence_ledger — distinct_models={result['evidence_ledger']['distinct_models']}\n")
    md.append(f"\n## 9. Resolution Status\n\n{result['resolution_summary']}\n")
    md.append(f"\n## 10. Track Record\n\n{result['track_record']}\n")
    md.append(f"\n## 11. Maturity\n\n{result['evidence_ledger']['maturity']} -> evidence_status={result['evidence_status']}\n")
    md.append(f"\n## 12. Coverage\n\n{result['capture_quality']}\n")
    md.append(f"\n## 13. Monitoring Health\n\nstatus={result['shadow_health_status']}\n")
    md.append("\n## 14. Alerts\n\n" + str(result["shadow_health"].get("alerts")) + "\n")
    md.append(f"\n## 15. Store Integrity\n\nbackup_path={result['backup_path']}, backup_validated={result['backup_validated']}\n")
    db = result["db_safety"]
    md.append(f"\n## 16. DB Safety\n\nBefore: {db['before']}\n\nAfter: {db['after']}\n\nUnchanged: **{db['unchanged']}**\n")
    md.append(f"\n## 17. Statistical Evidence\n\n{result['track_record']}\n\nJamais de conclusion 'better than production' avant STATISTICALLY_INFORMATIVE (§31).\n")
    md.append(
        "\n## 18. Limitations\n\n"
        "- Aucune heure de coup d'envoi réelle n'est persistée (ModelPrediction.match_date est typé `date`) — toute "
        "classification 'prospective'/'fenêtre T-Xh' est calculée contre un placeholder à minuit, jamais une preuve à heure exacte.\n"
        "- value_tracking reste NOT_AVAILABLE tant qu'aucune odds TEMPORALLY_VERIFIED n'existe (The Odds API SUPPORT_REQUIRED).\n"
        "- MODE_1_SHADOW_ONLY reste actif quel que soit le volume de données réelles accumulées cette phase.\n"
    )
    md.append(f"\n## 19. Evidence Accumulated\n\n{result['evidence_ledger']}\n")
    md.append(f"\n## 20. Production Readiness Impact\n\nBEFORE verdict={result['readiness_before_verdict']} -> AFTER verdict={result['readiness_after_verdict']}\n\n{result['readiness_impact']}\n")
    md.append(f"\n## 21. Verdict\n\n**{result['final_verdict']}**\n")
    md.append("\n---\n\n### EXISTING REGRESSION SUITES\n\n| Suite | Pass |\n|---|---|\n")
    for suite, r in result["existing_regression_suites"].items():
        md.append(f"| {suite} | {r['pass']} |\n")
    md.append(f"\n| test_prospective_shadow.py (this phase) | {result['this_phase_tests']['pass']} |\n")
    md.append("\n---\n\nPHASE 9.2 — XFOOT PROSPECTIVE SHADOW ACTIVATION & EVIDENCE ACCUMULATION V1 TERMINÉE. "
               "SHADOW PROSPECTIF CONFIGURÉ ET ÉVALUÉ AVEC DONNÉES RÉELLES OU LIMITATION DOCUMENTÉE. "
               "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION.\n")
    return "".join(md)


def _write_reports(result: dict, run_id: str) -> None:
    outdir = REPO_ROOT / "reports" / "shadow" / "prospective"
    outdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    json_path = outdir / f"xfoot_prospective_shadow_{ts}.json"
    md_path = outdir / f"xfoot_prospective_shadow_{ts}.md"
    json_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    logger.info("Rapports écrits : %s / %s", json_path, md_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as-of", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resolve", action="store_true")
    parser.add_argument("--market", type=str, default="1X2")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args()
    main(args.as_of, args.dry_run, args.resolve, args.market, args.json, args.markdown)
    sys.exit(0)
