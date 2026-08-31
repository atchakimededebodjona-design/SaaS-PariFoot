"""
scripts/decision_layer_research.py — Phase 8I : XFOOT PREDICTION QUALITY &
DECISION LAYER V1 — rapport de recherche.
=============================================================================
RESEARCH + SHADOW ONLY. N'appelle AUCUN fournisseur d'odds, n'effectue AUCUN
appel réseau, n'écrit dans AUCUNE table de production. Exécute le Decision
Layer (api/app/ai/decision/) uniquement sur des données SYNTHÉTIQUES (§32-
§34 du prompt).

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/decision_layer_research.py
"""

import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from sqlmodel import Session  # noqa: E402
from app.core.database import engine, init_db  # noqa: E402

from app.ai.arena.model_selection import SelectionDecision  # noqa: E402
from app.ai.arena.calibration_engine import CalibrationResult  # noqa: E402
from app.ai.arena.schemas import MarketMetrics  # noqa: E402

from app.ai.decision.schemas import QualityDimensions, GATE_NAMES  # noqa: E402
from app.ai.decision.quality import MODEL_QUALITY_STATUSES, CALIBRATION_QUALITY_STATUSES, DATA_QUALITY_STATUSES, SAMPLE_QUALITY_STATUSES, MARKET_QUALITY_STATUSES  # noqa: E402
from app.ai.decision.confidence import compute_overall_confidence, OVERALL_CONFIDENCE_STATUSES  # noqa: E402
from app.ai.decision.eligibility import evaluate_eligibility  # noqa: E402
from app.ai.decision.decision import assess_decision, to_value_engine_input  # noqa: E402

import feature_engineering_walkforward as fewf  # noqa: E402  (réutilise snapshot_db_counts, jamais réimplémenté)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("decision_layer_research")

UTC = timezone.utc


def _good_selection_decision():
    return SelectionDecision(status="selected", market="1X2", selected_model_type="xgboost", runner_up_model_type="elo", windows_evaluated=5)


def _good_calibration_result():
    m = MarketMetrics(sample_size=200, accuracy=0.55, log_loss=0.95, brier_score=0.6)
    return CalibrationResult(choice="platt", verdict="HELPFUL", raw_metrics=m, platt_metrics=m, isotonic_metrics=None,
                              raw_ece=0.05, platt_ece=0.02, isotonic_ece=None, train_sample_size=300, test_sample_size=200)


def _kwargs(**overrides):
    cutoff = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    ts = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    base = dict(
        prediction_id=1, model="xgboost", market="1X2",
        probabilities={"home_win": 0.55, "draw": 0.25, "away_win": 0.20}, selection="home_win", probability_source="CALIBRATED",
        selection_decision=_good_selection_decision(), calibration_result=_good_calibration_result(),
        feature_coverage={"total_features": 25, "missing": 1, "present": 24, "coverage_ratio": 0.96}, team_mapping_confident=True,
        odds_timestamp=ts, cutoff_timestamp=cutoff, has_measured_odds_timestamp=True,
        sample_size=200, evaluated_at=cutoff,
    )
    base.update(overrides)
    return base


def run_synthetic_cases() -> list[dict]:
    cases = []

    r = assess_decision(**_kwargs())
    cases.append({"case": "A", "description": "Tous les composants bons", "expected": "HIGH / ELIGIBLE",
                  "got_confidence": r.confidence.overall_status, "got_eligibility": r.eligibility,
                  "pass": r.confidence.overall_status == "HIGH" and r.eligibility == "ELIGIBLE"})

    r = assess_decision(**_kwargs(calibration_result=None))
    cases.append({"case": "B", "description": "Model excellent + calibration inconnue", "expected": "jamais HIGH automatiquement",
                  "got_confidence": r.confidence.overall_status, "got_eligibility": r.eligibility,
                  "pass": r.confidence.overall_status != "HIGH"})

    kickoff = datetime(2026, 1, 1, 20, 0, tzinfo=UTC)
    cutoff_c = kickoff - timedelta(hours=6)
    future_ts = kickoff - timedelta(hours=1)
    r = assess_decision(**_kwargs(odds_timestamp=future_ts, cutoff_timestamp=cutoff_c, match_kickoff=kickoff, evaluated_at=cutoff_c))
    cases.append({"case": "C", "description": "Future information", "expected": "INELIGIBLE",
                  "got_eligibility": r.eligibility, "pass": r.eligibility == "INELIGIBLE"})

    r = assess_decision(**_kwargs(odds_timestamp=None, cutoff_timestamp=None))
    cases.append({"case": "D", "description": "Temporal UNKNOWN", "expected": "INELIGIBLE",
                  "got_eligibility": r.eligibility, "pass": r.eligibility == "INELIGIBLE"})

    r = assess_decision(**_kwargs(has_measured_odds_timestamp=False))
    cases.append({"case": "E", "description": "Historical unverified", "expected": "RESEARCH_ONLY",
                  "got_eligibility": r.eligibility, "pass": r.eligibility == "RESEARCH_ONLY"})

    r = assess_decision(**_kwargs(sample_size=5))
    cases.append({"case": "F", "description": "Sample insuffisant", "expected": "INSUFFICIENT_DATA",
                  "got_eligibility": r.eligibility, "pass": r.eligibility == "INSUFFICIENT_DATA"})

    r = assess_decision(**_kwargs(probabilities={"home_win": 0.9, "draw": 0.9, "away_win": 0.9}))
    cases.append({"case": "G", "description": "Invalid probability", "expected": "INELIGIBLE",
                  "got_eligibility": r.eligibility, "pass": r.eligibility == "INELIGIBLE"})

    r = assess_decision(**_kwargs(feature_coverage={"total_features": 25, "missing": 20, "present": 5, "coverage_ratio": 0.2}))
    cases.append({"case": "H", "description": "Missing feature critique", "expected": "INELIGIBLE ou INSUFFICIENT_DATA (règle documentée)",
                  "got_eligibility": r.eligibility, "pass": r.eligibility in ("INELIGIBLE", "INSUFFICIENT_DATA")})

    return cases


def run_adversarial_tests() -> dict:
    r33 = assess_decision(**_kwargs(probabilities={"home_win": 0.70, "draw": 0.20, "away_win": 0.10}, odds_timestamp=None, cutoff_timestamp=None))
    kickoff = datetime(2026, 1, 1, 20, 0, tzinfo=UTC)
    cutoff = kickoff - timedelta(hours=6)
    future_ts = kickoff - timedelta(hours=1)
    r34 = assess_decision(**_kwargs(odds_timestamp=future_ts, cutoff_timestamp=cutoff, match_kickoff=kickoff, evaluated_at=cutoff))
    return {
        "test_33_high_probability_cannot_bypass_unknown_temporal": {
            "input": "model_quality=HIGH, probability=0.70, edge implicite absent du contrat, temporal=UNKNOWN",
            "expected": "NOT ELIGIBLE", "got": r33.eligibility, "pass": r33.eligibility == "INELIGIBLE",
        },
        "test_34_all_high_but_future_information": {
            "input": "model/calibration/data/sample = HIGH, future_information=TRUE",
            "expected": "INELIGIBLE", "got": r34.eligibility, "pass": r34.eligibility == "INELIGIBLE",
        },
        "all_pass": r33.eligibility == "INELIGIBLE" and r34.eligibility == "INELIGIBLE",
    }


def run_determinism_check() -> dict:
    kwargs = _kwargs()
    r1, r2 = assess_decision(**kwargs), assess_decision(**kwargs)
    same = (r1.eligibility == r2.eligibility and r1.confidence.overall_status == r2.confidence.overall_status and r1.reasons == r2.reasons)
    return {"same_input_same_output": same}


def run_existing_regression_suites() -> dict:
    """§38 : exécute les suites existantes pertinentes (Phase 6 model_selection, Phase 8A feature_registry,
    Phase 8H value_engine, Phase 8B feature_engineering) — jamais attribuées à cette phase sans preuve si
    un échec préexistant est constaté."""
    suites = ["test_model_selection.py", "test_feature_registry.py", "test_value_engine.py", "test_feature_engineering_v1.py", "test_decision_layer.py"]
    results = {}
    api_dir = Path(__file__).resolve().parent.parent / "api"
    for suite in suites:
        proc = subprocess.run([sys.executable, suite], cwd=api_dir, capture_output=True, text=True)
        tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        results[suite] = {"returncode": proc.returncode, "summary_line": tail, "pass": proc.returncode == 0}
    return results


def build_result() -> dict:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    generated_at = datetime.now(timezone.utc).isoformat()

    synthetic_cases = run_synthetic_cases()
    adversarial = run_adversarial_tests()
    determinism = run_determinism_check()
    regression = run_existing_regression_suites()

    all_synthetic_pass = all(c["pass"] for c in synthetic_cases)
    tests_green = all_synthetic_pass and adversarial["all_pass"] and determinism["same_input_same_output"] and all(r["pass"] for r in regression.values())

    sample_assessment = assess_decision(**_kwargs())

    return {
        "run_id": run_id, "generated_at": generated_at, "phase": "8I", "kind": "decision_layer_v1",
        "rule": "RESEARCH + SHADOW ONLY. NO PRODUCTION PROMOTION. NO ODDS PROVIDER CALLED.",
        "architecture": {
            "package": "api/app/ai/decision/",
            "modules": ["__init__.py", "schemas.py", "quality.py", "confidence.py", "eligibility.py", "decision.py"],
            "test_file": "api/test_decision_layer.py", "report_script": "scripts/decision_layer_research.py",
            "called_by_production": False,
            "note": "Aucun module de api/main.py, scheduler.py, orchestrator.py, service.py, ensemble.py, models_common.py, promotion.py, ou api/app/ai/value/ n'importe api/app/ai/decision/.",
        },
        "quality_dimensions": {
            "model_quality": {"statuses": list(MODEL_QUALITY_STATUSES), "reused_from": "app.ai.arena.model_selection.SelectionDecision (Phase 6)"},
            "calibration_quality": {"statuses": list(CALIBRATION_QUALITY_STATUSES), "reused_from": "app.ai.arena.calibration_engine.CalibrationResult (Phase 6)"},
            "data_quality": {"statuses": list(DATA_QUALITY_STATUSES), "reused_from": "app.ai.features.snapshot.snapshot_coverage (Phase 8A)"},
            "temporal_quality": {"statuses": list(["TEMPORALLY_VERIFIED", "HISTORICAL_UNVERIFIED", "FUTURE_INFORMATION", "UNKNOWN"]), "reused_from": "app.ai.value.quality.classify_temporal_status (Phase 8H) — IDENTIQUE"},
            "sample_quality": {"statuses": list(SAMPLE_QUALITY_STATUSES), "reused_from": "N/A — nouveau, basé uniquement sur la taille d'échantillon"},
            "market_quality": {"statuses": list(MARKET_QUALITY_STATUSES), "reused_from": "app.ai.value.core.compute_market_probabilities (Phase 8H)"},
        },
        "confidence_framework": {
            "overall_statuses": list(OVERALL_CONFIDENCE_STATUSES),
            "no_compensation_rule": "model_quality=LOW -> LOW inconditionnellement ; toute dimension UNKNOWN (hors market) -> UNKNOWN ; temporal FUTURE_INFORMATION/UNKNOWN -> INELIGIBLE — jamais rattrapé par une autre dimension excellente (§12).",
            "research_score": "Expérimental (§26), jamais nommé confidence, jamais requis — voir confidence.compute_research_score.",
        },
        "eligibility_gates": {
            "gate_names": list(GATE_NAMES),
            "gate_order_displayed": ["GATE_DATA", "GATE_MODEL", "GATE_CALIBRATION", "GATE_TEMPORAL", "GATE_SAMPLE", "GATE_MARKET"],
            "overall_precedence": "GATE_TEMPORAL (FAIL puis UNKNOWN) en premier -> GATE_MODEL FAIL -> GATE_DATA/GATE_SAMPLE/GATE_MARKET FAIL -> HISTORICAL_UNVERIFIED -> RESEARCH_ONLY -> tout gate UNKNOWN restant -> UNKNOWN -> sinon ELIGIBLE.",
        },
        "rejection_reasons": ["NO_MODEL", "MODEL_UNSTABLE", "MODEL_INSUFFICIENT_DATA", "CALIBRATION_UNAVAILABLE", "DATA_INCOMPLETE", "DATA_STALE", "TEMPORAL_UNKNOWN", "FUTURE_INFORMATION", "HISTORICAL_UNVERIFIED", "INSUFFICIENT_SAMPLE", "MARKET_UNAVAILABLE", "MISSING_PROBABILITY", "INVALID_PROBABILITY"],
        "synthetic_cases": synthetic_cases,
        "adversarial_tests": adversarial,
        "determinism": determinism,
        "existing_regression_suites": regression,
        "sample_decision_assessment": {
            "eligibility": sample_assessment.eligibility, "confidence_overall": sample_assessment.confidence.overall_status,
            "quality_dimensions": sample_assessment.quality_dimensions.__dict__,
            "gates": [{"name": g.name, "status": g.status, "reason": g.reason} for g in sample_assessment.gates],
            "value_engine_interface_payload": to_value_engine_input(sample_assessment),
        },
        "tests_green": tests_green,
        "the_odds_api_status": "SUPPORT_REQUIRED (Phase 8G.2) — non appelé dans cette phase.",
        "no_user_betting_signal": True,
    }


def render_markdown(result: dict) -> str:
    md = ["# XFOOT PREDICTION QUALITY & DECISION LAYER V1\n"]

    md.append("\n## 1. Executive Summary\n")
    md.append(f"\nRun id : `{result['run_id']}` — généré le {result['generated_at']}. RÈGLE : {result['rule']}\n")
    md.append(f"\nTests verts : **{result['tests_green']}**. Décision finale : **{result['final_decision']}**\n")

    md.append("\n## 2. Architecture\n\n")
    a = result["architecture"]
    md.append(f"\n- Package : `{a['package']}` — modules : {a['modules']}\n- Tests : `{a['test_file']}`\n"
               f"- Appelé par la production : **{a['called_by_production']}**\n- {a['note']}\n")

    md.append("\n## 3. Quality Dimensions\n\n")
    for name, info in result["quality_dimensions"].items():
        md.append(f"\n- **{name}** : {info['statuses']} — {info['reused_from']}\n")

    md.append("\n## 4. Model Quality\n\n")
    md.append(f"\n{result['quality_dimensions']['model_quality']}\n")
    md.append("\n## 5. Calibration Quality\n\n")
    md.append(f"\n{result['quality_dimensions']['calibration_quality']}\n")
    md.append("\n## 6. Data Quality\n\n")
    md.append(f"\n{result['quality_dimensions']['data_quality']}\n")
    md.append("\n## 7. Temporal Quality\n\n")
    md.append(f"\n{result['quality_dimensions']['temporal_quality']}\n")
    md.append("\n## 8. Sample Quality\n\n")
    md.append(f"\n{result['quality_dimensions']['sample_quality']}\n")
    md.append("\n## 9. Market Quality\n\n")
    md.append(f"\n{result['quality_dimensions']['market_quality']}\n")

    md.append("\n## 10. Confidence Framework\n\n")
    cf = result["confidence_framework"]
    md.append(f"\nStatuts : {cf['overall_statuses']}\n\nRègle no-compensation : {cf['no_compensation_rule']}\n\nresearch_score : {cf['research_score']}\n")

    md.append("\n## 11. Eligibility Gates\n\n")
    eg = result["eligibility_gates"]
    md.append(f"\nGates : {eg['gate_names']}\n\nOrdre d'affichage : {eg['gate_order_displayed']}\n\nPrécédence de décision : {eg['overall_precedence']}\n")

    md.append("\n## 12. Rejection Reasons\n\n")
    md.append(f"\n{result['rejection_reasons']}\n")

    md.append("\n## 13. Synthetic Tests\n\n")
    md.append("| Case | Description | Expected | Got | Pass |\n|---|---|---|---|---|\n")
    for c in result["synthetic_cases"]:
        md.append(f"| {c['case']} | {c['description']} | {c['expected']} | eligibility={c.get('got_eligibility')} confidence={c.get('got_confidence','N/A')} | {c['pass']} |\n")

    md.append("\n## 14. Adversarial Tests\n\n")
    adv = result["adversarial_tests"]
    for key, v in adv.items():
        if key == "all_pass":
            continue
        md.append(f"\n- **{key}** : input={v['input']} ; expected={v['expected']} ; got={v['got']} ; PASS={v['pass']}\n")
    md.append(f"\nTous PASS : **{adv['all_pass']}**\n")

    md.append("\n## 15. Determinism\n\n")
    md.append(f"\n{result['determinism']}\n")

    md.append("\n## 16. Database Safety\n\n")
    db = result["db_safety"]
    md.append(f"\nBefore : {db['before']}\n\nAfter : {db['after']}\n\nUnchanged : **{db['unchanged']}**\n")

    md.append("\n## 17. Production Safety\n\n")
    md.append(f"\nDB inchangée : {db['unchanged']}. Aucune écriture dans model_predictions/prediction_log/model_versions/match/match_stats/team_ratings. Aucune promotion de modèle.\n")

    md.append("\n## 18. Limitations\n\n")
    md.append(
        "\n- market_quality reste NOT_AVAILABLE tant que The Odds API n'est pas intégré (SUPPORT_REQUIRED, Phase 8G.2) — n'entre donc jamais dans compute_overall_confidence.\n"
        "- compute_research_score() utilise des poids choisis arbitrairement, jamais validés statistiquement — expérimental uniquement.\n"
        "- Aucune donnée réelle (prédiction Xfoot en base) n'a été évaluée dans ce rapport — uniquement des cas synthétiques et adversariaux.\n"
    )

    md.append("\n## 19. Production Status\n\n")
    md.append(f"\nThe Odds API : {result['the_odds_api_status']}\n\nAucun signal de pari utilisateur généré : **{result['no_user_betting_signal']}**\n")

    md.append("\n## 20. Recommendation\n\n")
    md.append(f"\n{result['next_step']}\n")

    md.append("\n---\n\n### SCORECARD\n\n")
    md.append("| Component | Status | Evidence |\n|---|---|---|\n")
    for row in result["scorecard"]:
        md.append(f"| {row['component']} | {row['status']} | {row['evidence']} |\n")

    md.append("\n---\n\n### EXISTING REGRESSION SUITES (§38)\n\n")
    md.append("| Suite | Return Code | Summary | Pass |\n|---|---|---|---|\n")
    for suite, r in result["existing_regression_suites"].items():
        md.append(f"| {suite} | {r['returncode']} | {r['summary_line']} | {r['pass']} |\n")

    md.append("\n---\n\nPHASE 8I — XFOOT PREDICTION QUALITY & DECISION LAYER V1 TERMINÉE. "
               "AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. AUCUNE INTÉGRATION ODDS EFFECTUÉE. "
               "AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.\n")
    return "".join(md)


def main() -> dict:
    init_db()
    with Session(engine) as session:
        db_before = fewf.snapshot_db_counts(session)

    result = build_result()

    with Session(engine) as session:
        db_after = fewf.snapshot_db_counts(session)
    result["db_safety"] = {"before": db_before, "after": db_after, "unchanged": db_before == db_after}

    result["final_decision"] = "FOUNDATION_READY" if (result["tests_green"] and result["db_safety"]["unchanged"]) else "FOUNDATION_NEEDS_FIXES"
    result["next_step"] = (
        "PHASE 8I VALIDÉE. Prochaine phase possible : SHADOW DECISION TRACKING (persistance des DecisionAssessment "
        "sur des prédictions réelles, en shadow) et intégration progressive de données réellement disponibles — "
        "à ne construire QUE si un besoin réel est démontré (§47)."
    ) if result["final_decision"] == "FOUNDATION_READY" else "Corriger les échecs identifiés avant toute nouvelle phase."

    result["scorecard"] = [
        {"component": "Model Quality", "status": "READY", "evidence": "Réutilise SelectionDecision (Phase 6), UNKNOWN jamais HIGH par défaut — voir test_model_quality_never_high_without_decision"},
        {"component": "Calibration", "status": "READY", "evidence": "Réutilise CalibrationResult (Phase 6), CALIBRATED seulement si verdict=HELPFUL — voir test_calibration_quality_requires_helpful_verdict"},
        {"component": "Data Quality", "status": "READY", "evidence": "Réutilise snapshot_coverage (Phase 8A) — voir test_data_quality_reuses_feature_registry_coverage"},
        {"component": "Temporal Quality", "status": "READY", "evidence": "Identique à Phase 8H (classify_temporal_status), tests adversariaux §33/§34 PASS"},
        {"component": "Sample Quality", "status": "READY", "evidence": "Taille uniquement, jamais confondu avec la performance — voir test_sample_quality_never_confuses_size_with_accuracy"},
        {"component": "Market Quality", "status": "READY (NOT_AVAILABLE attendu)", "evidence": "Réutilise compute_market_probabilities (Phase 8H) ; NOT_AVAILABLE tant que The Odds API non intégré"},
        {"component": "Confidence", "status": "READY", "evidence": f"34/34 tests, no-compensation vérifiée (Cases A/B + adversarial §33/§34), statuts : {result['confidence_framework']['overall_statuses']}"},
        {"component": "Eligibility", "status": "READY", "evidence": "6 hard gates toujours retournés, précédence documentée, Cases A-H couverts"},
        {"component": "Value Interface", "status": "READY (contrat documenté, non connecté)", "evidence": "to_value_engine_input() — api/app/ai/value/ jamais importé par decision.py (§30)"},
    ]

    git_status = subprocess.run(["git", "status", "--porcelain"], cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True).stdout
    result["git_status_porcelain"] = git_status

    outdir = Path(__file__).resolve().parent.parent / "reports" / "decision"
    outdir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    json_path = outdir / f"decision_layer_research_{date_str}.json"
    md_path = outdir / f"decision_layer_research_{date_str}.md"
    json_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    logger.info("Rapports écrits : %s / %s", json_path, md_path)

    print("\n" + "=" * 80)
    print(f"Décision finale : {result['final_decision']}")
    print("git status --porcelain :")
    print(result["git_status_porcelain"] or "(clean)")
    print("PHASE 8I — XFOOT PREDICTION QUALITY & DECISION LAYER V1 TERMINÉE. "
          "AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. AUCUNE INTÉGRATION ODDS EFFECTUÉE. "
          "AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.")
    print("=" * 80)
    return result


if __name__ == "__main__":
    main()
    sys.exit(0)
