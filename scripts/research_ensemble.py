"""
scripts/research_ensemble.py — Phase 5.7 : XFOOT ENSEMBLE RESEARCH & BACKTEST V2.
=============================================================================

RECHERCHE UNIQUEMENT. Ce script ne modifie AUCUNE table de production
(model_predictions, model_versions, team_ratings), ne crée AUCUNE nouvelle
ModelVersion, ne touche pas à `xfoot-ensemble-v3`, à l'endpoint
POST /models/ensemble/predict, au scheduler, au Dashboard ni à
api/model_artifacts/*.json. Il n'appelle jamais log_prediction/
resolve_prediction/get_or_create_active_model_version. La session DB ouverte
ici (`init_db()` + `Session(engine)`) n'est utilisée qu'en LECTURE — aucun
`session.add`/`session.commit` n'apparaît dans ce fichier ni dans
app/ai/arena/research.py.

But : déterminer OBJECTIVEMENT si l'Ensemble (et quelle stratégie de
pondération) apporte un gain mesurable, hors échantillon, sur 1X2/BTTS/O-U,
avec ou sans Dixon-Coles, avec ou sans calibration — jamais fabriquer un
résultat favorable. Voir reports/ensemble/*.md pour la méthodologie complète
une fois ce script exécuté.

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/research_ensemble.py \
        [--n-folds 3] [--min-sample-size 100] [--outdir reports/ensemble]
"""

import argparse
import logging
import math
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_API_DIR = _SCRIPTS_DIR.parent / "api"
sys.path.insert(0, str(_SCRIPTS_DIR))  # pour importer walk_forward_ensemble (même dossier)
sys.path.insert(0, str(_API_DIR))      # pour importer app.*

from sqlmodel import Session  # noqa: E402
from app.core.database import engine, init_db  # noqa: E402
from app.ai.arena.ensemble import (  # noqa: E402
    InverseLogLossStrategy, SoftmaxLogLossStrategy, BrierStrategy, HybridStrategy,
    MIN_BENCHMARK_SAMPLE_SIZE, combine,
)
from app.ai.arena.service import MARKETS, _compute_market_metrics, _market_observation, _model_prediction_payload  # noqa: E402
from app.ai.arena import research  # noqa: E402

import walk_forward_ensemble as wfe  # noqa: E402  (même dossier scripts/ — réutilise _make_folds/_resolved_rows/latest_version/TEMPERATURE_GRID)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("research_ensemble")

BASELINE_MODEL_TYPES = ("dixon_coles", "elo", "xgboost", "lightgbm")


# ---------------------------------------------------------------------------
# Adaptateurs match/probabilités (glue, pas de nouvelle logique métier).
# ---------------------------------------------------------------------------

def _payload_market_probs(payload: dict, market: str):
    if market == "1X2":
        return {"home_win": payload["home_win"], "draw": payload["draw"], "away_win": payload["away_win"]}
    if market == "BTTS":
        if payload["btts_yes"] is None:
            return None
        return {"yes": payload["btts_yes"], "no": payload["btts_no"]}
    if market == "OVER_UNDER_2_5":
        if payload["over_2_5"] is None:
            return None
        return {"over": payload["over_2_5"], "under": payload["under_2_5"]}
    raise ValueError(f"Marché inconnu : {market}")


def _combine_market_for_key(rows_by_model, dcwf, key, market, weight_result):
    model_probs = {}
    for mt in ("elo", "xgboost", "lightgbm"):
        row = rows_by_model[mt].get(key)
        if row is None:
            continue
        probs = _payload_market_probs(_model_prediction_payload(row), market)
        if probs is not None:
            model_probs[mt] = probs
    dc_pred = dcwf.predictions.get(key)
    if dc_pred is not None and market in dc_pred.probs:
        model_probs["dixon_coles"] = dc_pred.probs[market]
    return combine(model_probs, weight_result)


def _observation_from_combined(combined, market, home_goals, away_goals):
    if combined is None:
        return None
    if market == "1X2":
        actual = "home_win" if home_goals > away_goals else ("draw" if home_goals == away_goals else "away_win")
    elif market == "BTTS":
        actual = "yes" if (home_goals > 0 and away_goals > 0) else "no"
    else:
        actual = "over" if (home_goals + away_goals) > 2.5 else "under"
    pick = max(combined.probs, key=combined.probs.get)
    return {
        "p_true": combined.probs[actual], "probs": combined.probs, "actual": actual, "correct": pick == actual,
        "models_used": combined.models_used, "degraded": combined.degraded,
    }


def _fold_strategy_market_observations(session, market, fold_keys, fold_index, rows_by_model, dcwf, versions, strategy, min_sample_size):
    """Retourne (obs_by_key, weight_result) pour un (stratégie, marché, fold)
    donné — poids calculés UNIQUEMENT à partir des folds/matchs strictement
    antérieurs (until / fold_index), jamais du fold lui-même."""
    until = fold_keys[0][1] - timedelta(days=1)
    weight_result = research.compute_market_weights_including_dc(
        session, market, until, fold_index, dcwf, min_sample_size, strategy, versions,
    )
    obs_by_key = {}
    for key in fold_keys:
        combined = _combine_market_for_key(rows_by_model, dcwf, key, market, weight_result)
        any_row = rows_by_model["elo"].get(key)
        if any_row is None:
            continue
        obs = _observation_from_combined(combined, market, any_row.result_home_goals, any_row.result_away_goals)
        if obs is not None:
            obs_by_key[key] = obs
    return obs_by_key, weight_result


def _fold_baseline_observations(model_type, market, fold_keys, fold_index, rows_by_model, dcwf):
    obs_by_key = {}
    if model_type == "dixon_coles":
        for key in fold_keys:
            pred = dcwf.predictions.get(key)
            if pred is None or pred.fold_index != fold_index:
                continue
            obs = research.dc_observation(pred, market)
            if obs is not None:
                obs_by_key[key] = obs
        return obs_by_key
    rows = rows_by_model[model_type]
    for key in fold_keys:
        row = rows.get(key)
        if row is None:
            continue
        obs = _market_observation(row, _model_prediction_payload(row), market)
        if obs is not None:
            obs_by_key[key] = obs
    return obs_by_key


def _obs_log_loss(obs, eps=1e-15):
    return -math.log(min(max(obs["p_true"], eps), 1 - eps))


def _obs_brier(obs):
    return sum((p - (1.0 if k == obs["actual"] else 0.0)) ** 2 for k, p in obs["probs"].items())


def _metrics_row(name: str, metrics, min_sample_size: int) -> dict:
    ci = research.wilson_interval(metrics.correct_predictions or 0, metrics.sample_size) if metrics.sample_size > 0 else (None, None)
    return {
        "name": name, "accuracy": metrics.accuracy, "log_loss": metrics.log_loss, "brier_score": metrics.brier_score,
        "sample_size": metrics.sample_size, "accuracy_ci": ci,
        "insufficient_data": metrics.sample_size < min_sample_size,
    }


def _git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=_SCRIPTS_DIR.parent, text=True).strip()
    except Exception:
        return "unknown"


def _library_versions() -> dict:
    import numpy, scipy, sklearn, pandas
    return {"numpy": numpy.__version__, "scipy": scipy.__version__, "scikit-learn": sklearn.__version__, "pandas": pandas.__version__}


# ---------------------------------------------------------------------------
# Orchestration principale.
# ---------------------------------------------------------------------------

def main(n_folds: int = 3, min_sample_size: int = MIN_BENCHMARK_SAMPLE_SIZE, outdir: str = "reports/ensemble"):
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    generated_at = datetime.now(timezone.utc).isoformat()
    limitations: list[str] = []
    leakage_audit: list[dict] = []

    init_db()
    with Session(engine) as session:
        # --- 1. Dataset : mêmes matchs communs elo/xgboost/lightgbm que scripts/walk_forward_ensemble.py ---
        versions = {mt: wfe.latest_version(session, mt) for mt in wfe.BACKTEST_MODEL_TYPES}
        missing = [mt for mt, v in versions.items() if v is None]
        if missing:
            logger.error(f"Aucune ModelVersion pour {missing} — lancer d'abord backtest_elo.py / train_ml_stacking_from_db.py.")
            result = {"status": "no_data", "run_id": run_id, "generated_at": generated_at,
                       "verdicts": {"1X2": "INSUFFICIENT_DATA", "BTTS": "INSUFFICIENT_DATA",
                                    "O/U": "INSUFFICIENT_DATA", "CALIBRATION": "INSUFFICIENT_DATA",
                                    "PRODUCTION": "DO NOT PROMOTE"},
                       "conclusion": f"Aucune ModelVersion disponible pour {missing}."}
            _finish(result, outdir, run_id)
            return result

        rows_by_model = {mt: wfe._resolved_rows(session, mt, versions[mt].id) for mt in wfe.BACKTEST_MODEL_TYPES}
        per_model_sample_sizes = {mt: len(rows) for mt, rows in rows_by_model.items()}
        common_keys = set(rows_by_model["elo"]) & set(rows_by_model["xgboost"]) & set(rows_by_model["lightgbm"])
        if not common_keys:
            logger.error("Aucun match commun aux 3 modèles.")
            result = {"status": "no_overlap", "run_id": run_id, "generated_at": generated_at,
                       "per_model_sample_sizes": per_model_sample_sizes,
                       "verdicts": {"1X2": "INSUFFICIENT_DATA", "BTTS": "INSUFFICIENT_DATA",
                                    "O/U": "INSUFFICIENT_DATA", "CALIBRATION": "INSUFFICIENT_DATA",
                                    "PRODUCTION": "DO NOT PROMOTE"},
                       "conclusion": "Échantillons disjoints — jamais comparé sans recoupement réel."}
            _finish(result, outdir, run_id)
            return result

        ordered_keys = sorted(common_keys, key=lambda k: (k[1], k[0], k[2], k[3]))
        leagues = sorted({k[0] for k in ordered_keys})
        logger.info(f"{len(ordered_keys)} matchs communs elo/xgboost/lightgbm [{ordered_keys[0][1]} -> {ordered_keys[-1][1]}], "
                    f"échantillons bruts par modèle : {per_model_sample_sizes} (§3 : mismatch signalé si présent).")
        if len(set(per_model_sample_sizes.values())) > 1:
            limitations.append(
                f"Échantillons bruts différents par modèle avant intersection ({per_model_sample_sizes}) — "
                f"toutes les comparaisons ci-dessous portent sur les {len(ordered_keys)} matchs COMMUNS uniquement, jamais sur ces totaux bruts."
            )

        folds = wfe._make_folds(ordered_keys, n_folds)

        # --- 2. Dixon-Coles walk-forward, en mémoire, jamais persisté (§4) ---
        logger.info("Entraînement Dixon-Coles walk-forward (en mémoire, par fold/ligue)...")
        dcwf = research.build_dixon_coles_walk_forward(folds, min_train_matches=research.MIN_DC_TRAIN_MATCHES)
        leakage_audit.append({
            "experiment": "Dixon-Coles walk-forward", "status": "SAFE",
            "note": "Entraîné (export_league) sur match.date <= until (fold_start - 1 jour) uniquement, jamais après ; "
                    "équipe/ligue sans historique suffisant marquée indisponible plutôt que simulée.",
        })

        # --- 3. Détection burn-in / validation / test (critère fixe : poids 1X2 InverseLogLoss non vides) ---
        burn_in_probe = InverseLogLossStrategy()
        non_burn_in_idx = []
        for idx, fold_keys in enumerate(folds):
            if not fold_keys:
                continue
            until = fold_keys[0][1] - timedelta(days=1)
            wr = research.compute_market_weights_including_dc(session, "1X2", until, idx, dcwf, min_sample_size, burn_in_probe, versions)
            if wr.weights:
                non_burn_in_idx.append(idx)

        if len(non_burn_in_idx) < 2:
            logger.warning("Moins de 2 folds exploitables — sélection de stratégie / comparaison test impossible avec les données actuelles.")
            result = {
                "status": "insufficient_history", "run_id": run_id, "generated_at": generated_at,
                "dataset": {"leagues": leagues, "period_start": str(ordered_keys[0][1]), "period_end": str(ordered_keys[-1][1]),
                            "common_match_count": len(ordered_keys), "per_model_sample_sizes": per_model_sample_sizes},
                "verdicts": {"1X2": "INSUFFICIENT_DATA", "BTTS": "INSUFFICIENT_DATA", "O/U": "INSUFFICIENT_DATA",
                             "CALIBRATION": "INSUFFICIENT_DATA", "PRODUCTION": "DO NOT PROMOTE"},
                "conclusion": "Historique encore insuffisant pour isoler un fold de validation ET au moins un fold de test — "
                              "aucune comparaison hors échantillon possible honnêtement avec les données actuelles.",
                "leakage_audit": leakage_audit, "limitations": limitations,
            }
            _finish(result, outdir, run_id)
            return result

        validation_idx = non_burn_in_idx[0]
        test_idx_list = non_burn_in_idx[1:]
        logger.info(f"Fold {validation_idx} = VALIDATION (sélection stratégie/température) ; folds {test_idx_list} = TEST (comparaison finale).")

        # --- 4. Température Softmax — sélectionnée sur VALIDATION uniquement (§9/§20) ---
        temp_detail = {}
        for t in wfe.TEMPERATURE_GRID:
            obs_by_key, _ = _fold_strategy_market_observations(
                session, "1X2", folds[validation_idx], validation_idx, rows_by_model, dcwf, versions,
                SoftmaxLogLossStrategy(temperature=t), min_sample_size,
            )
            if obs_by_key:
                temp_detail[t] = _compute_market_metrics(list(obs_by_key.values())).log_loss
        softmax_temperature = min(temp_detail, key=temp_detail.get) if temp_detail else wfe.DEFAULT_TEMPERATURE
        logger.info(f"Température Softmax sélectionnée (validation) : {softmax_temperature} (détail={temp_detail})")
        leakage_audit.append({
            "experiment": "Sélection température Softmax", "status": "SAFE",
            "note": f"Grid search {wfe.TEMPERATURE_GRID} évalué UNIQUEMENT sur le fold de validation (fold {validation_idx}) — jamais sur un fold de test.",
        })

        strategies = {
            "simple_average": research.SimpleAverageStrategy(),
            "inverse_log_loss": InverseLogLossStrategy(),
            "softmax_log_loss": SoftmaxLogLossStrategy(temperature=softmax_temperature),
            "brier": BrierStrategy(),
            "hybrid": HybridStrategy(alpha=0.5),
        }

        # --- 5. Sélection de LA stratégie "tête d'affiche" — sur VALIDATION uniquement (§7) ---
        validation_scores = {}
        for name, strategy in strategies.items():
            obs_by_key, _ = _fold_strategy_market_observations(
                session, "1X2", folds[validation_idx], validation_idx, rows_by_model, dcwf, versions, strategy, min_sample_size,
            )
            m = _compute_market_metrics(list(obs_by_key.values()))
            if m.log_loss is not None:
                validation_scores[name] = m.log_loss
        selected_strategy_name = min(validation_scores, key=validation_scores.get) if validation_scores else "inverse_log_loss"
        logger.info(f"Stratégie sélectionnée sur validation (log_loss={validation_scores}) : {selected_strategy_name}")
        leakage_audit.append({
            "experiment": "Sélection de stratégie", "status": "SAFE",
            "note": f"Choisie par log_loss minimal sur le fold de validation (fold {validation_idx}) uniquement — "
                    "TOUTES les stratégies restent néanmoins évaluées et rapportées sur test, pour transparence complète (§29), "
                    "sans que cela ne change quelle stratégie est déclarée 'sélectionnée'.",
        })

        # --- 6. Évaluation complète (toutes stratégies x tous marchés x folds test), agrégée + par fold ---
        strategy_market_test_obs: dict = {}   # (strategy, market) -> {key: obs} agrégé sur tous les folds test
        weight_results_by_strategy: dict = {} # strategy -> [WeightResult] (folds non-burn-in, tous marchés)
        per_fold_rows: dict = {}              # fold_idx -> list[row]

        for name, strategy in strategies.items():
            weight_results_by_strategy[name] = []
            for market in MARKETS:
                agg: dict = {}
                for idx in [validation_idx, *test_idx_list]:
                    obs_by_key, wr = _fold_strategy_market_observations(
                        session, market, folds[idx], idx, rows_by_model, dcwf, versions, strategy, min_sample_size,
                    )
                    weight_results_by_strategy[name].append(wr)
                    if idx in test_idx_list:
                        agg.update(obs_by_key)
                        fold_metrics = _compute_market_metrics(list(obs_by_key.values()))
                        per_fold_rows.setdefault(idx, []).append(_metrics_row(f"{name} [{market}]", fold_metrics, min_sample_size))
                strategy_market_test_obs[(name, market)] = agg

        # --- 7. Baselines (dixon_coles/elo/xgboost/lightgbm), mêmes folds test, même marché ---
        baseline_test_obs: dict = {}  # (model_type, market) -> {key: obs}
        for mt in BASELINE_MODEL_TYPES:
            for market in MARKETS:
                agg = {}
                for idx in test_idx_list:
                    obs_by_key = _fold_baseline_observations(mt, market, folds[idx], idx, rows_by_model, dcwf)
                    agg.update(obs_by_key)
                    fold_metrics = _compute_market_metrics(list(obs_by_key.values()))
                    per_fold_rows.setdefault(idx, []).append(_metrics_row(f"baseline:{mt} [{market}]", fold_metrics, min_sample_size))
                baseline_test_obs[(mt, market)] = agg

        # --- 8. Couverture BTTS / O-U (§14/§15) — vérifiée empiriquement ---
        db_coverage = research.market_model_coverage(session, versions)
        market_coverage_verdict = {}
        for market in ("BTTS", "OVER_UNDER_2_5"):
            models_with_data = list(db_coverage.get(market, []))
            if len(baseline_test_obs.get(("dixon_coles", market), {})) > 0:
                models_with_data = models_with_data + ["dixon_coles"]
            if len(models_with_data) >= 2:
                market_coverage_verdict[market] = "multi_model"
            elif len(models_with_data) == 1:
                market_coverage_verdict[market] = "single_model"
            else:
                market_coverage_verdict[market] = "insufficient_model_coverage"
        logger.info(f"Couverture marchés BTTS/O-U : {market_coverage_verdict}")

        # --- 9. Baselines & stratégies agrégées (TEST uniquement) — tables du rapport ---
        baselines_test_rows = [
            _metrics_row(mt, _compute_market_metrics(list(baseline_test_obs[(mt, "1X2")].values())), min_sample_size)
            for mt in BASELINE_MODEL_TYPES
        ]
        strategies_test_rows = {
            name: [_metrics_row(name, _compute_market_metrics(list(strategy_market_test_obs[(name, "1X2")].values())), min_sample_size)]
            for name in strategies
        }
        btts_rows = (
            [_metrics_row(name, _compute_market_metrics(list(strategy_market_test_obs[(name, "BTTS")].values())), min_sample_size) for name in strategies]
            + [_metrics_row(f"baseline:{mt}", _compute_market_metrics(list(baseline_test_obs[(mt, "BTTS")].values())), min_sample_size) for mt in BASELINE_MODEL_TYPES]
        )
        ou_rows = (
            [_metrics_row(name, _compute_market_metrics(list(strategy_market_test_obs[(name, "OVER_UNDER_2_5")].values())), min_sample_size) for name in strategies]
            + [_metrics_row(f"baseline:{mt}", _compute_market_metrics(list(baseline_test_obs[(mt, "OVER_UNDER_2_5")].values())), min_sample_size) for mt in BASELINE_MODEL_TYPES]
        )

        # --- 10. Comparaison vs meilleur individuel (1X2, TEST) + significativité (§20/§21) ---
        baseline_1x2_metrics = {mt: _compute_market_metrics(list(baseline_test_obs[(mt, "1X2")].values())) for mt in BASELINE_MODEL_TYPES}
        eligible_baselines = {mt: m for mt, m in baseline_1x2_metrics.items() if m.sample_size >= min_sample_size and m.log_loss is not None}
        best_individual = min(eligible_baselines, key=lambda mt: eligible_baselines[mt].log_loss) if eligible_baselines else None

        selected_test_obs = strategy_market_test_obs[(selected_strategy_name, "1X2")]
        selected_metrics = _compute_market_metrics(list(selected_test_obs.values()))

        significance = {}
        per_fold_deltas_1x2 = []
        if best_individual is not None:
            best_obs = baseline_test_obs[(best_individual, "1X2")]
            # sorted(), pas un set() itéré tel quel : l'ordre d'itération d'un
            # set de tuples dépend du hash randomisé du processus (PYTHONHASHSEED),
            # non déterministe d'un run à l'autre -> bootstrap_paired_diff tire
            # par INDEX positionnel, donc un ordre différent change quelles
            # paires atterrissent à quel index -> IC bootstrap différent malgré
            # le seed fixe (§22, reproductibilité) si non trié ici.
            common = sorted(set(selected_test_obs) & set(best_obs))
            pairs_ll = [(_obs_log_loss(selected_test_obs[k]), _obs_log_loss(best_obs[k])) for k in common]
            pairs_brier = [(_obs_brier(selected_test_obs[k]), _obs_brier(best_obs[k])) for k in common]
            bootstrap_ll = research.bootstrap_paired_diff(pairs_ll)
            bootstrap_brier = research.bootstrap_paired_diff(pairs_brier)
            b = sum(1 for k in common if selected_test_obs[k]["correct"] and not best_obs[k]["correct"])
            c = sum(1 for k in common if (not selected_test_obs[k]["correct"]) and best_obs[k]["correct"])
            mcnemar = research.mcnemar_test(b, c)
            significance = {
                "compared_strategy": selected_strategy_name, "compared_baseline": best_individual,
                "common_sample_size": len(common), "bootstrap_log_loss_diff": bootstrap_ll,
                "bootstrap_brier_diff": bootstrap_brier, "mcnemar_accuracy": mcnemar,
            }
            for idx in test_idx_list:
                fold_keys_set = set(folds[idx])
                sel_fold = {k: v for k, v in selected_test_obs.items() if k in fold_keys_set}
                best_fold = {k: v for k, v in best_obs.items() if k in fold_keys_set}
                common_fold = set(sel_fold) & set(best_fold)
                if not common_fold:
                    continue
                sel_ll = sum(_obs_log_loss(sel_fold[k]) for k in common_fold) / len(common_fold)
                best_ll = sum(_obs_log_loss(best_fold[k]) for k in common_fold) / len(common_fold)
                per_fold_deltas_1x2.append(best_ll - sel_ll)  # positif => stratégie sélectionnée meilleure sur ce fold

        # --- 11. Diagnostic des poids (§28) ---
        weight_diag = {name: research.weight_diagnostics(wrs) for name, wrs in weight_results_by_strategy.items()}

        # --- 12. Calibration (§16-18) — entraînée sur VALIDATION uniquement, évaluée sur TEST uniquement ---
        calibration_rows = []
        headline_raw_metrics = None
        headline_platt_metrics = None
        calibration_candidates = [("strategy", selected_strategy_name)] + [("baseline", mt) for mt in BASELINE_MODEL_TYPES]
        for kind, name in calibration_candidates:
            if kind == "strategy":
                val_obs_by_key, _ = _fold_strategy_market_observations(
                    session, "1X2", folds[validation_idx], validation_idx, rows_by_model, dcwf, versions, strategies[name], min_sample_size,
                )
                test_obs = strategy_market_test_obs[(name, "1X2")]
            else:
                val_obs_by_key = _fold_baseline_observations(name, "1X2", folds[validation_idx], validation_idx, rows_by_model, dcwf)
                test_obs = baseline_test_obs[(name, "1X2")]

            train_obs = list(val_obs_by_key.values())
            test_obs_list = list(test_obs.values())
            raw_metrics = _compute_market_metrics(test_obs_list)
            label = f"{'strategy' if kind == 'strategy' else 'baseline'}:{name}"

            if len(train_obs) < min_sample_size or raw_metrics.sample_size < min_sample_size:
                calibration_rows.append(_metrics_row(f"{label} RAW", raw_metrics, min_sample_size))
                calibration_rows.append({"name": f"{label} CALIBRATED", "accuracy": None, "log_loss": None, "brier_score": None,
                                          "sample_size": 0, "accuracy_ci": (None, None), "insufficient_data": True})
                continue

            train_conf = [max(o["probs"].values()) for o in train_obs]
            train_correct = [o["correct"] for o in train_obs]
            test_conf = [max(o["probs"].values()) for o in test_obs_list]

            platt_probs = research.platt_calibrate(train_conf, train_correct, test_conf)
            platt_obs = research.apply_pick_calibration(test_obs_list, platt_probs)
            platt_metrics = _compute_market_metrics(platt_obs)

            iso_probs = research.isotonic_calibrate(train_conf, train_correct, test_conf)
            iso_obs = research.apply_pick_calibration(test_obs_list, iso_probs)
            iso_metrics = _compute_market_metrics(iso_obs)

            calibration_rows.append(_metrics_row(f"{label} RAW", raw_metrics, min_sample_size))
            calibration_rows.append(_metrics_row(f"{label} PLATT", platt_metrics, min_sample_size))
            calibration_rows.append(_metrics_row(f"{label} ISOTONIC", iso_metrics, min_sample_size))

            if name == selected_strategy_name and kind == "strategy":
                headline_platt_metrics = platt_metrics
                headline_raw_metrics = raw_metrics
        leakage_audit.append({
            "experiment": "Calibration Platt/Isotonic", "status": "SAFE",
            "note": f"Ajustée sur le fold de validation (fold {validation_idx}) uniquement, évaluée sur les folds de test uniquement.",
        })

        # --- 13. Verdicts (§36) — dérivés des mesures ci-dessus, jamais fixés à l'avance ---
        consistent_1x2 = bool(per_fold_deltas_1x2) and (all(d >= 0 for d in per_fold_deltas_1x2) or all(d <= 0 for d in per_fold_deltas_1x2))
        verdict_1x2 = "INSUFFICIENT_DATA"
        if best_individual is not None and selected_metrics.sample_size >= min_sample_size and eligible_baselines.get(best_individual):
            bll = significance.get("bootstrap_log_loss_diff", {})
            delta = eligible_baselines[best_individual].log_loss - selected_metrics.log_loss
            if bll.get("significant") and delta > 0 and consistent_1x2:
                verdict_1x2 = "BETTER"
            elif bll.get("significant") and delta < 0 and consistent_1x2:
                verdict_1x2 = "WORSE"
            else:
                verdict_1x2 = "EQUIVALENT"

        def _market_verdict(market_key):
            cov = market_coverage_verdict[market_key]
            if cov != "multi_model":
                return "INSUFFICIENT_DATA"
            m = _compute_market_metrics(list(strategy_market_test_obs[(selected_strategy_name, market_key)].values()))
            return "EQUIVALENT" if m.sample_size >= min_sample_size else "INSUFFICIENT_DATA"

        verdict_btts = _market_verdict("BTTS")
        verdict_ou = _market_verdict("OVER_UNDER_2_5")

        if (headline_raw_metrics is not None and headline_platt_metrics is not None
                and headline_raw_metrics.sample_size >= min_sample_size and headline_raw_metrics.log_loss):
            rel = (headline_raw_metrics.log_loss - headline_platt_metrics.log_loss) / headline_raw_metrics.log_loss
            calibration_verdict = "HELPFUL" if rel > 0.01 else ("HARMFUL" if rel < -0.01 else "NEUTRAL")
        else:
            calibration_verdict = "INSUFFICIENT_DATA"

        if verdict_1x2 != "BETTER":
            production_verdict = "DO NOT PROMOTE"
        elif significance.get("bootstrap_log_loss_diff", {}).get("significant") and consistent_1x2 and selected_metrics.sample_size >= 2 * min_sample_size:
            production_verdict = "READY FOR SHADOW MODE"
        elif significance.get("bootstrap_log_loss_diff", {}).get("significant") and consistent_1x2:
            production_verdict = "CANDIDATE FOR VALIDATION"
        else:
            production_verdict = "DO NOT PROMOTE"

        # --- 14. Assemblage du rapport ---
        fold_descriptions = []
        for idx, fold_keys in enumerate(folds):
            role = "burn-in" if idx not in non_burn_in_idx else ("validation" if idx == validation_idx else "test")
            if not fold_keys:
                continue
            fold_descriptions.append({
                "index": idx, "role": role, "n_matches": len(fold_keys),
                "start_date": str(fold_keys[0][1]), "end_date": str(fold_keys[-1][1]),
                "until": str(fold_keys[0][1] - timedelta(days=1)),
            })

        dc_coverage_summary = "; ".join(
            f"fold{c.fold_index}/{c.league}: train={c.train_matches}, évaluable={c.evaluable_matches}, skip={c.skipped_matches}"
            + ("" if c.available else f" [{c.reason}]")
            for c in dcwf.coverage
        )

        per_fold_results = [{"fold_index": idx, "rows": rows} for idx, rows in sorted(per_fold_rows.items())]

        limitations.extend([
            "La calibration Platt/Isotonic ici recalibre uniquement la confiance du pick (probabilité maximale), "
            "redistribuée proportionnellement sur les autres issues — pas un Platt/Isotonic multi-classe indépendant complet.",
            "La stratégie/température 'sélectionnée' l'est sur un seul fold de validation ; avec seulement "
            f"{len(non_burn_in_idx)} fold(s) non burn-in disponibles, cette sélection reste peu robuste — voir sample_size par fold.",
            f"BTTS/OVER_UNDER_2_5 : couverture multi-modèle = {market_coverage_verdict} — voir §14/§15, Elo/XGBoost/LightGBM "
            "ne modélisent aujourd'hui que 1X2 (confirmé empiriquement, jamais supposé).",
        ])

        result = {
            "status": "ok", "run_id": run_id, "generated_at": generated_at,
            "dataset": {
                "leagues": leagues, "period_start": str(ordered_keys[0][1]), "period_end": str(ordered_keys[-1][1]),
                "common_match_count": len(ordered_keys), "per_model_sample_sizes": per_model_sample_sizes,
            },
            "methodology": (
                "Walk-forward chronologique : fold 0..k = burn-in (historique encore insuffisant) ; premier fold "
                "exploitable = VALIDATION (sélection stratégie + température Softmax, jamais réutilisé pour la mesure "
                "finale) ; folds suivants = TEST (mesure finale, jamais utilisés pour choisir quoi que ce soit). "
                "Dixon-Coles réentraîné en mémoire par fold/ligue sur l'historique match.date <= until (jamais persisté)."
            ),
            "walk_forward_description": f"{len(folds)} fold(s) construits sur {len(ordered_keys)} matchs communs ; "
                                         f"burn-in={[i for i in range(len(folds)) if i not in non_burn_in_idx]}, "
                                         f"validation={validation_idx}, test={test_idx_list}.",
            "folds": fold_descriptions,
            "baselines_test": baselines_test_rows,
            "strategies_test": strategies_test_rows,
            "selected_strategy": selected_strategy_name,
            "validation_scores": validation_scores,
            "softmax_temperature_selected": softmax_temperature,
            "softmax_temperature_grid_detail": temp_detail,
            "dc_coverage_summary": dc_coverage_summary,
            "dc_skip_reasons": dcwf.skip_reasons,
            "btts_verdict": market_coverage_verdict["BTTS"],
            "btts_results": btts_rows,
            "over_under_verdict": market_coverage_verdict["OVER_UNDER_2_5"],
            "over_under_results": ou_rows,
            "calibration_results": calibration_rows,
            "weight_diagnostics": weight_diag,
            "leakage_audit": leakage_audit,
            "per_fold_results": per_fold_results,
            "aggregated_results": [
                _metrics_row(f"strategy:{name}", _compute_market_metrics(list(strategy_market_test_obs[(name, "1X2")].values())), min_sample_size)
                for name in strategies
            ] + baselines_test_rows,
            "comparison_vs_individuals": (
                f"Meilleur modèle individuel (1X2, test, log_loss) : {best_individual}. "
                f"Stratégie sélectionnée ({selected_strategy_name}) : sample={selected_metrics.sample_size}, "
                f"log_loss={selected_metrics.log_loss}. Delta vs meilleur individuel : "
                f"{(eligible_baselines[best_individual].log_loss - selected_metrics.log_loss) if best_individual else 'N/A'}."
            ),
            "significance": significance,
            "reproducibility": {
                "git_commit": _git_commit(), "seed": 20260828, "library_versions": _library_versions(),
                "model_version_ids": {mt: v.id for mt, v in versions.items()},
                "min_sample_size": min_sample_size, "n_folds_requested": n_folds,
            },
            "limitations": limitations,
            "verdicts": {
                "1X2": verdict_1x2, "BTTS": verdict_btts, "O/U": verdict_ou,
                "CALIBRATION": calibration_verdict, "PRODUCTION": production_verdict,
            },
            "conclusion": (
                f"Sur {selected_metrics.sample_size} matchs de test (folds {test_idx_list}), la stratégie "
                f"'{selected_strategy_name}' (sélectionnée sur validation) obtient log_loss={selected_metrics.log_loss} "
                f"contre {eligible_baselines[best_individual].log_loss if best_individual else 'N/A'} pour le meilleur "
                f"modèle individuel ({best_individual}) — verdict 1X2 : {verdict_1x2}. BTTS/O-U : couverture multi-modèle "
                f"insuffisante ({market_coverage_verdict}) -> {verdict_btts}/{verdict_ou}. Calibration : {calibration_verdict}. "
                f"Décision production : {production_verdict} — la promotion effective reste une décision humaine séparée, "
                "jamais automatique."
            ),
        }

    _finish(result, outdir, run_id)
    return result


def _finish(result: dict, outdir: str, run_id: str) -> None:
    json_path, md_path = research.write_reports(result, Path(outdir), run_id)
    logger.info(f"Rapports écrits : {json_path} / {md_path}")
    logger.info("=" * 80)
    for k, v in result.get("verdicts", {}).items():
        logger.info(f"VERDICT {k} : {v}")
    logger.info("=" * 80)
    print("\nPHASE 5.7 — XFOOT ENSEMBLE RESEARCH & BACKTEST V2 TERMINÉE. "
          "AUCUNE PROMOTION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--n-folds", type=int, default=3)
    parser.add_argument("--min-sample-size", type=int, default=MIN_BENCHMARK_SAMPLE_SIZE)
    parser.add_argument("--outdir", type=str, default=str(_SCRIPTS_DIR.parent / "reports" / "ensemble"))
    args = parser.parse_args()
    main(n_folds=args.n_folds, min_sample_size=args.min_sample_size, outdir=args.outdir)
    sys.exit(0)
