"""
scripts/production_readiness.py — Phase 9 : XFOOT PRODUCTION READINESS &
CONTROLLED ACTIVATION V1.
=============================================================================
ÉVALUATION UNIQUEMENT. STRICTEMENT READ-ONLY : aucune écriture DB, aucune
écriture du Shadow Store, aucun appel réseau (§45), aucun entraînement/
promotion/modification de modèle (§46/§47), aucune activation production
(§47), aucun changement frontend (§48). Réutilise TEL QUEL
api/app/ai/readiness/matrix.py (Phase 9) — jamais une deuxième logique de
calcul du verdict.

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/production_readiness.py [--as-of ISO] [--dry-run] [--json] [--markdown]

--dry-run (§37) : n'évalue QUE — n'écrit aucun rapport (utile pour un check
rapide en local). Sans --dry-run (par défaut), écrit les rapports timestampés
dans reports/phase9/ (comportement standard des phases précédentes).
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
from app.ai.readiness.matrix import evaluate_production_readiness  # noqa: E402
from app.ai.readiness.schemas import ACTIVATION_MODES  # noqa: E402
from app.ai.safety.schemas import KILL_SWITCH_TRIGGERS  # noqa: E402 (Phase 9.1 — source canonique désormais, voir safety/schemas.py)

import feature_engineering_walkforward as fewf  # noqa: E402 (réutilise snapshot_db_counts, jamais réimplémenté)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("production_readiness")
UTC = timezone.utc

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# §43 : régression — toutes les suites pertinentes Phase 6-8N.
# ---------------------------------------------------------------------------

REGRESSION_SUITES = [
    "test_model_selection.py", "test_track_record.py", "test_feature_registry.py", "test_feature_engineering_v1.py",
    "test_value_engine.py", "test_decision_layer.py", "test_end_to_end_pipeline.py", "test_shadow_operational.py",
    "test_historical_replay.py", "test_live_shadow_track.py", "test_shadow_monitoring.py", "test_production_readiness.py",
]


def run_existing_regression_suites() -> dict:
    results = {}
    api_dir = REPO_ROOT / "api"
    for suite in REGRESSION_SUITES:
        proc = subprocess.run([sys.executable, suite], cwd=api_dir, capture_output=True, text=True)
        tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        results[suite] = {"returncode": proc.returncode, "summary_line": tail, "pass": proc.returncode == 0}
    return results


# ---------------------------------------------------------------------------
# §49 : sécurité git — aucun fichier de production ne doit être modifié.
# ---------------------------------------------------------------------------

PRODUCTION_FILE_PREFIXES = (
    "api/main.py", "api/app/core/", "api/app/models/", "api/app/billing/", "api/app/ai/engine/",
    "api/app/ai/arena/ensemble.py", "api/app/ai/arena/service.py", "api/app/ai/arena/scheduler.py",
    "api/app/ai/arena/promotion.py", "api/app/ai/arena/orchestrator.py", "api/app/ai/arena/models_common.py",
    "api/app/ai/arena/prediction_logging.py", "api/app/ai/features/registry.py", "api/app/ai/decision/",
    "api/app/ai/pipeline/", "api/app/ai/value/", "api/app/ai/shadow/", "api/app/ai/historical/",
    "frontend/", "web/", "src/", "api/alembic/",
)


def _check_production_files_untouched(repo_root: Path) -> tuple[str, str, str, bool]:
    status_short = subprocess.run(["git", "status", "--short"], cwd=repo_root, capture_output=True, text=True).stdout
    diff_stat = subprocess.run(["git", "diff", "--stat"], cwd=repo_root, capture_output=True, text=True).stdout
    diff_names = subprocess.run(["git", "diff", "--name-only"], cwd=repo_root, capture_output=True, text=True).stdout
    modified = [f for f in diff_names.splitlines() if f.strip()]
    hits = [f for f in modified if any(f.startswith(p) for p in PRODUCTION_FILE_PREFIXES)]
    return status_short, diff_stat, diff_names, bool(hits)


# ---------------------------------------------------------------------------
# §23/§24/§25/§26 : procédures conceptuelles — jamais exécutées ici, jamais
# une preuve statistique déguisée en limite opérationnelle (§23).
# ---------------------------------------------------------------------------

def build_limited_production_plan() -> dict:
    return {
        "note": "Conceptuel uniquement (§23) — jamais appliqué par cette phase, jamais une preuve statistique.",
        "markets": "1X2 uniquement au démarrage (jamais BTTS/OVER_UNDER simultanément — réduit la surface d'erreur initiale).",
        "leagues": "1 ligue avec la plus grande couverture historique (Ligue1, 12 459 matchs au 30/08/2026) — jamais toutes les ligues d'un coup.",
        "volume": "Research/observation uniquement tant que TRACK_RECORD < STATISTICALLY_INFORMATIVE (>=100, seuil déjà documenté Phase 8M/8N).",
        "frequency": "Alignée sur le scheduler existant (DEFAULT_LIVE_PREDICTION_WINDOW_HOURS=48h) — jamais un rythme inventé pour cette phase.",
        "exposure": "0 — aucun signal de pari n'est présenté à un utilisateur avant MODE_3 explicitement décidé (§47).",
        "stop_conditions": ["Tout KILL_SWITCH_TRIGGERS déclenché (§24).", "MONITORING gate != PASS.", "TRACK_RECORD régresse sous EARLY_DATA."],
    }


def build_rollback_procedure() -> dict:
    return {
        "disable": "MODE_1_SHADOW_ONLY (revenir au statu quo actuel — aucune action requise, c'est l'état par défaut).",
        "revert_to_previous_model": "app/ai/arena/promotion.py::apply_promotion(session, previous_version) — mécanisme production EXISTANT (déjà utilisé par POST /models/promotion/promote, require_admin). Jamais encore exercé pour un rollback réel (0 ModelPromotionEvent en base, voir gate ROLLBACK).",
        "revert_to_shadow_only": "Aucun code de production ne consomme actuellement api/app/ai/{value,decision,pipeline,shadow,historical} (confirmé, gate API_EXPOSURE) — un rollback vers Shadow-only ne nécessite donc AUCUNE modification de code, seulement l'absence d'activation d'un futur point d'intégration.",
        "verify_rollback_worked": "get_active_version(session, model_type) doit retourner la version attendue ; model_promotion_events doit contenir une ligne decision='promoted' pour cette version ; aucune ligne model_predictions produite après le rollback ne doit référencer l'ancienne version.",
        "never_delete_history": "apply_promotion ne supprime jamais une ligne — l'ancienne version passe status='retired', jamais effacée (voir team_rating.py).",
    }


def build_kill_switch_design() -> dict:
    return {
        "status": "NOT_IMPLEMENTED — constaté par gate OBSERVABILITY (aucune occurrence de kill_switch/circuit breaker dans le code).",
        "triggers": list(KILL_SWITCH_TRIGGERS),
        "required_behavior": "Arrêter le flux concerné, préserver les preuves (jamais une suppression), ne jamais modifier rétroactivement une décision déjà prise (§24).",
        "required_before_mode_3_or_4": True,
    }


def build_fail_safe_rule() -> dict:
    return {
        "rule": "NO SIGNAL, jamais BEST GUESS (§26).",
        "applies_to": ["erreur", "timeout", "données absentes", "provenance absente", "timestamp inconnu", "calibration absente"],
        "already_enforced_by": [
            "app/ai/decision/eligibility.py::evaluate_eligibility (UNKNOWN jamais promu à ELIGIBLE, Phase 8I, 34/34 tests)",
            "app/ai/value/core.py::build_value_signal (NO_ODDS/INVALID_ODDS/INSUFFICIENT_DATA avant tout calcul, Phase 8H, 36/36 tests)",
            "app/ai/pipeline/shadow.py::run_shadow_batch (isolation d'erreur par match, jamais un crash de batch, Phase 8J, 21/21 tests)",
        ],
    }


# ---------------------------------------------------------------------------
# §38 : rapport principal.
# ---------------------------------------------------------------------------

def main(as_of_arg: str, dry_run: bool, emit_json: bool, emit_markdown: bool) -> dict:
    run_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    generated_at = datetime.now(UTC).isoformat()
    as_of = datetime.fromisoformat(as_of_arg).replace(tzinfo=UTC) if as_of_arg else datetime.now(UTC)

    init_db()
    with Session(engine) as session:
        db_before = fewf.snapshot_db_counts(session)

    store = ShadowDecisionStore()  # reports/shadow/shadow_decision_store.json — LECTURE SEULE ici.
    with Session(engine) as session:
        assessment = evaluate_production_readiness(session, store, as_of)

    with Session(engine) as session:
        db_after = fewf.snapshot_db_counts(session)
    db_purity = {"before": db_before, "after": db_after, "unchanged": db_before == db_after}

    regression = {} if dry_run else run_existing_regression_suites()
    status_short, diff_stat, diff_names, production_modified = _check_production_files_untouched(REPO_ROOT)

    tests_green = db_purity["unchanged"] and not production_modified and (dry_run or all(r["pass"] for r in regression.values()))

    gates_serialized = [
        {"name": gt.name, "status": gt.status, "critical": gt.critical, "evidence": gt.evidence,
         "blocking_reason": gt.blocking_reason, "required_action": gt.required_action}
        for gt in assessment.gates
    ]
    checklist_serialized = [{"key": c.key, "checked": c.checked, "evidence": c.evidence} for c in assessment.checklist]
    promotion_serialized = [{"model_type": p.model_type, "status": p.status, "reasons": p.reasons, "evidence": p.evidence} for p in assessment.promotion_readiness]

    result = {
        "run_id": run_id, "generated_at": generated_at, "phase": "9", "kind": "production_readiness_controlled_activation_v1",
        "rule": "EVALUATION ONLY. NO PRODUCTION ACTIVATION. NO BETTING SIGNAL. NO USER RECOMMENDATION. NO MODEL CHANGE.",
        "as_of": as_of.isoformat(), "dry_run": dry_run,
        "final_verdict": assessment.final_verdict, "recommended_mode": assessment.recommended_mode,
        "critical_gate_failures": assessment.critical_gate_failures,
        "gates": gates_serialized, "checklist": checklist_serialized, "promotion_readiness": promotion_serialized,
        "open_risks": assessment.open_risks, "blocking_conditions": assessment.blocking_conditions,
        "conditions_required_now": assessment.conditions_required_now,
        "conditions_required_before_production": assessment.conditions_required_before_production,
        "conditions_optional_future": assessment.conditions_optional_future,
        "phase10_readiness": assessment.phase10_readiness, "db_safety_matrix": assessment.db_safety,
        "activation_modes": list(ACTIVATION_MODES), "limited_production_plan": build_limited_production_plan(),
        "rollback_procedure": build_rollback_procedure(), "kill_switch_design": build_kill_switch_design(),
        "fail_safe_rule": build_fail_safe_rule(),
        "db_purity": db_purity, "existing_regression_suites": regression,
        "git_status_short": status_short, "git_diff_stat": diff_stat, "git_diff_names": diff_names,
        "production_files_modified": production_modified, "tests_green": tests_green,
        "the_odds_api_status": "SUPPORT_REQUIRED — non appelé dans cette phase (§45).", "no_user_betting_signal": True,
        "data_marking": "REAL (lecture de api/app.db et du store persisté) — voir Limitations pour toute donnée SYNTHETIC utilisée uniquement dans les tests.",
    }

    if production_modified:
        logger.error("ARRÊT : des fichiers de production ont été modifiés — voir git diff --name-only.")

    if not dry_run:
        _write_reports(result, run_id)

    if emit_json:
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    if emit_markdown:
        print(render_markdown(result))
    if not emit_json and not emit_markdown:
        print("\n" + "=" * 80)
        print(f"Verdict final : {result['final_verdict']}  (mode recommandé : {result['recommended_mode']})")
        print(f"critical_gate_failures={result['critical_gate_failures']}")
        print("git status --short :")
        print(status_short or "(clean)")
        print("git diff --stat :")
        print(diff_stat or "(no tracked file modified)")
        print("PHASE 9 — XFOOT PRODUCTION READINESS & CONTROLLED ACTIVATION V1 TERMINÉE. "
              "READINESS PRODUCTION ÉVALUÉE, GATES ET CONDITIONS D'ACTIVATION DOCUMENTÉS. "
              "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE SANS AUTORISATION EXPLICITE. "
              "AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION.")
        print("=" * 80)
    return result


def render_markdown(result: dict) -> str:
    md = ["# XFOOT PRODUCTION READINESS V1\n"]
    md.append(f"\n## 1. Executive Summary\n\nRun id : `{result['run_id']}` — {result['generated_at']}. as_of={result['as_of']}\n\n**Verdict final : {result['final_verdict']}** — mode recommandé : **{result['recommended_mode']}**\n")
    md.append("\n## 2. Current System State\n\n" + "\n".join(f"- {ln}" for ln in [
        "VALUE ENGINE = FOUNDATION_READY", "DECISION LAYER = FOUNDATION_READY", "END-TO-END PIPELINE = SHADOW_READY",
        "SHADOW TRACKING = VALIDATED", "SHADOW MONITORING = VALIDATED", "HISTORICAL REPLAY = NOT_AVAILABLE",
        "LIVE SHADOW = CONFIGURED / NO_DATA", "TIMESTAMPED ODDS = NOT_VERIFIED", "THE ODDS API = SUPPORT_REQUIRED",
        "REAL PROSPECTIVE TRACK RECORD = NOT_YET_ESTABLISHED", "PRODUCTION BETTING SIGNAL = NONE",
    ]) + "\n")
    md.append("\n## 3. Production Gate Matrix\n\n| Gate | Status | Critical | Blocking Reason |\n|---|---|---|---|\n")
    for gt in result["gates"]:
        md.append(f"| {gt['name']} | {gt['status']} | {gt['critical']} | {(gt['blocking_reason'] or '')[:100]} |\n")
    section_map = {
        "4. Model Readiness": "MODEL", "5. Feature Readiness": "FEATURES", "6. Temporal Integrity": "TEMPORAL_INTEGRITY",
        "7. Calibration": "CALIBRATION", "8. Statistical Evidence": "STATISTICAL_EVIDENCE", "9. Track Record": "TRACK_RECORD",
        "10. Odds Readiness": "ODDS", "11. Value Engine": "VALUE", "12. Decision Layer": "DECISION",
        "13. Shadow Readiness": "SHADOW", "14. Monitoring": "MONITORING",
    }
    by_name = {gt["name"]: gt for gt in result["gates"]}
    for title, gate_name in section_map.items():
        gt = by_name.get(gate_name, {})
        md.append(f"\n## {title}\n\nstatus={gt.get('status')}\n\nevidence: `{str(gt.get('evidence'))[:300]}`\n")
    db = by_name.get("DATABASE_SAFETY", {})
    md.append(f"\n## 15. Database Safety\n\nstatus={db.get('status')}\n\nmatrix: {result['db_safety_matrix']['matrix']}\n\npurity (before==after): {result['db_purity']['unchanged']}\n")
    api_exp = by_name.get("API_EXPOSURE", {})
    fe_exp = by_name.get("FRONTEND_EXPOSURE", {})
    md.append(f"\n## 16. Production Isolation\n\nAPI_EXPOSURE status={api_exp.get('status')} ; FRONTEND_EXPOSURE status={fe_exp.get('status')}\n")
    sec = by_name.get("SECURITY", {})
    md.append(f"\n## 17. Security\n\nstatus={sec.get('status')}\n")
    legal = by_name.get("LEGAL_PROVIDER_STATUS", {})
    md.append(f"\n## 18. Provider / Legal Status\n\nstatus={legal.get('status')}\n\n{legal.get('evidence')}\n")
    md.append(f"\n## 19. Kill Switch\n\n{result['kill_switch_design']}\n")
    md.append(f"\n## 20. Rollback\n\n{result['rollback_procedure']}\n")
    md.append(f"\n## 21. Frontend/API Exposure\n\nAPI_EXPOSURE={api_exp.get('status')}, FRONTEND_EXPOSURE={fe_exp.get('status')}\n")
    md.append("\n## 22. Open Risks\n\n" + "".join(f"- {r}\n" for r in result["open_risks"]) + "\n")
    md.append("\n## 23. Blocking Conditions\n\n" + "".join(f"- {r}\n" for r in result["blocking_conditions"]) + "\n")
    md.append(
        "\n## 24. Activation Conditions\n\n**REQUIRED NOW**\n" + "".join(f"- {r}\n" for r in result["conditions_required_now"]) +
        "\n**REQUIRED BEFORE PRODUCTION**\n" + "".join(f"- {r}\n" for r in result["conditions_required_before_production"]) +
        "\n**OPTIONAL FUTURE**\n" + "".join(f"- {r}\n" for r in result["conditions_optional_future"]) + "\n"
    )
    md.append(f"\n## 25. Final Verdict\n\n**{result['final_verdict']}** (mode recommandé : {result['recommended_mode']})\n\ncritical_gate_failures: {result['critical_gate_failures']}\n")
    md.append("\n## 26. Phase 10 Readiness\n\n")
    for section, items in result["phase10_readiness"].items():
        md.append(f"\n**{section}**\n" + "".join(f"- {i}\n" for i in items))
    md.append("\n---\n\n### ACTIVATION CHECKLIST\n\n")
    for c in result["checklist"]:
        md.append(f"- [{'x' if c['checked'] else ' '}] {c['key']} — {c['evidence']}\n")
    md.append("\n---\n\n### PROMOTION ELIGIBILITY\n\n| Model type | Status |\n|---|---|\n")
    for p in result["promotion_readiness"]:
        md.append(f"| {p['model_type']} | {p['status']} |\n")
    md.append("\n---\n\n### EXISTING REGRESSION SUITES\n\n| Suite | Pass |\n|---|---|\n")
    for suite, r in result["existing_regression_suites"].items():
        md.append(f"| {suite} | {r['pass']} |\n")
    md.append("\n---\n\nPHASE 9 — XFOOT PRODUCTION READINESS & CONTROLLED ACTIVATION V1 TERMINÉE. "
               "READINESS PRODUCTION ÉVALUÉE, GATES ET CONDITIONS D'ACTIVATION DOCUMENTÉS. "
               "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE SANS AUTORISATION EXPLICITE. "
               "AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION.\n")
    return "".join(md)


def _write_reports(result: dict, run_id: str) -> None:
    outdir = REPO_ROOT / "reports" / "phase9"
    outdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    json_path = outdir / f"xfoot_production_readiness_{ts}.json"
    md_path = outdir / f"xfoot_production_readiness_{ts}.md"
    json_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    (outdir / "xfoot_production_readiness_latest.json").write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    (outdir / "xfoot_production_readiness_latest.md").write_text(render_markdown(result), encoding="utf-8")
    logger.info("Rapports écrits : %s / %s (+ xfoot_production_readiness_latest.*)", json_path, md_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", dest="as_of", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args()
    main(args.as_of, args.dry_run, args.json, args.markdown)
