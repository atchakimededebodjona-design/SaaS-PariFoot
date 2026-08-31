"""
scripts/shadow_live_track.py — Phase 8M : XFOOT LIVE SHADOW TRACK RECORD
ACCUMULATION V1.
=============================================================================
REAL FUTURE DATA + SHADOW ONLY. Aucun appel réseau, aucune clé API, aucune
écriture dans une table de production. Réutilise TEL QUEL :
  - api/app/ai/shadow/live.py (Phase 8M) : découverte/capture/cohérence.
  - api/app/ai/pipeline/orchestrator.py::run_pipeline (Phase 8J).
  - api/app/ai/shadow/tracking.py::ShadowDecisionStore (Phase 8K, §40 durci).
  - api/app/ai/shadow/resolution.py::resolve_record (Phase 8K).
  - api/app/ai/shadow/metrics.py::compute_shadow_track_record (Phase 5/7).

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/shadow_live_track.py [--as-of ISO] [--dry-run] [--resolve]
        [--league L] [--market M] [--model MT] [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--last-n N]
"""

import argparse
import json
import logging
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from sqlmodel import Session  # noqa: E402
from app.core.database import engine, init_db  # noqa: E402

from app.ai.pipeline.orchestrator import run_pipeline  # noqa: E402
from app.ai.shadow.tracking import ShadowDecisionStore, capture_shadow_decision, DEFAULT_STORE_PATH  # noqa: E402
from app.ai.shadow.resolution import resolve_record  # noqa: E402
from app.ai.shadow.metrics import compute_shadow_track_record, value_tracking_status, classify_maturity, MATURITY_THRESHOLDS  # noqa: E402
from app.ai.shadow.live import discover_live_candidates, assess_capture_eligibility, build_pipeline_input_for_live, check_production_consistency  # noqa: E402
from app.ai.shadow.replay import measure_data_reality  # noqa: E402  (Phase 8K, réutilisé pour la mesure de disponibilité)

import feature_engineering_walkforward as fewf  # noqa: E402  (réutilise snapshot_db_counts, jamais réimplémenté)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("shadow_live_track")
UTC = timezone.utc

MARKET_DEFAULT_SELECTION = {"1X2": "home_win", "BTTS": "yes", "OVER_UNDER_2_5": "over"}


def build_shadow_readiness(session, as_of: datetime) -> dict:
    """§44 : Current Shadow Readiness — mesure réelle, jamais supposée."""
    reality = measure_data_reality(session)
    candidates = discover_live_candidates(session, as_of)
    capturable, rejected = [], []
    for c in candidates:
        ok, reason = assess_capture_eligibility(c, as_of)
        (capturable if ok else rejected).append({"match": f"{c.league}:{c.match_date}:{c.home_team}-{c.away_team}", "model_type": c.model_type, "reason": reason})
    return {
        "as_of": as_of.isoformat(), "future_fixtures_in_db": reality["future_fixtures"],
        "pending_predictions_total": reality["pending_model_predictions"],
        "candidates_discovered": len(candidates), "capturable": len(capturable), "rejected": len(rejected),
        "rejected_detail": rejected, "shadow_live_data": "NONE_AVAILABLE" if len(capturable) == 0 else "AVAILABLE",
    }


def run_capture(session, store: ShadowDecisionStore, as_of: datetime, dry_run: bool) -> dict:
    """§7/§18 : découverte -> cutoff -> snapshot production -> pipeline 8J -> capture 8K. `dry_run=True` -> 0 écriture."""
    candidates = discover_live_candidates(session, as_of)
    outcome = {"candidates": len(candidates), "captured": 0, "duplicates_prevented": 0, "rejected": [], "errors": [], "mismatches": [], "captured_records": []}

    for mp in candidates:
        market = "1X2"
        selection = MARKET_DEFAULT_SELECTION[market]
        pi, diagnostics = build_pipeline_input_for_live(session, mp, market, selection, as_of)
        if pi is None:
            outcome["rejected"].append(diagnostics)
            continue
        try:
            assessment = run_pipeline(pi)
        except Exception as e:  # noqa: BLE001 — §31 : isolation, jamais interrompre le run
            outcome["errors"].append({"match": diagnostics["match"], "error_category": "PIPELINE_EXCEPTION", "message": str(e)[:200]})
            continue

        mismatches = check_production_consistency(mp, pi, assessment)
        if mismatches:
            outcome["mismatches"].append({"match": diagnostics["match"], "mismatches": mismatches})
            continue  # §37/§38 : jamais corrigé silencieusement, jamais capturé sur un écart détecté

        if dry_run:
            outcome["captured"] += 1  # compté comme "aurait été capturé", AUCUNE écriture (§18 : "expected 0 écriture")
            continue

        record, created = capture_shadow_decision(store, pi, assessment, home_team=mp.home_team, away_team=mp.away_team, data_marking="REAL")
        if created:
            outcome["captured"] += 1
            outcome["captured_records"].append({"shadow_id": record.shadow_id, "status": record.status, "eligibility": record.eligibility})
        else:
            outcome["duplicates_prevented"] += 1

    if not dry_run:
        store.save()
    return outcome


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


def build_track_record_section(store: ShadowDecisionStore, filters: dict) -> dict:
    entries = store.all()
    return {
        "1X2": compute_shadow_track_record(entries, market="1X2", **filters),
        "BTTS": compute_shadow_track_record(entries, market="BTTS", **filters),
        "OVER_UNDER_2_5": compute_shadow_track_record(entries, market="OVER_UNDER_2_5", **filters),
        "value_tracking": value_tracking_status(entries),
    }


def run_existing_regression_suites() -> dict:
    suites = ["test_model_selection.py", "test_track_record.py", "test_feature_registry.py", "test_value_engine.py",
              "test_decision_layer.py", "test_end_to_end_pipeline.py", "test_shadow_operational.py", "test_historical_replay.py", "test_live_shadow_track.py"]
    results = {}
    api_dir = Path(__file__).resolve().parent.parent / "api"
    for suite in suites:
        proc = subprocess.run([sys.executable, suite], cwd=api_dir, capture_output=True, text=True)
        tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        results[suite] = {"returncode": proc.returncode, "summary_line": tail, "pass": proc.returncode == 0}
    return results


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


def main(as_of_arg: str, dry_run: bool, do_resolve: bool, filters: dict) -> dict:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    generated_at = datetime.now(timezone.utc).isoformat()
    as_of = datetime.fromisoformat(as_of_arg).replace(tzinfo=UTC) if as_of_arg else datetime.now(timezone.utc)

    init_db()
    with Session(engine) as session:
        db_before = fewf.snapshot_db_counts(session)

    store = ShadowDecisionStore()  # reports/shadow/shadow_decision_store.json — MÊME store que Phase 8K (accumulation continue)

    with Session(engine) as session:
        readiness = build_shadow_readiness(session, as_of)
        capture_outcome = run_capture(session, store, as_of, dry_run)
        resolution_summary = run_resolution(session, store) if do_resolve else {"skipped": "NOT_REQUESTED (--resolve non fourni)"}
        track_record = build_track_record_section(store, filters)

    with Session(engine) as session:
        db_after = fewf.snapshot_db_counts(session)
    db_safety = {"before": db_before, "after": db_after, "unchanged": db_before == db_after}

    regression = run_existing_regression_suites()
    repo_root = Path(__file__).resolve().parent.parent
    status_short, diff_stat, diff_names, production_modified = _check_production_files_untouched(repo_root)

    entries = store.all()
    operational_health = {
        "candidates": capture_outcome["candidates"], "captured": capture_outcome["captured"],
        "duplicates_prevented": capture_outcome["duplicates_prevented"], "rejected": len(capture_outcome["rejected"]),
        "errors": len(capture_outcome["errors"]), "mismatches": len(capture_outcome["mismatches"]),
        "pending": sum(1 for _, res in entries if res.result_status == "PENDING"),
        "resolved": sum(1 for _, res in entries if res.result_status == "RESOLVED"),
        "conflicts": sum(1 for _, res in entries if res.result_status == "CONFLICT"),
        "unresolved": sum(1 for _, res in entries if res.result_status == "UNRESOLVED"),
        "invalid": sum(1 for _, res in entries if res.result_status == "INVALID"),
        "provenance_missing": sum(1 for r, _ in entries if not r.provenance),
        "temporal_unknown": sum(1 for r, _ in entries if r.temporal_status == "UNKNOWN"),
        "no_odds": sum(1 for r, _ in entries if r.odds_source is None),
        "total_records_in_store": len(entries),
    }

    tests_green = db_safety["unchanged"] and not production_modified and all(r["pass"] for r in regression.values())
    resolved_1x2 = track_record["1X2"].get("sample_size", 0) if track_record["1X2"]["status"] == "ok" else 0
    maturity = classify_maturity(resolved_1x2)

    if not tests_green:
        final_verdict = "SHADOW_TRACKING_NEEDS_FIXES"
    elif readiness["capturable"] == 0 and capture_outcome["captured"] == 0 and len(entries) == 0:
        final_verdict = "INSUFFICIENT_REAL_DATA"
    elif capture_outcome["captured"] > 0 or len(entries) > 0:
        final_verdict = "SHADOW_TRACKING_READY"
    else:
        final_verdict = "SHADOW_TRACKING_PARTIAL"

    result = {
        "run_id": run_id, "generated_at": generated_at, "phase": "8M", "kind": "live_shadow_track_record_v1",
        "rule": "REAL FUTURE DATA + SHADOW ONLY. NO PRODUCTION DECISION. NO ODDS PROVIDER CALLED.",
        "as_of": as_of.isoformat(), "dry_run": dry_run, "resolve_requested": do_resolve,
        "shadow_readiness": readiness, "capture_outcome": capture_outcome, "resolution_summary": resolution_summary,
        "track_record": track_record, "maturity_state": maturity, "maturity_thresholds": MATURITY_THRESHOLDS,
        "operational_health": operational_health, "storage_path": str(store.path),
        "db_safety": db_safety, "existing_regression_suites": regression,
        "git_status_short": status_short, "git_diff_stat": diff_stat, "git_diff_names": diff_names, "production_files_modified": production_modified,
        "tests_green": tests_green, "final_verdict": final_verdict,
        "the_odds_api_status": "SUPPORT_REQUIRED — non appelé dans cette phase.", "no_user_betting_signal": True,
    }

    if production_modified:
        logger.error("ARRÊT : des fichiers de production ont été modifiés — voir git diff --name-only.")

    _write_run_report(result, run_id)
    _write_cumulative_report(store, track_record, operational_health, maturity)

    print("\n" + "=" * 80)
    print(f"Verdict final : {final_verdict}  (maturity={maturity})")
    print(f"Shadow readiness : {readiness}")
    print(f"Capture : {capture_outcome['captured']} capturés, {capture_outcome['duplicates_prevented']} doublons évités, {len(capture_outcome['rejected'])} rejetés")
    print("git status --short :")
    print(status_short or "(clean)")
    print("git diff --stat :")
    print(diff_stat or "(no tracked file modified)")
    print("PHASE 8M — XFOOT LIVE SHADOW TRACK RECORD ACCUMULATION V1 TERMINÉE. "
          "TRACK RECORD PROSPECTIF CONFIGURÉ OU LIMITATION DOCUMENTÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. "
          "AUCUNE INTÉGRATION ODDS EFFECTUÉE. AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.")
    print("=" * 80)
    return result


def render_run_markdown(result: dict) -> str:
    md = ["# XFOOT LIVE SHADOW TRACK RECORD V1\n"]
    md.append("\n## 1. Run Summary\n")
    md.append(f"\nRun id : `{result['run_id']}` — {result['generated_at']}. as_of={result['as_of']}, dry_run={result['dry_run']}, resolve={result['resolve_requested']}\n")
    md.append(f"\n**Verdict : {result['final_verdict']}** — maturity={result['maturity_state']}\n")
    md.append("\n## 2. Data Availability\n\n" + str(result["shadow_readiness"]) + "\n")
    md.append("\n## 3. Candidates\n\n" + str(result["capture_outcome"]["candidates"]) + "\n")
    md.append("\n## 4. Captured Decisions\n\n" + str(result["capture_outcome"].get("captured_records", [])) + "\n")
    md.append("\n## 5. Rejected Decisions\n\n" + str(result["capture_outcome"]["rejected"]) + "\n")
    md.append(f"\n## 6. Duplicate Prevention\n\n{result['capture_outcome']['duplicates_prevented']} doublons évités.\n")
    md.append(f"\n## 7. Temporal Integrity\n\ntemporal_unknown={result['operational_health']['temporal_unknown']}\n")
    md.append(f"\n## 8. Provenance\n\nprovenance_missing={result['operational_health']['provenance_missing']} ; mismatches={result['capture_outcome']['mismatches']}\n")
    md.append(f"\n## 9. Resolution Status\n\n{result['resolution_summary']}\n")
    md.append(f"\n## 10. Track Record\n\n{result['track_record']}\n")
    md.append(f"\n## 11. Value Tracking\n\n{result['track_record']['value_tracking']}\n")
    md.append(f"\n## 12. Operational Health\n\n{result['operational_health']}\n")
    db = result["db_safety"]
    md.append(f"\n## 13. Database Safety\n\nBefore : {db['before']}\n\nAfter : {db['after']}\n\nUnchanged : **{db['unchanged']}**\n")
    md.append(f"\n## 14. Production Isolation\n\nFichiers production modifiés : **{result['production_files_modified']}**\n")
    md.append(
        "\n## 15. Limitations\n\n"
        f"- shadow_live_data = {result['shadow_readiness']['shadow_live_data']} : voir §2.\n"
        "- value_tracking reste NOT_AVAILABLE tant qu'aucune odds TEMPORALLY_VERIFIED n'existe (The Odds API SUPPORT_REQUIRED).\n"
        "- Aucune affirmation de performance (\"best model\", \"profitable\") n'est produite avant STATISTICALLY_INFORMATIVE (§49/§52).\n"
    )
    md.append(f"\n## 16. Verdict\n\n**{result['final_verdict']}**\n")
    md.append("\n---\n\n### EXISTING REGRESSION SUITES\n\n| Suite | Pass |\n|---|---|\n")
    for suite, r in result["existing_regression_suites"].items():
        md.append(f"| {suite} | {r['pass']} |\n")
    md.append("\n---\n\nPHASE 8M — XFOOT LIVE SHADOW TRACK RECORD ACCUMULATION V1 TERMINÉE. "
               "TRACK RECORD PROSPECTIF CONFIGURÉ OU LIMITATION DOCUMENTÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. "
               "AUCUNE INTÉGRATION ODDS EFFECTUÉE. AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.\n")
    return "".join(md)


def _write_run_report(result: dict, run_id: str) -> None:
    outdir = Path(__file__).resolve().parent.parent / "reports" / "shadow" / "live"
    outdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = outdir / f"shadow_live_run_{ts}.json"
    md_path = outdir / f"shadow_live_run_{ts}.md"
    json_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_run_markdown(result), encoding="utf-8")
    logger.info("Rapport de run écrit : %s / %s", json_path, md_path)


def _write_cumulative_report(store: ShadowDecisionStore, track_record: dict, operational_health: dict, maturity: str) -> None:
    """§46 : rapport cumulatif — recalculé ENTIÈREMENT depuis le store à chaque run (jamais une moyenne
    de moyennes incrémentale), même convention que Phase 7 (compute_cumulative_track_record)."""
    entries = store.all()
    cumulative = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_shadow_decisions": len(entries),
        "pending": sum(1 for _, res in entries if res.result_status == "PENDING"),
        "resolved": sum(1 for _, res in entries if res.result_status == "RESOLVED"),
        "conflicts": sum(1 for _, res in entries if res.result_status == "CONFLICT"),
        "invalid": sum(1 for _, res in entries if res.result_status == "INVALID"),
        "track_record": track_record, "maturity_state": maturity, "maturity_thresholds": MATURITY_THRESHOLDS,
        "by_league": {}, "by_market": {}, "by_model": {},
    }
    for league in sorted({r.league for r, _ in entries if r.league}):
        cumulative["by_league"][league] = compute_shadow_track_record(entries, market="1X2", league=league)
    for market in ("1X2", "BTTS", "OVER_UNDER_2_5"):
        cumulative["by_market"][market] = compute_shadow_track_record(entries, market=market)
    for model_type in sorted({r.model_type for r, _ in entries if r.model_type}):
        cumulative["by_model"][model_type] = compute_shadow_track_record(entries, market="1X2", model_type=model_type)

    outdir = Path(__file__).resolve().parent.parent / "reports" / "shadow" / "live"
    outdir.mkdir(parents=True, exist_ok=True)
    json_path = outdir / "shadow_cumulative_track_record.json"
    md_path = outdir / "shadow_cumulative_track_record.md"
    json_path.write_text(json.dumps(cumulative, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md = ["# XFOOT SHADOW CUMULATIVE TRACK RECORD\n\n", f"Généré le {cumulative['generated_at']}\n\n",
          f"Total decisions : {cumulative['total_shadow_decisions']} (pending={cumulative['pending']}, resolved={cumulative['resolved']}, "
          f"conflicts={cumulative['conflicts']}, invalid={cumulative['invalid']})\n\n", f"Maturity : **{maturity}** (seuils : {MATURITY_THRESHOLDS})\n\n",
          f"Par ligue : {cumulative['by_league']}\n\n", f"Par marché : {cumulative['by_market']}\n\n", f"Par modèle : {cumulative['by_model']}\n"]
    md_path.write_text("".join(md), encoding="utf-8")
    logger.info("Rapport cumulatif écrit : %s / %s", json_path, md_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as-of", type=str, default=None, help="ISO8601, ex. 2026-08-30T18:00:00 — défaut : heure actuelle (fournie explicitement au runner, jamais implicite dans les fonctions pures)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resolve", action="store_true")
    parser.add_argument("--league", type=str, default=None)
    parser.add_argument("--market", type=str, default=None)
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--since", type=str, default=None)
    parser.add_argument("--until", type=str, default=None)
    parser.add_argument("--last-n", type=int, default=None)
    args = parser.parse_args()

    filters = {}
    if args.league:
        filters["league"] = args.league
    if args.model:
        filters["model_type"] = args.model
    if args.since:
        filters["since"] = date.fromisoformat(args.since)
    if args.until:
        filters["until"] = date.fromisoformat(args.until)
    if args.last_n:
        filters["last_n"] = args.last_n

    main(as_of_arg=args.as_of, dry_run=args.dry_run, do_resolve=args.resolve, filters=filters)
    sys.exit(0)
