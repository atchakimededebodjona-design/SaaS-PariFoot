"""
scripts/value_engine_research.py — Phase 8H : XFOOT VALUE ENGINE & MARKET
INTELLIGENCE FOUNDATION V1 — rapport de recherche.
=============================================================================
RESEARCH + SHADOW ONLY. N'appelle AUCUN fournisseur d'odds, n'effectue AUCUN
appel réseau, n'écrit dans AUCUNE table de production (match, match_stats,
model_predictions, model_versions, team_ratings, prediction_log). Exécute le
Value Engine (api/app/ai/value/) uniquement sur des données SYNTHÉTIQUES
(§32-§34 du prompt) — aucune donnée odds réellement temporellement vérifiée
n'est disponible (Phase 8G.2 : SUPPORT_REQUIRED, The Odds API non intégré).

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/value_engine_research.py
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

from app.ai.value.schemas import ModelProbability, OddsSnapshot, RESEARCH_DEFAULT_THRESHOLDS  # noqa: E402
from app.ai.value.quality import classify_temporal_status, TEMPORAL_STATUSES  # noqa: E402
from app.ai.value.core import (  # noqa: E402
    compute_market_probabilities, edge, expected_value, classify_value_type,
    build_market_consensus, bookmaker_dispersion, build_value_signal,
    rank_value_signals, evaluate_threshold_grid, VALUE_TYPES, REJECTION_REASONS,
    EDGE_GRID, EV_GRID,
)

import feature_engineering_walkforward as fewf  # noqa: E402  (réutilise snapshot_db_counts, jamais réimplémenté)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("value_engine_research")

UTC = timezone.utc


def _snap(selection, odds, ts=None, measured=False, bookmaker=None):
    return OddsSnapshot(market="1X2", selection=selection, decimal_odds=odds, bookmaker=bookmaker, odds_timestamp=ts, has_measured_timestamp=measured)


# ---------------------------------------------------------------------------
# §32 : cases synthétiques A-E
# ---------------------------------------------------------------------------

def run_synthetic_cases() -> list[dict]:
    cutoff = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    ts = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    kickoff = datetime(2026, 1, 1, 20, 0, tzinfo=UTC)
    future_ts = kickoff - timedelta(hours=1)

    cases = []

    s = build_value_signal(
        match_id=None, market="1X2", selection="home_win",
        model_probability=ModelProbability(market="1X2", selection="home_win", raw_probability=0.60),
        market_odds={"home_win": _snap("home_win", 2.00, ts, True), "draw": _snap("draw", 4.0, ts, True), "away_win": _snap("away_win", 5.0, ts, True)},
        cutoff_timestamp=cutoff,
    )
    cases.append({"case": "A", "input": "p_model=0.60, odds=2.00", "expected": "EV=+20%", "status": s.status, "ev": s.expected_value, "pass": s.status == "POSITIVE_VALUE" and abs(s.expected_value - 0.20) < 1e-9})

    s = build_value_signal(
        match_id=None, market="1X2", selection="home_win",
        model_probability=ModelProbability(market="1X2", selection="home_win", raw_probability=0.45),
        market_odds={"home_win": _snap("home_win", 2.00, ts, True), "draw": _snap("draw", 4.0, ts, True), "away_win": _snap("away_win", 5.0, ts, True)},
        cutoff_timestamp=cutoff,
    )
    cases.append({"case": "B", "input": "p_model=0.45, odds=2.00", "expected": "EV=-10%", "status": s.status, "ev": s.expected_value, "pass": abs(s.expected_value - (-0.10)) < 1e-9})

    s = build_value_signal(
        match_id=None, market="1X2", selection="home_win",
        model_probability=ModelProbability(market="1X2", selection="home_win", raw_probability=0.60),
        market_odds={"home_win": _snap("home_win", 2.0, future_ts, True), "draw": _snap("draw", 4.0, future_ts, True), "away_win": _snap("away_win", 5.0, future_ts, True)},
        cutoff_timestamp=cutoff, match_kickoff=kickoff,
    )
    cases.append({"case": "C", "input": "future odds (after cutoff)", "expected": "TEMPORALLY_UNSAFE", "status": s.status, "reason": s.reason, "pass": s.status == "TEMPORALLY_UNSAFE" and s.reason == "FUTURE_INFORMATION"})

    s = build_value_signal(
        match_id=None, market="1X2", selection="home_win",
        model_probability=ModelProbability(market="1X2", selection="home_win", raw_probability=0.60),
        market_odds={"home_win": _snap("home_win", 2.0, None, False), "draw": _snap("draw", 4.0, None, False), "away_win": _snap("away_win", 5.0, None, False)},
        cutoff_timestamp=cutoff,
    )
    cases.append({"case": "D", "input": "unknown timestamp", "expected": "TEMPORAL_UNVERIFIED", "status": s.status, "reason": s.reason, "pass": s.status == "TEMPORALLY_UNSAFE" and s.reason == "TEMPORAL_UNVERIFIED"})

    s = build_value_signal(
        match_id=None, market="1X2", selection="home_win",
        model_probability=ModelProbability(market="1X2", selection="home_win", raw_probability=0.60),
        market_odds={"home_win": _snap("home_win", 0.8, ts, True), "draw": _snap("draw", 4.0, ts, True), "away_win": _snap("away_win", 5.0, ts, True)},
        cutoff_timestamp=cutoff,
    )
    cases.append({"case": "E", "input": "invalid odds <= 1", "expected": "INVALID_ODDS", "status": s.status, "reason": s.reason, "pass": s.status == "INVALID_ODDS"})

    return cases


# ---------------------------------------------------------------------------
# §33/§34 : tests adversariaux
# ---------------------------------------------------------------------------

def run_adversarial_tests() -> dict:
    day = datetime(2026, 3, 16, tzinfo=UTC)
    kickoff = day.replace(hour=20)
    cutoff = day.replace(hour=14)
    candidates = {
        "14:00": day.replace(hour=14), "18:30": day.replace(hour=18, minute=30),
        "19:45": day.replace(hour=19, minute=45), "20:10": day.replace(hour=20, minute=10),
    }
    expected = {"14:00": "TEMPORALLY_VERIFIED", "18:30": "FUTURE_INFORMATION", "19:45": "FUTURE_INFORMATION", "20:10": "FUTURE_INFORMATION"}
    snapshot_results = {}
    all_pass = True
    for label, ts in candidates.items():
        got = classify_temporal_status(ts, cutoff, kickoff, has_measured_timestamp=True)
        snapshot_results[label] = {"got": got, "expected": expected[label], "pass": got == expected[label]}
        all_pass = all_pass and (got == expected[label])

    cons_cutoff = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    snapshots = [
        _snap("home_win", 2.0, datetime(2026, 1, 1, 10, 0, tzinfo=UTC), True, "A"),
        _snap("home_win", 2.1, datetime(2026, 1, 1, 12, 0, tzinfo=UTC), True, "B"),
        _snap("home_win", 2.5, datetime(2026, 1, 1, 18, 0, tzinfo=UTC), True, "C"),
    ]
    consensus = build_market_consensus(snapshots, "home_win", cons_cutoff, min_bookmakers=2)
    consensus_pass = consensus is not None and consensus["bookmakers_included"] == ["A", "B"] and consensus["bookmakers_excluded"] == ["C"]

    return {
        "snapshot_exclusion_tests": snapshot_results, "snapshot_tests_all_pass": all_pass,
        "consensus_test": {"included": consensus["bookmakers_included"] if consensus else None, "excluded": consensus["bookmakers_excluded"] if consensus else None, "pass": consensus_pass},
        "all_adversarial_tests_pass": all_pass and consensus_pass,
    }


# ---------------------------------------------------------------------------
# Démonstrations §5/§6/§7/§8/§12/§14/§15/§18/§24/§28 sur données synthétiques
# ---------------------------------------------------------------------------

def run_demonstrations() -> dict:
    market_1x2 = compute_market_probabilities({"home_win": 1.8, "draw": 3.6, "away_win": 4.5})
    overround_example = compute_market_probabilities({"home": 1 / 0.50, "draw": 1 / 0.30, "away": 1 / 0.25})

    cutoff = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    ts = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    signals = []
    for p in (0.60, 0.55, 0.50, 0.45, 0.40):
        signals.append(build_value_signal(
            match_id=None, market="1X2", selection="home_win",
            model_probability=ModelProbability(market="1X2", selection="home_win", raw_probability=p),
            market_odds={"home_win": _snap("home_win", 2.0, ts, True), "draw": _snap("draw", 4.0, ts, True), "away_win": _snap("away_win", 5.0, ts, True)},
            cutoff_timestamp=cutoff,
        ))
    ranked = rank_value_signals(signals, ["expected_value", "edge"])
    grid = evaluate_threshold_grid(signals)

    return {
        "market_1x2_example": market_1x2,
        "overround_prompt_example": overround_example,
        "value_signals_generated": len(signals),
        "value_signals_by_status": {v: sum(1 for s in signals if s.status == v) for v in VALUE_TYPES},
        "ranked_by_ev_then_edge": [{"model_probability": s.model_probability, "expected_value": s.expected_value, "edge": s.edge} for s in ranked],
        "threshold_grid_size": len(grid),
        "threshold_grid_sample": grid[:5],
    }


def build_result() -> dict:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    generated_at = datetime.now(timezone.utc).isoformat()

    synthetic_cases = run_synthetic_cases()
    adversarial = run_adversarial_tests()
    demo = run_demonstrations()

    all_synthetic_pass = all(c["pass"] for c in synthetic_cases)
    tests_green = all_synthetic_pass and adversarial["all_adversarial_tests_pass"]

    return {
        "run_id": run_id, "generated_at": generated_at, "phase": "8H", "kind": "value_engine_foundation_v1",
        "rule": "RESEARCH + SHADOW ONLY. NO PRODUCTION INTEGRATION. NO ODDS PROVIDER CALLED.",
        "architecture": {
            "package": "api/app/ai/value/",
            "modules": ["__init__.py", "schemas.py", "quality.py", "core.py", "provider.py"],
            "test_file": "api/test_value_engine.py",
            "report_script": "scripts/value_engine_research.py",
            "called_by_production": False,
            "note": "Aucun module de api/main.py, scheduler.py, orchestrator.py, service.py, ensemble.py, models_common.py ou promotion.py n'importe api/app/ai/value/.",
        },
        "input_contract": ["ModelProbability", "MarketProbability", "OddsSnapshot", "TemporalMetadata", "PredictionQuality", "ValueSignal", "ValueThresholds"],
        "temporal_statuses": list(TEMPORAL_STATUSES),
        "value_types": list(VALUE_TYPES),
        "rejection_reasons": list(REJECTION_REASONS),
        "demonstrations": demo,
        "synthetic_cases": synthetic_cases,
        "adversarial_tests": adversarial,
        "threshold_framework": {
            "parameters": ["min_edge", "min_ev", "min_probability", "min_confidence", "max_odds_age_hours"],
            "research_default": {
                "min_edge": RESEARCH_DEFAULT_THRESHOLDS.min_edge, "min_ev": RESEARCH_DEFAULT_THRESHOLDS.min_ev,
                "min_probability": RESEARCH_DEFAULT_THRESHOLDS.min_probability, "min_confidence": RESEARCH_DEFAULT_THRESHOLDS.min_confidence,
                "max_odds_age_hours": RESEARCH_DEFAULT_THRESHOLDS.max_odds_age_hours,
            },
            "note": "RESEARCH_DEFAULT — jamais un seuil de production. Grille de recherche : EDGE_GRID=" + str(EDGE_GRID) + ", EV_GRID=" + str(EV_GRID),
        },
        "statistical_value_validation": "NOT_AVAILABLE",
        "statistical_value_validation_reason": "Aucune donnée odds réellement temporellement vérifiée disponible (Phase 8G.2 : SUPPORT_REQUIRED, The Odds API non intégré).",
        "the_odds_api_status": "SUPPORT_REQUIRED (Phase 8G.2) — NON intégré, aucun appel effectué dans cette phase.",
        "football_data_co_uk_status": "HISTORICAL_BUT_UNTIMESTAMPED (Phase 8D/8E) — jamais utilisé comme source temporellement sûre dans ce module.",
        "tests_green": tests_green,
    }


def render_markdown(result: dict) -> str:
    md = ["# XFOOT VALUE ENGINE FOUNDATION V1\n"]

    md.append("\n## 1. Executive Summary\n")
    md.append(f"\nRun id : `{result['run_id']}` — généré le {result['generated_at']}. RÈGLE : {result['rule']}\n")
    md.append(f"\nTests verts : **{result['tests_green']}**. Décision finale : **{result['final_decision']}**\n")

    md.append("\n## 2. Architecture\n\n")
    a = result["architecture"]
    md.append(f"\n- Package : `{a['package']}` — modules : {a['modules']}\n- Tests : `{a['test_file']}`\n"
               f"- Appelé par la production : **{a['called_by_production']}**\n- {a['note']}\n")

    md.append("\n## 3. Input Contract\n\n")
    md.append(f"\n{result['input_contract']}\n")

    md.append("\n## 4. Implied Probability\n\n")
    md.append(f"\np_raw = 1/odds — réutilise `app.ai.odds_research.core.implied_probability` (Phase 8D), jamais réimplémentée. "
               f"Exemple 1X2 : {result['demonstrations']['market_1x2_example']}\n")

    md.append("\n## 5. Market Normalization\n\n")
    md.append("\nnormalized_i = raw_i / sum(raw) — raw et normalized gardées séparées dans MarketProbability, jamais mélangées.\n")

    md.append("\n## 6. Overround\n\n")
    md.append(f"\nExemple exact du prompt (Home 0.50/Draw 0.30/Away 0.25) : {result['demonstrations']['overround_prompt_example']}\n")

    md.append("\n## 7. Edge\n\n")
    md.append("\nedge = p_model - p_market. Positif = Xfoot voit une probabilité plus élevée que le marché.\n")

    md.append("\n## 8. EV\n\n")
    md.append("\nEV = p_model x odds - 1. Convention : retour NET attendu par unité misée — jamais une garantie.\n")

    md.append("\n## 9. Temporal Safety\n\n")
    md.append(f"\nStatuts : {result['temporal_statuses']}. FUTURE_INFORMATION -> REJECT. UNKNOWN -> jamais SAFE. "
               "HISTORICAL_UNVERIFIED -> recherche uniquement, jamais production (voir quality.is_production_eligible).\n")

    md.append("\n## 10. Quality Gates\n\n")
    md.append("\nODDS_VALID, MODEL_PROBABILITY_VALID, TEMPORAL_STATUS_VALID, MARKET_VALID, SAMPLE_VALID — ordre de vérification fixe et déterministe (voir api/app/ai/value/quality.py::evaluate_quality_gates).\n")

    md.append("\n## 11. Value Signals\n\n")
    demo = result["demonstrations"]
    md.append(f"\n{demo['value_signals_generated']} signaux générés (synthétiques) — répartition : {demo['value_signals_by_status']}\n\n"
               f"Classement multi-critères (expected_value puis edge) :\n\n" + "\n".join(f"- {r}" for r in demo["ranked_by_ev_then_edge"]) + "\n")

    md.append("\n## 12. Market Consensus\n\n")
    adv = result["adversarial_tests"]
    md.append(f"\nTest adversarial consensus (§34) : inclus={adv['consensus_test']['included']}, exclus={adv['consensus_test']['excluded']}, PASS={adv['consensus_test']['pass']}\n")

    md.append("\n## 13. Model vs Market\n\n")
    md.append("\nedge/EV exposent la comparaison modèle vs marché — jamais qualifiée automatiquement de \"BET\", uniquement VALUE_CANDIDATE (statuts POSITIVE_VALUE/NEUTRAL/NEGATIVE_VALUE). classify_market_dominance() : UNKNOWN sans score de qualité explicite, jamais déduit d'un simple edge (§26).\n")

    md.append("\n## 14. Threshold Framework\n\n")
    tf = result["threshold_framework"]
    md.append(f"\nParamètres : {tf['parameters']}\n\nRESEARCH_DEFAULT : {tf['research_default']}\n\n{tf['note']}\n")

    md.append("\n## 15. Synthetic Tests\n\n")
    md.append("| Case | Input | Expected | Got | Pass |\n|---|---|---|---|---|\n")
    for c in result["synthetic_cases"]:
        md.append(f"| {c['case']} | {c['input']} | {c['expected']} | {c.get('status')} ({c.get('reason', c.get('ev'))}) | {c['pass']} |\n")

    md.append("\n## 16. Leakage Tests\n\n")
    md.append(f"\nTest adversarial exclusion snapshot (§33) : {adv['snapshot_exclusion_tests']}\n\nTous PASS : **{adv['all_adversarial_tests_pass']}**\n")

    md.append("\n## 17. Database Safety\n\n")
    db = result["db_safety"]
    md.append(f"\nBefore : {db['before']}\n\nAfter : {db['after']}\n\nUnchanged : **{db['unchanged']}**\n")

    md.append("\n## 18. Limitations\n\n")
    md.append(f"\n- {result['statistical_value_validation_reason']}\n"
               "- Le consensus/dispersion multi-bookmaker n'a été exercé que sur des snapshots synthétiques — jamais sur des cotes réelles.\n"
               "- classify_market_dominance() ne reçoit aucun score de qualité réel dans cette V1 (aucun historique de calibration n'y est câblé).\n")

    md.append("\n## 19. Production Status\n\n")
    md.append(f"\nThe Odds API : {result['the_odds_api_status']}\n\nfootball-data.co.uk : {result['football_data_co_uk_status']}\n\n"
               f"STATISTICAL_VALUE_VALIDATION = **{result['statistical_value_validation']}**\n")

    md.append("\n## 20. Next Step\n\n")
    md.append(f"\n{result['next_step']}\n")

    md.append("\n---\n\nPHASE 8H — XFOOT VALUE ENGINE & MARKET INTELLIGENCE FOUNDATION V1 TERMINÉE. "
               "AUCUNE INTÉGRATION ODDS EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. "
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
        "Recommandation : PHASE 8I, uniquement après validation de cette fondation. La Phase 8I devra traiter la "
        "prochaine priorité réelle de Xfoot (ex. lever SUPPORT_REQUIRED sur The Odds API, ou une autre source de "
        "données) SANS supposer que The Odds API sera le fournisseur final."
    ) if result["final_decision"] == "FOUNDATION_READY" else "Corriger les échecs identifiés avant toute Phase 8I."

    git_status = subprocess.run(["git", "status", "--porcelain"], cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True).stdout
    result["git_status_porcelain"] = git_status

    outdir = Path(__file__).resolve().parent.parent / "reports" / "value_engine"
    outdir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    json_path = outdir / f"value_engine_research_{date_str}.json"
    md_path = outdir / f"value_engine_research_{date_str}.md"
    json_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    logger.info("Rapports écrits : %s / %s", json_path, md_path)

    print("\n" + "=" * 80)
    print(f"Décision finale : {result['final_decision']}")
    print("git status --porcelain :")
    print(result["git_status_porcelain"] or "(clean)")
    print("PHASE 8H — XFOOT VALUE ENGINE & MARKET INTELLIGENCE FOUNDATION V1 TERMINÉE. "
          "AUCUNE INTÉGRATION ODDS EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. "
          "AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.")
    print("=" * 80)
    return result


if __name__ == "__main__":
    main()
    sys.exit(0)
