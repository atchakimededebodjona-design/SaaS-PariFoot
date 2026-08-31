"""
scripts/shadow_evidence_watch.py — Phase 9.5 : XFOOT SHADOW EVIDENCE WATCH &
LONGITUDINAL TRACKING V1.
=============================================================================
CONTINUOUS EVIDENCE — NO PRODUCTION ACTIVATION. Couche d'OBSERVATION légère,
PAS un second moteur Shadow. Orchestre UNIQUEMENT des composants déjà
existants (§1/§2 : "ne pas dupliquer") :

    PREFLIGHT -> DISCOVER -> INSPECT -> EVIDENCE -> TRACK RECORD -> MONITOR
    -> READINESS -> REPORT

Réutilise TEL QUEL :
  - api/app/ai/shadow/tracking.py::ShadowDecisionStore (Phase 8K/8M).
  - api/app/ai/shadow/prospective.py::run_prospective_capture (Phase 9.2) — DISCOVER/VALIDATE/CAPTURE.
  - scripts/prospective_shadow.py::run_resolution (Phase 9.2) — RESOLVE.
  - api/app/ai/shadow/monitoring.py::compute_shadow_health (Phase 8N) — MONITOR.
  - api/app/ai/shadow/evidence.py (Phase 9.3) — evidence ledger étendu / breakdown / model-version / data gaps.
  - api/app/ai/shadow/watch.py (Phase 9.5, nouveau) — snapshot / historique longitudinal / tendance / blocker evolution.
  - api/app/ai/shadow/operations.py::run_preflight_safety (Phase 9.4) — PRE-FLIGHT, réutilisé tel quel
    (les contrôles §3 de cette phase sont IDENTIQUES à ceux de Phase 9.4).
  - api/app/ai/readiness/matrix.py::evaluate_production_readiness (Phase 9) — READINESS.
  - api/app/ai/safety/guards.py::can_activate_production / kill_switch.KillSwitchStore (Phase 9.1, LECTURE SEULE).

Modes (§5/§6/§23) : par défaut LECTURE SEULE (dry-run implicite). --capture/
--resolve activent une écriture réelle (Shadow Store uniquement) seulement
si demandés explicitement ; --dry-run reste l'override le plus sûr.

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/shadow_evidence_watch.py \
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
    backup_store, restore_and_validate,
)
from app.ai.shadow.metrics import compute_shadow_track_record, value_tracking_status  # noqa: E402
from app.ai.shadow.monitoring import compute_shadow_health  # noqa: E402
from app.ai.shadow.evidence import (  # noqa: E402
    compute_full_evidence_ledger, compute_model_version_tracking, compute_breakdown, compute_data_gaps,
)
from app.ai.shadow.watch import (  # noqa: E402
    EvidenceHistoryStore, compute_evidence_snapshot, filter_real_prospective_entries,
    compute_evidence_trend, readiness_blockers, compute_blocker_evolution, derive_watch_verdict,
)
from app.ai.shadow.operations import run_preflight_safety  # noqa: E402
from app.ai.readiness.matrix import evaluate_production_readiness  # noqa: E402
from app.ai.safety.kill_switch import KillSwitchStore  # noqa: E402
from app.ai.safety.guards import can_activate_production  # noqa: E402

import feature_engineering_walkforward as fewf  # noqa: E402 (réutilise snapshot_db_counts, jamais réimplémenté)
from prospective_shadow import run_resolution  # noqa: E402 (Phase 9.2 — RESOLVE réutilisé tel quel)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("shadow_evidence_watch")
UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parent.parent
MARKETS = ("1X2", "BTTS", "OVER_UNDER_2_5")

REGRESSION_SUITES = [
    "test_model_selection.py", "test_track_record.py", "test_feature_registry.py", "test_feature_engineering_v1.py",
    "test_value_engine.py", "test_decision_layer.py", "test_end_to_end_pipeline.py", "test_shadow_operational.py",
    "test_historical_replay.py", "test_live_shadow_track.py", "test_shadow_monitoring.py",
    "test_production_readiness.py", "test_safety_controls.py", "test_prospective_shadow.py",
    "test_phase9_3.py", "test_phase9_4.py",
]


def run_existing_regression_suites() -> dict:
    """§33 : jamais une suite non exécutée comptée comme PASS — `pass` défaut False."""
    results = {}
    api_dir = REPO_ROOT / "api"
    for suite in REGRESSION_SUITES:
        proc = subprocess.run([sys.executable, suite], cwd=api_dir, capture_output=True, text=True)
        tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        results[suite] = {"returncode": proc.returncode, "summary_line": tail, "pass": proc.returncode == 0}
    return results


def run_this_phase_tests() -> dict:
    api_dir = REPO_ROOT / "api"
    proc = subprocess.run([sys.executable, "test_phase9_5.py"], cwd=api_dir, capture_output=True, text=True)
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

    # §5/§23 : aucun mode -> lecture seule. --dry-run est TOUJOURS l'override le plus sûr.
    read_only = force_dry_run or not (do_capture or do_resolve_flag)
    do_resolve = do_resolve_flag and not read_only

    init_db()
    store = ShadowDecisionStore()  # reports/shadow/shadow_decision_store.json — MÊME store que Phase 8K-9.4.
    kill_switch_store = KillSwitchStore()  # LECTURE SEULE dans cette phase.
    history_store = EvidenceHistoryStore()  # reports/shadow/watch/evidence_snapshots.json — nouveau, Phase 9.5.

    with Session(engine) as session:
        db_before = fewf.snapshot_db_counts(session)

    # ---- PRE-FLIGHT (§3, réutilise run_preflight_safety Phase 9.4 tel quel) ----
    with Session(engine) as session:
        preflight = run_preflight_safety(session, store, kill_switch_store, as_of)

    if preflight["status"] == "FAIL":
        logger.error("PRE-FLIGHT SAFETY FAIL — STOP (%s). Aucun snapshot calculé (données non fiables).", preflight["blocking"])
        with Session(engine) as session:
            db_after = fewf.snapshot_db_counts(session)
        status_short, diff_stat, diff_names, production_modified = _check_production_files_untouched(REPO_ROOT)
        result = {
            "run_id": run_id, "generated_at": generated_at, "phase": "9.5", "kind": "shadow_evidence_watch_v1",
            "rule": "CONTINUOUS EVIDENCE. MODE_1_SHADOW_ONLY. NO PRODUCTION ACTIVATION. OBSERVATION ONLY.",
            "as_of": as_of.isoformat(), "market": market, "read_only": True,
            "preflight": preflight, "final_verdict": "BLOCKED", "human_review_status": "NOT_READY_FOR_HUMAN_REVIEW",
            "db_safety": {"before": db_before, "after": db_after, "unchanged": db_before == db_after},
            "git_status_short": status_short, "git_diff_stat": diff_stat, "git_diff_names": diff_names,
            "production_files_modified": production_modified,
            "mode": "MODE_1_SHADOW_ONLY", "production_activation": "BLOCKED", "no_user_betting_signal": True,
        }
        _write_reports(result, render_markdown_blocked(result))
        if emit_json:
            print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        print("PHASE 9.5 — XFOOT SHADOW EVIDENCE WATCH & LONGITUDINAL TRACKING V1 TERMINÉE. "
              "ÉVIDENCE SHADOW SUIVIE DANS LE TEMPS, DONNÉES RÉELLES ÉVALUÉES OU LIMITATION DOCUMENTÉE. "
              "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION.")
        return result

    # ---- backup AVANT toute écriture réelle du Shadow Store (§24). ----
    backup_path = None
    backup_validated = None
    if not read_only:
        backup_dir = REPO_ROOT / "reports" / "shadow" / "watch" / "backups"
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

    # ---- DISCOVER + VALIDATE + CAPTURE (§4/§5) ----
    with Session(engine) as session:
        capture_outcome = run_prospective_capture(session, store, as_of, dry_run=read_only, market=market)

    # ---- RESOLVE (§6) ----
    with Session(engine) as session:
        resolution_summary = run_resolution(session, store) if do_resolve else {"skipped": "NOT_REQUESTED_OR_READ_ONLY"}

    entries = store.all()
    basic_ledger = compute_evidence_ledger(entries)
    capture_quality = classify_capture_quality(capture_outcome, entries)

    # ---- EVIDENCE + TRACK RECORD (§7/§10, REAL_PROSPECTIVE + RESOLVED uniquement) ----
    with Session(engine) as session:
        full_ledger = compute_full_evidence_ledger(session, entries)
        prospective_entries = filter_real_prospective_entries(session, entries)
        model_version_tracking = compute_model_version_tracking(entries)
        data_gaps = compute_data_gaps(session, entries)

    track_record = {m: compute_shadow_track_record(prospective_entries, market=m) for m in MARKETS}
    track_record["value_tracking"] = value_tracking_status(entries)
    breakdown = {m: compute_breakdown(prospective_entries, market=m) for m in MARKETS}
    track_record_sample_size = sum(tr.get("sample_size", 0) or 0 for m, tr in track_record.items() if m in MARKETS)

    # ---- MONITOR (§16) ----
    with Session(engine) as session:
        health = compute_shadow_health(session, store, as_of)

    # ---- READINESS (§17, information ONLY — jamais une gate modifiée/override) ----
    with Session(engine) as session:
        readiness = evaluate_production_readiness(session, store, as_of)
    current_blockers = readiness_blockers(readiness)
    activation_guard = can_activate_production(kill_switch_store, readiness.gates, scope="PRODUCTION_PREDICTION_ACTIVATION")

    # ---- LONGITUDINAL HISTORY + TREND + BLOCKER EVOLUTION (§8/§9/§18) ----
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

    with Session(engine) as session:
        db_after = fewf.snapshot_db_counts(session)
    db_safety = {"before": db_before, "after": db_after, "unchanged": db_before == db_after}

    regression = run_existing_regression_suites()
    this_phase_tests = run_this_phase_tests()
    status_short, diff_stat, diff_names, production_modified = _check_production_files_untouched(REPO_ROOT)

    tests_green = db_safety["unchanged"] and not production_modified and this_phase_tests["pass"] and all(r["pass"] for r in regression.values())

    maturity = full_ledger["maturity_real_prospective_resolved"]
    final_verdict = derive_watch_verdict(
        preflight_status=preflight["status"], tests_green=tests_green, future_fixtures=health["reality"]["future_fixtures"],
        real_prospective_resolved=full_ledger["real_prospective_resolved_count"], maturity=maturity,
        blockers=current_blockers, readiness_verdict=readiness.final_verdict,
    )
    human_review_status = "READY_FOR_HUMAN_REVIEW" if final_verdict == "READY_FOR_HUMAN_REVIEW" else "NOT_READY_FOR_HUMAN_REVIEW"

    result = {
        "run_id": run_id, "generated_at": generated_at, "phase": "9.5", "kind": "shadow_evidence_watch_v1",
        "rule": "CONTINUOUS EVIDENCE. MODE_1_SHADOW_ONLY. NO PRODUCTION ACTIVATION. OBSERVATION ONLY.",
        "as_of": as_of.isoformat(), "market": market, "read_only": read_only,
        "requested_capture": do_capture, "requested_resolve": do_resolve_flag, "resolve_executed": do_resolve,
        "requested_monitor": do_monitor,
        "preflight": preflight,
        "capture_outcome": capture_outcome, "resolution_summary": resolution_summary,
        "evidence_ledger": basic_ledger, "full_evidence_ledger": full_ledger, "capture_quality": capture_quality,
        "track_record": track_record, "breakdown": breakdown, "model_version_tracking": model_version_tracking,
        "shadow_health_status": health["status"], "shadow_health": health,
        "readiness_verdict": readiness.final_verdict, "readiness_critical_failures": readiness.critical_gate_failures,
        "current_blockers": current_blockers, "activation_guard_allowed": activation_guard.allowed,
        "activation_guard_blocking_reasons": activation_guard.blocking_reasons,
        "evidence_snapshot": snapshot, "longitudinal_history_count": len(history_after),
        "evidence_trend": trend, "blocker_evolution": blocker_evolution, "data_gaps": data_gaps,
        "backup_path": str(backup_path) if backup_path else None, "backup_validated": backup_validated,
        "db_safety": db_safety, "existing_regression_suites": regression, "this_phase_tests": this_phase_tests,
        "git_status_short": status_short, "git_diff_stat": diff_stat, "git_diff_names": diff_names,
        "production_files_modified": production_modified, "tests_green": tests_green,
        "final_verdict": final_verdict, "human_review_status": human_review_status,
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
        print(f"Verdict final : {final_verdict}  (maturity={maturity}, human_review={human_review_status})")
        print(f"Snapshot : future_fixtures={snapshot['future_fixtures']}, real_prospective={snapshot['real_prospective']}, "
              f"resolved={snapshot['resolved']}, history_size={len(history_after)}")
        print(f"Trend : {trend.get('status')}")
        print(f"Blocker evolution : {blocker_evolution.get('status')} — new={blocker_evolution.get('new')}, cleared={blocker_evolution.get('cleared')}")
        print("git status --short :")
        print(status_short or "(clean)")
        print("git diff --stat :")
        print(diff_stat or "(no tracked file modified)")
        print("PHASE 9.5 — XFOOT SHADOW EVIDENCE WATCH & LONGITUDINAL TRACKING V1 TERMINÉE. "
              "ÉVIDENCE SHADOW SUIVIE DANS LE TEMPS, DONNÉES RÉELLES ÉVALUÉES OU LIMITATION DOCUMENTÉE. "
              "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION.")
        print("=" * 80)
    return result


def render_markdown_blocked(result: dict) -> str:
    md = ["# XFOOT PHASE 9.5\n\n# SHADOW EVIDENCE WATCH\n"]
    md.append(f"\n## 1. Executive Summary\n\nRun id `{result['run_id']}` — {result['generated_at']}.\n\n"
               f"**PRE-FLIGHT SAFETY FAIL — STOP.** Aucun snapshot n'a été calculé/persisté (§3).\n")
    md.append(f"\n## 3. Preflight\n\n{result['preflight']}\n")
    md.append("\n## 4-21. Current Shadow State / Evidence Snapshot / Longitudinal History / Evidence Trends / "
               "Prospective Integrity / Provenance / Track Record / Maturity / Model-Version / League-Market / "
               "Monitoring / Readiness / Blocker Evolution / Alerts / Data Gaps / DB Safety / Production Safety / Limitations\n\n"
               "Non applicables — le pré-vol a stoppé le run avant toute observation (§3).\n")
    md.append("\n## 22. Verdict\n\n**BLOCKED**\n")
    md.append("\n---\n\nPHASE 9.5 — XFOOT SHADOW EVIDENCE WATCH & LONGITUDINAL TRACKING V1 TERMINÉE. "
               "ÉVIDENCE SHADOW SUIVIE DANS LE TEMPS, DONNÉES RÉELLES ÉVALUÉES OU LIMITATION DOCUMENTÉE. "
               "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION.\n")
    return "".join(md)


def render_markdown(result: dict) -> str:
    md = ["# XFOOT PHASE 9.5\n\n# SHADOW EVIDENCE WATCH\n"]
    md.append(f"\n## 1. Executive Summary\n\nRun id `{result['run_id']}` — {result['generated_at']}. as_of={result['as_of']}, "
               f"read_only={result['read_only']}\n\n**Verdict : {result['final_verdict']}** — {result['human_review_status']}\n")
    md.append(f"\n## 2. Operating Mode\n\n{result['mode']} — production_activation={result['production_activation']}\n")
    md.append(f"\n## 3. Preflight\n\n{result['preflight']}\n")
    md.append(f"\n## 4. Current Shadow State\n\n{result['shadow_health']['reality']}\n\ncapturable={result['shadow_health']['capturable']}, "
               f"captured={result['shadow_health']['captured']}\n")
    md.append(f"\n## 5. Evidence Snapshot\n\n{result['evidence_snapshot']}\n")
    md.append(f"\n## 6. Longitudinal History\n\n{result['longitudinal_history_count']} snapshot(s) au total (append-only, "
               f"jamais supprimé) — reports/shadow/watch/evidence_snapshots.json\n")
    md.append(f"\n## 7. Evidence Trends\n\n{result['evidence_trend']}\n")
    md.append(f"\n## 8. Prospective Integrity\n\nby_data_marking_class={result['full_evidence_ledger']['by_data_marking_class']}\n")
    md.append(f"\n## 9. Provenance\n\ncomplete={result['full_evidence_ledger']['provenance_complete']}, "
               f"incomplete={result['full_evidence_ledger']['provenance_incomplete']}, unknown={result['full_evidence_ledger']['provenance_unknown']}\n")
    md.append(f"\n## 10. Track Record\n\n(REAL_PROSPECTIVE + RESOLVED uniquement, §10)\n\n{result['track_record']}\n")
    md.append(f"\n## 11. Maturity\n\n{result['full_evidence_ledger']['maturity_real_prospective_resolved']}\n")
    md.append(f"\n## 12. Model / Version\n\n{result['model_version_tracking']}\n")
    md.append(f"\n## 13. League / Market\n\n{result['breakdown']}\n")
    md.append(f"\n## 14. Monitoring\n\nstatus={result['shadow_health_status']}\n\nalerts={result['shadow_health'].get('alerts')}\n")
    md.append(f"\n## 15. Readiness\n\nverdict={result['readiness_verdict']} (critical failures: {result['readiness_critical_failures']})\n\n"
               f"can_activate_production (information uniquement, jamais appliqué) : allowed={result['activation_guard_allowed']}, "
               f"reasons={result['activation_guard_blocking_reasons']}\n")
    md.append(f"\n## 16. Blocker Evolution\n\n{result['blocker_evolution']}\n")
    md.append(f"\n## 17. Alerts\n\n{result['shadow_health'].get('alerts')}\n")
    md.append(f"\n## 18. Data Gaps\n\n{result['data_gaps']}\n")
    db = result["db_safety"]
    md.append(f"\n## 19. DB Safety\n\nBefore: {db['before']}\n\nAfter: {db['after']}\n\nUnchanged: **{db['unchanged']}**\n")
    md.append(f"\n## 20. Production Safety\n\nmode={result['mode']}, production_activation={result['production_activation']}, "
               f"kill_switch/readiness lus uniquement (jamais modifiés, §17/§30).\n")
    md.append(
        "\n## 21. Limitations\n\n"
        "- Aucune heure de coup d'envoi réelle n'est persistée — toute classification prospective reste qualifiée (CONSISTENT_WITH_PLACEHOLDER_KICKOFF).\n"
        "- value_tracking reste NOT_AVAILABLE tant qu'aucune odds TEMPORALLY_VERIFIED n'existe.\n"
        "- La tendance (§7) et l'évolution des blockers (§16) ne portent que sur les runs RÉELS de ce watch — jamais une projection.\n"
        "- readiness (§15) est lue et comparée à titre purement informatif — jamais modifiée/override par cette phase.\n"
    )
    md.append(f"\n## 22. Verdict\n\n**{result['final_verdict']}** — {result['human_review_status']}\n")
    md.append("\n---\n\n### EXISTING REGRESSION SUITES\n\n| Suite | Pass |\n|---|---|\n")
    for suite, r in result["existing_regression_suites"].items():
        md.append(f"| {suite} | {r['pass']} |\n")
    md.append(f"\n| test_phase9_5.py (this phase) | {result['this_phase_tests']['pass']} |\n")
    md.append("\n---\n\nPHASE 9.5 — XFOOT SHADOW EVIDENCE WATCH & LONGITUDINAL TRACKING V1 TERMINÉE. "
               "ÉVIDENCE SHADOW SUIVIE DANS LE TEMPS, DONNÉES RÉELLES ÉVALUÉES OU LIMITATION DOCUMENTÉE. "
               "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION.\n")
    return "".join(md)


def _write_reports(result: dict, markdown: str) -> None:
    outdir = REPO_ROOT / "reports" / "phase9_5"
    outdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    json_path = outdir / f"xfoot_phase9_5_{ts}.json"
    md_path = outdir / f"xfoot_phase9_5_{ts}.md"
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
    main(args.as_of, args.market, args.capture, args.resolve, args.monitor, args.dry_run, args.json, args.markdown)
    sys.exit(0)
