"""
scripts/internal_shadow_operation.py — Phase 11 : XFOOT CONTROLLED INTERNAL
OPERATION & REAL EVIDENCE ACCUMULATION V1.
=============================================================================
MODE_1_SHADOW_ONLY UNIQUEMENT — aucun paramètre --mode n'est exposé (§4 :
"imposé par configuration, aucune variable utilisateur ne doit pouvoir
contourner cette restriction"). Orchestration MINCE (§2) — réutilise
UNIQUEMENT des composants déjà existants :

    PREFLIGHT -> DISCOVER -> CAPTURE -> VERIFY -> MONITOR -> REPORT
    puis (si une observation Shadow est désormais résoluble) :
    RESOLVE -> VERIFY -> TRACK -> MONITOR -> REPORT

Réutilise TEL QUEL :
  - api/app/ai/shadow/tracking.py::ShadowDecisionStore (Phase 8K).
  - api/app/ai/shadow/prospective.py::run_prospective_capture (Phase 9.2) — DISCOVER/CAPTURE/VERIFY.
  - scripts/prospective_shadow.py::run_resolution (Phase 9.2) — RESOLVE.
  - api/app/ai/shadow/monitoring.py::compute_shadow_health (Phase 8N) — MONITOR.
  - api/app/ai/shadow/evidence.py (Phase 9.3) — evidence ledger étendu / breakdown / drift / model-version.
  - api/app/ai/shadow/watch.py::EvidenceHistoryStore/compute_evidence_snapshot/compute_evidence_trend/
    compute_blocker_evolution/filter_real_prospective_entries/readiness_blockers (Phase 9.5) — MÊME fichier
    d'historique que Phase 9.5, jamais un second historique parallèle (§22 : "réutiliser EvidenceHistoryStore").
  - api/app/ai/shadow/internal_operation.py (Phase 11, nouveau) — mode enforcement / MODE_2 evaluation / Phase 10 comparison.
  - api/app/ai/readiness/matrix.py::evaluate_production_readiness (Phase 9).
  - api/app/ai/readiness/human_review.py::classify_evidence_status/human_review_gate (Phase 10).
  - api/app/ai/safety/kill_switch.py / operations.py::run_preflight_safety (Phase 9.1/9.4).

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/internal_shadow_operation.py \
        [--as-of ISO] [--market M] [--capture] [--resolve] [--monitor] [--dry-run] [--json] [--markdown]
"""

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from sqlmodel import Session  # noqa: E402
from app.core.database import engine, init_db  # noqa: E402

from app.ai.shadow.tracking import ShadowDecisionStore  # noqa: E402
from app.ai.shadow.prospective import (  # noqa: E402
    run_prospective_capture, compute_evidence_ledger, classify_capture_quality,
    backup_store, restore_and_validate,
)
from app.ai.shadow.metrics import compute_shadow_track_record, value_tracking_status  # noqa: E402
from app.ai.shadow.monitoring import compute_shadow_health  # noqa: E402
from app.ai.shadow.evidence import (  # noqa: E402
    compute_full_evidence_ledger, compute_model_version_tracking, compute_breakdown,
    compute_temporal_drift, compute_data_gaps,
)
from app.ai.shadow.watch import (  # noqa: E402
    EvidenceHistoryStore, compute_evidence_snapshot, filter_real_prospective_entries,
    compute_evidence_trend, readiness_blockers, compute_blocker_evolution,
)
from app.ai.shadow.internal_operation import (  # noqa: E402
    OPERATING_MODE, assert_mode_1_only, evaluate_mode2_conditions, compare_to_phase10_baseline,
)
from app.ai.shadow.operations import run_preflight_safety  # noqa: E402
from app.ai.readiness.matrix import evaluate_production_readiness  # noqa: E402
from app.ai.readiness.human_review import classify_evidence_status, human_review_gate  # noqa: E402
from app.ai.safety.kill_switch import KillSwitchStore  # noqa: E402

import feature_engineering_walkforward as fewf  # noqa: E402 (réutilise snapshot_db_counts, jamais réimplémenté)
from prospective_shadow import run_resolution  # noqa: E402 (Phase 9.2 — RESOLVE réutilisé tel quel)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("internal_shadow_operation")
UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parent.parent
MARKETS = ("1X2", "BTTS", "OVER_UNDER_2_5")

REGRESSION_SUITES = [
    "test_model_selection.py", "test_track_record.py", "test_feature_registry.py", "test_feature_engineering_v1.py",
    "test_value_engine.py", "test_decision_layer.py", "test_end_to_end_pipeline.py", "test_shadow_operational.py",
    "test_historical_replay.py", "test_live_shadow_track.py", "test_shadow_monitoring.py",
    "test_production_readiness.py", "test_safety_controls.py", "test_prospective_shadow.py",
    "test_phase9_3.py", "test_phase9_4.py", "test_phase9_5.py", "test_phase10.py",
]


def run_existing_regression_suites() -> dict:
    """§41 : PASSED/FAILED/NOT_RUN explicite — jamais une suite non exécutée comptée comme PASS."""
    results = {}
    api_dir = REPO_ROOT / "api"
    for suite in REGRESSION_SUITES:
        proc = subprocess.run([sys.executable, suite], cwd=api_dir, capture_output=True, text=True)
        tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        results[suite] = {"status": "PASSED" if proc.returncode == 0 else "FAILED", "returncode": proc.returncode, "summary_line": tail, "pass": proc.returncode == 0}
    return results


def run_this_phase_tests() -> dict:
    api_dir = REPO_ROOT / "api"
    proc = subprocess.run([sys.executable, "test_phase11.py"], cwd=api_dir, capture_output=True, text=True)
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    return {"status": "PASSED" if proc.returncode == 0 else "FAILED", "returncode": proc.returncode, "summary_line": tail, "pass": proc.returncode == 0}


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


def find_latest_phase10_report(repo_root: Path) -> Optional[dict]:
    """§25/§39 : baseline RÉELLE — le dernier rapport Phase 10 déjà écrit sur disque, jamais une valeur
    supposée. None si aucun rapport n'existe encore (limitation honnêtement documentée, jamais fabriquée)."""
    outdir = repo_root / "reports" / "phase10"
    if not outdir.exists():
        return None
    candidates = sorted(outdir.glob("xfoot_phase10_*.json"))
    if not candidates:
        return None
    latest = candidates[-1]
    try:
        return json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Rapport Phase 10 illisible (%s) : %s — comparaison ignorée, jamais fabriquée.", latest, e)
        return None


def main(as_of_arg: str, market: str, do_capture: bool, do_resolve_flag: bool, do_monitor: bool,
         force_dry_run: bool, emit_json: bool, emit_markdown: bool) -> dict:
    assert_mode_1_only(OPERATING_MODE)  # §4 : défense en profondeur — jamais dérivé d'un argument utilisateur.

    run_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    generated_at = datetime.now(UTC).isoformat()
    as_of = datetime.fromisoformat(as_of_arg).replace(tzinfo=UTC) if as_of_arg else datetime.now(UTC)

    # §5/§23 : aucun mode -> lecture seule. --dry-run est TOUJOURS l'override le plus sûr.
    read_only = force_dry_run or not (do_capture or do_resolve_flag)
    do_resolve = do_resolve_flag and not read_only

    init_db()
    store = ShadowDecisionStore()  # reports/shadow/shadow_decision_store.json — MÊME store que Phase 8K-10.
    kill_switch_store = KillSwitchStore()  # LECTURE SEULE.
    history_store = EvidenceHistoryStore()  # reports/shadow/watch/evidence_snapshots.json — MÊME historique que Phase 9.5.

    with Session(engine) as session:
        db_before = fewf.snapshot_db_counts(session)

    # ---- PREFLIGHT (§3, réutilise Phase 9.4 tel quel). ----
    with Session(engine) as session:
        preflight = run_preflight_safety(session, store, kill_switch_store, as_of)

    if preflight["status"] == "FAIL":
        logger.error("PRE-FLIGHT SAFETY FAIL — STOP (%s). Aucune capture tentée (§3).", preflight["blocking"])
        with Session(engine) as session:
            db_after = fewf.snapshot_db_counts(session)
        status_short, diff_stat, diff_names, production_modified = _check_production_files_untouched(REPO_ROOT)
        result = {
            "run_id": run_id, "generated_at": generated_at, "phase": "11", "kind": "controlled_internal_operation_real_evidence_v1",
            "rule": "CONTROLLED INTERNAL OPERATION. MODE_1_SHADOW_ONLY. NO PRODUCTION ACTIVATION. NO MODE_2/3/4 ACTIVATION.",
            "as_of": as_of.isoformat(), "market": market, "read_only": True, "operating_mode": OPERATING_MODE,
            "preflight": preflight, "final_verdict": "BLOCKED", "human_review_status": "NOT_READY_FOR_HUMAN_REVIEW",
            "db_safety": {"before": db_before, "after": db_after, "unchanged": db_before == db_after},
            "git_status_short": status_short, "git_diff_stat": diff_stat, "git_diff_names": diff_names,
            "production_files_modified": production_modified,
            "mode": OPERATING_MODE, "production_activation": "BLOCKED", "no_user_betting_signal": True,
        }
        _write_reports(result, render_markdown_blocked(result))
        if emit_json:
            print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        print("PHASE 11 — XFOOT CONTROLLED INTERNAL OPERATION & REAL EVIDENCE ACCUMULATION V1 TERMINÉE. "
              "OPÉRATION INTERNE CONTRÔLÉE ÉVALUÉE SUR DONNÉES RÉELLES OU LIMITATIONS DOCUMENTÉES. "
              "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION HUMAINE.")
        return result

    # ---- §13/§24 : backup AVANT toute écriture réelle. ----
    backup_path = None
    backup_validated = None
    if not read_only:
        backup_dir = REPO_ROOT / "reports" / "shadow" / "internal_operation" / "backups"
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

    # ---- DISCOVER + CAPTURE + VERIFY (§5/§6/§7/§8). ----
    with Session(engine) as session:
        capture_outcome = run_prospective_capture(session, store, as_of, dry_run=read_only, market=market)

    # ---- RESOLVE + VERIFY (§14/§15) — pour les observations DÉJÀ capturées, désormais résolubles. ----
    with Session(engine) as session:
        resolution_summary = run_resolution(session, store) if do_resolve else {"skipped": "NOT_REQUESTED_OR_READ_ONLY"}

    entries = store.all()
    basic_ledger = compute_evidence_ledger(entries)
    capture_quality = classify_capture_quality(capture_outcome, entries)

    # ---- TRACK (§16/§17/§18/§19/§20) — REAL_PROSPECTIVE + RESOLVED uniquement. ----
    with Session(engine) as session:
        full_ledger = compute_full_evidence_ledger(session, entries)
        prospective_entries = filter_real_prospective_entries(session, entries)
        model_version_tracking = compute_model_version_tracking(entries)
        data_gaps = compute_data_gaps(session, entries)

    track_record = {m: compute_shadow_track_record(prospective_entries, market=m) for m in MARKETS}
    track_record["value_tracking"] = value_tracking_status(entries)
    temporal_drift = {m: compute_temporal_drift(prospective_entries, m) for m in MARKETS}
    breakdown = {m: compute_breakdown(prospective_entries, market=m) for m in MARKETS}
    track_record_sample_size = sum((tr.get("sample_size") or 0) for m, tr in track_record.items() if m in MARKETS)

    # ---- MONITOR (§21). ----
    with Session(engine) as session:
        health = compute_shadow_health(session, store, as_of)

    # ---- READINESS (§23, information ONLY — jamais une gate modifiée). ----
    with Session(engine) as session:
        readiness = evaluate_production_readiness(session, store, as_of)
    current_blockers = readiness_blockers(readiness)
    by_gate_status = {g.name: g.status for g in readiness.gates}

    # ---- MODE_2 EVALUATION (§19/§38) — DOCUMENTARY_ONLY, jamais activé. ----
    mode2_evaluation = evaluate_mode2_conditions(readiness)

    maturity = full_ledger["maturity_real_prospective_resolved"]
    evidence_status = classify_evidence_status(readiness)
    human_review_status = human_review_gate(maturity=maturity, blockers=current_blockers, readiness_verdict=readiness.final_verdict)

    # ---- LONGITUDINAL HISTORY + TREND + BLOCKER EVOLUTION (§22, MÊME historique que Phase 9.5). ----
    history_before = history_store.read()
    history_blockers = [h.get("blockers", []) for h in history_before]
    blocker_evolution = compute_blocker_evolution(history_blockers, current_blockers)
    snapshot = compute_evidence_snapshot(
        as_of=as_of, health=health, full_ledger=full_ledger, capture_outcome=capture_outcome,
        track_record_sample_size=track_record_sample_size, readiness_verdict=readiness.final_verdict,
    )
    snapshot["blockers"] = current_blockers
    history_after = history_store.append(snapshot)
    trend = compute_evidence_trend(history_after)

    # ---- COMPARISON WITH PHASE 10 (§25/§39). ----
    phase10_baseline = find_latest_phase10_report(REPO_ROOT)
    if phase10_baseline is not None:
        baseline_track_record_sample_size = sum((phase10_baseline.get("track_record", {}).get(m, {}) or {}).get("sample_size") or 0 for m in MARKETS)
        baseline_gate_statuses = {c["gate"]: c["status"] for c in phase10_baseline.get("checklist", [])}
        comparison_with_phase10 = compare_to_phase10_baseline(
            current_readiness_verdict=readiness.final_verdict, baseline_readiness_verdict=phase10_baseline.get("readiness_verdict", "UNKNOWN"),
            current_real_prospective_count=full_ledger["real_prospective_resolved_count"],
            baseline_real_prospective_count=(phase10_baseline.get("full_evidence_ledger", {}) or {}).get("real_prospective_resolved_count", 0),
            current_track_record_sample_size=track_record_sample_size, baseline_track_record_sample_size=baseline_track_record_sample_size,
            current_provenance_complete=full_ledger["provenance_complete"],
            baseline_provenance_complete=(phase10_baseline.get("full_evidence_ledger", {}) or {}).get("provenance_complete", 0),
            current_gate_statuses=by_gate_status, baseline_gate_statuses=baseline_gate_statuses,
        )
        comparison_with_phase10["baseline_run_id"] = phase10_baseline.get("run_id")
    else:
        comparison_with_phase10 = {"status": "NO_BASELINE_AVAILABLE", "reason": "Aucun rapport reports/phase10/*.json trouvé — comparaison non fabriquée."}

    with Session(engine) as session:
        db_after = fewf.snapshot_db_counts(session)
    db_safety = {"before": db_before, "after": db_after, "unchanged": db_before == db_after}

    regression = run_existing_regression_suites()
    this_phase_tests = run_this_phase_tests()
    status_short, diff_stat, diff_names, production_modified = _check_production_files_untouched(REPO_ROOT)

    tests_green = db_safety["unchanged"] and not production_modified and this_phase_tests["pass"] and all(r["pass"] for r in regression.values())

    # §44 : verdict — jamais PRODUCTION_READY. §35 : NO_DATA/INSUFFICIENT_REAL_DATA honnêtes, jamais forcés à mieux.
    if preflight["status"] == "FAIL":
        final_verdict = "BLOCKED"
    elif not tests_green:
        final_verdict = "NEEDS_FIXES"
    elif capture_outcome.get("blocked"):
        final_verdict = "BLOCKED"
    elif health["reality"]["future_fixtures"] == 0 and full_ledger["real_prospective_resolved_count"] == 0:
        final_verdict = "NO_DATA"
    elif capture_outcome["candidates"] == 0 or full_ledger["real_prospective_resolved_count"] == 0:
        final_verdict = "INSUFFICIENT_REAL_DATA"
    elif readiness.final_verdict == "NO_GO" and maturity not in ("TRACKING", "STATISTICALLY_INFORMATIVE"):
        final_verdict = "INSUFFICIENT_REAL_DATA"
    elif readiness.final_verdict == "NO_GO":
        final_verdict = "NO_GO"
    elif maturity == "STATISTICALLY_INFORMATIVE":
        final_verdict = "READY_FOR_HUMAN_REVIEW" if (not current_blockers and readiness.final_verdict in ("CONDITIONALLY_READY", "PRODUCTION_READY")) else "STATISTICALLY_INFORMATIVE"
    elif maturity == "TRACKING":
        final_verdict = "TRACKING"
    elif maturity == "EARLY_DATA":
        final_verdict = "EARLY_EVIDENCE"
    else:
        final_verdict = "INSUFFICIENT_REAL_DATA"

    result = {
        "run_id": run_id, "generated_at": generated_at, "phase": "11", "kind": "controlled_internal_operation_real_evidence_v1",
        "rule": "CONTROLLED INTERNAL OPERATION. MODE_1_SHADOW_ONLY. NO PRODUCTION ACTIVATION. NO MODE_2/3/4 ACTIVATION.",
        "as_of": as_of.isoformat(), "market": market, "read_only": read_only, "operating_mode": OPERATING_MODE,
        "requested_capture": do_capture, "requested_resolve": do_resolve_flag, "resolve_executed": do_resolve,
        "requested_monitor": do_monitor, "preflight": preflight,
        "capture_outcome": capture_outcome, "resolution_summary": resolution_summary,
        "evidence_ledger": basic_ledger, "full_evidence_ledger": full_ledger, "capture_quality": capture_quality,
        "track_record": track_record, "temporal_drift": temporal_drift, "breakdown": breakdown,
        "model_version_tracking": model_version_tracking, "data_gaps": data_gaps,
        "shadow_health_status": health["status"], "shadow_health": health,
        "readiness_verdict": readiness.final_verdict, "readiness_critical_failures": readiness.critical_gate_failures,
        "current_blockers": current_blockers, "mode2_evaluation": mode2_evaluation,
        "maturity": maturity, "evidence_status": evidence_status, "human_review_status": human_review_status,
        "evidence_snapshot": snapshot, "longitudinal_history_count": len(history_after),
        "evidence_trend": trend, "blocker_evolution": blocker_evolution, "comparison_with_phase10": comparison_with_phase10,
        "backup_path": str(backup_path) if backup_path else None, "backup_validated": backup_validated,
        "db_safety": db_safety, "existing_regression_suites": regression, "this_phase_tests": this_phase_tests,
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
        print(f"Verdict final : {final_verdict}  (readiness={readiness.final_verdict}, maturity={maturity}, human_review={human_review_status})")
        print(f"MODE_2 conditions met : {mode2_evaluation['conditions_met']} (DOCUMENTARY_ONLY, jamais activé)")
        print(f"Comparison with Phase 10 : {comparison_with_phase10.get('status', 'ok')}")
        print("git status --short :")
        print(status_short or "(clean)")
        print("git diff --stat :")
        print(diff_stat or "(no tracked file modified)")
        print("PHASE 11 — XFOOT CONTROLLED INTERNAL OPERATION & REAL EVIDENCE ACCUMULATION V1 TERMINÉE. "
              "OPÉRATION INTERNE CONTRÔLÉE ÉVALUÉE SUR DONNÉES RÉELLES OU LIMITATIONS DOCUMENTÉES. "
              "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION HUMAINE.")
        print("=" * 80)
    return result


def render_markdown_blocked(result: dict) -> str:
    md = ["# XFOOT PHASE 11\n\n# CONTROLLED INTERNAL OPERATION & REAL EVIDENCE\n"]
    md.append(f"\n## 1. Executive Summary\n\nRun id `{result['run_id']}` — {result['generated_at']}.\n\n"
               f"**PRE-FLIGHT SAFETY FAIL — STOP.** Aucune opération DISCOVER/CAPTURE/RESOLVE n'a été tentée (§3).\n")
    md.append(f"\n## 3. Preflight\n\n{result['preflight']}\n")
    md.append("\n## 4-27. Discovery / Captures / Temporal Integrity / Provenance / Consistency / Resolution / "
               "Track Record / Maturity / Drift / Model Versions / League-Market / Monitoring / Evidence History / "
               "Blocker Evolution / Readiness / MODE_2 Evaluation / Safety / Rollback / DB Safety / Production Isolation / "
               "Human Review / Comparison With Phase 10 / Data Gaps / Limitations\n\n"
               "Non applicables — le pré-vol a stoppé le run avant toute observation (§3).\n")
    md.append("\n## 28. Final Verdict\n\n**BLOCKED**\n")
    md.append("\n---\n\nPHASE 11 — XFOOT CONTROLLED INTERNAL OPERATION & REAL EVIDENCE ACCUMULATION V1 TERMINÉE. "
               "OPÉRATION INTERNE CONTRÔLÉE ÉVALUÉE SUR DONNÉES RÉELLES OU LIMITATIONS DOCUMENTÉES. "
               "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION HUMAINE.\n")
    return "".join(md)


def render_markdown(result: dict) -> str:
    md = ["# XFOOT PHASE 11\n\n# CONTROLLED INTERNAL OPERATION & REAL EVIDENCE\n"]
    md.append(f"\n## 1. Executive Summary\n\nRun id `{result['run_id']}` — {result['generated_at']}. as_of={result['as_of']}, "
               f"read_only={result['read_only']}\n\n**Verdict : {result['final_verdict']}** — {result['human_review_status']}\n")
    md.append(f"\n## 2. Operating Mode\n\n{result['operating_mode']} — production_activation={result['production_activation']} "
               f"(MODE_2/3/4 refusés structurellement, §4)\n")
    md.append(f"\n## 3. Preflight\n\n{result['preflight']}\n")
    md.append(f"\n## 4. Discovery\n\ncandidates={result['capture_outcome'].get('candidates')}\n")
    md.append(f"\n## 5. Captures\n\n{result['capture_outcome']}\n")
    md.append("\n## 6. Temporal Integrity\n\nVoir prospective_status par record capturé — vocabulaire réutilisé "
               "(UNKNOWN/CONSISTENT_WITH_PLACEHOLDER_KICKOFF/TIMING_VIOLATION).\n")
    md.append(f"\n## 7. Provenance\n\ncomplete={result['full_evidence_ledger']['provenance_complete']}, "
               f"incomplete={result['full_evidence_ledger']['provenance_incomplete']}, unknown={result['full_evidence_ledger']['provenance_unknown']}\n")
    md.append(f"\n## 8. Consistency\n\nmismatches={result['capture_outcome'].get('mismatches')}\n")
    md.append(f"\n## 9. Resolution\n\n{result['resolution_summary']}\n")
    md.append(f"\n## 10. Track Record\n\n(REAL_PROSPECTIVE + RESOLVED uniquement)\n\n{result['track_record']}\n")
    md.append(f"\n## 11. Maturity\n\n{result['maturity']}\n")
    md.append(f"\n## 12. Drift\n\n{result['temporal_drift']}\n")
    md.append(f"\n## 13. Model Versions\n\n{result['model_version_tracking']}\n")
    md.append(f"\n## 14. League / Market\n\n{result['breakdown']}\n")
    md.append(f"\n## 15. Monitoring\n\nstatus={result['shadow_health_status']}\n\nalerts={result['shadow_health'].get('alerts')}\n")
    md.append(f"\n## 16. Evidence History\n\n{result['longitudinal_history_count']} snapshot(s) au total (append-only, "
               f"MÊME historique que Phase 9.5) — reports/shadow/watch/evidence_snapshots.json\n\ntrend={result['evidence_trend']}\n")
    md.append(f"\n## 17. Blocker Evolution\n\n{result['blocker_evolution']}\n")
    md.append(f"\n## 18. Readiness\n\nverdict={result['readiness_verdict']} (critical failures: {result['readiness_critical_failures']})\n")
    md.append(f"\n## 19. MODE_2 Evaluation\n\n(DOCUMENTARY_ONLY — jamais activé)\n\n{result['mode2_evaluation']}\n")
    md.append(f"\n## 20. Safety\n\npreflight={result['preflight']['status']}, kill_switch={result['preflight']['checks'].get('kill_switch')}\n")
    md.append("\n## 21. Rollback\n\nDémonstration empirique sur DB ISOLÉE uniquement — voir api/test_phase11.py "
               f"(idempotence/audit trail), api/test_phase10.py, api/test_safety_controls.py. this_phase_tests.pass={result['this_phase_tests']['pass']}\n")
    db = result["db_safety"]
    md.append(f"\n## 22. DB Safety\n\nBefore: {db['before']}\n\nAfter: {db['after']}\n\nUnchanged: **{db['unchanged']}**\n")
    md.append(f"\n## 23. Production Isolation\n\nmode={result['mode']}, production_activation={result['production_activation']}. "
               "Ce script n'appelle jamais capture/resolve de production, execute_rollback/apply_promotion/train.\n")
    md.append(f"\n## 24. Human Review\n\n**{result['human_review_status']}** — aucune activation automatique même si READY (§37).\n")
    md.append(f"\n## 25. Comparison With Phase 10\n\n{result['comparison_with_phase10']}\n")
    md.append(f"\n## 26. Data Gaps\n\n{result['data_gaps']}\n")
    md.append(
        "\n## 27. Limitations\n\n"
        "**WHAT IS PROVEN** : " + str([e["gate"] for e in result["evidence_status"]["proven"]]) + "\n\n"
        "**WHAT IS OBSERVED** : " + str([e["gate"] for e in result["evidence_status"]["observed"]]) + "\n\n"
        "**WHAT IS UNKNOWN** : " + str([e["gate"] for e in result["evidence_status"]["unknown"]]) + "\n\n"
        "**WHAT IS BLOCKED** : " + str([e["gate"] for e in result["evidence_status"]["blocked"]]) + "\n\n"
        "**WHAT CHANGED SINCE PHASE 10** : " + str(result["comparison_with_phase10"]) + "\n\n"
        "**WHAT IS REQUIRED NEXT** : " + str(result["evidence_status"]["required_next"]) + "\n\n"
        "- Aucune heure de coup d'envoi réelle n'est persistée — toute classification prospective reste qualifiée.\n"
        "- value_tracking reste NOT_AVAILABLE tant qu'aucune odds TEMPORALLY_VERIFIED n'existe.\n"
        "- MODE_2/3/4 restent structurellement refusés (§4/§45) quel que soit le volume de données accumulées.\n"
    )
    md.append(f"\n## 28. Final Verdict\n\n**{result['final_verdict']}** — {result['human_review_status']}\n")
    md.append("\n---\n\n### EXISTING REGRESSION SUITES\n\n| Suite | Status |\n|---|---|\n")
    for suite, r in result["existing_regression_suites"].items():
        md.append(f"| {suite} | {r['status']} |\n")
    md.append(f"\n| api/test_phase11.py (this phase) | {result['this_phase_tests']['status']} |\n")
    md.append("\n---\n\nPHASE 11 — XFOOT CONTROLLED INTERNAL OPERATION & REAL EVIDENCE ACCUMULATION V1 TERMINÉE. "
               "OPÉRATION INTERNE CONTRÔLÉE ÉVALUÉE SUR DONNÉES RÉELLES OU LIMITATIONS DOCUMENTÉES. "
               "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION HUMAINE.\n")
    return "".join(md)


def _write_reports(result: dict, markdown: str) -> None:
    outdir = REPO_ROOT / "reports" / "phase11"
    outdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    json_path = outdir / f"xfoot_phase11_{ts}.json"
    md_path = outdir / f"xfoot_phase11_{ts}.md"
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
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args()
    # §4 : AUCUN de ces arguments ne porte le mode d'exploitation — celui-ci reste la constante OPERATING_MODE,
    # jamais dérivée de la CLI (aucun --mode n'existe, volontairement).
    main(args.as_of, args.market, args.capture, args.resolve, args.monitor, args.dry_run, args.json, args.markdown)
    sys.exit(0)
