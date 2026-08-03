"""
train_xgboost_stacking.py — XGBoost en stacking par-dessus Dixon-Coles
========================================================================

Source : data/ligue1_features_xgboost.csv (produit par build_features.py,
déjà audité pour fuite de données dans audit_features.py).

AVERTISSEMENT SUR LES DONNÉES (déjà signalé dans les sessions précédentes,
répété ici car il change directement l'interprétation des résultats) :
ce fichier contient 821 matchs réels 2019-2025, PAS 2031 — c'est un
échantillon Ligue 1 réel mais épars (~1 date par mois), pas le calendrier
complet. Après filtrage sur dc_home_win non-NaN (point 1 ci-dessous), il
reste 712 lignes exploitables. Avec un test de 300 matchs demandé, il ne
reste que 412 lignes de train, dont 100 réservées à la validation
early-stopping : le fit final se fait donc sur ~312 exemples pour un
problème à 3 classes et ~17 features. C'est petit — les métriques ci-dessous
doivent être lues comme un premier passage exploratoire, pas comme un
résultat de production. Le rapport le redit en conclusion.

Étapes :
  1. Filtrage sur dc_home_win non-NaN
  2. Sélection explicite des features (liste blanche) + exclusion explicite
     des colonnes qui encodent le résultat du match
  3. Cible 1X2 (0=nul, 1=victoire domicile, 2=victoire extérieur)
  4. Split temporel : 300 derniers matchs en test, le reste en train
  5. XGBoost multi:softprob avec early stopping sur les 100 derniers matchs
     du train (jamais le test)
  6. Comparaison log-loss / accuracy : Dixon-Coles seul vs XGBoost (stacking)
  7. Importance des features (gain)
"""

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import log_loss, accuracy_score

FEATURES_FILE = "data/ligue1_features_xgboost.csv"

N_TEST = 300
N_VAL = 100  # pris dans le train, pour l'early stopping — jamais dans le test

# Liste blanche explicite : uniquement des features PRÉ-match.
FEATURE_COLUMNS = [
    "home_form_points_avg", "home_form_goals_scored_avg", "home_form_goals_conceded_avg",
    "away_form_points_avg", "away_form_goals_scored_avg", "away_form_goals_conceded_avg",
    "home_days_since_last_match", "away_days_since_last_match",
    "home_returning_from_break", "away_returning_from_break",
    "h2h_matches_found", "h2h_home_win_rate",
    "dc_home_win", "dc_draw", "dc_away_win", "dc_over_2_5", "dc_under_2_5",
]

# Exclusion explicite : tout ce qui encode ou dérive du résultat du match
# courant. Une assertion plus bas garantit qu'aucune de ces colonnes ne
# s'est glissée dans FEATURE_COLUMNS par erreur.
EXCLUDED_RESULT_COLUMNS = ["home_goals", "away_goals", "date", "home_team", "away_team"]

# Ordre de classe fixé une fois pour toutes : 0=nul, 1=victoire domicile,
# 2=victoire extérieur. Les colonnes Dixon-Coles doivent être réordonnées
# pour matcher CET ordre — dc_home_win/dc_draw/dc_away_win ne sont pas dans
# cet ordre dans le fichier, une erreur d'alignement ici fausserait
# silencieusement toute comparaison de log-loss.
CLASS_LABELS = [0, 1, 2]  # nul, domicile, extérieur
DC_PROBA_COLUMNS_IN_LABEL_ORDER = ["dc_draw", "dc_home_win", "dc_away_win"]


def load_and_filter():
    df = pd.read_csv(FEATURES_FILE, parse_dates=["date"])
    df = df.sort_values("date", kind="stable").reset_index(drop=True)
    n_before = len(df)

    df = df[df["dc_home_win"].notna()].reset_index(drop=True)
    n_after = len(df)

    print("=" * 80)
    print("1. FILTRAGE — dc_home_win non-NaN")
    print("=" * 80)
    print(f"  Lignes avant filtrage : {n_before}")
    print(f"  Lignes après filtrage : {n_after}  ({n_before - n_after} écartées, "
          f"période de rodage Dixon-Coles / équipes jamais vues)")
    print()
    return df


def build_target(df: pd.DataFrame) -> pd.Series:
    conditions = [df["home_goals"] > df["away_goals"], df["home_goals"] == df["away_goals"]]
    choices = [1, 0]
    y = np.select(conditions, choices, default=2)
    print("=" * 80)
    print("3. CIBLE — distribution 1X2 (0=nul, 1=domicile, 2=extérieur)")
    print("=" * 80)
    print(pd.Series(y).value_counts().sort_index().to_string())
    print()
    return pd.Series(y, index=df.index, name="target")


def select_features(df: pd.DataFrame) -> pd.DataFrame:
    print("=" * 80)
    print("2. FEATURES D'ENTRÉE")
    print("=" * 80)
    overlap = set(FEATURE_COLUMNS) & set(EXCLUDED_RESULT_COLUMNS)
    assert not overlap, f"Fuite potentielle : colonnes de résultat dans la liste de features : {overlap}"
    missing = [c for c in FEATURE_COLUMNS if c not in df.columns]
    assert not missing, f"Colonnes de features manquantes dans le fichier : {missing}"

    X = df[FEATURE_COLUMNS].copy()
    print(f"  {len(FEATURE_COLUMNS)} features retenues :")
    for c in FEATURE_COLUMNS:
        print(f"    - {c}")
    print(f"\n  Colonnes explicitement exclues (encodent le résultat courant) : {EXCLUDED_RESULT_COLUMNS}")

    nan_counts = X.isna().sum()
    nan_counts = nan_counts[nan_counts > 0]
    if len(nan_counts):
        print(f"\n  NaN résiduels (laissés tels quels — XGBoost gère nativement les valeurs manquantes) :")
        print(nan_counts.to_string())
    print()
    return X


def temporal_split(df, X, y):
    print("=" * 80)
    print("4. SPLIT TEMPOREL")
    print("=" * 80)
    n = len(df)
    test_start = n - N_TEST
    val_start = test_start - N_VAL

    X_test, y_test = X.iloc[test_start:], y.iloc[test_start:]
    X_val, y_val = X.iloc[val_start:test_start], y.iloc[val_start:test_start]
    X_fit, y_fit = X.iloc[:val_start], y.iloc[:val_start]

    print(f"  Train (fit)       : {len(X_fit)} matchs   [{df['date'].iloc[0].date()} -> {df['date'].iloc[val_start - 1].date()}]")
    print(f"  Validation (ES)   : {len(X_val)} matchs   [{df['date'].iloc[val_start].date()} -> {df['date'].iloc[test_start - 1].date()}]")
    print(f"  Test              : {len(X_test)} matchs   [{df['date'].iloc[test_start].date()} -> {df['date'].iloc[-1].date()}]")
    print(f"  Date de coupure train/validation : {df['date'].iloc[val_start].date()}")
    print(f"  Date de coupure validation/test  : {df['date'].iloc[test_start].date()}")
    print()

    return (X_fit, y_fit), (X_val, y_val), (X_test, y_test), df.iloc[test_start:]


def train_model(X_fit, y_fit, X_val, y_val):
    print("=" * 80)
    print("5. ENTRAÎNEMENT XGBOOST")
    print("=" * 80)
    model = xgb.XGBClassifier(
        max_depth=4,
        learning_rate=0.05,
        n_estimators=300,
        objective="multi:softprob",
        num_class=3,
        eval_metric="mlogloss",
        early_stopping_rounds=20,
        random_state=42,
    )
    model.fit(X_fit, y_fit, eval_set=[(X_val, y_val)], verbose=False)

    best_iter = getattr(model, "best_iteration", None)
    print(f"  n_estimators demandés : 300")
    print(f"  best_iteration (early stopping) : {best_iter}")
    if best_iter is not None and best_iter < 30:
        print(f"  -> arrêt très précoce : signal probable d'un jeu d'entraînement trop petit "
              f"({len(X_fit)} exemples) pour 300 arbres à max_depth=4.")
    print()
    return model


def evaluate(model, df_test, X_test, y_test):
    print("=" * 80)
    print("6. ÉVALUATION — XGBoost (stacking) vs Dixon-Coles seul")
    print("=" * 80)

    # --- Dixon-Coles seul ---
    proba_dc = df_test[DC_PROBA_COLUMNS_IN_LABEL_ORDER].to_numpy()
    row_sums = proba_dc.sum(axis=1)
    assert np.allclose(row_sums, 1.0, atol=1e-6), "Probabilités Dixon-Coles ne somment pas à 1 sur le test"
    dc_pred = np.array(CLASS_LABELS)[np.argmax(proba_dc, axis=1)]
    dc_logloss = log_loss(y_test, proba_dc, labels=CLASS_LABELS)
    dc_acc = accuracy_score(y_test, dc_pred)

    # --- XGBoost (stacking) ---
    proba_xgb_raw = model.predict_proba(X_test)  # colonnes dans l'ordre de model.classes_
    class_order = list(model.classes_)
    # réordonne au format CLASS_LABELS = [0,1,2] par sécurité (ne pas supposer l'ordre)
    reorder = [class_order.index(c) for c in CLASS_LABELS]
    proba_xgb = proba_xgb_raw[:, reorder]
    xgb_pred = np.array(CLASS_LABELS)[np.argmax(proba_xgb, axis=1)]
    xgb_logloss = log_loss(y_test, proba_xgb, labels=CLASS_LABELS)
    xgb_acc = accuracy_score(y_test, xgb_pred)

    print(f"  {'Modèle':<20} {'log-loss':>10} {'accuracy':>10}")
    print(f"  {'Dixon-Coles seul':<20} {dc_logloss:>10.4f} {dc_acc:>10.4f}")
    print(f"  {'XGBoost (stacking)':<20} {xgb_logloss:>10.4f} {xgb_acc:>10.4f}")

    delta = dc_logloss - xgb_logloss
    print(f"\n  Delta log-loss (DC - XGB) : {delta:+.4f}  "
          f"({'XGBoost meilleur' if delta > 0 else 'Dixon-Coles meilleur ou égal'})")

    # --- Robustesse du delta : bootstrap apparié sur les 300 matchs de test ---
    rng = np.random.default_rng(42)
    n = len(y_test)
    y_arr = y_test.to_numpy()
    deltas = []
    for _ in range(2000):
        idx = rng.integers(0, n, size=n)
        dc_ll = log_loss(y_arr[idx], proba_dc[idx], labels=CLASS_LABELS)
        xgb_ll = log_loss(y_arr[idx], proba_xgb[idx], labels=CLASS_LABELS)
        deltas.append(dc_ll - xgb_ll)
    deltas = np.array(deltas)
    frac_xgb_better = (deltas > 0).mean()
    print(f"  Bootstrap apparié (2000 tirages, n={n}) : XGBoost meilleur dans {frac_xgb_better:.1%} des tirages, "
          f"IC95% du delta = [{np.quantile(deltas, 0.025):+.4f}, {np.quantile(deltas, 0.975):+.4f}]")

    print()
    honest_verdict = (
        "VERDICT : le stacking XGBoost améliore la log-loss de façon crédible "
        "(gain positif et robuste au bootstrap)."
        if frac_xgb_better > 0.90 and delta > 0 else
        "VERDICT : PAS DE GAIN CLAIR — l'écart de log-loss est faible et/ou instable au bootstrap. "
        "Ne pas présenter ce stacking comme une amélioration confirmée sur ce volume de données."
    )
    print(f">>> {honest_verdict}\n")

    return {
        "dc_logloss": dc_logloss, "dc_acc": dc_acc,
        "xgb_logloss": xgb_logloss, "xgb_acc": xgb_acc,
        "frac_xgb_better": frac_xgb_better,
    }


def feature_importance(model, X_fit):
    print("=" * 80)
    print("7. IMPORTANCE DES FEATURES (gain)")
    print("=" * 80)
    booster = model.get_booster()
    gain = booster.get_score(importance_type="gain")
    # les features jamais utilisées dans un split n'apparaissent pas dans get_score
    for c in X_fit.columns:
        gain.setdefault(c, 0.0)

    total = sum(gain.values()) or 1.0
    table = pd.DataFrame(
        [{"feature": k, "gain": v, "gain_pct": 100 * v / total} for k, v in gain.items()]
    ).sort_values("gain", ascending=False).reset_index(drop=True)
    print(table.to_string(index=False))

    top_pct = table["gain_pct"].iloc[0]
    dc_pct = table[table["feature"].str.startswith("dc_")]["gain_pct"].sum()
    print(f"\n  Part de gain totale des features dc_* : {dc_pct:.1f}%")
    if top_pct > 60:
        print(f"  ATTENTION : une seule feature ({table['feature'].iloc[0]}) capte {top_pct:.1f}% du gain — "
              f"à examiner comme signal potentiel de fuite de données non détectée.")
    if dc_pct < 5:
        print(f"  ATTENTION : les probabilités Dixon-Coles pèsent très peu ({dc_pct:.1f}%) — "
              f"le stacking n'apporte probablement rien par rapport à un modèle XGBoost entraîné seul.")
    print()
    return table


def main():
    df = load_and_filter()
    y = build_target(df)
    X = select_features(df)

    (X_fit, y_fit), (X_val, y_val), (X_test, y_test), df_test = temporal_split(df, X, y)
    model = train_model(X_fit, y_fit, X_val, y_val)
    metrics = evaluate(model, df_test, X_test, y_test)
    importance = feature_importance(model, X_fit)

    return model, metrics, importance


if __name__ == "__main__":
    main()
