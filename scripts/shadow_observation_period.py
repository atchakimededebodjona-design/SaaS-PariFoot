"""
scripts/shadow_observation_period.py — Phase 12 : XFOOT CONTROLLED SHADOW
OBSERVATION PERIOD V1.
=============================================================================
§2 du prompt Phase 12 : "NOUVEAU CODE : MINIMUM ABSOLU — avant de créer un
fichier, démontrer qu'une capacité existante ne peut pas être réutilisée."
L'infrastructure Phase 8K-11 couvre déjà DISCOVER/CAPTURE/VERIFY/RESOLVE/
TRACK/MONITOR/READINESS/MODE_2 evaluation/evidence history/blocker evolution
— ce script n'y ajoute AUCUNE logique de capture/résolution/track record. Il
se contente de :

  1. Exécuter RÉELLEMENT (§40, jamais simulé) le runner Phase 11
     (scripts/internal_shadow_operation.py, --dry-run puis --monitor) et le
     watch Phase 9.5 (scripts/shadow_evidence_watch.py) — AUCUNE opération
     de capture/résolution n'est réimplémentée ici, uniquement invoquée.
  2. Lire les rapports RÉELS déjà écrits (reports/phase10/, reports/phase11/)
     pour construire une comparaison à 3 colonnes (Phase 10 / Phase 11 /
     Phase 12) — réutilise app.ai.shadow.internal_operation.
     compare_to_phase10_baseline (Phase 11) TEL QUEL, appelée deux fois avec
     des baselines différentes (Phase 10, puis Phase 11) — AUCUNE nouvelle
     fonction de comparaison.
  3. Réutiliser app.ai.shadow.evidence.build_activation_matrix (Phase 9.3)
     pour la matrice MODE_1-4 (§37) — le SEUL calcul local ajouté ici est une
     boucle de ~5 lignes qui compare `readiness_critical_failures` (déjà
     produit par Phase 11) aux `critical_gates_required` de chaque mode —
     jamais un nouveau gate, jamais un nouveau seuil.
  4. Assembler le rapport Phase 12 (§42) et l'écrire dans reports/phase12/.

Le verdict et le statut de human review (§45/§36) sont repris TELS QUELS du
rapport Phase 11 fraîchement régénéré — cette phase n'invente aucune
nouvelle classification d'évidence (§2 : "l'objectif est CONTROLLED SHADOW
OBSERVATION", pas une nouvelle couche de décision).

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/shadow_observation_period.py [--json] [--markdown]
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

from app.ai.shadow.internal_operation import OPERATING_MODE, assert_mode_1_only, compare_to_phase10_baseline  # noqa: E402
from app.ai.shadow.evidence import build_activation_matrix  # noqa: E402
from app.ai.readiness.schemas import CRITICAL_GATES  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("shadow_observation_period")
UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parent.parent
MARKETS = ("1X2", "BTTS", "OVER_UNDER_2_5")

# §34 : cadence recommandée — PUREMENT documentaire (jamais automatisée, aucun scheduler/cron créé).
RECOMMENDED_OBSERVATION_CADENCE = {
    "before_matches": "python scripts/internal_shadow_operation.py --dry-run  (puis --capture si de vraies candidates existent)",
    "after_matches": "python scripts/internal_shadow_operation.py --resolve",
    "periodic_check": "python scripts/shadow_evidence_watch.py --monitor",
    "reassessment": "python scripts/shadow_observation_period.py  (ce script — comparaison longitudinale)",
    "note": "§34 : aucune automatisation créée (pas de tâche Windows/cron) — cadence manuelle, opérateur humain déclenche chaque étape.",
}

REGRESSION_SUITES = [
    "test_model_selection.py", "test_track_record.py", "test_feature_registry.py", "test_feature_engineering_v1.py",
    "test_value_engine.py", "test_decision_layer.py", "test_end_to_end_pipeline.py", "test_shadow_operational.py",
    "test_historical_replay.py", "test_live_shadow_track.py", "test_shadow_monitoring.py",
    "test_production_readiness.py", "test_safety_controls.py", "test_prospective_shadow.py",
    "test_phase9_3.py", "test_phase9_4.py", "test_phase9_5.py", "test_phase10.py", "test_phase11.py",
]


def run_existing_regression_suites() -> dict:
    """§39 (prompt Phase 11, réaffirmé ici) : PASSED/FAILED/NOT_RUN explicite — jamais une suite non exécutée
    comptée comme PASS."""
    results = {}
    api_dir = REPO_ROOT / "api"
    for suite in REGRESSION_SUITES:
        proc = subprocess.run([sys.executable, suite], cwd=api_dir, capture_output=True, text=True)
        tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        results[suite] = {"status": "PASSED" if proc.returncode == 0 else "FAILED", "returncode": proc.returncode, "summary_line": tail, "pass": proc.returncode == 0}
    return results


def run_this_phase_tests() -> dict:
    api_dir = REPO_ROOT / "api"
    test_file = api_dir / "test_phase12.py"
    if not test_file.exists():
        return {"status": "NOT_RUN", "returncode": None, "summary_line": "api/test_phase12.py absent (§39 : aucune nouvelle garantie nécessitant un test dédié).", "pass": True}
    proc = subprocess.run([sys.executable, "test_phase12.py"], cwd=api_dir, capture_output=True, text=True)
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    return {"status": "PASSED" if proc.returncode == 0 else "FAILED", "returncode": proc.returncode, "summary_line": tail, "pass": proc.returncode == 0}


def run_real_execution_log() -> dict:
    """§40 : exécute RÉELLEMENT les commandes CLI littéralement prescrites (jamais simulées)."""
    log = {}
    commands = [
        ("scripts/internal_shadow_operation.py --dry-run", [sys.executable, "scripts/internal_shadow_operation.py", "--dry-run"]),
        ("scripts/internal_shadow_operation.py --monitor", [sys.executable, "scripts/internal_shadow_operation.py", "--monitor"]),
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


def find_latest_report(dirpath: Path, prefix: str) -> Optional[dict]:
    """Rapport RÉEL déjà écrit sur disque, jamais une valeur supposée — None si aucun n'existe encore
    (limitation honnêtement documentée, jamais fabriquée). Généralise find_latest_phase10_report (Phase 11)
    à n'importe quel répertoire de rapports."""
    if not dirpath.exists():
        return None
    candidates = sorted(dirpath.glob(f"{prefix}_*.json"))
    if not candidates:
        return None
    try:
        return json.loads(candidates[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Rapport illisible (%s) : %s — ignoré, jamais fabriqué.", candidates[-1], e)
        return None


def _track_record_sample_size(report: dict) -> int:
    tr = report.get("track_record") or {}
    return sum((tr.get(m, {}) or {}).get("sample_size") or 0 for m in MARKETS)


def _gate_statuses_from_critical_failures(critical_failures: list) -> dict:
    """§39 (prompt Phase 11) : ni Phase 10 ni Phase 11 n'exposent nécessairement le même format de statut par
    gate (Phase 10 a un `checklist` détaillé, Phase 11 seulement `readiness_critical_failures`) — dérivation
    UNIFORME à partir de la liste des gates critiques en échec (CRITICAL_GATES, Phase 9, réutilisé tel quel) :
    absent de la liste -> PASS, présent -> FAIL. Jamais une inférence au-delà de PASS/FAIL."""
    return {name: ("FAIL" if name in critical_failures else "PASS") for name in CRITICAL_GATES}


def build_activation_matrix_status(readiness_critical_failures: list) -> dict:
    """§37 : matrice MODE_1-4, `activated=False` pour tous, TOUJOURS. Réutilise build_activation_matrix
    (Phase 9.3) pour la description/prérequis — le SEUL calcul ajouté ici est la comparaison des
    `critical_gates_required` de chaque mode à `readiness_critical_failures` déjà produit par Phase 11."""
    matrix = build_activation_matrix()
    out = {}
    for mode_key, spec in matrix.items():
        required = list(CRITICAL_GATES) if spec["critical_gates_required"] == ["ALL"] else spec["critical_gates_required"]
        unmet = [g for g in required if g in readiness_critical_failures]
        out[mode_key] = {
            "description": spec["description"], "required_gates": required, "unmet_gates": unmet,
            "conditions_met": not unmet, "activated": False,
            "note": "DOCUMENTARY_ONLY (§26/§37) — jamais activé automatiquement, quel que soit le résultat.",
        }
    return out


def main(emit_json: bool, emit_markdown: bool) -> dict:
    assert_mode_1_only(OPERATING_MODE)  # §4 : défense en profondeur, jamais dérivé d'un argument utilisateur.

    run_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    generated_at = datetime.now(UTC).isoformat()

    # §29 (implicite) / §38 : baselines RÉELLES capturées AVANT toute nouvelle exécution.
    phase10_baseline = find_latest_report(REPO_ROOT / "reports" / "phase10", "xfoot_phase10")
    phase11_baseline = find_latest_report(REPO_ROOT / "reports" / "phase11", "xfoot_phase11")

    # §40 : REAL EXECUTION — jamais simulée. C'est ICI que DISCOVER/CAPTURE/VERIFY/MONITOR/READINESS/
    # MODE_2 evaluation/evidence history/blocker evolution se produisent réellement, via le runner Phase 11
    # (jamais réimplémentés dans ce script, §2/§3).
    real_execution_log = run_real_execution_log()

    # État "Phase 12 courant" = le rapport Phase 11 le PLUS RÉCENT, désormais régénéré par les commandes
    # ci-dessus (--monitor, la dernière invocation, la plus complète) — jamais recalculé séparément ici.
    phase12_current = find_latest_report(REPO_ROOT / "reports" / "phase11", "xfoot_phase11")

    status_short, diff_stat, diff_names, production_modified = _check_production_files_untouched(REPO_ROOT)
    regression = run_existing_regression_suites()
    this_phase_tests = run_this_phase_tests()

    tests_green = (not production_modified and this_phase_tests["pass"] and all(r["pass"] for r in regression.values())
                   and all(r["pass"] for r in real_execution_log.values()))

    if phase12_current is None:
        logger.error("Aucun rapport Phase 11 disponible après exécution réelle — situation anormale, STOP.")
        final_verdict = "BLOCKED"
        human_review_status = "NOT_READY_FOR_HUMAN_REVIEW"
        readiness_verdict = "UNKNOWN"
        readiness_critical_failures = []
        comparison_vs_phase10 = {"status": "NO_CURRENT_DATA"}
        comparison_vs_phase11 = {"status": "NO_CURRENT_DATA"}
        activation_matrix = {}
    elif not tests_green:
        final_verdict = "NEEDS_FIXES"
        human_review_status = phase12_current.get("human_review_status", "NOT_READY_FOR_HUMAN_REVIEW")
        readiness_verdict = phase12_current.get("readiness_verdict", "UNKNOWN")
        readiness_critical_failures = phase12_current.get("readiness_critical_failures", [])
        comparison_vs_phase10 = {"status": "SKIPPED_TESTS_NOT_GREEN"}
        comparison_vs_phase11 = {"status": "SKIPPED_TESTS_NOT_GREEN"}
        activation_matrix = build_activation_matrix_status(readiness_critical_failures)
    else:
        # §45/§36 : verdict et human review REPRIS TELS QUELS du rapport Phase 11 — aucune reclassification
        # d'évidence n'est introduite par cette phase (§2).
        final_verdict = phase12_current.get("final_verdict", "NEEDS_FIXES")
        human_review_status = phase12_current.get("human_review_status", "NOT_READY_FOR_HUMAN_REVIEW")
        readiness_verdict = phase12_current.get("readiness_verdict", "UNKNOWN")
        readiness_critical_failures = phase12_current.get("readiness_critical_failures", [])
        current_full_ledger = phase12_current.get("full_evidence_ledger", {}) or {}
        current_gate_statuses = _gate_statuses_from_critical_failures(readiness_critical_failures)

        if phase10_baseline is not None:
            baseline_full_ledger = phase10_baseline.get("full_evidence_ledger", {}) or {}
            comparison_vs_phase10 = compare_to_phase10_baseline(
                current_readiness_verdict=readiness_verdict, baseline_readiness_verdict=phase10_baseline.get("readiness_verdict", "UNKNOWN"),
                current_real_prospective_count=current_full_ledger.get("real_prospective_resolved_count", 0),
                baseline_real_prospective_count=baseline_full_ledger.get("real_prospective_resolved_count", 0),
                current_track_record_sample_size=_track_record_sample_size(phase12_current), baseline_track_record_sample_size=_track_record_sample_size(phase10_baseline),
                current_provenance_complete=current_full_ledger.get("provenance_complete", 0), baseline_provenance_complete=baseline_full_ledger.get("provenance_complete", 0),
                current_gate_statuses=current_gate_statuses,
                # Restreint aux CRITICAL_GATES : le dict "current" (dérivé de readiness_critical_failures) ne
                # couvre QUE ces 9 gates — comparer contre le checklist Phase 10 complet (16 items, incluant
                # API_EXPOSURE/FRONTEND_EXPOSURE/SECURITY/ODDS/VALUE) produirait de faux REGRESSED pour des
                # gates jamais mesurées côté "current" (§37 : jamais fabriquer un résultat).
                baseline_gate_statuses={c["gate"]: c["status"] for c in phase10_baseline.get("checklist", []) if c["gate"] in CRITICAL_GATES},
            )
            comparison_vs_phase10["baseline_run_id"] = phase10_baseline.get("run_id")
        else:
            comparison_vs_phase10 = {"status": "NO_BASELINE_AVAILABLE", "reason": "Aucun rapport reports/phase10/*.json trouvé."}

        if phase11_baseline is not None and phase11_baseline.get("run_id") != phase12_current.get("run_id"):
            baseline_full_ledger = phase11_baseline.get("full_evidence_ledger", {}) or {}
            comparison_vs_phase11 = compare_to_phase10_baseline(
                current_readiness_verdict=readiness_verdict, baseline_readiness_verdict=phase11_baseline.get("readiness_verdict", "UNKNOWN"),
                current_real_prospective_count=current_full_ledger.get("real_prospective_resolved_count", 0),
                baseline_real_prospective_count=baseline_full_ledger.get("real_prospective_resolved_count", 0),
                current_track_record_sample_size=_track_record_sample_size(phase12_current), baseline_track_record_sample_size=_track_record_sample_size(phase11_baseline),
                current_provenance_complete=current_full_ledger.get("provenance_complete", 0), baseline_provenance_complete=baseline_full_ledger.get("provenance_complete", 0),
                current_gate_statuses=current_gate_statuses,
                baseline_gate_statuses=_gate_statuses_from_critical_failures(phase11_baseline.get("readiness_critical_failures", [])),
            )
            comparison_vs_phase11["baseline_run_id"] = phase11_baseline.get("run_id")
        else:
            comparison_vs_phase11 = {"status": "NO_PRIOR_PHASE11_BASELINE_AVAILABLE",
                                      "reason": "Aucun rapport Phase 11 antérieur à cette exécution (première fois, ou seul le rapport généré à l'instant existe)."}

        activation_matrix = build_activation_matrix_status(readiness_critical_failures)

    result = {
        "run_id": run_id, "generated_at": generated_at, "phase": "12", "kind": "controlled_shadow_observation_period_v1",
        "rule": "CONTROLLED SHADOW OBSERVATION. MODE_1_SHADOW_ONLY. NO PRODUCTION ACTIVATION. NO MODE_2/3/4 ACTIVATION.",
        "operating_mode": OPERATING_MODE, "recommended_cadence": RECOMMENDED_OBSERVATION_CADENCE,
        "real_execution_log": real_execution_log,
        "phase10_baseline_run_id": phase10_baseline.get("run_id") if phase10_baseline else None,
        "phase11_baseline_run_id": phase11_baseline.get("run_id") if phase11_baseline else None,
        "phase12_current_run_id": phase12_current.get("run_id") if phase12_current else None,
        "readiness_verdict": readiness_verdict, "readiness_critical_failures": readiness_critical_failures,
        "activation_matrix": activation_matrix,
        "comparison_vs_phase10": comparison_vs_phase10, "comparison_vs_phase11": comparison_vs_phase11,
        "phase12_current_report": phase12_current,
        "human_review_status": human_review_status,
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
        print(f"Verdict final : {final_verdict}  (readiness={readiness_verdict}, human_review={human_review_status})")
        print(f"Comparison vs Phase 10 : {comparison_vs_phase10.get('status', 'ok')}")
        print(f"Comparison vs Phase 11 : {comparison_vs_phase11.get('status', 'ok')}")
        print("git status --short :")
        print(status_short or "(clean)")
        print("git diff --stat :")
        print(diff_stat or "(no tracked file modified)")
        print("PHASE 12 — XFOOT CONTROLLED SHADOW OBSERVATION PERIOD V1 TERMINÉE. "
              "OPÉRATION SHADOW CONTRÔLÉE OBSERVÉE SUR DONNÉES RÉELLES OU LIMITATIONS DOCUMENTÉES. "
              "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION HUMAINE.")
        print("=" * 80)
    return result


def render_markdown(result: dict) -> str:
    md = ["# XFOOT PHASE 12\n\n# CONTROLLED SHADOW OBSERVATION PERIOD\n"]
    md.append(f"\n## 1. Executive Summary\n\nRun id `{result['run_id']}` — {result['generated_at']}\n\n"
               f"**Verdict : {result['final_verdict']}** — {result['human_review_status']}\n")
    md.append(f"\n## 2. Operating Period\n\n{result['recommended_cadence']}\n")
    md.append(f"\n## 3. Operating Mode\n\n{result['operating_mode']} — MODE_2/3/4 refusés structurellement (§4).\n")
    pc = result.get("phase12_current_report") or {}
    md.append(f"\n## 4. Preflight\n\n{pc.get('preflight', 'Voir rapport Phase 11 référencé (run_id={0}) — non recalculé ici, §2.'.format(result['phase12_current_run_id']))}\n")
    md.append(f"\n## 5. Discovery\n\n{pc.get('capture_outcome', {}).get('candidates', 'N/A')} candidate(s) — détail complet dans le rapport Phase 11 référencé.\n")
    md.append(f"\n## 6. Capture\n\n{pc.get('capture_outcome', 'N/A')}\n")
    md.append("\n## 7. Temporal Integrity\n\nVoir rapport Phase 11 référencé — vocabulaire réutilisé, jamais recalculé ici.\n")
    md.append(f"\n## 8. Provenance\n\n{(pc.get('full_evidence_ledger') or {}).get('provenance_complete', 'N/A')} complete / "
               f"{(pc.get('full_evidence_ledger') or {}).get('provenance_incomplete', 'N/A')} incomplete\n")
    md.append(f"\n## 9. Consistency\n\nmismatches={pc.get('capture_outcome', {}).get('mismatches', 'N/A')}\n")
    md.append(f"\n## 10. Resolution\n\n{pc.get('resolution_summary', 'N/A')}\n")
    md.append(f"\n## 11. Track Record\n\n{pc.get('track_record', 'N/A')}\n")
    md.append(f"\n## 12. Maturity\n\n{pc.get('maturity', 'N/A')}\n")
    md.append(f"\n## 13. Model Versions\n\n{pc.get('model_version_tracking', 'N/A')}\n")
    md.append(f"\n## 14. League Breakdown\n\nVoir breakdown.by_league dans le rapport Phase 11 référencé : {pc.get('breakdown', 'N/A')}\n")
    md.append(f"\n## 15. Market Breakdown\n\n{pc.get('breakdown', 'N/A')}\n")
    md.append(f"\n## 16. Temporal Drift\n\n{pc.get('temporal_drift', 'N/A')}\n")
    md.append(f"\n## 17. Monitoring\n\nstatus={pc.get('shadow_health_status', 'N/A')}\n")
    md.append(f"\n## 18. Evidence History\n\n{pc.get('longitudinal_history_count', 'N/A')} snapshot(s) au total (append-only, "
               f"MÊME historique que Phase 9.5/11) — trend={pc.get('evidence_trend', 'N/A')}\n")
    md.append(f"\n## 19. Blocker Evolution\n\n{pc.get('blocker_evolution', 'N/A')}\n")
    md.append(f"\n## 20. Readiness\n\nverdict={result['readiness_verdict']} (critical failures: {result['readiness_critical_failures']})\n")
    md.append(f"\n## 21. MODE_2 Documentary Evaluation\n\n{pc.get('mode2_evaluation', 'N/A')}\n")
    md.append(f"\n## 22. Safety\n\nKill Switch / preflight — voir rapport Phase 11 référencé. Jamais reset en production (§29).\n")
    md.append("\n## 23. Rollback\n\nDémonstration empirique sur DB ISOLÉE uniquement (api/test_phase11.py, api/test_phase10.py, "
               "api/test_safety_controls.py) — jamais api/app.db.\n")
    md.append(f"\n## 24. Database Safety\n\n{pc.get('db_safety', 'N/A')}\n")
    md.append(f"\n## 25. Production Isolation\n\nmode={result['mode']}, production_activation={result['production_activation']}. "
               "Ce script n'appelle jamais capture/resolve/training/promotion — uniquement le runner Phase 11 en sous-processus.\n")
    md.append(f"\n## 26. Odds\n\n{(pc.get('track_record') or {}).get('value_tracking', 'NOT_AVAILABLE')}\n")
    md.append("\n## 27. Value\n\nSans odds temporellement vérifiées : VALUE=NOT_AVAILABLE, aucun signal de pari (§28).\n")
    md.append(f"\n## 28. Human Review\n\n**{result['human_review_status']}** — ne signifie jamais production ready (§36).\n")
    md.append("\n## 29. Phase 10/11/12 Comparison\n\n### vs Phase 10\n\n" + str(result["comparison_vs_phase10"]) +
               "\n\n### vs Phase 11\n\n" + str(result["comparison_vs_phase11"]) + "\n\n### Activation Matrix (§37, DOCUMENTARY_ONLY)\n\n" + str(result["activation_matrix"]) + "\n")
    md.append(f"\n## 30. Data Gaps\n\n{pc.get('data_gaps', 'N/A')}\n")
    md.append(
        "\n## 31. Limitations\n\n"
        "- Ce script ne recalcule RIEN — il lit les rapports Phase 10/11 déjà écrits et exécute le runner Phase 11 pour de vrai (§2/§3).\n"
        "- Aucune heure de coup d'envoi réelle n'est persistée — toute classification prospective reste qualifiée.\n"
        "- value_tracking reste NOT_AVAILABLE tant qu'aucune odds TEMPORALLY_VERIFIED n'existe.\n"
        "- MODE_2/3/4 restent structurellement refusés (§4/§46) quel que soit le volume de données accumulées.\n"
    )
    md.append(f"\n## 32. Final Verdict\n\n**{result['final_verdict']}** — {result['human_review_status']}\n")
    md.append("\n---\n\n### EXISTING REGRESSION SUITES\n\n| Suite | Status |\n|---|---|\n")
    for suite, r in result["existing_regression_suites"].items():
        md.append(f"| {suite} | {r['status']} |\n")
    md.append(f"\n| api/test_phase12.py (this phase) | {result['this_phase_tests']['status']} |\n")
    md.append("\n---\n\nPHASE 12 — XFOOT CONTROLLED SHADOW OBSERVATION PERIOD V1 TERMINÉE. "
               "OPÉRATION SHADOW CONTRÔLÉE OBSERVÉE SUR DONNÉES RÉELLES OU LIMITATIONS DOCUMENTÉES. "
               "AUCUNE ACTIVATION PRODUCTION EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. EN ATTENTE DE VALIDATION HUMAINE.\n")
    return "".join(md)


def _write_reports(result: dict, markdown: str) -> None:
    outdir = REPO_ROOT / "reports" / "phase12"
    outdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    json_path = outdir / f"xfoot_phase12_{ts}.json"
    md_path = outdir / f"xfoot_phase12_{ts}.md"
    json_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")
    logger.info("Rapports écrits : %s / %s", json_path, md_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args()
    # §4 : AUCUN argument --mode n'existe, volontairement — le mode reste la constante OPERATING_MODE.
    main(args.json, args.markdown)
    sys.exit(0)
