"""
scripts/shadow_operations.py — Phase 9.4 : XFOOT REAL PROSPECTIVE SHADOW
OPERATIONS & EVIDENCE COLLECTION V1.
=============================================================================
OPERATIONAL SHADOW — NO PRODUCTION ACTIVATION. Orchestre UNIQUEMENT des
composants déjà existants (§2/§1 : "ne pas réimplémenter") :

    DISCOVER -> VALIDATE -> CAPTURE -> VERIFY -> MONITOR -> REPORT

Réutilise TEL QUEL :
  - api/app/ai/shadow/live.py::discover_live_candidates (Phase 8M) — DISCOVER.
  - api/app/ai/shadow/prospective.py::run_prospective_capture (Phase 9.2) —
    DISCOVER+VALIDATE+CAPTURE (verrou exclusif, dédoublonnage, mismatch
    check déjà inclus — ce runner ne les réimplémente jamais).
  - scripts/prospective_shadow.py::run_resolution (Phase 9.2) — RESOLVE.
  - api/app/ai/shadow/monitoring.py::compute_shadow_health (Phase 8N) — MONITOR/VERIFY.
  - api/app/ai/shadow/operations.py::run_preflight_safety (Phase 9.4, nouveau) — PRE-FLIGHT.
  - api/app/ai/shadow/evidence.py (Phase 9.3) — Evidence Ledger étendu / data gaps.
  - api/app/ai/readiness/matrix.py::evaluate_production_readiness (Phase 9) — READINESS.
  - api/app/ai/safety/kill_switch.py::KillSwitchStore (Phase 9.1, LECTURE SEULE).

Modes (§26) : --dry-run / --capture / --resolve / --monitor / --report.
Aucun mode fourni -> lecture seule (0 write, §27). --dry-run force la
lecture seule même si --capture/--resolve sont également fournis (override
le plus sûr, jamais l'inverse).

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/shadow_operations.py \
        [--as-of ISO] [--market M] [--capture] [--resolve] [--monitor] [--json] [--markdown]
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
from app.ai.shadow.prospective import (  # noqa: E402
    run_prospective_capture, compute_evidence_ledger, classify_capture_quality,
    backup_store, restore_and_validate, compute_readiness_impact,
)
from app.ai.shadow.metrics import compute_shadow_track_record, value_tracking_status  # noqa: E402
from app.ai.shadow.monitoring import compute_shadow_health  # noqa: E402
from app.ai.shadow.evidence import compute_data_gaps  # noqa: E402
from app.ai.shadow.operations import run_preflight_safety, summarize_multi_as_of_runs, derive_final_verdict  # noqa: E402
from app.ai.readiness.matrix import evaluate_production_readiness  # noqa: E402
from app.ai.safety.kill_switch import KillSwitchStore  # noqa: E402

import feature_engineering_walkforward as fewf  # noqa: E402 (réutilise snapshot_db_counts, jamais réimplémenté)
from prospective_shadow import run_resolution  # noqa: E402 (Phase 9.2 — RESOLVE réutilisé tel quel, jamais réimplémenté)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("shadow_operations")
UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parent.parent

REGRESSION_SUITES = [
    "test_model_selection.py", "test_track_record.py", "test_feature_registry.py", "test_feature_engineering_v1.py",
    "test_value_engine.py", "test_decision_layer.py", "test_end_to_end_pipeline.py", "test_shadow_operational.py",
    "test_historical_replay.py", "test_live_shadow_track.py", "test_shadow_monitoring.py",
    "test_production_readiness.py", "test_safety_controls.py", "test_prospective_shadow.py", "test_phase9_3.py",
]


def run_existing_regression_suites() -> dict:
    """§36 : jamais une suite non exécutée comptée comme PASS — `pass` défaut False."""
    results = {}
    api_dir = REPO_ROOT / "api"
    for suite in REGRESSION_SUITES:
        proc = subprocess.run([sys.executable, suite], cwd=api_dir, capture_output=True, text=True)
        tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        results[suite] = {"returncode": proc.returncode, "summary_line": tail, "pass": proc.returncode == 0}
    return results


def run_this_phase_tests() -> dict:
    api_dir = REPO_ROOT / "api"
    proc = subprocess.run([sys.executable, "test_phase9_4.py"], cwd=api_dir, capture_output=True, text=True)
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


def main(as_of_arg: str, market: str, do_capture: bool, do_resolve_flag: bool, do_monitor: bool,
         force_dry_run: bool, emit_json: bool, emit_markdown: bool) -> dict:
    run_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    generated_at = datetime.now(UTC).isoformat()
    as_of = datetime.fromisoformat(as_of_arg).replace(tzinfo=UTC) if as_of_arg else datetime.now(UTC)

    # §26/§27 : aucun mode -> lecture seule. --dry-run est TOUJOURS l'override le plus sûr.
    read_only = force_dry_run or not (do_capture or do_resolve_flag)
    do_resolve = do_resolve_flag and not read_only

    init_db()
    store = ShadowDecisionStore()  # reports/shadow/shadow_decision_store.json — MÊME store que Phase 8K-9.3.
    kill_switch_store = KillSwitchStore()  # reports/safety/kill_switch_state.json — LECTURE SEULE dans cette phase.

    with Session(engine) as session:
        db_before = fewf.snapshot_db_counts(session)

    # ---- PRE-FLIGHT (§3/§40) : GO/STOP unique, AVANT toute opération. ----
    with Session(engine) as session:
        preflight = run_preflight_safety(session, store, kill_switch_store, as_of)

    if preflight["status"] == "FAIL":
        logger.error("PRE-FLIGHT SAFETY FAIL — STOP (%s). Aucune opération tentée (§3 : aucun contournement).", preflight["blocking"])
        with Session(engine) as session:
            db_after = fewf.snapshot_db_counts(session)
        status_short, diff_stat, diff_names, production_modified = _check_production_files_untouched(REPO_ROOT)
        result = {
            "run_id": run_id, "generated_at": generated_at, "phase": "9.4", "kind": "real_prospective_shadow_operations_v1",
            "rule": "OPERATIONAL SHADOW. MODE_1_SHADOW_ONLY. NO PRODUCTION ACTIVATION. NO NEW PREDICTION. NO NETWORK.",
            "as_of": as_of.isoformat(), "market": market, "read_only": True, "requested_capture": do_capture,
            "requested_resolve": do_resolve_flag, "requested_monitor": do_monitor,
            "preflight": preflight, "final_verdict": "BLOCKED",
            "db_safety": {"before": db_before, "after": db_after, "unchanged": db_before == db_after},
            "git_status_short": status_short, "git_diff_stat": diff_stat, "git_diff_names": diff_names,
            "production_files_modified": production_modified,
            "mode": "MODE_1_SHADOW_ONLY", "production_activation": "BLOCKED", "no_user_betting_signal": True,
        }
        _write_reports(result, render_markdown_blocked(result))
        if emit_json:
            print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        print("PHASE 9.4 — XFOOT REAL PROSPECTIVE SHADOW OPERATIONS & EVIDENCE COLLECTION V1 TERMINÉE. "
              "OPÉRATIONS SHADOW PROSPECTIVES EXÉCUTÉES OU LIMITATION DOCUMENTÉE. "
              "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION.")
        return result

    # ---- §27 : backup AVANT toute écriture réelle. ----
    backup_path = None
    backup_validated = None
    if not read_only:
        backup_dir = REPO_ROOT / "reports" / "shadow" / "operations" / "backups"
        backup_path = backup_store(store, backup_dir)
        tmp_restore_path = backup_dir / f"_validate_{run_id}.json"
        try:
            restore_and_validate(backup_path, tmp_restore_path)
            backup_validated = True
        except ValueError as e:
            backup_validated = False
            logger.error("Sauvegarde non restaurable : %s", e)
        finally:
            tmp_restore_path.unlink(missing_ok=True)

    # ---- DISCOVER + VALIDATE + CAPTURE (§4/§5/§6/§8/§9/§10/§11) ----
    with Session(engine) as session:
        capture_outcome = run_prospective_capture(session, store, as_of, dry_run=read_only, market=market)

    # ---- RESOLVE (§12/§13/§29) ----
    with Session(engine) as session:
        resolution_summary = run_resolution(session, store) if do_resolve else {"skipped": "NOT_REQUESTED_OR_READ_ONLY"}

    entries = store.all()
    evidence_ledger = compute_evidence_ledger(entries)
    capture_quality = classify_capture_quality(capture_outcome, entries)
    multi_as_of_summary = summarize_multi_as_of_runs(entries)
    track_record = {
        "1X2": compute_shadow_track_record(entries, market="1X2"),
        "BTTS": compute_shadow_track_record(entries, market="BTTS"),
        "OVER_UNDER_2_5": compute_shadow_track_record(entries, market="OVER_UNDER_2_5"),
        "value_tracking": value_tracking_status(entries),
    }

    # ---- MONITOR / VERIFY (§17/§30) — read-only, réutilise Phase 8N tel quel. ----
    with Session(engine) as session:
        health = compute_shadow_health(session, store, as_of)

    # ---- READINESS REASSESSMENT (§18/§45) — before = celle du pré-vol, after = fraîche. ----
    readiness_before = preflight["readiness_assessment"]
    with Session(engine) as session:
        readiness_after = evaluate_production_readiness(session, store, as_of)
    readiness_impact = compute_readiness_impact(readiness_before.gates, readiness_after.gates) if readiness_before else None

    with Session(engine) as session:
        data_gaps = compute_data_gaps(session, entries)

    with Session(engine) as session:
        db_after = fewf.snapshot_db_counts(session)
    db_safety = {"before": db_before, "after": db_after, "unchanged": db_before == db_after}

    regression = run_existing_regression_suites()
    this_phase_tests = run_this_phase_tests()
    status_short, diff_stat, diff_names, production_modified = _check_production_files_untouched(REPO_ROOT)

    tests_green = db_safety["unchanged"] and not production_modified and this_phase_tests["pass"] and all(r["pass"] for r in regression.values())

    maturity = evidence_ledger["maturity"]
    blockers_after = [g.name for g in readiness_after.gates if g.critical and g.status != "PASS"]

    # §48 : verdict — fonction PURE réutilisée telle quelle (api/app/ai/shadow/operations.py), jamais un
    # if/elif dupliqué ici. Jamais PRODUCTION_READY (§49 — absent de FINAL_VERDICTS).
    final_verdict = derive_final_verdict(
        preflight_status=preflight["status"], tests_green=tests_green, capture_blocked=bool(capture_outcome.get("blocked")),
        candidates=capture_outcome["candidates"], total_real_observations=evidence_ledger["total_real_observations"],
        maturity=maturity, blockers_after=blockers_after, readiness_after_verdict=readiness_after.final_verdict,
    )

    result = {
        "run_id": run_id, "generated_at": generated_at, "phase": "9.4", "kind": "real_prospective_shadow_operations_v1",
        "rule": "OPERATIONAL SHADOW. MODE_1_SHADOW_ONLY. NO PRODUCTION ACTIVATION. NO NEW PREDICTION. NO NETWORK.",
        "as_of": as_of.isoformat(), "market": market, "read_only": read_only,
        "requested_capture": do_capture, "requested_resolve": do_resolve_flag, "resolve_executed": do_resolve,
        "requested_monitor": do_monitor,  # §30 : purement déclaratif — MONITOR est déjà toujours read-only et toujours inclus (compute_shadow_health), qu'il soit demandé ou non.
        "preflight": preflight,
        "capture_outcome": capture_outcome, "resolution_summary": resolution_summary,
        "evidence_ledger": evidence_ledger, "capture_quality": capture_quality, "multi_as_of_summary": multi_as_of_summary,
        "track_record": track_record, "shadow_health_status": health["status"], "shadow_health": health,
        "readiness_impact": readiness_impact,
        "readiness_before_verdict": readiness_before.final_verdict if readiness_before else "NOT_EVALUATED",
        "readiness_after_verdict": readiness_after.final_verdict, "readiness_after_critical_failures": blockers_after,
        "data_gaps": data_gaps,
        "backup_path": str(backup_path) if backup_path else None, "backup_validated": backup_validated,
        "db_safety": db_safety, "existing_regression_suites": regression, "this_phase_tests": this_phase_tests,
        "git_status_short": status_short, "git_diff_stat": diff_stat, "git_diff_names": diff_names,
        "production_files_modified": production_modified, "tests_green": tests_green, "final_verdict": final_verdict,
        "the_odds_api_status": "SUPPORT_REQUIRED — non appelé dans cette phase.", "no_user_betting_signal": True,
        "mode": "MODE_1_SHADOW_ONLY", "production_activation": "BLOCKED",
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
        print(f"Verdict final : {final_verdict}  (maturity={maturity})")
        print(f"Capture : {capture_outcome.get('captured', 0)} capturé(s), {capture_outcome.get('candidates', 0)} candidat(s), "
              f"blocked={capture_outcome.get('blocked')}, read_only={read_only}")
        print(f"Readiness : before={result['readiness_before_verdict']} -> after={result['readiness_after_verdict']}")
        print("git status --short :")
        print(status_short or "(clean)")
        print("git diff --stat :")
        print(diff_stat or "(no tracked file modified)")
        print("PHASE 9.4 — XFOOT REAL PROSPECTIVE SHADOW OPERATIONS & EVIDENCE COLLECTION V1 TERMINÉE. "
              "OPÉRATIONS SHADOW PROSPECTIVES EXÉCUTÉES OU LIMITATION DOCUMENTÉE. "
              "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION.")
        print("=" * 80)
    return result


def render_markdown_blocked(result: dict) -> str:
    md = ["# XFOOT PHASE 9.4\n\n# REAL PROSPECTIVE SHADOW OPERATIONS\n"]
    md.append(f"\n## 1. Executive Summary\n\nRun id `{result['run_id']}` — {result['generated_at']}.\n\n"
               f"**PRE-FLIGHT SAFETY FAIL — STOP.** Aucune opération DISCOVER/CAPTURE/RESOLVE/MONITOR n'a été "
               f"tentée (§3 : aucune tentative de contournement).\n")
    md.append(f"\n## 3. Pre-Flight Safety\n\n{result['preflight']}\n")
    md.append("\n## 4-21. Candidates / Captures / Rejections / Temporal Integrity / Production Consistency / "
               "Provenance / Resolutions / Conflicts / Track Record / Maturity / Monitoring / Evidence Ledger / "
               "Readiness / Data Gaps / Alerts / DB Safety / Production Isolation / Limitations\n\n"
               "Non applicables — le pré-vol a stoppé le run avant toute opération DISCOVER/CAPTURE/RESOLVE/MONITOR (§3).\n")
    md.append("\n## 22. Verdict\n\n**BLOCKED**\n")
    md.append("\n---\n\nPHASE 9.4 — XFOOT REAL PROSPECTIVE SHADOW OPERATIONS & EVIDENCE COLLECTION V1 TERMINÉE. "
               "OPÉRATIONS SHADOW PROSPECTIVES EXÉCUTÉES OU LIMITATION DOCUMENTÉE. "
               "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION.\n")
    return "".join(md)


def render_markdown(result: dict) -> str:
    md = ["# XFOOT PHASE 9.4\n\n# REAL PROSPECTIVE SHADOW OPERATIONS\n"]
    md.append(f"\n## 1. Executive Summary\n\nRun id `{result['run_id']}` — {result['generated_at']}. as_of={result['as_of']}, "
               f"read_only={result['read_only']}\n\n**Verdict : {result['final_verdict']}**\n")
    md.append(f"\n## 2. Operating Mode\n\n{result['mode']} — production_activation={result['production_activation']}\n")
    md.append(f"\n## 3. Pre-Flight Safety\n\n{result['preflight']}\n")
    md.append(f"\n## 4. Candidates\n\ncandidates={result['capture_outcome'].get('candidates')}\n")
    md.append(f"\n## 5. Captures\n\n{result['capture_outcome']}\n")
    md.append(f"\n## 6. Rejections\n\nrejected={result['capture_outcome'].get('rejected')}\n\nmismatches={result['capture_outcome'].get('mismatches')}\n\n"
               f"missed_captures (Phase 8N categories, réutilisées telles quelles)={result['shadow_health'].get('missed_captures_detail')}\n")
    md.append("\n## 7. Temporal Integrity\n\nVoir prospective_status par record capturé dans capture_outcome.captured_records — "
               "vocabulaire réutilisé (UNKNOWN/CONSISTENT_WITH_PLACEHOLDER_KICKOFF/TIMING_VIOLATION), jamais un REAL_PROSPECTIVE non qualifié.\n")
    md.append(f"\n## 8. Production Consistency\n\nmismatches détectés (jamais corrigés silencieusement) : {result['capture_outcome'].get('mismatches')}\n")
    md.append(f"\n## 9. Provenance\n\ndistinct_models={result['evidence_ledger']['distinct_models']}\n")
    md.append(f"\n## 10. Resolutions\n\n{result['resolution_summary']}\n")
    md.append(f"\n## 11. Conflicts\n\nconflicts={result['evidence_ledger'].get('conflicts')}\n")
    md.append(f"\n## 12. Track Record\n\n{result['track_record']}\n")
    md.append(f"\n## 13. Maturity\n\n{result['evidence_ledger']['maturity']}\n")
    md.append(f"\n## 14. Monitoring\n\nstatus={result['shadow_health_status']}\n\nalerts={result['shadow_health'].get('alerts')}\n")
    md.append(f"\n## 15. Evidence Ledger\n\n{result['evidence_ledger']}\n\nmulti_as_of_summary={result['multi_as_of_summary']}\n")
    md.append(f"\n## 16. Readiness\n\nBEFORE={result['readiness_before_verdict']} -> AFTER={result['readiness_after_verdict']} "
               f"(critical failures : {result['readiness_after_critical_failures']})\n\n{result['readiness_impact']}\n")
    md.append(f"\n## 17. Data Gaps\n\n{result['data_gaps']}\n")
    md.append(f"\n## 18. Alerts\n\n{result['shadow_health'].get('alerts')}\n")
    db = result["db_safety"]
    md.append(f"\n## 19. DB Safety\n\nBefore: {db['before']}\n\nAfter: {db['after']}\n\nUnchanged: **{db['unchanged']}**\n")
    md.append(f"\n## 20. Production Isolation\n\nproduction_activation={result['production_activation']} — "
               f"écritures limitées au Shadow Store (§28/§37) ; kill_switch/readiness lus, jamais modifiés (§18/§40).\n")
    md.append(
        "\n## 21. Limitations\n\n"
        "- Aucune heure de coup d'envoi réelle n'est persistée (ModelPrediction.match_date typé `date`) — toute "
        "classification 'prospective'/'fenêtre' reste qualifiée (CONSISTENT_WITH_PLACEHOLDER_KICKOFF), jamais une preuve à heure exacte.\n"
        "- value_tracking reste NOT_AVAILABLE tant qu'aucune odds TEMPORALLY_VERIFIED n'existe (The Odds API SUPPORT_REQUIRED).\n"
        "- Le multi-as_of (§7) n'est constatable qu'entre exécutions séparées de ce runner — jamais fabriqué au sein d'un seul run.\n"
        "- MODE_1_SHADOW_ONLY reste actif quel que soit le volume de données réelles accumulées cette phase.\n"
    )
    md.append(f"\n## 22. Verdict\n\n**{result['final_verdict']}**\n")
    md.append("\n---\n\n### EXISTING REGRESSION SUITES\n\n| Suite | Pass |\n|---|---|\n")
    for suite, r in result["existing_regression_suites"].items():
        md.append(f"| {suite} | {r['pass']} |\n")
    md.append(f"\n| test_phase9_4.py (this phase) | {result['this_phase_tests']['pass']} |\n")
    md.append("\n---\n\nPHASE 9.4 — XFOOT REAL PROSPECTIVE SHADOW OPERATIONS & EVIDENCE COLLECTION V1 TERMINÉE. "
               "OPÉRATIONS SHADOW PROSPECTIVES EXÉCUTÉES OU LIMITATION DOCUMENTÉE. "
               "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION.\n")
    return "".join(md)


def _write_reports(result: dict, markdown: str) -> None:
    outdir = REPO_ROOT / "reports" / "phase9_4"
    outdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    json_path = outdir / f"xfoot_phase9_4_{ts}.json"
    md_path = outdir / f"xfoot_phase9_4_{ts}.md"
    json_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")
    logger.info("Rapports écrits : %s / %s", json_path, md_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as-of", type=str, default=None)
    parser.add_argument("--market", type=str, default="1X2")
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--resolve", action="store_true")
    parser.add_argument("--monitor", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", action="store_true")  # reports sont TOUJOURS écrits (§31) — flag conservé pour compatibilité CLI/§26, purement documentaire
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args()
    main(args.as_of, args.market, args.capture, args.resolve, args.monitor, args.dry_run, args.json, args.markdown)
    sys.exit(0)
