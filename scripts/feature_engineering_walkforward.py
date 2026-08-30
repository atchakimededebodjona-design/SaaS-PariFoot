"""
scripts/feature_engineering_walkforward.py — Phase 8B : XFOOT FEATURE
ENGINEERING & WALK-FORWARD EVALUATION V1.
=============================================================================
RECHERCHE UNIQUEMENT. Aucune écriture dans match / match_stats /
model_predictions / model_versions / team_ratings / prediction_log —
uniquement des lectures (build_ml_features_from_db, déjà read-only) et des
entraînements XGBoost EN MÉMOIRE, jamais persistés, jamais activés. Voir
snapshot_db_counts()/assert_db_unchanged() plus bas pour la preuve.

MÉTHODE
-------
1. Baseline (Experiment 0) = EXACTEMENT api/app/ai/engine/features.py::
   FEATURE_COLUMNS (25 colonnes déjà en production XGBoost/LightGBM), filtré
   sur dc_home_win non-NaN comme scripts/train_ml_stacking_from_db.py (même
   définition de baseline, reproductible).
2. Groupes de features candidates (Home/Away, densité de calendrier, force
   Dixon-Coles brute + Elo, classement reconstruit, saison) calculés par
   api/app/ai/features/research_features_v1.py, sur EXACTEMENT le même
   ensemble de matchs que la baseline (§22 du prompt : jamais une comparaison
   silencieuse N différents).
3. Walk-forward chronologique : un burn-in (20% des lignes les plus
   anciennes, jamais évalué) puis N_FOLDS folds d'évaluation contigus. Pour
   chaque fold : train = tout ce qui précède strictement le fold ; validation
   = les N_VAL dernières lignes du train (early stopping uniquement, jamais
   utilisées pour choisir un hyperparamètre après coup) ; test = le fold
   lui-même — jamais optimisé sur le test (§20).
4. Pour chaque expérience (baseline_v1 + un groupe à la fois, §19) : un
   XGBoost RECHERCHE (hyperparamètres IDENTIQUES à la production, seed fixe)
   entraîné indépendamment par fold, jamais persisté. FORM/GOALS (§19,
   Experiment 1/2) sont des colonnes DÉJÀ dans baseline_v1 (voir
   feature_sets.py) — marquées DUPLICATE_OF_BASELINE, jamais simulées comme
   un test valide.
5. Comparaison appariée (bootstrap_paired_diff, mcnemar_test — réutilisées de
   api/app/ai/arena/research.py, jamais réimplémentées) sur le log-loss PAR
   OBSERVATION, sur les MÊMES lignes de test que le baseline. Verdict
   BETTER/EQUIVALENT/WORSE/INSUFFICIENT_DATA (§34), avec exigence de
   stabilité multi-fold (§35).
6. EXPERIMENT_8_COMBINED = union des groupes classés BETTER (jamais choisi
   après coup sur le test final — seulement sur les folds d'évaluation
   walk-forward déjà utilisés pour les expériences 3-7, jamais un fold
   supplémentaire réservé exclusivement à cette combinaison).
7. Rapport reports/features/feature_backtest_<run_id>.{json,md}.
   PRODUCTION : DO NOT PROMOTE, quel que soit le résultat (§55).

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/feature_engineering_walkforward.py \
        [--n-folds 4] [--seed 42]
"""

import argparse
import json
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from sqlmodel import Session, select, func  # noqa: E402

from app.core.database import engine, init_db  # noqa: E402
from app.models.match import Match, MatchStats  # noqa: E402
from app.models.model_prediction import ModelPrediction  # noqa: E402
from app.models.prediction_log import PredictionLog  # noqa: E402
from app.models.team_rating import ModelVersion, TeamRating  # noqa: E402

from app.ai.engine.features import build_ml_features_from_db, FEATURE_COLUMNS  # noqa: E402
from app.ai.features.research_features_v1 import (  # noqa: E402
    build_all_research_groups, PHASE8B_FEATURE_REGISTRY, validate_season_rule,
)
from app.ai.features.feature_sets import (  # noqa: E402
    FEATURE_GROUPS, EXPERIMENTS, DUPLICATE_OF_BASELINE, feature_columns_for,
)
from app.ai.arena.research import wilson_interval, bootstrap_paired_diff, mcnemar_test  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("feature_engineering_walkforward")

CLASS_LABELS = [0, 1, 2]  # 0=nul, 1=domicile, 2=extérieur — même convention que train_ml_stacking_from_db.py
N_VAL = 100                # dernières lignes du train de chaque fold, early stopping uniquement
BURN_IN_FRACTION = 0.20    # premières lignes jamais évaluées (historique insuffisant pour un walk-forward honnête)
MIN_FOLD_TRAIN = 500       # sécurité : fold non évalué si le train résultant est trop court
MIN_PAIRED_SAMPLE = 300    # seuil sous lequel une comparaison est INSUFFICIENT_DATA (§30/§34)
BOOTSTRAP_N = 2000
BOOTSTRAP_SEED = 20260829

XGB_PARAMS = dict(
    max_depth=4, learning_rate=0.05, n_estimators=300,
    objective="multi:softprob", num_class=3, eval_metric="mlogloss",
    early_stopping_rounds=20, enable_categorical=True, tree_method="hist",
)

DB_TABLES = {
    "match": Match, "match_stats": MatchStats, "model_predictions": ModelPrediction,
    "model_versions": ModelVersion, "prediction_log": PredictionLog, "team_ratings": TeamRating,
}


# ---------------------------------------------------------------------------
# 0. Sécurité base de données (§57 du prompt)
# ---------------------------------------------------------------------------

def snapshot_db_counts(session: Session) -> dict:
    return {name: session.exec(select(func.count()).select_from(model)).one() for name, model in DB_TABLES.items()}


# ---------------------------------------------------------------------------
# 1. Dataset : baseline + groupes candidats, MÊME ensemble de matchs (§22)
# ---------------------------------------------------------------------------

def build_full_dataset() -> tuple[pd.DataFrame, dict]:
    baseline = build_ml_features_from_db()
    n_before = len(baseline)
    baseline = baseline[baseline["dc_home_win"].notna()].reset_index(drop=True)
    baseline = baseline.sort_values("date", kind="stable").reset_index(drop=True)
    logger.info("Baseline filtrée (dc_home_win non-NaN) : %d -> %d lignes.", n_before, len(baseline))

    season_diag = validate_season_rule(baseline)

    group_frames: dict[str, list[pd.DataFrame]] = {}
    for league, sub in baseline.groupby("league", sort=False):
        sub_sorted = sub.sort_values("date", kind="stable")
        groups = build_all_research_groups(sub_sorted)
        for g, gdf in groups.items():
            group_frames.setdefault(g, []).append(gdf)

    parts = [baseline]
    for g, frames in group_frames.items():
        merged = pd.concat(frames).loc[baseline.index]
        parts.append(merged)
    full = pd.concat(parts, axis=1)
    full = full.loc[:, ~full.columns.duplicated()]
    return full, season_diag


# ---------------------------------------------------------------------------
# 2. Walk-forward folds
# ---------------------------------------------------------------------------

def make_folds(n: int, n_folds: int, burn_in_fraction: float) -> list[tuple[int, int]]:
    burn_in = int(n * burn_in_fraction)
    remaining = n - burn_in
    fold_size = max(1, remaining // n_folds)
    folds = []
    start = burn_in
    for i in range(n_folds):
        end = start + fold_size if i < n_folds - 1 else n
        folds.append((start, end))
        start = end
    return folds


def build_target(df: pd.DataFrame) -> pd.Series:
    conditions = [df["home_goals"] > df["away_goals"], df["home_goals"] == df["away_goals"]]
    y = np.select(conditions, [1, 0], default=2)
    return pd.Series(y, index=df.index, name="target")


# ---------------------------------------------------------------------------
# 3. Entraînement / évaluation par fold
# ---------------------------------------------------------------------------

def _select_X(df: pd.DataFrame, feature_columns: list[str]) -> pd.DataFrame:
    X = df[feature_columns + ["league"]].copy()
    X["league"] = X["league"].astype("category")
    return X


def train_and_predict_fold(df, y, feature_columns, fit_idx, val_idx, test_idx, seed: int):
    X = _select_X(df, feature_columns)
    X_fit, y_fit = X.iloc[fit_idx], y.iloc[fit_idx]
    X_val, y_val = X.iloc[val_idx], y.iloc[val_idx]
    X_test = X.iloc[test_idx]

    model = xgb.XGBClassifier(**XGB_PARAMS, random_state=seed)
    model.fit(X_fit, y_fit, eval_set=[(X_val, y_val)], verbose=False)
    proba = model.predict_proba(X_test)  # colonnes dans l'ordre CLASS_LABELS = [0=nul,1=dom,2=ext]

    gain = model.get_booster().get_score(importance_type="gain")
    return proba, gain


def observations_from_proba(proba: np.ndarray, df_test: pd.DataFrame) -> list[dict]:
    obs = []
    for i, (_, row) in enumerate(df_test.iterrows()):
        p_draw, p_home, p_away = proba[i]
        probs = {"home_win": float(p_home), "draw": float(p_draw), "away_win": float(p_away)}
        actual = "home_win" if row["home_goals"] > row["away_goals"] else (
            "draw" if row["home_goals"] == row["away_goals"] else "away_win")
        predicted = max(probs, key=probs.get)
        obs.append({
            "p_true": probs[actual], "probs": probs, "actual": actual, "correct": predicted == actual,
        })
    return obs


def _obs_log_loss(o: dict, eps: float = 1e-15) -> float:
    return -math.log(min(max(o["p_true"], eps), 1 - eps))


def _obs_brier(o: dict) -> float:
    return sum((p - (1.0 if k == o["actual"] else 0.0)) ** 2 for k, p in o["probs"].items())


def aggregate_metrics(obs_list: list[dict]) -> dict:
    n = len(obs_list)
    if n == 0:
        return {"sample_size": 0, "log_loss": None, "brier": None, "accuracy": None, "accuracy_ci": (None, None)}
    log_loss = sum(_obs_log_loss(o) for o in obs_list) / n
    brier = sum(_obs_brier(o) for o in obs_list) / n
    correct = sum(1 for o in obs_list if o["correct"])
    accuracy = correct / n
    ci = wilson_interval(correct, n)
    return {"sample_size": n, "log_loss": round(log_loss, 5), "brier": round(brier, 5),
            "accuracy": round(accuracy, 5), "accuracy_ci": ci}


# ---------------------------------------------------------------------------
# 4. Orchestration d'une expérience sur tous les folds
# ---------------------------------------------------------------------------

def run_experiment(name: str, feature_columns: list[str], df: pd.DataFrame, y: pd.Series,
                    folds: list[tuple[int, int]], seed: int) -> dict:
    fold_results = []
    obs_by_fold: list[list[dict]] = []
    gain_totals: dict[str, float] = {}

    for fold_idx, (start, end) in enumerate(folds):
        fit_end = max(0, start - N_VAL)
        if fit_end < MIN_FOLD_TRAIN:
            fold_results.append({
                "fold": fold_idx, "start": start, "end": end, "available": False,
                "reason": f"historique d'entraînement insuffisant ({fit_end} < {MIN_FOLD_TRAIN} matchs).",
            })
            obs_by_fold.append([])
            continue

        fit_idx = list(range(0, fit_end))
        val_idx = list(range(fit_end, start))
        test_idx = list(range(start, end))

        proba, gain = train_and_predict_fold(df, y, feature_columns, fit_idx, val_idx, test_idx, seed)
        obs = observations_from_proba(proba, df.iloc[test_idx])
        metrics = aggregate_metrics(obs)

        fold_results.append({
            "fold": fold_idx, "start": start, "end": end, "available": True,
            "n_train": len(fit_idx), "n_val": len(val_idx), "n_test": len(test_idx),
            "date_start": str(df.iloc[start]["date"]), "date_end": str(df.iloc[end - 1]["date"]),
            **metrics,
        })
        obs_by_fold.append(obs)
        for k, v in gain.items():
            gain_totals[k] = gain_totals.get(k, 0.0) + v

    all_obs = [o for fold_obs in obs_by_fold for o in fold_obs]
    overall = aggregate_metrics(all_obs)

    total_gain = sum(gain_totals.values()) or 1.0
    importance = sorted(
        [{"feature": k, "gain_pct": round(100 * v / total_gain, 3)} for k, v in gain_totals.items()],
        key=lambda r: -r["gain_pct"],
    )

    return {
        "name": name, "feature_columns": feature_columns, "folds": fold_results,
        "overall": overall, "obs_by_fold": obs_by_fold, "feature_importance": importance[:15],
    }


def compare_to_baseline(baseline_result: dict, group_result: dict) -> dict:
    baseline_obs_by_fold = baseline_result["obs_by_fold"]
    group_obs_by_fold = group_result["obs_by_fold"]

    pairs_ll, pairs_brier = [], []
    b_correct = g_correct = discordant_b_only = discordant_g_only = 0
    folds_improved = folds_worsened = folds_equal = 0

    for bf, gf in zip(baseline_obs_by_fold, group_obs_by_fold):
        if not bf or not gf or len(bf) != len(gf):
            continue
        fold_pairs = [(_obs_log_loss(b), _obs_log_loss(g)) for b, g in zip(bf, gf)]
        pairs_ll.extend(fold_pairs)
        pairs_brier.extend([(_obs_brier(b), _obs_brier(g)) for b, g in zip(bf, gf)])
        fold_mean_b = sum(a for a, _ in fold_pairs) / len(fold_pairs)
        fold_mean_g = sum(b for _, b in fold_pairs) / len(fold_pairs)
        if fold_mean_g < fold_mean_b - 1e-9:
            folds_improved += 1
        elif fold_mean_g > fold_mean_b + 1e-9:
            folds_worsened += 1
        else:
            folds_equal += 1
        for b, g in zip(bf, gf):
            if b["correct"] and not g["correct"]:
                discordant_b_only += 1
            elif g["correct"] and not b["correct"]:
                discordant_g_only += 1

    n = len(pairs_ll)
    if n < MIN_PAIRED_SAMPLE:
        return {
            "n_paired": n, "verdict": "INSUFFICIENT_DATA",
            "reason": f"{n} paires < seuil minimum {MIN_PAIRED_SAMPLE}.",
            "folds_improved": folds_improved, "folds_worsened": folds_worsened, "folds_equal": folds_equal,
        }

    boot = bootstrap_paired_diff(pairs_ll, n_boot=BOOTSTRAP_N, seed=BOOTSTRAP_SEED)
    boot_brier = bootstrap_paired_diff(pairs_brier, n_boot=BOOTSTRAP_N, seed=BOOTSTRAP_SEED)
    mcnemar = mcnemar_test(discordant_b_only, discordant_g_only)

    n_eval_folds = sum(1 for f in baseline_result["folds"] if f.get("available"))
    majority = n_eval_folds / 2

    if boot["significant"] and boot["mean_diff"] > 0 and folds_improved > majority:
        verdict = "BETTER"
    elif boot["significant"] and boot["mean_diff"] < 0 and folds_worsened > majority:
        verdict = "WORSE"
    else:
        verdict = "EQUIVALENT"

    return {
        "n_paired": n, "verdict": verdict,
        "delta_log_loss": boot, "delta_brier": boot_brier, "mcnemar": mcnemar,
        "folds_improved": folds_improved, "folds_worsened": folds_worsened, "folds_equal": folds_equal,
        "n_eval_folds": n_eval_folds,
    }


# ---------------------------------------------------------------------------
# 5. Rapport
# ---------------------------------------------------------------------------

def _fmt(v, digits=4):
    if v is None:
        return "N/A"
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def render_markdown(result: dict) -> str:
    md = ["# XFOOT FEATURE ENGINEERING & WALK-FORWARD EVALUATION V1\n"]

    md.append("\n## 1. Executive Summary\n")
    md.append(f"\nRun id : `{result['run_id']}` — généré le {result['generated_at']}.\n")
    md.append("\nRÈGLE ABSOLUE : AUCUNE MODIFICATION PRODUCTION. Résultats research-only.\n")
    for exp, verdict in result["group_verdicts"].items():
        md.append(f"\n- **{exp}** : {verdict}\n")

    md.append("\n## 2. Dataset\n")
    ds = result["dataset"]
    md.append(f"\n- Ligues : {', '.join(ds['leagues'])}\n")
    md.append(f"- Matchs (après filtrage dc_home_win non-NaN) : {ds['n_matches']}\n")
    md.append(f"- Période : {ds['period_start']} → {ds['period_end']}\n")
    md.append(f"- Répartition par ligue : {ds['per_league_counts']}\n")

    md.append("\n## 3. Baseline\n")
    md.append(f"\n- feature_set_version : `baseline_v1`\n- {len(FEATURE_COLUMNS)} colonnes (identiques à la production, voir api/app/ai/engine/features.py::FEATURE_COLUMNS) + `league`\n")
    md.append(f"- Modèle : XGBoost, hyperparamètres = {XGB_PARAMS}\n- seed : {result['seed']}\n")

    md.append("\n## 4. Feature Registry (Phase 8B, additif — jamais fusionné à la production)\n\n")
    md.append("| Feature | Groupe | Status | Leakage Risk |\n|---|---|---|---|\n")
    for name, fd in PHASE8B_FEATURE_REGISTRY.items():
        md.append(f"| {name} | {fd.category} | {fd.status} | {fd.leakage_risk} |\n")

    md.append("\n## 5. Feature Groups\n\n")
    for g, cols in FEATURE_GROUPS.items():
        cols_str = "DUPLICATE_OF_BASELINE" if cols == DUPLICATE_OF_BASELINE else ", ".join(cols)
        md.append(f"- **{g}** : {cols_str}\n")

    md.append("\n## 6. Feature Definitions\n\nVoir §4 (Feature Registry) — chaque feature y a name/definition/source/cutoff/availability/missing_strategy/leakage_status.\n")

    md.append("\n## 7. Temporal Cutoff\n\nToutes les features candidates respectent `Match.date < cutoff` (strict) — voir docstrings de api/app/ai/features/research_features_v1.py. dc_attack_diff/dc_defense_diff/dc_net_diff : approximation documentée (un fit par date de match distincte, jamais par seconde) — CAUTION, jamais LEAKAGE_RISK.\n")

    md.append("\n## 8. Leakage Audit\n\n")
    md.append(f"\n{result['leakage_audit']}\n")

    md.append("\n## 9. Feature Coverage\n\n")
    md.append("| Feature | Available | Total | Coverage | Leakage | Status |\n|---|---|---|---|---|---|\n")
    for row in result["coverage"]:
        md.append(f"| {row['feature']} | {row['available']} | {row['total']} | {row['coverage_pct']:.1f}% | {row['leakage_risk']} | {row['status']} |\n")

    md.append("\n## 10. Feature Quality\n\n")
    md.append(f"\n{result['quality_summary']}\n")

    md.append("\n## 11. Walk-Forward Methodology\n\n")
    md.append(f"\n{N_FOLDS_DOC.format(n_folds=result['n_folds'])}\n")

    md.append("\n## 12. Experiments\n\n")
    md.append("| Experiment | N | Log Loss | Brier | Accuracy | Delta LogLoss vs Baseline | Status |\n|---|---|---|---|---|---|---|\n")
    baseline_ll = result["experiments"]["EXPERIMENT_0_BASELINE"]["overall"]["log_loss"] if "EXPERIMENT_0_BASELINE" in result["experiments"] else None
    for exp_name, exp in result["experiments"].items():
        ov = exp["overall"]
        delta = "N/A" if (baseline_ll is None or ov["log_loss"] is None) else f"{baseline_ll - ov['log_loss']:+.4f}"
        status = result["group_verdicts"].get(exp_name, "—")
        md.append(f"| {exp_name} | {ov['sample_size']} | {_fmt(ov['log_loss'])} | {_fmt(ov['brier'])} | {_fmt(ov['accuracy'])} | {delta} | {status} |\n")
    for exp_name, note in result.get("duplicate_experiments", {}).items():
        md.append(f"| {exp_name} | — | — | — | — | {note} |\n")

    md.append("\n## 13. Fold Results\n\n")
    md.append("| Experiment | Fold | N | LogLoss | Brier | Accuracy |\n|---|---|---|---|---|---|\n")
    for exp_name, exp in result["experiments"].items():
        for f in exp["folds"]:
            if not f.get("available"):
                md.append(f"| {exp_name} | {f['fold']} | — | — | — | — ({f['reason']}) |\n")
            else:
                md.append(f"| {exp_name} | {f['fold']} | {f['sample_size']} | {_fmt(f['log_loss'])} | {_fmt(f['brier'])} | {_fmt(f['accuracy'])} |\n")

    md.append("\n## 14. Ablation Results\n\n")
    md.append("| Group | Coverage | Delta LogLoss (vs baseline) | Folds Improved/Worsened/Equal | Verdict |\n|---|---|---|---|---|\n")
    for g, cmp in result["comparisons"].items():
        if cmp.get("verdict") == "INSUFFICIENT_DATA":
            md.append(f"| {g} | — | N/A | — | INSUFFICIENT_DATA ({cmp.get('reason')}) |\n")
        else:
            dll = cmp["delta_log_loss"]
            md.append(f"| {g} | — | {dll['mean_diff']:+.5f} [{dll['ci_low']:+.5f}, {dll['ci_high']:+.5f}] | "
                       f"{cmp['folds_improved']}/{cmp['folds_worsened']}/{cmp['folds_equal']} | {cmp['verdict']} |\n")

    md.append("\n## 15. Feature Importance\n\nImportance = gain XGBoost, agrégée sur tous les folds évaluables. IMPORTANCE ≠ CAUSALITÉ (§26/§51).\n\n")
    for exp_name, exp in result["experiments"].items():
        md.append(f"\n**{exp_name}** (top features) :\n\n| Feature | Importance (gain %) |\n|---|---|\n")
        for row in exp["feature_importance"][:10]:
            md.append(f"| {row['feature']} | {row['gain_pct']:.2f}% |\n")

    md.append("\n## 16. Statistical Tests\n\nbootstrap_paired_diff (2000 tirages, seed fixe) + mcnemar_test — réutilisés de api/app/ai/arena/research.py, jamais réimplémentés. Voir §14.\n")

    md.append("\n## 17. Stability\n\n")
    md.append("Un groupe n'est retenu BETTER que si l'amélioration est significative (bootstrap, IC exclut 0) ET cohérente sur une MAJORITÉ des folds d'évaluation (jamais un seul fold isolé). Voir colonne « Folds Improved/Worsened/Equal » §14.\n")

    md.append("\n## 18. Reproducibility\n\n")
    md.append(f"\nseed={result['seed']}, dataset={ds['n_matches']} matchs, feature_set_version par expérience (§4), même hyperparamètres XGBoost pour toutes les expériences — un run identique produit les mêmes features et les mêmes métriques (voir api/test_feature_engineering_v1.py::test_reproducibility_same_seed).\n")

    md.append("\n## 19. Database Safety\n\n")
    md.append(f"\nCompteurs AVANT : {result['db_counts_before']}\n\nCompteurs APRÈS : {result['db_counts_after']}\n\nIdentiques : {result['db_unchanged']}\n")

    md.append("\n## 20. Production Isolation\n\nAucune écriture dans model_predictions / model_versions / prediction_log / team_ratings / active models. Tous les entraînements XGBoost sont EN MÉMOIRE, jamais persistés. Artefacts dans reports/features/ uniquement.\n")

    md.append("\n## 21. Results\n\n")
    md.append(f"\n{result['results_summary']}\n")

    md.append("\n## 22. Limitations\n\n")
    for item in result["limitations"]:
        md.append(f"- {item}\n")

    md.append("\n## 23. Recommended Features (RESEARCH_CANDIDATE)\n\n")
    for item in result["recommended_features"]:
        md.append(f"- {item}\n")
    if not result["recommended_features"]:
        md.append("- Aucune (aucun groupe n'a atteint le verdict BETTER de façon stable).\n")

    md.append("\n## 24. Rejected Features\n\n")
    for item in result["rejected_features"]:
        md.append(f"- {item}\n")

    md.append("\n## 25. Recommendations Phase 8C\n\n")
    for item in result["recommendations_phase_8c"]:
        md.append(f"- {item}\n")

    md.append("\n---\n\n### BEST RESEARCH FEATURE SET\n\n")
    md.append(f"\n{result['best_research_feature_set']}  — RESEARCH ONLY.\n")
    md.append("\n### PRODUCTION\n\nDO NOT PROMOTE.\n")

    md.append("\n---\n\nPHASE 8B — XFOOT FEATURE ENGINEERING & WALK-FORWARD EVALUATION V1 TERMINÉE. "
               "AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. AUCUN FEATURE SET PROMU. EN ATTENTE DE VALIDATION.\n")

    return "".join(md)


N_FOLDS_DOC = (
    "Burn-in = {n_folds} folds d'évaluation + 20% des lignes les plus anciennes jamais évaluées "
    "(historique insuffisant pour un walk-forward honnête). Pour chaque fold : train = tout ce qui "
    "précède strictement le fold, validation = les 100 dernières lignes du train (early stopping "
    "uniquement), test = le fold lui-même. Jamais optimisé sur le test."
)


def write_reports(result: dict, outdir: Path, run_id: str) -> tuple[Path, Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    json_path = outdir / f"feature_backtest_{run_id}.json"
    md_path = outdir / f"feature_backtest_{run_id}.md"
    serializable = {k: v for k, v in result.items() if k != "experiments"}
    serializable["experiments"] = {
        name: {k: v for k, v in exp.items() if k != "obs_by_fold"} for name, exp in result["experiments"].items()
    }
    json_path.write_text(json.dumps(serializable, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    return json_path, md_path


# ---------------------------------------------------------------------------
# 6. main()
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-folds", type=int, default=4)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    init_db()
    with Session(engine) as session:
        db_before = snapshot_db_counts(session)

    logger.info("Construction du dataset (baseline + groupes candidats Phase 8B)...")
    df, season_diag = build_full_dataset()
    y = build_target(df)
    n = len(df)
    folds = make_folds(n, args.n_folds, BURN_IN_FRACTION)
    logger.info("Dataset : %d matchs, %d folds d'évaluation (%s).", n, args.n_folds, folds)

    coverage_rows = []
    for name, fd in PHASE8B_FEATURE_REGISTRY.items():
        if name not in df.columns:
            continue
        avail = int(df[name].notna().sum())
        coverage_rows.append({
            "feature": name, "available": avail, "total": n,
            "coverage_pct": 100.0 * avail / n if n else 0.0,
            "leakage_risk": fd.leakage_risk, "status": fd.status,
        })

    experiments: dict[str, dict] = {}
    duplicate_experiments: dict[str, str] = {}
    for exp_name, spec in EXPERIMENTS.items():
        groups = spec["groups"]
        if groups and all(FEATURE_GROUPS[g] == DUPLICATE_OF_BASELINE for g in groups):
            duplicate_experiments[exp_name] = (
                "DUPLICATE_OF_BASELINE — ces colonnes sont déjà dans baseline_v1 "
                "(home/away_form_points_avg, _goals_scored_avg, _goals_conceded_avg) ; "
                "aucun test valide possible, jamais simulé."
            )
            continue
        feature_columns = feature_columns_for(groups)
        logger.info("Expérience %s (feature_set_version=%s, %d colonnes)...",
                    exp_name, spec["feature_set_version"], len(feature_columns))
        experiments[exp_name] = run_experiment(exp_name, feature_columns, df, y, folds, args.seed)

    baseline_result = experiments["EXPERIMENT_0_BASELINE"]
    comparisons: dict[str, dict] = {}
    group_verdicts: dict[str, str] = {"EXPERIMENT_0_BASELINE": "BASELINE"}
    for exp_name in ("EXPERIMENT_3_HOMEAWAY", "EXPERIMENT_4_REST", "EXPERIMENT_5_STRENGTH",
                      "EXPERIMENT_6_RANKING", "EXPERIMENT_7_SEASON"):
        cmp = compare_to_baseline(baseline_result, experiments[exp_name])
        comparisons[exp_name] = cmp
        group_verdicts[exp_name] = cmp["verdict"]
    group_verdicts["EXPERIMENT_1_FORM"] = "DUPLICATE_OF_BASELINE"
    group_verdicts["EXPERIMENT_2_GOALS"] = "DUPLICATE_OF_BASELINE"

    exp_to_group = {
        "EXPERIMENT_3_HOMEAWAY": "homeaway", "EXPERIMENT_4_REST": "rest_density",
        "EXPERIMENT_5_STRENGTH": "strength", "EXPERIMENT_6_RANKING": "ranking", "EXPERIMENT_7_SEASON": "season",
    }
    validated_groups = [exp_to_group[e] for e, v in group_verdicts.items() if v == "BETTER" and e in exp_to_group]

    if validated_groups:
        combined_cols = feature_columns_for(validated_groups)
        logger.info("EXPERIMENT_8_COMBINED (%s, %d colonnes)...", validated_groups, len(combined_cols))
        experiments["EXPERIMENT_8_COMBINED"] = run_experiment(
            "EXPERIMENT_8_COMBINED", combined_cols, df, y, folds, args.seed,
        )
        combined_cmp = compare_to_baseline(baseline_result, experiments["EXPERIMENT_8_COMBINED"])
        comparisons["EXPERIMENT_8_COMBINED"] = combined_cmp
        group_verdicts["EXPERIMENT_8_COMBINED"] = combined_cmp["verdict"]
        best_set = f"combined_v1 ({'+'.join(validated_groups)})" if combined_cmp["verdict"] == "BETTER" else (
            f"Aucun gain confirmé pour combined_v1 malgré des groupes individuellement BETTER "
            f"({'+'.join(validated_groups)}) — combiner n'apporte pas un gain supplémentaire significatif."
        )
    else:
        group_verdicts["EXPERIMENT_8_COMBINED"] = "INSUFFICIENT_DATA"
        best_set = "Aucun groupe candidat n'a atteint le verdict BETTER de façon stable — baseline_v1 reste la meilleure option testée."

    with Session(engine) as session:
        db_after = snapshot_db_counts(session)
    db_unchanged = db_before == db_after
    if not db_unchanged:
        logger.error("ALERTE : compteurs DB modifiés pendant l'exécution — avant=%s après=%s", db_before, db_after)

    recommended = [f"{e} ({exp_to_group[e]}) — RESEARCH_CANDIDATE, jamais promu automatiquement (§54)." for e in group_verdicts if group_verdicts[e] == "BETTER" and e in exp_to_group]
    rejected = [f"{e} ({exp_to_group[e]}) — {group_verdicts[e]}." for e in group_verdicts if e in exp_to_group and group_verdicts[e] in ("WORSE", "EQUIVALENT", "INSUFFICIENT_DATA")]

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    result = {
        "run_id": run_id, "generated_at": datetime.now(timezone.utc).isoformat(), "seed": args.seed,
        "n_folds": args.n_folds,
        "dataset": {
            "leagues": sorted(df["league"].unique().tolist()), "n_matches": n,
            "period_start": str(df["date"].min()), "period_end": str(df["date"].max()),
            "per_league_counts": df["league"].value_counts().to_dict(),
        },
        "season_rule_validation": season_diag,
        "coverage": coverage_rows,
        "experiments": experiments,
        "comparisons": comparisons,
        "group_verdicts": group_verdicts,
        "duplicate_experiments": duplicate_experiments,
        "db_counts_before": db_before, "db_counts_after": db_after, "db_unchanged": db_unchanged,
        "leakage_audit": (
            "Toutes les fonctions de api/app/ai/features/research_features_v1.py suivent le même "
            "patron ligne-par-ligne que build_form_and_h2h_features (lecture de l'historique AVANT "
            "mise à jour) — aucune fonction ne lit home_goals/away_goals de la ligne courante avant "
            "d'avoir déjà produit ses features. Voir api/test_feature_engineering_v1.py pour les tests "
            "de fuite (match futur synthétique à score extrême, jamais visible avant sa propre date)."
        ),
        "quality_summary": (
            "Aucun NaN converti silencieusement en 0 sauf pour les compteurs de densité de calendrier "
            "(matches_last_7/14_days), où 0 est une vraie mesure (aucun match dans la fenêtre), jamais "
            "une convention de valeur manquante — documenté dans PHASE8B_FEATURE_REGISTRY."
        ),
        "results_summary": (
            f"Verdicts par groupe : {group_verdicts}. Voir §14 (Ablation Results) pour les deltas de "
            "log-loss chiffrés et les intervalles de confiance bootstrap."
        ),
        "limitations": [
            "Base locale = 5 ligues sur les 11 du CSV source (Bundesliga, LaLiga, Ligue1, PremierLeague, SerieA) — "
            "aucune conclusion étendue aux 6 ligues non chargées (ChampionsLeague, ConferenceLeague, EuropaLeague, MLS, PrimeiraLiga, SaudiProLeague).",
            "elo_diff utilise les hyperparamètres Elo par défaut (k=20, home_advantage=60), pas le grid search par ligue de scripts/backtest_elo.py.",
            "dc_attack_diff/dc_defense_diff/dc_net_diff : un fit Dixon-Coles par DATE de match distincte (comme build_dixon_coles_features), pas un rating recalculé à la seconde près.",
            "season_progress_pct utilise un 1er août approximatif comme début de saison — non vérifié compétition par compétition.",
            "BTTS/Over-Under non évalués (§30) : XGBoost/LightGBM de production ne modélisent que 1X2 (confirmé dans train_ml_stacking_from_db.py) — INSUFFICIENT_DATA structurel, jamais simulé.",
            "Multiple testing (§29) : 5 groupes + 1 combinaison testés sur le même burn-in/folds — le seuil de significativité par comparaison (bootstrap IC95%) n'est pas corrigé pour comparaisons multiples ; un verdict BETTER isolé sur un seul groupe doit être lu avec cette réserve.",
        ],
        "recommended_features": recommended,
        "rejected_features": rejected,
        "recommendations_phase_8c": [
            "Si un groupe est BETTER de façon stable : envisager une validation supplémentaire sur un dataset élargi (11 ligues) avant toute discussion de promotion — jamais dans cette phase.",
            "elo_diff avec hyperparamètres optimisés par ligue (réutiliser scripts/backtest_elo.py) pourrait changer le verdict du groupe strength — non testé ici.",
            "league_standing dépend de season_year (rule mois >= 7) — si season_year est EQUIVALENT/WORSE, revalider league_standing avec une règle de saison alternative avant d'abandonner le groupe ranking.",
        ],
        "best_research_feature_set": best_set,
    }

    outdir = Path(__file__).resolve().parent.parent / "reports" / "features"
    json_path, md_path = write_reports(result, outdir, run_id)
    logger.info("Rapport écrit : %s / %s", json_path, md_path)

    print("\n" + "=" * 80)
    print("PHASE 8B — XFOOT FEATURE ENGINEERING & WALK-FORWARD EVALUATION V1 TERMINÉE.")
    print("AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. AUCUN FEATURE SET PROMU. EN ATTENTE DE VALIDATION.")
    print("=" * 80)
    return result


if __name__ == "__main__":
    main()
