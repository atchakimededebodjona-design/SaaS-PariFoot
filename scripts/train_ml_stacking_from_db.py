"""
scripts/train_ml_stacking_from_db.py — Phase 4 : XGBoost/LightGBM avec
features réellement nouvelles, sourcées en base (match/match_stats).
=============================================================================

Le stacking XGBoost par-dessus Dixon-Coles a déjà été testé deux fois dans ce
projet, même méthodologie (split temporel, bootstrap apparié 2000 tirages) :

  - 712 lignes exploitables (Ligue1 seule, train_xgboost_stacking.py) :
    log-loss XGBoost=1.0713 vs Dixon-Coles=1.0219, DC meilleur dans 97.4%
    des tirages bootstrap.
  - ~10165 lignes (5 ligues, train_xgboost_stacking_multileague.py) : même
    verdict, DC meilleur dans ~98.1% des tirages, écart significatif dans
    les 5 ligues sans exception.

Verdict les deux fois : PAS DE GAIN, Dixon-Coles reste seul en production.
Mêmes 17 features (forme 5 matchs, repos, head-to-head, probabilités
Dixon-Coles en stacking) — CE SCRIPT NE REJOUE PAS CE TEST À L'IDENTIQUE.

Ce qui change ici :
  1. Source : match/match_stats (Phase 2) via api.app.ai.engine.features,
     jamais le CSV.
  2. Features : les 17 existantes + 8 réellement nouvelles (jamais testées
     avant) — voir api/app/ai/engine/features.py pour le détail et la
     justification (moyennes glissantes de tirs/tirs cadrés/corners sur une
     fenêtre plus longue que la forme, séries de résultats en cours). La
     piste « repos multi-compétitions » de l'audit n'est PAS réalisable
     avec les données actuelles (pas de colonne de compétition en base,
     uniquement `league`) — non testée, rapportée comme telle plutôt que
     forcée.
  3. Modèle : XGBoost (hyperparamètres IDENTIQUES aux tentatives passées,
     pour isoler l'effet des nouvelles features) ET LightGBM (config
     équivalente), tous deux comparés à Dixon-Coles sur le même test set.
  4. Persistance : ModelVersion créée pour chaque modèle entraîné
     (model_type="xgboost"/"lightgbm"), is_active=False INCONDITIONNELLEMENT
     dans ce ticket (l'activation reste une décision humaine ultérieure,
     même avec un gain démontré). Aucune TeamRating créée — n'a pas de sens
     pour un modèle à features (pas de rating par équipe), contrairement à
     Dixon-Coles/Elo. Le modèle entraîné lui-même (arbres/poids) n'est PAS
     sérialisé/stocké : aucun mécanisme d'artefact générique n'existe
     aujourd'hui pour ce type de modèle (model_artifact est réservé, 1 ligne
     par ligue, au JSON Dixon-Coles de production) — à traiter dans un
     ticket séparé si un vrai gain est un jour démontré.

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/train_ml_stacking_from_db.py
"""

import sys
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
import lightgbm as lgb
from sklearn.metrics import log_loss, accuracy_score

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from sqlmodel import Session  # noqa: E402
from app.core.database import engine, init_db  # noqa: E402
from app.models.team_rating import ModelVersion, next_version_name  # noqa: E402
from app.ai.engine.features import (  # noqa: E402
    build_ml_features_from_db, FEATURE_COLUMNS, CATEGORICAL_COLUMNS,
    EXISTING_FEATURE_COLUMNS, NEW_FEATURE_COLUMNS,
    PREVIOUSLY_UNUSED_MATCH_STATS_COLUMNS,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("train_ml_stacking_from_db")

N_TEST = 300
N_VAL = 100  # pris dans le train, pour l'early stopping — jamais dans le test

# Ordre de classe fixé : 0=nul, 1=domicile, 2=extérieur — mêmes conventions
# que train_xgboost_stacking_multileague.py. Une erreur d'alignement ici
# fausserait silencieusement toute comparaison de log-loss.
CLASS_LABELS = [0, 1, 2]
DC_PROBA_COLUMNS_IN_LABEL_ORDER = ["dc_draw", "dc_home_win", "dc_away_win"]

# Seuil de verdict "gain crédible" — identique aux deux tentatives passées,
# pour ne pas assouplir la barre au moment où on ajoute des features.
VERDICT_FRAC_THRESHOLD = 0.90


def report_unused_columns():
    print("=" * 80)
    print("0. RECENSEMENT — colonnes match_stats jamais exploitées avant ce ticket")
    print("=" * 80)
    print("  Colonnes présentes dans match_stats depuis la Phase 2, toujours explicitement")
    print("  exclues de FEATURE_COLUMNS dans les deux tentatives XGBoost passées (valeur")
    print("  BRUTE du match courant = fuite) :")
    for c in PREVIOUSLY_UNUSED_MATCH_STATS_COLUMNS:
        print(f"    - {c}")
    print("\n  Exploitées ici pour la première fois, mais UNIQUEMENT via des moyennes")
    print("  glissantes sur les matchs PASSÉS de chaque équipe (jamais la valeur du match")
    print("  courant) — voir api/app/ai/engine/features.py::build_shot_stats_features :")
    for c in NEW_FEATURE_COLUMNS[:6]:
        print(f"    -> {c}")
    print("\n  Features de séquence (nouvelles, dérivées de home_goals/away_goals — déjà")
    print("  utilisées pour la moyenne de forme, mais jamais pour l'ORDRE des résultats) :")
    for c in NEW_FEATURE_COLUMNS[6:]:
        print(f"    -> {c}")
    print("\n  Piste NON RETENUE (rapportée, pas forcée) : repos réel multi-compétitions —")
    print("  `match` n'a pas de colonne de compétition/coupe, uniquement `league`")
    print("  (championnat). Seul le repos intra-championnat est calculable, déjà capturé")
    print("  par home/away_days_since_last_match (feature existante, pas nouvelle ici).")
    print()


def load_and_filter():
    df = build_ml_features_from_db()
    n_before = len(df)
    df = df[df["dc_home_win"].notna()].reset_index(drop=True)
    n_after = len(df)

    print("=" * 80)
    print("1. FILTRAGE — dc_home_win non-NaN (5 ligues confondues)")
    print("=" * 80)
    print(f"  Lignes avant filtrage : {n_before}")
    print(f"  Lignes après filtrage : {n_after}  ({n_before - n_after} écartées, "
          f"période de rodage Dixon-Coles / équipes jamais vues, PAR LIGUE)")
    print("  Répartition par ligue après filtrage :")
    print(df["league"].value_counts().to_string())
    print()
    return df


def build_target(df: pd.DataFrame) -> pd.Series:
    conditions = [df["home_goals"] > df["away_goals"], df["home_goals"] == df["away_goals"]]
    choices = [1, 0]
    y = np.select(conditions, choices, default=2)
    print("=" * 80)
    print("2. CIBLE — distribution 1X2 (0=nul, 1=domicile, 2=extérieur)")
    print("=" * 80)
    print(pd.Series(y).value_counts().sort_index().to_string())
    print()
    return pd.Series(y, index=df.index, name="target")


def select_features(df: pd.DataFrame) -> pd.DataFrame:
    print("=" * 80)
    print(f"3. FEATURES D'ENTRÉE — {len(EXISTING_FEATURE_COLUMNS)} existantes + "
          f"{len(NEW_FEATURE_COLUMNS)} nouvelles + league (catégorielle native)")
    print("=" * 80)
    missing = [c for c in FEATURE_COLUMNS if c not in df.columns]
    assert not missing, f"Colonnes de features manquantes : {missing}"

    all_columns = FEATURE_COLUMNS + CATEGORICAL_COLUMNS
    X = df[all_columns].copy()
    X["league"] = X["league"].astype("category")

    print(f"  Existantes ({len(EXISTING_FEATURE_COLUMNS)}) : {EXISTING_FEATURE_COLUMNS}")
    print(f"  Nouvelles ({len(NEW_FEATURE_COLUMNS)})   : {NEW_FEATURE_COLUMNS}")

    nan_counts = X.isna().sum()
    nan_counts = nan_counts[nan_counts > 0]
    if len(nan_counts):
        print("\n  NaN résiduels (laissés tels quels — XGBoost/LightGBM gèrent nativement) :")
        print(nan_counts.to_string())
    print()
    return X


def temporal_split(df, X, y):
    print("=" * 80)
    print("4. SPLIT TEMPOREL — 300 derniers matchs, TOUTES LIGUES CONFONDUES")
    print("=" * 80)
    n = len(df)
    test_start = n - N_TEST
    val_start = test_start - N_VAL

    X_test, y_test = X.iloc[test_start:], y.iloc[test_start:]
    X_val, y_val = X.iloc[val_start:test_start], y.iloc[val_start:test_start]
    X_fit, y_fit = X.iloc[:val_start], y.iloc[:val_start]

    print(f"  Train (fit)       : {len(X_fit)} matchs   [{df['date'].iloc[0]} -> {df['date'].iloc[val_start - 1]}]")
    print(f"  Validation (ES)   : {len(X_val)} matchs   [{df['date'].iloc[val_start]} -> {df['date'].iloc[test_start - 1]}]")
    print(f"  Test              : {len(X_test)} matchs   [{df['date'].iloc[test_start]} -> {df['date'].iloc[-1]}]")

    df_test = df.iloc[test_start:]
    print(f"\n  Composition en ligues du test set ({N_TEST} matchs) :")
    print(df_test["league"].value_counts().to_string())
    print()

    return (X_fit, y_fit), (X_val, y_val), (X_test, y_test), df_test


def train_xgboost(X_fit, y_fit, X_val, y_val):
    print("=" * 80)
    print("5a. ENTRAÎNEMENT XGBOOST (hyperparamètres identiques aux tentatives passées)")
    print("=" * 80)
    model = xgb.XGBClassifier(
        max_depth=4, learning_rate=0.05, n_estimators=300,
        objective="multi:softprob", num_class=3, eval_metric="mlogloss",
        early_stopping_rounds=20, random_state=42,
        enable_categorical=True, tree_method="hist",
    )
    model.fit(X_fit, y_fit, eval_set=[(X_val, y_val)], verbose=False)
    best_iter = getattr(model, "best_iteration", None)
    print(f"  best_iteration (early stopping) : {best_iter}\n")
    return model


def train_lightgbm(X_fit, y_fit, X_val, y_val):
    print("=" * 80)
    print("5b. ENTRAÎNEMENT LIGHTGBM (configuration équivalente à XGBoost ci-dessus)")
    print("=" * 80)
    X_fit_lgb, X_val_lgb = X_fit.copy(), X_val.copy()
    X_fit_lgb["league"] = X_fit_lgb["league"].cat.codes.astype("category")
    X_val_lgb["league"] = pd.Categorical(X_val_lgb["league"], categories=X_fit["league"].cat.categories).codes
    X_val_lgb["league"] = X_val_lgb["league"].astype("category")

    model = lgb.LGBMClassifier(
        max_depth=4, num_leaves=15, learning_rate=0.05, n_estimators=300,
        objective="multiclass", num_class=3, random_state=42, verbosity=-1,
    )
    model.fit(
        X_fit_lgb, y_fit, eval_set=[(X_val_lgb, y_val)],
        categorical_feature=["league"],
        callbacks=[lgb.early_stopping(stopping_rounds=20, verbose=False)],
    )
    print(f"  best_iteration (early stopping) : {model.best_iteration_}\n")
    return model, X_fit["league"].cat.categories


def _reorder_proba(proba_raw, class_order):
    reorder = [class_order.index(c) for c in CLASS_LABELS]
    return proba_raw[:, reorder]


def evaluate_model(name, proba_model, df_test, y_test, dc_logloss, dc_acc, proba_dc):
    print("=" * 80)
    print(f"6. ÉVALUATION — {name} vs Dixon-Coles seul")
    print("=" * 80)

    pred = np.array(CLASS_LABELS)[np.argmax(proba_model, axis=1)]
    model_logloss = log_loss(y_test, proba_model, labels=CLASS_LABELS)
    model_acc = accuracy_score(y_test, pred)

    print(f"  {'Modèle':<20} {'log-loss':>10} {'accuracy':>10}")
    print(f"  {'Dixon-Coles seul':<20} {dc_logloss:>10.4f} {dc_acc:>10.4f}")
    print(f"  {name:<20} {model_logloss:>10.4f} {model_acc:>10.4f}")

    delta = dc_logloss - model_logloss
    print(f"\n  Delta log-loss (DC - {name}) : {delta:+.4f}  "
          f"({name + ' meilleur' if delta > 0 else 'Dixon-Coles meilleur ou égal'})")

    rng = np.random.default_rng(42)
    n = len(y_test)
    y_arr = y_test.to_numpy()
    deltas = []
    for _ in range(2000):
        idx = rng.integers(0, n, size=n)
        dc_ll = log_loss(y_arr[idx], proba_dc[idx], labels=CLASS_LABELS)
        model_ll = log_loss(y_arr[idx], proba_model[idx], labels=CLASS_LABELS)
        deltas.append(dc_ll - model_ll)
    deltas = np.array(deltas)
    frac_better = (deltas > 0).mean()
    print(f"  Bootstrap apparié (2000 tirages, n={n}) : {name} meilleur dans {frac_better:.1%} des tirages, "
          f"IC95% du delta = [{np.quantile(deltas, 0.025):+.4f}, {np.quantile(deltas, 0.975):+.4f}]")

    verdict_gain = bool(frac_better > VERDICT_FRAC_THRESHOLD and delta > 0)
    honest_verdict = (
        f"VERDICT : {name} améliore la log-loss de façon crédible (gain positif et robuste au bootstrap)."
        if verdict_gain else
        f"VERDICT : PAS DE GAIN CLAIR pour {name} — l'écart de log-loss est faible et/ou instable au bootstrap. "
        f"Ne pas présenter comme une amélioration confirmée, même avec les nouvelles features."
    )
    print(f">>> {honest_verdict}\n")

    return {
        "model_logloss": model_logloss, "model_acc": model_acc,
        "dc_logloss": dc_logloss, "dc_acc": dc_acc,
        "frac_better": frac_better, "delta": delta, "gain": verdict_gain,
        "delta_ci95": (float(np.quantile(deltas, 0.025)), float(np.quantile(deltas, 0.975))),
    }


def feature_importance(name, gain_dict, feature_names):
    print("=" * 80)
    print(f"7. IMPORTANCE DES FEATURES ({name}, gain)")
    print("=" * 80)
    for c in feature_names:
        gain_dict.setdefault(c, 0.0)
    total = sum(gain_dict.values()) or 1.0
    table = pd.DataFrame(
        [{"feature": k, "gain": v, "gain_pct": 100 * v / total} for k, v in gain_dict.items()]
    ).sort_values("gain", ascending=False).reset_index(drop=True)
    print(table.to_string(index=False))

    new_pct = table[table["feature"].isin(NEW_FEATURE_COLUMNS)]["gain_pct"].sum()
    print(f"\n  Part de gain totale des 8 NOUVELLES features : {new_pct:.1f}%")
    if new_pct < 2:
        print("  -> Quasi nulle : les nouvelles features n'apportent pas d'information exploitée par ce modèle.")
    print()
    return table, new_pct


def persist_model_version(model_type: str, metrics: dict, top_features: list[str]) -> int:
    """
    Crée une NOUVELLE ModelVersion à chaque run (voir next_version_name),
    is_active=False INCONDITIONNELLEMENT dans ce ticket — même avec un gain
    démontré, l'activation reste une décision humaine ultérieure (contrainte
    explicite du ticket, différente de Dixon-Coles/Elo qui s'auto-activent
    sur verdict positif). Aucune TeamRating : n'a pas de sens pour un modèle
    à features (pas de rating par équipe à stocker).
    """
    init_db()
    notes = (
        f"log-loss={metrics['model_logloss']:.4f} vs Dixon-Coles={metrics['dc_logloss']:.4f} "
        f"(delta={metrics['delta']:+.4f}) ; accuracy={metrics['model_acc']:.4f} vs DC={metrics['dc_acc']:.4f} ; "
        f"bootstrap 2000 tirages : meilleur dans {metrics['frac_better']:.1%} des cas, "
        f"IC95%=[{metrics['delta_ci95'][0]:+.4f}, {metrics['delta_ci95'][1]:+.4f}] ; "
        f"{len(EXISTING_FEATURE_COLUMNS)} features existantes + {len(NEW_FEATURE_COLUMNS)} nouvelles "
        f"(voir api/app/ai/engine/features.py) ; top features : {', '.join(top_features[:5])} ; "
        f"verdict : {'GAIN CRÉDIBLE' if metrics['gain'] else 'PAS DE GAIN CLAIR'} — "
        "is_active=False (activation = décision humaine, hors scope de ce ticket)."
    )
    with Session(engine) as session:
        version = ModelVersion(
            name=next_version_name(session, f"xfoot-{model_type}"),
            model_type=model_type,
            trained_at=datetime.now(timezone.utc),
            is_active=False,
            notes=notes,
        )
        session.add(version)
        session.commit()
        session.refresh(version)
        return version.id


def main():
    report_unused_columns()
    df = load_and_filter()
    y = build_target(df)
    X = select_features(df)

    (X_fit, y_fit), (X_val, y_val), (X_test, y_test), df_test = temporal_split(df, X, y)

    proba_dc = df_test[DC_PROBA_COLUMNS_IN_LABEL_ORDER].to_numpy()
    row_sums = proba_dc.sum(axis=1)
    assert np.allclose(row_sums, 1.0, atol=1e-6), "Probabilités Dixon-Coles ne somment pas à 1 sur le test"
    dc_pred = np.array(CLASS_LABELS)[np.argmax(proba_dc, axis=1)]
    dc_logloss = log_loss(y_test, proba_dc, labels=CLASS_LABELS)
    dc_acc = accuracy_score(y_test, dc_pred)

    results = {}

    xgb_model = train_xgboost(X_fit, y_fit, X_val, y_val)
    proba_xgb = _reorder_proba(xgb_model.predict_proba(X_test), list(xgb_model.classes_))
    xgb_metrics = evaluate_model("XGBoost", proba_xgb, df_test, y_test, dc_logloss, dc_acc, proba_dc)
    xgb_gain = xgb_model.get_booster().get_score(importance_type="gain")
    xgb_table, xgb_new_pct = feature_importance("XGBoost", xgb_gain, list(X_fit.columns))
    xgb_version_id = persist_model_version("xgboost", xgb_metrics, xgb_table["feature"].tolist())
    results["xgboost"] = {**xgb_metrics, "new_features_gain_pct": xgb_new_pct, "model_version_id": xgb_version_id}

    lgb_model, league_categories = train_lightgbm(X_fit, y_fit, X_val, y_val)
    X_test_lgb = X_test.copy()
    X_test_lgb["league"] = pd.Categorical(X_test_lgb["league"], categories=league_categories).codes
    X_test_lgb["league"] = X_test_lgb["league"].astype("category")
    proba_lgb = _reorder_proba(lgb_model.predict_proba(X_test_lgb), list(lgb_model.classes_))
    lgb_metrics = evaluate_model("LightGBM", proba_lgb, df_test, y_test, dc_logloss, dc_acc, proba_dc)
    lgb_gain = dict(zip(lgb_model.feature_name_, lgb_model.booster_.feature_importance(importance_type="gain")))
    lgb_table, lgb_new_pct = feature_importance("LightGBM", lgb_gain, list(X_fit.columns))
    lgb_version_id = persist_model_version("lightgbm", lgb_metrics, lgb_table["feature"].tolist())
    results["lightgbm"] = {**lgb_metrics, "new_features_gain_pct": lgb_new_pct, "model_version_id": lgb_version_id}

    print("=" * 80)
    print("RÉSUMÉ FINAL")
    print("=" * 80)
    for name, r in results.items():
        print(f"  {name:<10} log-loss={r['model_logloss']:.4f} (DC={r['dc_logloss']:.4f})  "
              f"gain crédible={r['gain']}  ModelVersion #{r['model_version_id']} (is_active=False)")
    any_gain = any(r["gain"] for r in results.values())
    print(f"\n>>> VERDICT GLOBAL PHASE 4 : "
          f"{'au moins un modèle montre un gain crédible — voir détail ci-dessus, activation à décider par l’utilisateur.' if any_gain else 'AUCUN GAIN CLAIR — Dixon-Coles reste seul en production, comme après les deux tentatives XGBoost précédentes et le backtest Elo.'}")

    return results


if __name__ == "__main__":
    main()
    sys.exit(0)  # un "pas de gain" documenté n'est pas un échec de ce script
