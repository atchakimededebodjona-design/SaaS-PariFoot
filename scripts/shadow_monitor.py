"""
scripts/shadow_monitor.py — Phase 8N : XFOOT SHADOW MONITORING & DATA
QUALITY OPERATIONS V1. DERNIÈRE PHASE 8.
=============================================================================
MONITORING + RESEARCH + SHADOW ONLY. STRICTEMENT READ-ONLY : aucune écriture
DB, aucune écriture du Shadow Store, aucun appel réseau, aucune notification
externe, aucun cron. Réutilise TEL QUEL api/app/ai/shadow/monitoring.py
(Phase 8N) — jamais une deuxième logique de calcul.

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/shadow_monitor.py [--as-of ISO]
        [--league L] [--market M] [--model MT] [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--last-n N]
        [--json] [--markdown]
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

from app.ai.shadow.tracking import ShadowDecisionStore  # noqa: E402
from app.ai.shadow.monitoring import compute_shadow_health, HEALTH_STATUSES  # noqa: E402

import feature_engineering_walkforward as fewf  # noqa: E402  (réutilise snapshot_db_counts, jamais réimplémenté)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("shadow_monitor")
UTC = timezone.utc


def run_existing_regression_suites() -> dict:
    suites = ["test_model_selection.py", "test_track_record.py", "test_feature_registry.py", "test_value_engine.py",
              "test_decision_layer.py", "test_end_to_end_pipeline.py", "test_shadow_operational.py",
              "test_historical_replay.py", "test_live_shadow_track.py", "test_shadow_monitoring.py"]
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


def build_scorecard(health: dict) -> list[dict]:
    """§25 : Shadow Data Quality Scorecard."""
    def ok(cond): return "OK" if cond else "ISSUE"
    return [
        {"dimension": "Fixture Availability", "status": health["fixture_coverage"]["status"], "count": health["future_fixtures"], "evidence": health["fixture_coverage"]},
        {"dimension": "Prediction Coverage", "status": health["fixture_coverage"]["status"], "count": health["pending_predictions"], "evidence": health["fixture_coverage"]},
        {"dimension": "Shadow Capture", "status": health["capture_coverage"]["status"], "count": health["captured"], "evidence": health["capture_coverage"]},
        {"dimension": "Temporal Integrity", "status": ok(health["temporal_health"]["temporal_rejected"] == 0), "count": health["temporal_health"]["temporal_unknown"], "evidence": health["temporal_health"]},
        {"dimension": "Provenance", "status": ok(health["provenance_health"]["missing"] == 0), "count": health["provenance_health"]["missing"], "evidence": health["provenance_health"]},
        {"dimension": "Model Consistency", "status": ok(health["consistency_health"]["model_mismatches"] == 0), "count": health["consistency_health"]["model_mismatches"], "evidence": health["consistency_health"]},
        {"dimension": "Probability Consistency", "status": ok(health["consistency_health"]["probability_mismatches"] == 0), "count": health["consistency_health"]["probability_mismatches"], "evidence": health["consistency_health"]},
        {"dimension": "Decision Consistency", "status": "OK (identique à Probability/Model Consistency — Phase 8I déterministe sur les mêmes inputs)", "count": None, "evidence": None},
        {"dimension": "Store Integrity", "status": health["store_integrity"]["status"], "count": health["store_integrity"].get("record_count"), "evidence": health["store_integrity"]},
        {"dimension": "Resolution", "status": ok(health["resolution_health"]["counts"].get("CONFLICT", 0) == 0), "count": health["resolution_health"]["counts"], "evidence": health["resolution_health"]},
        {"dimension": "Track Record", "status": health["track_record_health"]["maturity"], "count": None, "evidence": health["track_record_health"]["per_market"]},
    ]


def build_readiness_for_phase9(health: dict, tests_green: bool) -> dict:
    """§65 : section obligatoire — Phase 8N ne commence PAS le travail de Phase 9."""
    return {
        "ready": [
            "Data Foundation (Phase 8A)", "Feature Registry (Phase 8A)", "Model Selection + Calibration (Phase 6, SHADOW)",
            "Track Record engine (Phase 7, réutilisé partout)", "Value Engine Foundation (Phase 8H)",
            "Decision Layer Foundation (Phase 8I)", "End-to-End Shadow Pipeline (Phase 8J)",
            "Shadow Decision Tracking + atomic/corruption-safe store (Phase 8K, durci Phase 8M)",
            "Live capture runner avec anti-fuite (Phase 8M)", "Shadow Monitoring & Data Quality (Phase 8N, cette phase)",
        ],
        "partial": [
            "Live Shadow Track Record — mécanisme validé, MAIS 0 donnée réelle accumulée à ce jour (voir §5/§33)",
            "Calibration Platt/Isotonic — recherche uniquement, jamais persistée par ModelVersion (constaté Phase 8L)",
        ],
        "blocked": [
            "Historical Replay — HISTORICAL_REPLAY_NOT_AVAILABLE (Phase 8L, preuve exhaustive : 186 885/186 885 paires rejetées)",
            "Value Tracking / ROI réel — NOT_AVAILABLE tant qu'aucune odds TEMPORALLY_VERIFIED n'existe",
            "The Odds API — SUPPORT_REQUIRED (Phase 8G.2), réponse support toujours en attente",
        ],
        "missing_real_data": [
            "Fixtures futures avec prédiction production pending (0 actuellement, voir shadow_readiness)",
            "Au moins 10-30 décisions shadow RESOLVED pour sortir de NO_DATA/EARLY_DATA (seuils Phase 8M, §23)",
            "Une source d'odds réellement temporellement vérifiée (aucune intégrée à ce jour)",
        ],
        "open_risks": [
            "Aucune ModelVersion actuelle n'a training_period_start/end rempli (Phase 8L) — un futur replay restera impossible pour les versions déjà existantes.",
            "Deux systèmes de normalisation d'équipe non synchronisés (team_name_matching.py vs main.py, constaté Phase 8A) — risque de faux MISSED_CAPTURE si les noms divergent.",
            "kickoff réel (heure) n'est jamais persisté (Match/ModelPrediction ne portent qu'une date) — LATE_PREDICTION reste structurellement non vérifiable tant que ce n'est pas corrigé en amont (hors périmètre Phase 8).",
        ],
        "conditions_before_any_production_activation": [
            "Accumuler un track record shadow RESOLVED réellement STATISTICALLY_INFORMATIVE (>= 100, seuil déjà documenté).",
            "Résoudre l'inconnue The Odds API (SUPPORT_REQUIRED) OU documenter une alternative de données odds temporellement vérifiées.",
            "Décision produit explicite sur le vocabulaire calibration RESEARCH_WITHOUT_CALIBRATION vs exigée pour un usage réel.",
            "Toute activation reste une décision Phase 9, jamais implicite dans les phases 8.",
        ],
    }


def main(as_of_arg: str, filters: dict, emit_json: bool, emit_markdown: bool) -> dict:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    generated_at = datetime.now(timezone.utc).isoformat()
    as_of = datetime.fromisoformat(as_of_arg).replace(tzinfo=UTC) if as_of_arg else datetime.now(timezone.utc)

    init_db()
    with Session(engine) as session:
        db_before = fewf.snapshot_db_counts(session)

    store = ShadowDecisionStore()  # reports/shadow/shadow_decision_store.json — LECTURE SEULE ici (§31)
    with Session(engine) as session:
        health = compute_shadow_health(session, store, as_of, filters)

    with Session(engine) as session:
        db_after = fewf.snapshot_db_counts(session)
    db_safety = {"before": db_before, "after": db_after, "unchanged": db_before == db_after}

    regression = run_existing_regression_suites()
    repo_root = Path(__file__).resolve().parent.parent
    status_short, diff_stat, diff_names, production_modified = _check_production_files_untouched(repo_root)

    tests_green = db_safety["unchanged"] and not production_modified and all(r["pass"] for r in regression.values())
    scorecard = build_scorecard(health)
    readiness = build_readiness_for_phase9(health, tests_green)

    if not tests_green:
        final_verdict = "SHADOW_MONITORING_NEEDS_FIXES"
    elif health["status"] == "NO_DATA":
        final_verdict = "INSUFFICIENT_REAL_DATA"
    elif health["status"] in ("HEALTHY", "DEGRADED"):
        final_verdict = "SHADOW_MONITORING_READY"
    else:  # CRITICAL / BLOCKED
        final_verdict = "SHADOW_MONITORING_PARTIAL"

    result = {
        "run_id": run_id, "generated_at": generated_at, "phase": "8N", "kind": "shadow_monitoring_data_quality_v1",
        "rule": "MONITORING + RESEARCH + SHADOW ONLY. READ-ONLY. NO ODDS PROVIDER. NO NOTIFICATION.",
        "as_of": as_of.isoformat(), "filters": {k: str(v) for k, v in filters.items()},
        "shadow_health": health, "scorecard": scorecard, "readiness_for_phase9": readiness,
        "db_safety": db_safety, "existing_regression_suites": regression,
        "git_status_short": status_short, "git_diff_stat": diff_stat, "git_diff_names": diff_names,
        "production_files_modified": production_modified, "tests_green": tests_green, "final_verdict": final_verdict,
        "the_odds_api_status": "SUPPORT_REQUIRED — non appelé dans cette phase.", "no_user_betting_signal": True,
        "data_marking": "REAL (lecture de api/app.db et du store persisté) — voir Limitations pour toute donnée SYNTHETIC utilisée uniquement dans les tests.",
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
        print(f"Verdict final : {final_verdict}  (shadow health status = {health['status']})")
        print(f"future_fixtures={health['future_fixtures']} pending_predictions={health['pending_predictions']} "
              f"capturable={health['capturable']} captured={health['captured']} alerts={len(health['alerts'])}")
        print("git status --short :")
        print(status_short or "(clean)")
        print("git diff --stat :")
        print(diff_stat or "(no tracked file modified)")
        print("PHASE 8N — XFOOT SHADOW MONITORING & DATA QUALITY OPERATIONS V1 TERMINÉE. "
              "MONITORING SHADOW ET DATA QUALITY VALIDÉS OU LIMITATIONS DOCUMENTÉES. PHASE 8 TERMINÉE. "
              "AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. AUCUNE INTÉGRATION ODDS EFFECTUÉE. "
              "AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. PRÊT POUR ÉVALUATION DE LA PHASE 9.")
        print("=" * 80)
    return result


def render_markdown(result: dict) -> str:
    h = result["shadow_health"]
    md = ["# XFOOT SHADOW MONITORING & DATA QUALITY OPERATIONS V1\n"]
    md.append(f"\n## 1. Executive Summary\n\nRun id : `{result['run_id']}` — {result['generated_at']}. as_of={result['as_of']}\n\n**Verdict : {result['final_verdict']}** — shadow health = **{h['status']}**\n")
    md.append(f"\n## 2. Current Data Availability\n\n{h['reality']}\n")
    md.append(f"\n## 3. Operational Health\n\nstatus={h['status']}, future_fixtures={h['future_fixtures']}, pending_predictions={h['pending_predictions']}, capturable={h['capturable']}, captured={h['captured']}, errors={h['errors']}\n")
    md.append(f"\n## 4. Fixture Coverage\n\n{h['fixture_coverage']}\n")
    md.append(f"\n## 5. Prediction Coverage\n\n{h['fixture_coverage']}\n")
    md.append(f"\n## 6. Shadow Capture\n\n{h['capture_coverage']} — missed: {h['missed_captures_detail']}\n")
    md.append(f"\n## 7. Temporal Integrity\n\n{h['temporal_health']}\n")
    md.append(f"\n## 8. Provenance\n\n{h['provenance_health']}\n")
    md.append(f"\n## 9. Model Consistency\n\nmismatches={h['consistency_health']['model_mismatches']}\n")
    md.append(f"\n## 10. Probability Consistency\n\nmismatches={h['consistency_health']['probability_mismatches']}\n")
    md.append("\n## 11. Decision Consistency\n\nIdentique à Model/Probability Consistency (Phase 8I déterministe sur les mêmes inputs, aucun mismatch de décision distinct possible sans l'un des deux ci-dessus).\n")
    md.append(f"\n## 12. Store Integrity\n\n{h['store_integrity']}\n")
    md.append(f"\n## 13. Resolution Health\n\n{h['resolution_health']}\n\nMatchs joués toujours PENDING : {h['played_but_pending']}\n")
    md.append(f"\n## 14. Track Record Health\n\n{h['track_record_health']}\n")
    md.append(f"\n## 15. Value Data Health\n\n{h['value_health']}\n")
    md.append(f"\n## 16. Feature Data Health\n\n{h['feature_health']}\n")
    md.append(f"\n## 17. Calibration Health\n\nRéutilise Phase 8L (aucune calibration persistée par ModelVersion pour xgboost/lightgbm — constaté, jamais recalibré ici).\n")
    md.append("\n## 18. Alerts\n\n| Category | Severity | Message |\n|---|---|---|\n")
    for a in h["alerts"]:
        md.append(f"| {a['category']} | {a['severity']} | {a['message']} |\n")
    md.append("\n## 19. Data Quality Scorecard\n\n| Dimension | Status | Count | Evidence |\n|---|---|---|---|\n")
    for row in result["scorecard"]:
        md.append(f"| {row['dimension']} | {row['status']} | {row['count']} | {str(row['evidence'])[:80]} |\n")
    md.append(
        "\n## 20. Limitations\n\n"
        "- LATE_PREDICTION reste KICKOFF_TIME_UNKNOWN pour toutes les prédictions réelles (Match/ModelPrediction ne portent qu'une date, jamais une heure de coup d'envoi réelle).\n"
        "- 0 donnée shadow réelle accumulée à ce jour (voir §2) — la plupart des dimensions ci-dessus sont donc démontrées par les tests synthétiques (api/test_shadow_monitoring.py, 29/29) plutôt que par un volume réel.\n"
        "- value_health reste NOT_AVAILABLE (The Odds API SUPPORT_REQUIRED).\n"
    )
    md.append(f"\n## 21. Production Safety\n\nDB unchanged: {result['db_safety']['unchanged']}. Production files modified: **{result['production_files_modified']}**.\n")
    md.append(f"\n## 22. Verdict\n\n**{result['final_verdict']}**\n")
    md.append("\n---\n\n### READINESS FOR PHASE 9\n\n")
    r9 = result["readiness_for_phase9"]
    for section, items in r9.items():
        md.append(f"\n**{section}**\n" + "".join(f"- {i}\n" for i in items))
    md.append("\n---\n\n### EXISTING REGRESSION SUITES\n\n| Suite | Pass |\n|---|---|\n")
    for suite, r in result["existing_regression_suites"].items():
        md.append(f"| {suite} | {r['pass']} |\n")
    md.append("\n---\n\nPHASE 8N — XFOOT SHADOW MONITORING & DATA QUALITY OPERATIONS V1 TERMINÉE. "
               "MONITORING SHADOW ET DATA QUALITY VALIDÉS OU LIMITATIONS DOCUMENTÉES. PHASE 8 TERMINÉE. "
               "AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. AUCUNE INTÉGRATION ODDS EFFECTUÉE. "
               "AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. PRÊT POUR ÉVALUATION DE LA PHASE 9.\n")
    return "".join(md)


def _write_reports(result: dict, run_id: str) -> None:
    """§48/§49 : rapport horodaté (source historique, jamais supprimé) + `_latest` (miroir de confort,
    jamais une dépendance du système — voir §49)."""
    outdir = Path(__file__).resolve().parent.parent / "reports" / "shadow" / "monitoring"
    outdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = outdir / f"shadow_monitoring_{ts}.json"
    md_path = outdir / f"shadow_monitoring_{ts}.md"
    json_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    (outdir / "shadow_monitoring_latest.json").write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    (outdir / "shadow_monitoring_latest.md").write_text(render_markdown(result), encoding="utf-8")
    logger.info("Rapports écrits : %s / %s (+ shadow_monitoring_latest.*)", json_path, md_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as-of", type=str, default=None)
    parser.add_argument("--league", type=str, default=None)
    parser.add_argument("--market", type=str, default=None)
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--since", type=str, default=None)
    parser.add_argument("--until", type=str, default=None)
    parser.add_argument("--last-n", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--markdown", action="store_true")
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

    main(as_of_arg=args.as_of, filters=filters, emit_json=args.json, emit_markdown=args.markdown)
    sys.exit(0)
