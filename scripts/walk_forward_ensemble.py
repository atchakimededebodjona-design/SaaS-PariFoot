"""
scripts/walk_forward_ensemble.py — Phase 7/8 : évaluation walk-forward de
l'Ensemble (api/app/ai/arena/ensemble.py) contre Elo/XGBoost/LightGBM pris
individuellement, SANS FUITE (§19-§20-§21 Phase 8, §19-§20-§21 Phase 7).
=============================================================================

Audit préalable (vérifié en base, pas supposé) : les derniers backtests
Elo (scripts/backtest_elo.py) et XGBoost/LightGBM (scripts/train_ml_stacking_from_db.py)
ont, par construction indépendante, un test-set qui SE RECOUVRE exactement
sur les mêmes 300 matchs (les 300 derniers matchs, toutes ligues confondues,
utilisés par train_ml_stacking_from_db.py — inclus dans la fenêtre plus
large des 100-derniers-matchs-PAR-LIGUE d'Elo). Dixon-Coles n'a, lui, AUCUNE
prédiction dans model_predictions qui recoupe cette fenêtre (ses seules
prédictions "backtest" n'existent pas — il n'écrit que du "live", voir
prediction_logging.py) : il est donc structurellement absent de cette
évaluation walk-forward, jamais artificiellement ajouté.

Méthode (partie PERSISTÉE, Phase 7 — inchangée) :
  1. Prend la version la PLUS RÉCENTE de chaque modèle (elo/xgboost/lightgbm)
     — pas nécessairement sa version "active" : is_active reflète depuis la
     Phase 8 un état opérationnel (déployé ou non), pas un critère
     d'éligibilité Ensemble (voir docstring de
     EnsembleEngine.compute_market_weights, paramètre `model_versions`).
  2. Restreint aux matchs COMMUNS aux 3 modèles, triés chronologiquement.
  3. Découpe en N folds chronologiques (--n-folds, défaut 3). Les poids de
     chaque fold sont calculés à partir des SEULES prédictions dont le match
     a eu lieu AVANT le premier match du fold — jamais les résultats du fold
     lui-même. Le fold 0 sert de pur burn-in (aucun historique disponible).
  4. Chaque prédiction Ensemble (stratégie InverseLogLoss, la baseline) est
     journalisée dans model_predictions (model_type="ensemble",
     source="backtest") — c'est CETTE version qui alimente GET
     /models/performance.

Méthode (partie COMPARAISON DE STRATÉGIES, Phase 8 §14-§20, --compare-strategies) :
  5. La température Softmax est sélectionnée par GRID SEARCH sur un fold de
     VALIDATION dédié (celui immédiatement après le burn-in, typiquement le
     fold 1) — jamais sur le fold finalement rapporté dans le tableau
     comparatif (§20 : anti-overfitting). Si aucun fold de validation
     n'est exploitable (historique encore insuffisant), replie sur une
     valeur par défaut documentée (5.0), jamais choisie après coup contre
     le test.
  6. inverse_log_loss / softmax_log_loss (température sélectionnée) / brier
     / hybrid (alpha=0.5, fixe — jamais optimisé, voir §18) sont ensuite
     évalués EN MÉMOIRE (jamais persistés dans model_predictions — un seul
     Ensemble "officiel" existe, celui de l'étape 4) sur le(s) fold(s)
     RESTANT(S) après le fold de validation, pour une comparaison à armes
     égales entre les 4 stratégies sur exactement le même sous-ensemble de
     matchs.

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/walk_forward_ensemble.py \
        [--n-folds 3] [--min-sample-size 100] [--compare-strategies]
"""

import argparse
import logging
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from sqlmodel import Session, select  # noqa: E402
from app.core.database import engine, init_db  # noqa: E402
from app.models.model_prediction import ModelPrediction  # noqa: E402
from app.models.team_rating import ModelVersion, next_version_name  # noqa: E402
from app.ai.arena.prediction_logging import PredictionRecord, log_prediction, resolve_prediction  # noqa: E402
from app.ai.arena.ensemble import (  # noqa: E402
    compute_market_weights, combine, latest_version, MIN_BENCHMARK_SAMPLE_SIZE,
    InverseLogLossStrategy, SoftmaxLogLossStrategy, BrierStrategy, HybridStrategy,
)
from app.ai.arena.service import _compute_market_metrics, _market_observation, _model_prediction_payload  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("walk_forward_ensemble")

BACKTEST_MODEL_TYPES = ("elo", "xgboost", "lightgbm")  # dixon_coles exclu ici — voir docstring module
TEMPERATURE_GRID = (1.0, 2.0, 5.0, 10.0, 20.0, 50.0)
DEFAULT_TEMPERATURE = 5.0  # repli documenté si aucune validation n'est possible (jamais choisi après coup)


def _resolved_rows(session, model_type: str, version_id: int) -> dict[tuple, ModelPrediction]:
    rows = session.exec(
        select(ModelPrediction).where(
            ModelPrediction.model_type == model_type,
            ModelPrediction.model_version_id == version_id,
            ModelPrediction.status == "resolved",
        )
    ).all()
    return {(r.league, r.match_date, r.home_team, r.away_team): r for r in rows}


def _make_folds(ordered_keys: list[tuple], n_folds: int) -> list[list[tuple]]:
    fold_size = max(1, len(ordered_keys) // n_folds)
    folds = [ordered_keys[i:i + fold_size] for i in range(0, len(ordered_keys), fold_size)]
    if len(folds) > n_folds:  # reste de la division -> jamais un fold final minuscule séparé
        folds[-2].extend(folds[-1])
        folds = folds[:-1]
    return folds


def _model_probs_for_key(rows_by_model: dict, key: tuple) -> dict:
    return {
        mt: {"home_win": rows_by_model[mt][key].prob_home, "draw": rows_by_model[mt][key].prob_draw,
             "away_win": rows_by_model[mt][key].prob_away}
        for mt in BACKTEST_MODEL_TYPES
    }


def _evaluate_predictions(predictions: list[tuple[dict, int, int]]) -> dict:
    """
    Accuracy/log_loss/brier_score sur une liste de (probs_1X2, home_goals,
    away_goals) — MÊME définition que service.py::_compute_market_metrics
    (clip à 1e-15 contre log(0), brier = somme des carrés d'écart sur les 3
    issues) : réimplémentation locale volontaire (évite de fabriquer de
    faux objets ModelPrediction juste pour réutiliser _market_observation,
    qui attend des lignes déjà en base) — jamais une formule différente.
    """
    if not predictions:
        return {"sample_size": 0, "accuracy": None, "log_loss": None, "brier_score": None}
    eps = 1e-15
    n = len(predictions)
    correct, ll_sum, brier_sum = 0, 0.0, 0.0
    for probs, hg, ag in predictions:
        actual = "home_win" if hg > ag else ("draw" if hg == ag else "away_win")
        predicted = max(probs, key=probs.get)
        if predicted == actual:
            correct += 1
        p_true = min(max(probs[actual], eps), 1 - eps)
        ll_sum += -math.log(p_true)
        brier_sum += sum((p - (1.0 if k == actual else 0.0)) ** 2 for k, p in probs.items())
    return {"sample_size": n, "accuracy": correct / n, "log_loss": ll_sum / n, "brier_score": brier_sum / n}


def _run_folds_in_memory(session, folds, versions, rows_by_model, strategy, min_sample_size) -> list[tuple[dict, int, int]]:
    """Rejoue le même découpage en folds que la persistance walk-forward,
    mais SANS écrire en base — un seul Ensemble "officiel" existe (voir
    §4 dans le docstring module), les autres stratégies restent une analyse
    en mémoire, jamais un doublon persistant."""
    predictions = []
    for fold_keys in folds:
        until = fold_keys[0][1] - timedelta(days=1)
        weight_result = compute_market_weights(session, "1X2", until, min_sample_size, model_versions=versions, strategy=strategy)
        if not weight_result.weights:
            continue
        for key in fold_keys:
            combined = combine(_model_probs_for_key(rows_by_model, key), weight_result)
            if combined is None:
                continue
            any_row = rows_by_model["elo"][key]
            predictions.append((combined.probs, any_row.result_home_goals, any_row.result_away_goals))
    return predictions


def select_softmax_temperature(session, validation_fold: list[tuple], versions, rows_by_model, min_sample_size) -> tuple[float, dict]:
    """
    Choisit `temperature` dans TEMPERATURE_GRID en minimisant le log_loss
    SUR `validation_fold` UNIQUEMENT (§16/§20 : anti-overfitting — jamais
    sur le fold finalement rapporté dans le tableau comparatif). Retourne
    (température choisie, détail par candidat) — repli sur
    DEFAULT_TEMPERATURE, documenté, si aucun candidat n'a pu être évalué
    (historique encore insuffisant sur le fold de validation lui-même).
    """
    until = validation_fold[0][1] - timedelta(days=1)
    detail = {}
    for t in TEMPERATURE_GRID:
        weight_result = compute_market_weights(session, "1X2", until, min_sample_size, model_versions=versions,
                                                 strategy=SoftmaxLogLossStrategy(temperature=t))
        if not weight_result.weights:
            continue
        preds = []
        for key in validation_fold:
            combined = combine(_model_probs_for_key(rows_by_model, key), weight_result)
            if combined is None:
                continue
            any_row = rows_by_model["elo"][key]
            preds.append((combined.probs, any_row.result_home_goals, any_row.result_away_goals))
        if preds:
            detail[t] = _evaluate_predictions(preds)

    if not detail:
        logger.info(f"Sélection de température Softmax impossible sur le fold de validation "
                    f"(historique encore insuffisant) -> repli documenté sur {DEFAULT_TEMPERATURE}.")
        return DEFAULT_TEMPERATURE, {}

    best_temp = min(detail, key=lambda t: detail[t]["log_loss"])
    logger.info(f"Température Softmax sélectionnée par validation ({len(detail)} candidates évaluées "
                f"sur {len(validation_fold)} matchs) : {best_temp} (log_loss validation={detail[best_temp]['log_loss']:.4f})")
    return best_temp, detail


def compare_strategies(session, folds, versions, rows_by_model, min_sample_size) -> dict:
    """
    §14-§20 : compare les 4 stratégies sur le(s) fold(s) suivant le fold de
    validation (réservé à la sélection de température Softmax, jamais
    réutilisé dans la comparaison elle-même) — à armes égales, même
    sous-ensemble de matchs pour les 4.
    """
    non_burn_in = [f for f in folds if compute_market_weights(
        session, "1X2", f[0][1] - timedelta(days=1), min_sample_size, model_versions=versions,
    ).weights]
    if len(non_burn_in) < 2:
        logger.warning("Moins de 2 folds exploitables (historique insuffisant) — comparaison de stratégies impossible "
                        "avec les données actuelles.")
        return {}

    validation_fold, comparison_folds = non_burn_in[0], non_burn_in[1:]
    temperature, temp_detail = select_softmax_temperature(session, validation_fold, versions, rows_by_model, min_sample_size)

    strategies = {
        "inverse_log_loss": InverseLogLossStrategy(),
        "softmax_log_loss": SoftmaxLogLossStrategy(temperature=temperature),
        "brier": BrierStrategy(),
        "hybrid": HybridStrategy(alpha=0.5),
    }

    results = {}
    for name, strategy in strategies.items():
        preds = _run_folds_in_memory(session, comparison_folds, versions, rows_by_model, strategy, min_sample_size)
        results[name] = _evaluate_predictions(preds)

    logger.info("=" * 80)
    logger.info(f"COMPARAISON DE STRATÉGIES — fold de validation (température) : {len(validation_fold)} matchs ; "
                f"fold(s) de comparaison : {sum(len(f) for f in comparison_folds)} matchs")
    logger.info(f"  {'Stratégie':<18} {'Sample':>8} {'Accuracy':>10} {'LogLoss':>10} {'Brier':>10}")
    for name, m in results.items():
        if m["sample_size"] == 0:
            logger.info(f"  {name:<18} {'0':>8}   (aucune prédiction évaluable)")
            continue
        logger.info(f"  {name:<18} {m['sample_size']:>8} {m['accuracy']:>10.4f} {m['log_loss']:>10.4f} {m['brier_score']:>10.4f}")
    logger.info("=" * 80)

    return {
        "results": results, "temperature_selected": temperature, "temperature_grid_detail": temp_detail,
        "validation_fold_size": len(validation_fold), "comparison_sample_size": sum(len(f) for f in comparison_folds),
    }


def main(n_folds: int, min_sample_size: int, do_compare_strategies: bool):
    init_db()
    with Session(engine) as session:
        versions = {mt: latest_version(session, mt) for mt in BACKTEST_MODEL_TYPES}
        missing = [mt for mt, v in versions.items() if v is None]
        if missing:
            logger.error(f"Aucune ModelVersion pour {missing} — lancer d'abord backtest_elo.py / train_ml_stacking_from_db.py.")
            return {"status": "no_data"}

        logger.info("Versions utilisées : " + ", ".join(f"{mt}={v.name}(#{v.id})" for mt, v in versions.items()))

        rows_by_model = {mt: _resolved_rows(session, mt, versions[mt].id) for mt in BACKTEST_MODEL_TYPES}
        common_keys = set(rows_by_model["elo"]) & set(rows_by_model["xgboost"]) & set(rows_by_model["lightgbm"])
        if not common_keys:
            logger.error("Aucun match commun aux 3 modèles — impossible d'évaluer un Ensemble comparable.")
            return {"status": "no_overlap"}

        ordered_keys = sorted(common_keys, key=lambda k: (k[1], k[0], k[2], k[3]))  # match_date, league, home, away
        logger.info(f"{len(ordered_keys)} matchs communs à elo/xgboost/lightgbm "
                    f"[{ordered_keys[0][1]} -> {ordered_keys[-1][1]}] — base de l'évaluation walk-forward.")

        folds = _make_folds(ordered_keys, n_folds)

        # --- Partie persistée (Phase 7, inchangée) : InverseLogLossStrategy, LE seul Ensemble officiel. ---
        default_strategy = InverseLogLossStrategy()
        ensemble_version = None
        evaluated_keys: list[tuple] = []
        burn_in_skipped = 0

        for fold_idx, fold_keys in enumerate(folds):
            until = fold_keys[0][1] - timedelta(days=1)
            weight_result = compute_market_weights(session, "1X2", until, min_sample_size, model_versions=versions, strategy=default_strategy)

            if not weight_result.weights:
                logger.info(f"[fold {fold_idx}] {len(fold_keys)} matchs à partir de {fold_keys[0][1]} — "
                            f"AUCUN modèle éligible (historique < {min_sample_size} avant cette date) : "
                            "fold de burn-in, aucune prédiction Ensemble possible.")
                burn_in_skipped += len(fold_keys)
                continue

            logger.info(f"[fold {fold_idx}] {len(fold_keys)} matchs à partir de {fold_keys[0][1]} — "
                        f"poids : " + ", ".join(f"{mt}={w.weight:.4f}(n={w.sample_size})" for mt, w in weight_result.weights.items()))

            if ensemble_version is None:
                ensemble_version = ModelVersion(
                    name=next_version_name(session, "xfoot-ensemble"), model_type="ensemble",
                    trained_at=datetime.now(timezone.utc), is_active=False,
                    notes=(f"Évaluation walk-forward (elo+xgboost+lightgbm), stratégie=inverse_log_loss, "
                           f"{n_folds} folds, min_sample_size={min_sample_size} — voir scripts/walk_forward_ensemble.py."),
                )
                session.add(ensemble_version)
                session.commit()
                session.refresh(ensemble_version)

            for key in fold_keys:
                combined = combine(_model_probs_for_key(rows_by_model, key), weight_result)
                if combined is None:
                    continue
                league, match_date, home_team, away_team = key
                any_row = rows_by_model["elo"][key]
                record = PredictionRecord(
                    league=league, match_date=match_date, home_team=home_team, away_team=away_team,
                    model_type="ensemble",
                    prob_home=combined.probs["home_win"], prob_draw=combined.probs["draw"], prob_away=combined.probs["away_win"],
                    source="backtest",
                )
                pred_row = log_prediction(session, record, ensemble_version.id)
                if pred_row.status != "resolved":
                    resolve_prediction(pred_row, any_row.result_home_goals, any_row.result_away_goals)
                    session.add(pred_row)
                session.commit()
                evaluated_keys.append(key)

        if not evaluated_keys:
            logger.warning("Aucun match n'a pu recevoir de prédiction Ensemble (historique toujours insuffisant) — "
                            "AUCUNE COMPARAISON POSSIBLE avec les données actuelles.")
            return {"status": "insufficient_history", "burn_in_skipped": burn_in_skipped}

        logger.info("=" * 80)
        logger.info(f"COMPARAISON — {len(evaluated_keys)} matchs évalués par l'Ensemble (inverse_log_loss) "
                    f"({burn_in_skipped} matchs de burn-in ignorés, historique insuffisant)")
        logger.info("=" * 80)

        results = {}
        for mt in BACKTEST_MODEL_TYPES:
            observations = []
            for key in evaluated_keys:
                row = rows_by_model[mt][key]
                obs = _market_observation(row, _model_prediction_payload(row), "1X2")
                if obs is not None:
                    observations.append(obs)
            results[mt] = _compute_market_metrics(observations)

        ensemble_rows = session.exec(
            select(ModelPrediction).where(
                ModelPrediction.model_type == "ensemble", ModelPrediction.model_version_id == ensemble_version.id,
            )
        ).all()
        ens_observations = [o for o in (_market_observation(r, _model_prediction_payload(r), "1X2") for r in ensemble_rows) if o is not None]
        results["ensemble"] = _compute_market_metrics(ens_observations)

        logger.info(f"  {'Modèle':<12} {'Sample':>8} {'Accuracy':>10} {'LogLoss':>10} {'Brier':>10}")
        for mt in (*BACKTEST_MODEL_TYPES, "ensemble"):
            m = results[mt]
            logger.info(f"  {mt:<12} {m.sample_size:>8} {m.accuracy:>10.4f} {m.log_loss:>10.4f} {m.brier_score:>10.4f}")

        best_individual = min(BACKTEST_MODEL_TYPES, key=lambda mt: results[mt].log_loss)
        ens_ll, best_ll = results["ensemble"].log_loss, results[best_individual].log_loss
        delta = best_ll - ens_ll
        if abs(delta) < 0.005:
            verdict = f"Ensemble ≈ meilleur modèle individuel ({best_individual}, log_loss quasi identique, delta={delta:+.4f})."
        elif delta > 0:
            verdict = f"Ensemble MEILLEUR que le meilleur modèle individuel ({best_individual}) sur log_loss (delta={delta:+.4f})."
        else:
            verdict = f"Ensemble MOINS BON que le meilleur modèle individuel ({best_individual}) sur log_loss (delta={delta:+.4f})."
        logger.info(f"\n>>> VERDICT (jamais forcé) : {verdict}")
        logger.info("=" * 80)

        strategy_comparison = compare_strategies(session, folds, versions, rows_by_model, min_sample_size) if do_compare_strategies else {}

        return {
            "status": "ok", "results": results, "evaluated": len(evaluated_keys), "burn_in_skipped": burn_in_skipped,
            "ensemble_version_id": ensemble_version.id, "best_individual": best_individual, "delta_log_loss": delta,
            "strategy_comparison": strategy_comparison,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--n-folds", type=int, default=3)
    parser.add_argument("--min-sample-size", type=int, default=MIN_BENCHMARK_SAMPLE_SIZE)
    parser.add_argument("--compare-strategies", action="store_true")
    args = parser.parse_args()
    main(n_folds=args.n_folds, min_sample_size=args.min_sample_size, do_compare_strategies=args.compare_strategies)
    sys.exit(0)  # un verdict "pas de gain" documenté n'est pas un échec de ce script
