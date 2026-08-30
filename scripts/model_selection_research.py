"""
scripts/model_selection_research.py — Phase 6 : Model Selection Engine V1 +
Calibration Engine V1, MODE RECHERCHE.
=============================================================================

RECHERCHE UNIQUEMENT — aucune écriture DB (contrairement à
scripts/model_selection_shadow.py, qui persiste ses décisions dans les
tables Phase 6 dédiées). Ce script ne touche ni model_predictions, ni
model_versions, ni team_ratings, ni ModelVersion.status/is_active, ni
api/model_artifacts/*.json — lecture seule, écrit uniquement des rapports
sous reports/model_selection/.

Réutilise TEL QUEL l'infrastructure walk-forward déjà validée en Phase 5.7 :
- scripts/walk_forward_ensemble.py::latest_version/_resolved_rows/_make_folds
  (mêmes matchs communs elo/xgboost/lightgbm, même découpage chronologique).
- app/ai/arena/research.py::build_dixon_coles_walk_forward (ré-entraînement
  Dixon-Coles en mémoire, jamais persisté).
- scripts/research_ensemble.py::_fold_baseline_observations/_obs_log_loss
  (observations par modèle sur une fenêtre donnée — même fonctions, pas de
  seconde implémentation).

Découpage temporel : les N-1 premières fenêtres non-burn-in servent à juger
la STABILITÉ (porte 2 de model_selection.select_candidate_model) ; la
DERNIÈRE fenêtre sert UNIQUEMENT de test de CRÉDIBILITÉ STATISTIQUE (porte
3) — jamais réutilisée dans le jugement de stabilité (anti-fuite, même
discipline que la sélection de stratégie en Phase 5.7).

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/model_selection_research.py \
        [--n-windows 5] [--min-sample-size 100] [--outdir reports/model_selection]
"""

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_API_DIR = _SCRIPTS_DIR.parent / "api"
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_API_DIR))

from sqlmodel import Session  # noqa: E402
from app.core.database import engine, init_db  # noqa: E402
from app.ai.arena.ensemble import MIN_BENCHMARK_SAMPLE_SIZE  # noqa: E402
from app.ai.arena.service import MARKETS  # noqa: E402
from app.ai.arena import research, model_selection, calibration_engine  # noqa: E402

import walk_forward_ensemble as wfe  # noqa: E402
from research_ensemble import _fold_baseline_observations, _obs_log_loss, _metrics_row, _git_commit, _library_versions  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("model_selection_research")

MODEL_TYPES = ("dixon_coles", "elo", "xgboost", "lightgbm")


def _window_metrics(session, model_type, market, window_keys, versions, dcwf):
    since, until = window_keys[0][1], window_keys[-1][1]
    version = versions.get(model_type)
    return model_selection.evaluate_model_window(
        session, model_type, market, since, until,
        model_version_id=version.id if version else None, dcwf=dcwf,
    )


def main(n_windows: int = 5, min_sample_size: int = MIN_BENCHMARK_SAMPLE_SIZE, outdir: str = "reports/model_selection"):
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    generated_at = datetime.now(timezone.utc).isoformat()
    leakage_audit = []
    limitations = []

    init_db()
    with Session(engine) as session:
        versions = {mt: wfe.latest_version(session, mt) for mt in wfe.BACKTEST_MODEL_TYPES}
        missing = [mt for mt, v in versions.items() if v is None]
        if missing:
            logger.error(f"Aucune ModelVersion pour {missing}.")
            result = {"status": "no_data", "run_id": run_id, "generated_at": generated_at, "mode": "research",
                       "conclusion": f"Aucune ModelVersion disponible pour {missing}."}
            _finish(result, outdir, run_id)
            return result

        rows_by_model = {mt: wfe._resolved_rows(session, mt, versions[mt].id) for mt in wfe.BACKTEST_MODEL_TYPES}
        common_keys = set(rows_by_model["elo"]) & set(rows_by_model["xgboost"]) & set(rows_by_model["lightgbm"])
        if not common_keys:
            result = {"status": "no_overlap", "run_id": run_id, "generated_at": generated_at, "mode": "research",
                       "conclusion": "Échantillons disjoints — aucune fenêtre comparable."}
            _finish(result, outdir, run_id)
            return result

        ordered_keys = sorted(common_keys, key=lambda k: (k[1], k[0], k[2], k[3]))
        leagues = sorted({k[0] for k in ordered_keys})
        windows = wfe._make_folds(ordered_keys, n_windows)

        if len(windows) < 2:
            result = {"status": "insufficient_history", "run_id": run_id, "generated_at": generated_at, "mode": "research",
                       "conclusion": "Moins de 2 fenêtres exploitables — impossible de séparer stabilité et test de crédibilité."}
            _finish(result, outdir, run_id)
            return result

        logger.info(f"{len(ordered_keys)} matchs communs, {len(windows)} fenêtres construites.")
        dcwf = research.build_dixon_coles_walk_forward(windows, min_train_matches=research.MIN_DC_TRAIN_MATCHES)
        leakage_audit.append({
            "experiment": "Dixon-Coles walk-forward", "status": "SAFE",
            "note": "Entraîné sur match.date <= (début de fenêtre - 1 jour) uniquement, jamais persisté.",
        })

        stability_windows = windows[:-1]
        test_window = windows[-1]
        test_window_index = len(windows) - 1
        logger.info(f"Fenêtres de stabilité : 0..{len(stability_windows) - 1} ; fenêtre de test (crédibilité) : {test_window_index}.")
        leakage_audit.append({
            "experiment": "Sélection de candidat (stabilité + crédibilité)", "status": "SAFE",
            "note": f"Stabilité jugée sur les fenêtres 0..{len(stability_windows) - 1} ; crédibilité statistique "
                    f"testée UNIQUEMENT sur la fenêtre {test_window_index}, jamais réutilisée pour juger la stabilité.",
        })

        window_descriptions = [
            {"index": i, "role": "stability" if i < len(stability_windows) else "test",
             "n_matches": len(w), "start_date": str(w[0][1]), "end_date": str(w[-1][1])}
            for i, w in enumerate(windows)
        ]

        selection_by_market = {}
        calibration_by_market = {}
        calibration_rows = []
        aggregated_rows = []

        for market in MARKETS:
            window_results_by_model = {mt: [] for mt in MODEL_TYPES}
            for w_keys in stability_windows:
                for mt in MODEL_TYPES:
                    window_results_by_model[mt].append(_window_metrics(session, mt, market, w_keys, versions, dcwf))

            def _credibility_pairs(candidate, runner_up, market=market):
                cand_obs = _fold_baseline_observations(candidate, market, test_window, test_window_index, rows_by_model, dcwf)
                other_obs = _fold_baseline_observations(runner_up, market, test_window, test_window_index, rows_by_model, dcwf)
                common = sorted(set(cand_obs) & set(other_obs))  # trié : jamais un set() itéré directement (ordre non déterministe, voir §22)
                return [(_obs_log_loss(other_obs[k]), _obs_log_loss(cand_obs[k])) for k in common]

            decision = model_selection.select_candidate_model(
                window_results_by_model, market, min_sample_size=min_sample_size,
                credibility_pairs_provider=_credibility_pairs,
            )
            selection_by_market[market] = {
                "status": decision.status, "selected_model_type": decision.selected_model_type,
                "runner_up_model_type": decision.runner_up_model_type, "windows_evaluated": decision.windows_evaluated,
                "eligible_models": decision.eligible_models, "top_rank_counts": decision.top_rank_counts,
                "log_loss_cv": decision.log_loss_cv, "credibility": decision.credibility, "reason": decision.reason,
            }
            logger.info(f"[{market}] {decision.status} — {decision.reason}")

            if decision.status == "selected":
                # Calibration : "train" = fenêtres de stabilité (chronologique), "test" = fenêtre de test.
                train_obs = {}
                for w_idx, w_keys in enumerate(stability_windows):
                    train_obs.update(_fold_baseline_observations(decision.selected_model_type, market, w_keys, w_idx, rows_by_model, dcwf))
                train_obs_list = [train_obs[k] for k in sorted(train_obs)]
                test_obs = _fold_baseline_observations(decision.selected_model_type, market, test_window, test_window_index, rows_by_model, dcwf)
                test_obs_list = [test_obs[k] for k in sorted(test_obs)]

                calib_result = calibration_engine.evaluate_calibration(train_obs_list, test_obs_list, min_sample_size)
                calibration_by_market[market] = {
                    "choice": calib_result.choice, "verdict": calib_result.verdict,
                    "raw_ece": calib_result.raw_ece, "platt_ece": calib_result.platt_ece, "isotonic_ece": calib_result.isotonic_ece,
                }
                calibration_rows.append(_metrics_row(f"{market} RAW", calib_result.raw_metrics, min_sample_size))
                if calib_result.platt_metrics:
                    calibration_rows.append(_metrics_row(f"{market} PLATT", calib_result.platt_metrics, min_sample_size))
                if calib_result.isotonic_metrics:
                    calibration_rows.append(_metrics_row(f"{market} ISOTONIC", calib_result.isotonic_metrics, min_sample_size))
                aggregated_rows.append(_metrics_row(f"{market} candidate:{decision.selected_model_type}", calib_result.raw_metrics, min_sample_size))
                leakage_audit.append({
                    "experiment": f"Calibration [{market}]", "status": "SAFE",
                    "note": "Platt/Isotonic ajustés sur les fenêtres de stabilité uniquement, mesurés sur la fenêtre de test uniquement.",
                })

        limitations.append(
            "La stabilité (porte 2) est jugée sur un nombre de fenêtres nécessairement restreint par l'historique "
            "walk-forward disponible — voir windows_evaluated par marché."
        )
        limitations.append(
            "Comme en Phase 5.7, la recalibration Platt/Isotonic ne recalibre que la confiance du pick, "
            "redistribuée proportionnellement sur les autres issues."
        )

        result = {
            "status": "ok", "run_id": run_id, "generated_at": generated_at, "mode": "research",
            "dataset": {"leagues": leagues, "period_start": str(ordered_keys[0][1]), "period_end": str(ordered_keys[-1][1]),
                        "common_match_count": len(ordered_keys)},
            "methodology": (
                f"{len(stability_windows)} fenêtre(s) de stabilité + 1 fenêtre de test de crédibilité statistique, "
                "construites sur les matchs communs à elo/xgboost/lightgbm (mêmes primitives que Phase 5.7). "
                "Dixon-Coles walk-forward réentraîné en mémoire par fenêtre/ligue."
            ),
            "windows": window_descriptions,
            "selection_by_market": selection_by_market,
            "calibration_by_market": calibration_by_market,
            "calibration_rows": calibration_rows,
            "aggregated_results": aggregated_rows,
            "leakage_audit": leakage_audit,
            "reproducibility": {
                "git_commit": _git_commit(), "seed": 20260828, "library_versions": _library_versions(),
                "model_version_ids": {mt: v.id for mt, v in versions.items()},
                "min_sample_size": min_sample_size, "n_windows_requested": n_windows,
            },
            "limitations": limitations,
            "conclusion": (
                "Décisions de sélection par marché : " +
                "; ".join(f"{m}={d['status']}" + (f" ({d['selected_model_type']})" if d["status"] == "selected" else "")
                          for m, d in selection_by_market.items()) +
                ". Aucune promotion production effectuée — voir scripts/model_selection_shadow.py pour le suivi shadow."
            ),
        }

    _finish(result, outdir, run_id)
    return result


def _finish(result: dict, outdir: str, run_id: str) -> None:
    json_path, md_path = research.write_model_selection_reports(result, Path(outdir), run_id)
    logger.info(f"Rapports écrits : {json_path} / {md_path}")
    print("\nPHASE 6 — XFOOT MODEL SELECTION ENGINE V1 + CALIBRATION ENGINE V1 TERMINÉE (RECHERCHE). "
          "AUCUNE PROMOTION PRODUCTION EFFECTUÉE. AUCUN MODÈLE DE PRODUCTION REMPLACÉ.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--n-windows", type=int, default=5)
    parser.add_argument("--min-sample-size", type=int, default=MIN_BENCHMARK_SAMPLE_SIZE)
    parser.add_argument("--outdir", type=str, default=str(_SCRIPTS_DIR.parent / "reports" / "model_selection"))
    args = parser.parse_args()
    main(n_windows=args.n_windows, min_sample_size=args.min_sample_size, outdir=args.outdir)
    sys.exit(0)
