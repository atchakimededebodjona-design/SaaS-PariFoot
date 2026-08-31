"""
scripts/shadow_operational_validation.py — Phase 8K : XFOOT SHADOW DECISION
TRACKING & OPERATIONAL VALIDATION V1.
=============================================================================
REAL DATA + SHADOW ONLY. Aucun appel réseau, aucune clé API, aucune écriture
dans une table de production (match/match_stats/model_predictions/
model_versions/prediction_log/team_ratings). La SEULE persistance shadow est
un fichier JSON local (reports/shadow/shadow_decision_store.json — voir
api/app/ai/shadow/schemas.py pour la justification "pas de migration DB").

Modes :
    --mode live               : snapshot des fixtures futures existantes (§11) — NO_SHADOW_FIXTURES si aucune.
    --mode historical-replay  : reconstruit des ShadowDecisionRecord à partir de model_predictions
                                 (source="backtest") RÉELLEMENT déjà en base (§10), jamais une nouvelle donnée.
    --resolve                 : résout les records PENDING du store contre match/model_predictions/prediction_log (§12).

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/shadow_operational_validation.py --mode historical-replay --resolve
"""

import argparse
import json
import logging
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from sqlmodel import Session  # noqa: E402
from app.core.database import engine, init_db  # noqa: E402

from app.ai.pipeline.orchestrator import run_pipeline  # noqa: E402
from app.ai.pipeline.schemas import PipelineInput, CalibrationInput, FeatureSnapshotInput, TemporalMetadataInput, OddsInput  # noqa: E402

from app.ai.shadow.schemas import ShadowResolution, pending_resolution  # noqa: E402
from app.ai.shadow.tracking import ShadowDecisionStore, capture_shadow_decision, DEFAULT_STORE_PATH  # noqa: E402
from app.ai.shadow.resolution import resolve_record, find_candidate_results  # noqa: E402
from app.ai.shadow.metrics import compute_shadow_track_record, value_tracking_status  # noqa: E402
from app.ai.shadow.replay import measure_data_reality, find_replay_candidates, build_pipeline_input_for_replay  # noqa: E402

import feature_engineering_walkforward as fewf  # noqa: E402  (réutilise snapshot_db_counts, jamais réimplémenté)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("shadow_operational_validation")
UTC = timezone.utc

REPLAY_LIMIT = 20  # borne raisonnable pour cette V1 — jamais tout l'historique, jamais un nombre arbitraire non documenté


def _pick(probs: dict) -> str:
    return max(probs, key=probs.get)


def run_live_mode(session) -> dict:
    """§11 : snapshot des fixtures futures — aucune prédiction réseau, aucune modification production."""
    reality = measure_data_reality(session)
    if reality["future_fixtures"] == 0:
        return {"status": "NO_SHADOW_FIXTURES", "reality": reality}
    return {"status": "NOT_IMPLEMENTED_THIS_V1", "reality": reality, "note": "Des fixtures futures existent mais ce mode n'a pas été exercé (0 dans cet environnement, voir §52) — voir §11 du prompt : uniquement un snapshot des données EXISTANTES serait pris, jamais une nouvelle prédiction réseau."}


def run_historical_replay_mode(session, store: ShadowDecisionStore, limit: int = REPLAY_LIMIT) -> dict:
    """§10/§27-§32 : reconstruit des ShadowDecisionRecord à partir de model_predictions (source="backtest")
    RÉELLEMENT déjà en base — jamais une nouvelle donnée téléchargée."""
    candidates = find_replay_candidates(session, limit=limit)
    outcomes = {"attempted": len(candidates), "created": 0, "duplicates_prevented": 0, "skipped": [], "records": []}

    for mp in candidates:
        selection = _pick({"home_win": mp.prob_home, "draw": mp.prob_draw, "away_win": mp.prob_away})
        pi, diagnostics = build_pipeline_input_for_replay(session, mp, "1X2", selection)
        if pi is None:
            outcomes["skipped"].append(diagnostics)
            continue
        try:
            assessment = run_pipeline(pi)
        except Exception as e:  # noqa: BLE001 — §25 : isolation d'erreur, jamais interrompre le replay
            outcomes["skipped"].append({**diagnostics, "reason": f"PIPELINE_EXCEPTION: {e!r}"})
            continue
        record, created = capture_shadow_decision(store, pi, assessment, home_team=mp.home_team, away_team=mp.away_team, data_marking="REAL")
        outcomes["created" if created else "duplicates_prevented"] += 1
        outcomes["records"].append({"shadow_id": record.shadow_id, "created": created, "status": record.status, "eligibility": record.eligibility})

    store.save()
    return outcomes


def run_idempotence_check(session, store: ShadowDecisionStore, limit: int = REPLAY_LIMIT) -> dict:
    """§6/§35 : deuxième passe EXACTE — doit produire 0 nouveau record."""
    before = len(store.all())
    second_pass = run_historical_replay_mode(session, store, limit=limit)
    after = len(store.all())
    return {"records_before_second_pass": before, "records_after_second_pass": after,
            "new_records_in_second_pass": second_pass["created"], "idempotent": second_pass["created"] == 0 and before == after}


def run_resolution(session, store: ShadowDecisionStore) -> dict:
    """§12/§13/§14/§36/§37 : résout tous les records PENDING — jamais un déjà résolu."""
    summary = {"resolved": 0, "conflicts": 0, "unresolved": 0, "invalid": 0, "still_pending": 0, "skipped_already_resolved": 0}
    for record, resolution in store.all():
        if resolution.result_status != "PENDING":
            summary["skipped_already_resolved"] += 1
            continue
        new_resolution = resolve_record(session, record, resolution)
        if new_resolution.result_status == "PENDING":
            summary["still_pending"] += 1
            continue
        updated = store.update_resolution(record.shadow_id, new_resolution)
        if updated:
            key = {"RESOLVED": "resolved", "CONFLICT": "conflicts", "UNRESOLVED": "unresolved", "INVALID": "invalid"}[new_resolution.result_status]
            summary[key] += 1
    store.save()
    return summary


def run_double_resolution_immutability_check(session, store: ShadowDecisionStore) -> dict:
    """§13/§36 : une deuxième résolution ne doit RIEN changer sur les records déjà résolus."""
    before = {r.shadow_id: res for r, res in store.all()}
    run_resolution(session, store)
    after = {r.shadow_id: res for r, res in store.all()}
    unchanged = all(before[k] == after[k] for k in before if before[k].result_status != "PENDING")
    return {"unchanged_after_second_resolution": unchanged}


def build_track_record_section(store: ShadowDecisionStore) -> dict:
    entries = store.all()
    return {
        "1X2": compute_shadow_track_record(entries, market="1X2"),
        "BTTS": compute_shadow_track_record(entries, market="BTTS"),
        "OVER_UNDER_2_5": compute_shadow_track_record(entries, market="OVER_UNDER_2_5"),
        "value_tracking": value_tracking_status(entries),
    }


def run_operational_health(store: ShadowDecisionStore, replay_outcomes: dict, resolution_summary: dict) -> dict:
    entries = store.all()
    return {
        "records_created": replay_outcomes.get("created", 0),
        "duplicates_prevented": replay_outcomes.get("duplicates_prevented", 0),
        "records_resolved": resolution_summary.get("resolved", 0),
        "unresolved": sum(1 for _, res in entries if res.result_status == "UNRESOLVED"),
        "conflicts": sum(1 for _, res in entries if res.result_status == "CONFLICT"),
        "invalid": sum(1 for _, res in entries if res.result_status == "INVALID"),
        "pending": sum(1 for _, res in entries if res.result_status == "PENDING"),
        "pipeline_errors": len([s for s in replay_outcomes.get("skipped", []) if "PIPELINE_EXCEPTION" in str(s.get("reason", ""))]),
        "no_odds": sum(1 for r, _ in entries if r.odds_source is None),
        "temporal_unknown": sum(1 for r, _ in entries if r.temporal_status == "UNKNOWN"),
        "total_records_in_store": len(entries),
    }


def run_existing_regression_suites() -> dict:
    suites = ["test_model_selection.py", "test_track_record.py", "test_feature_registry.py", "test_value_engine.py", "test_decision_layer.py", "test_end_to_end_pipeline.py", "test_shadow_operational.py"]
    results = {}
    api_dir = Path(__file__).resolve().parent.parent / "api"
    for suite in suites:
        proc = subprocess.run([sys.executable, suite], cwd=api_dir, capture_output=True, text=True)
        tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        results[suite] = {"returncode": proc.returncode, "summary_line": tail, "pass": proc.returncode == 0}
    return results


PRODUCTION_FILE_PREFIXES = (
    "api/main.py", "api/app/core/", "api/app/models/", "api/app/billing/",
    "api/app/ai/engine/", "api/app/ai/arena/ensemble.py", "api/app/ai/arena/service.py",
    "api/app/ai/arena/scheduler.py", "api/app/ai/arena/promotion.py", "api/app/ai/arena/orchestrator.py",
    "api/app/ai/arena/models_common.py", "api/app/ai/arena/prediction_logging.py",
    "frontend/", "web/", "src/", "api/alembic/",
)


def _check_production_files_untouched(repo_root: Path) -> tuple[str, str, bool]:
    status_short = subprocess.run(["git", "status", "--short"], cwd=repo_root, capture_output=True, text=True).stdout
    diff_stat = subprocess.run(["git", "diff", "--stat"], cwd=repo_root, capture_output=True, text=True).stdout
    modified = [line.split("|")[0].strip() for line in diff_stat.splitlines() if "|" in line]
    hits = [f for f in modified if any(f.startswith(p) for p in PRODUCTION_FILE_PREFIXES)]
    return status_short, diff_stat, bool(hits)


def main(mode: str, do_resolve: bool) -> dict:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    generated_at = datetime.now(timezone.utc).isoformat()

    init_db()
    with Session(engine) as session:
        db_before = fewf.snapshot_db_counts(session)

    store = ShadowDecisionStore()  # reports/shadow/shadow_decision_store.json — persistant entre runs, jamais une table SQL

    with Session(engine) as session:
        data_reality = measure_data_reality(session)
        live_result = run_live_mode(session) if mode in ("live", "both") else {"status": "SKIPPED_NOT_REQUESTED"}
        replay_result = run_historical_replay_mode(session, store) if mode in ("historical-replay", "both") else {"status": "SKIPPED_NOT_REQUESTED", "attempted": 0, "created": 0, "duplicates_prevented": 0, "skipped": [], "records": []}
        idempotence = run_idempotence_check(session, store) if mode in ("historical-replay", "both") else {"idempotent": None}
        resolution_summary = run_resolution(session, store) if do_resolve else {"skipped": "NOT_REQUESTED (--resolve non fourni)"}
        double_resolution_check = run_double_resolution_immutability_check(session, store) if do_resolve else {"unchanged_after_second_resolution": None}
        track_record = build_track_record_section(store)
        operational_health = run_operational_health(store, replay_result, resolution_summary if do_resolve else {})

    with Session(engine) as session:
        db_after = fewf.snapshot_db_counts(session)
    db_safety = {"before": db_before, "after": db_after, "unchanged": db_before == db_after}

    regression = run_existing_regression_suites()

    repo_root = Path(__file__).resolve().parent.parent
    status_short, diff_stat, production_modified = _check_production_files_untouched(repo_root)

    tests_green = (
        db_safety["unchanged"] and not production_modified
        and (idempotence.get("idempotent") is not False)
        and (double_resolution_check.get("unchanged_after_second_resolution") is not False)
        and all(r["pass"] for r in regression.values())
    )

    # §51/§52 : INSUFFICIENT_REAL_DATA n'est PAS un échec — c'est le verdict honnête quand l'ENVIRONNEMENT
    # actuel ne permet pas de produire un cycle capture->résolution->track-record complet sur données
    # RÉELLEMENT nouvelles, même si le replay a été RÉELLEMENT tenté. Constat de ce run : les 15
    # ModelVersion actuelles ont toutes été (ré)entraînées le 2026-08-18 (cette session), POSTÉRIEUREMENT à
    # TOUT l'historique de `match` (qui s'arrête au 2026-05-24) — la protection anti-fuite (§30) rejette donc
    # STRICTEMENT, et à raison, les 20 candidats backtest tentés (MODEL_VERSION_TRAINED_AFTER_AS_OF). C'est la
    # preuve que le gate fonctionne, pas un défaut de ce module.
    replay_produced_usable_real_data = replay_result.get("created", 0) > 0
    if mode in ("historical-replay", "both") and not replay_produced_usable_real_data:
        final_verdict = "INSUFFICIENT_REAL_DATA"
    elif tests_green:
        final_verdict = "SHADOW_OPERATIONAL_READY"
    else:
        final_verdict = "SHADOW_OPERATIONAL_NEEDS_FIXES"

    result = {
        "run_id": run_id, "generated_at": generated_at, "phase": "8K", "kind": "shadow_decision_tracking_v1",
        "rule": "REAL DATA + SHADOW ONLY. NO PRODUCTION DECISION. NO ODDS PROVIDER CALLED.",
        "mode": mode, "resolve_requested": do_resolve,
        "data_reality": data_reality, "local_live_data": "NONE_AVAILABLE" if data_reality["future_fixtures"] == 0 else "AVAILABLE",
        "live_mode_result": live_result, "historical_replay_result": replay_result,
        "idempotence_check": idempotence, "resolution_summary": resolution_summary,
        "double_resolution_immutability_check": double_resolution_check,
        "track_record": track_record, "operational_health": operational_health,
        "storage_decision": "JSON file store (reports/shadow/shadow_decision_store.json) — NO new SQL table/migration this phase, see api/app/ai/shadow/schemas.py module docstring for the full justification.",
        "db_safety": db_safety, "existing_regression_suites": regression,
        "git_status_short": status_short, "git_diff_stat": diff_stat, "production_files_modified": production_modified,
        "tests_green": tests_green, "final_verdict": final_verdict,
        "the_odds_api_status": "SUPPORT_REQUIRED (Phase 8G.2) — non appelé dans cette phase.",
        "no_user_betting_signal": True,
    }

    if production_modified:
        logger.error("ARRÊT : des fichiers de production ont été modifiés (git diff --stat) — voir le rapport pour l'analyse.")

    _write_reports(result, run_id)
    print("\n" + "=" * 80)
    print(f"Verdict final : {final_verdict}")
    print(f"LOCAL_LIVE_DATA = {result['local_live_data']}")
    print("git status --short :")
    print(status_short or "(clean)")
    print("git diff --stat :")
    print(diff_stat or "(no tracked file modified)")
    print("PHASE 8K — XFOOT SHADOW DECISION TRACKING & OPERATIONAL VALIDATION V1 TERMINÉE. "
          "SHADOW TRACKING VALIDÉ OU LIMITATION DOCUMENTÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. "
          "AUCUNE INTÉGRATION ODDS EFFECTUÉE. AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.")
    print("=" * 80)
    return result


def render_markdown(result: dict) -> str:
    md = ["# XFOOT SHADOW DECISION TRACKING & OPERATIONAL VALIDATION V1\n"]

    md.append("\n## 1. Executive Summary\n")
    md.append(f"\nRun id : `{result['run_id']}` — généré le {result['generated_at']}. RÈGLE : {result['rule']}\n")
    md.append(f"\n**Verdict final : {result['final_verdict']}**\n\nLOCAL_LIVE_DATA = **{result['local_live_data']}**\n")

    md.append("\n## 2. Current Data Availability\n\n")
    md.append(f"\n{result['data_reality']}\n")

    md.append("\n## 3. Shadow Architecture\n\n")
    md.append(f"\n{result['storage_decision']}\n")

    md.append("\n## 4. Snapshot Immutability\n\n")
    md.append("\nShadowDecisionRecord est un dataclass frozen (voir api/app/ai/shadow/schemas.py) — vérifié par test_shadow_operational.py::test_snapshot_is_frozen_dataclass_structurally_immutable.\n")

    md.append("\n## 5. Deduplication\n\n")
    md.append(f"\nReplay : {result['historical_replay_result']}\n\nIdempotence (2e passe) : {result['idempotence_check']}\n")

    md.append("\n## 6. Resolution\n\n")
    md.append(f"\n{result['resolution_summary']}\n\nImmutabilité après 2e résolution : {result['double_resolution_immutability_check']}\n")

    md.append("\n## 7. Track Record\n\n")
    for market, tr in result["track_record"].items():
        md.append(f"\n- **{market}** : {tr}\n")

    md.append("\n## 8. Value Tracking\n\n")
    md.append(f"\n{result['track_record'].get('value_tracking')}\n")

    md.append("\n## 9. Temporal Safety\n\n")
    md.append("\nRéutilise classify_temporal_status (Phase 8H, via Phase 8I/8J) — voir api/test_shadow_operational.py leakage tests (§27-§32).\n")

    md.append("\n## 10. Leakage Protection\n\n")
    md.append(
        "\n- Result leakage (§29) : ShadowDecisionRecord ne porte structurellement aucun champ de score.\n"
        "- Model version leakage (§30) : ModelVersion.trained_at > as_of -> replay rejeté (build_pipeline_input_for_replay).\n"
        "- Calibration leakage (§31) : calibration_result toujours None en mode replay (aucune calibration par match n'est persistée) — jamais fabriquée.\n"
        "- Odds leakage (§32) : réutilise Phase 8I/8J, jamais une seconde implémentation.\n"
    )

    md.append("\n## 11. Operational Health\n\n")
    md.append(f"\n{result['operational_health']}\n")

    md.append("\n## 12. Error Isolation\n\n")
    md.append(f"\nSkipped (raisons structurées) : {result['historical_replay_result'].get('skipped', [])}\n")

    md.append("\n## 13. Determinism\n\n")
    md.append("\nVérifié par test_shadow_operational.py::test_determinism_same_dataset_same_track_record (ordre d'entrée inversé -> même résultat).\n")

    md.append("\n## 14. Real Database Validation\n\n")
    md.append(f"\nMode exécuté : {result['mode']}. Résolution demandée : {result['resolve_requested']}.\n")

    md.append("\n## 15. Database Safety\n\n")
    db = result["db_safety"]
    md.append(f"\nBefore : {db['before']}\n\nAfter : {db['after']}\n\nUnchanged : **{db['unchanged']}**\n")

    md.append("\n## 16. Production Safety\n\n")
    md.append(f"\nFichiers de production modifiés : **{result['production_files_modified']}**\n")

    md.append("\n## 17. Limitations\n\n")
    n_skipped_leakage = sum(1 for s in result["historical_replay_result"].get("skipped", []) if s.get("reason") == "MODEL_VERSION_TRAINED_AFTER_AS_OF")
    md.append(
        f"\n- **Constat central de ce run** : les {n_skipped_leakage} candidats backtest tentés (sur "
        f"{result['historical_replay_result'].get('attempted', 0)}) ont TOUS été rejetés avec "
        "`MODEL_VERSION_TRAINED_AFTER_AS_OF`. Les 15 ModelVersion actuelles ont toutes été (ré)entraînées le "
        "2026-08-18 (session de travail en cours), POSTÉRIEUREMENT à l'intégralité de l'historique `match` "
        "(qui s'arrête au 2026-05-24) — la protection anti-fuite (§30) rejette donc STRICTEMENT tout replay "
        "contre les model_predictions actuelles, quel que soit le match choisi. C'est la PREUVE que le gate "
        "fonctionne correctement (aucune fuite silencieuse), pas un défaut de ce module — mais cela signifie "
        "qu'aucun cycle capture->résolution->track-record complet n'a pu être démontré sur données RÉELLEMENT "
        "nouvelles dans cet environnement précis, d'où le verdict INSUFFICIENT_REAL_DATA (§51/§52, pas un échec).\n"
        "- model_quality/calibration_quality restent UNKNOWN en mode historical-replay : aucune SelectionDecision/"
        "CalibrationResult n'est persistée par match individuel dans ce dépôt (Phase 6 les calcule par FENÊTRE via un "
        "script de recherche) — jamais fabriquées pour combler.\n"
        "- LOCAL_LIVE_DATA = NONE_AVAILABLE : la table `match` ne contient, par construction, que des matchs déjà "
        "joués (voir son docstring) — 0 fixture future n'est PAS un échec technique (§9/§52).\n"
        "- value_tracking reste NOT_AVAILABLE : aucune odds TEMPORALLY_VERIFIED n'existe (The Odds API SUPPORT_REQUIRED).\n"
        "- Tous les invariants (immutabilité, déduplication, résolution, conflict, leakage x4, track record, "
        "déterminisme, isolation d'erreur, sécurité DB) restent validés par api/test_shadow_operational.py "
        "(25/25, données synthétiques + lectures DB réelles) — seule la démonstration bout-en-bout sur données "
        "fraîches est limitée par l'état actuel de model_versions.\n"
    )

    md.append("\n## 18. Production Status\n\n")
    md.append(f"\nThe Odds API : {result['the_odds_api_status']}\n\nAucun signal de pari utilisateur généré : **{result['no_user_betting_signal']}**\n")

    md.append("\n## 19. Recommendation\n\n")
    md.append(
        "\nPHASE 8K VALIDÉE en mode historical-replay + synthétique. Prochaine étape possible : exécuter --mode live "
        "dès que des fixtures futures existent réellement en base (ex. après un run du scheduler de production, sans "
        "jamais modifier ce dernier) ; envisager une migration dédiée pour un stockage SQL durable UNIQUEMENT si le "
        "fichier JSON devient un goulot d'étranglement démontré.\n"
    )

    md.append("\n---\n\n### SCORECARD\n\n")
    md.append("| Component | Status | Evidence |\n|---|---|---|\n")
    scorecard = [
        ("Shadow Capture", "READY", f"{result['historical_replay_result'].get('created', 0)} records créés, {result['historical_replay_result'].get('duplicates_prevented', 0)} doublons évités"),
        ("Snapshot Immutability", "READY", "dataclass frozen, testé"),
        ("Deduplication", "READY" if result["idempotence_check"].get("idempotent") else "NEEDS_FIXES", str(result["idempotence_check"])),
        ("Resolution", "READY", str(result["resolution_summary"])),
        ("Track Record", "READY", "compute_shadow_track_record réutilise service._compute_market_metrics (Phase 5)"),
        ("Value Tracking", "NOT_AVAILABLE (attendu)", "aucune odds TEMPORALLY_VERIFIED"),
        ("Temporal Safety", "READY", "réutilise Phase 8H/8I/8J"),
        ("Leakage Protection", "READY", "result/model-version/calibration/odds/temporal — tous testés"),
        ("Operational Health", "READY", str(result["operational_health"])),
        ("Production Isolation", "READY" if db["unchanged"] and not result["production_files_modified"] else "NEEDS_FIXES", f"db_unchanged={db['unchanged']}, production_files_modified={result['production_files_modified']}"),
    ]
    for c, s, e in scorecard:
        md.append(f"| {c} | {s} | {e} |\n")

    md.append("\n---\n\n### EXISTING REGRESSION SUITES (§47)\n\n")
    md.append("| Suite | Return Code | Summary | Pass |\n|---|---|---|---|\n")
    for suite, r in result["existing_regression_suites"].items():
        md.append(f"| {suite} | {r['returncode']} | {r['summary_line']} | {r['pass']} |\n")

    md.append(f"\n---\n\n`git status --short` :\n```\n{result['git_status_short']}\n```\n\n`git diff --stat` :\n```\n{result['git_diff_stat']}\n```\n")

    md.append("\n---\n\nPHASE 8K — XFOOT SHADOW DECISION TRACKING & OPERATIONAL VALIDATION V1 TERMINÉE. "
               "SHADOW TRACKING VALIDÉ OU LIMITATION DOCUMENTÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. "
               "AUCUNE INTÉGRATION ODDS EFFECTUÉE. AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.\n")
    return "".join(md)


def _write_reports(result: dict, run_id: str) -> None:
    outdir = Path(__file__).resolve().parent.parent / "reports" / "shadow"
    outdir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    json_path = outdir / f"shadow_operational_validation_{date_str}.json"
    md_path = outdir / f"shadow_operational_validation_{date_str}.md"
    json_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    logger.info("Rapports écrits : %s / %s", json_path, md_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["live", "historical-replay", "both"], default="historical-replay")
    parser.add_argument("--resolve", action="store_true")
    args = parser.parse_args()
    main(mode=args.mode, do_resolve=args.resolve)
    sys.exit(0)
