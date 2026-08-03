"""
train_xgboost_stacking_multileague.py — XGBoost en stacking par-dessus Dixon-Coles (5 ligues)
================================================================================================

Source : data/all_leagues_features_xgboost.csv (10 707 lignes, 5 ligues,
produit par build_features.py, déjà audité par-ligue dans
audit_features_multileague.py — aucune fuite détectée, dc_* somme à 1.0
dans chaque ligue, NaN de rodage isolés par ligue).

Reprend exactement le protocole de train_xgboost_stacking.py (Ligue 1 seule,
712 lignes exploitables, conclusion : PAS DE GAIN CLAIR — log-loss
XGBoost=1.0713 vs Dixon-Coles=1.0219, DC meilleur dans 97.4% des tirages
bootstrap) mais sur le pool des 5 ligues (~10 165 lignes exploitables après
filtrage), pour vérifier si le volume x10 change cette conclusion.

Différences avec la version Ligue 1 seule :

  1. FILTRAGE — identique (dc_home_win non-NaN), mais appliqué aux 5 ligues
     confondues.
  2. FEATURES — mêmes 17 features pré-match, PLUS `league` encodée en
     catégorie native XGBoost (dtype pandas "category" + enable_categorical
     =True), pas en one-hot. Choix motivé par l'audit demandé au point 7 :
     une catégorie native donne UNE seule ligne dans le classement
     d'importance (gain total de la feature "league"), directement
     interprétable, alors que 4 colonnes one-hot auraient dispersé ce
     signal sur 4 lignes qu'il aurait fallu resommer à la main pour
     répondre à la même question. Exclusion inchangée des colonnes
     post-match (goals, shots, corners) : ce fichier les conserve
     brutes (comme home_goals/away_goals l'étaient déjà dans la version
     Ligue 1), elles ne sont PAS dans FEATURE_COLUMNS.
  3. CIBLE — inchangée.
  4. SPLIT TEMPOREL — 300 derniers matchs chronologiquement, TOUTES LIGUES
     CONFONDUES (un seul tri global par date, pas un split par ligue) : la
     composition en ligues de ce test set dépend donc de la fin de saison
     de chacune, documentée explicitement ci-dessous.
  5. HYPERPARAMÈTRES — identiques (max_depth=4, learning_rate=0.05,
     n_estimators=300, early stopping sur les 100 derniers matchs du train).
  6. ÉVALUATION — log-loss / accuracy globales + bootstrap apparié (2000
     tirages), PLUS une décomposition par ligue au sein du test set (300
     lignes) pour voir si le stacking gagne dans certaines ligues et pas
     d'autres — nombre de matchs par ligue dans ce test set étant faible,
     ces sous-résultats sont à lire avec prudence (petits échantillons).
  7. IMPORTANCE DES FEATURES — classement par gain, avec la position de
     `league` explicitement commentée.
"""

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import log_loss, accuracy_score

FEATURES_FILE = "data/all_leagues_features_xgboost.csv"

N_TEST = 300
N_VAL = 100  # pris dans le train, pour l'early stopping — jamais dans le test

# Liste blanche explicite : uniquement des features PRÉ-match. `league` est
# ajoutée séparément (dtype catégoriel) dans select_features().
FEATURE_COLUMNS = [
    "home_form_points_avg", "home_form_goals_scored_avg", "home_form_goals_conceded_avg",
    "away_form_points_avg", "away_form_goals_scored_avg", "away_form_goals_conceded_avg",
    "home_days_since_last_match", "away_days_since_last_match",
    "home_returning_from_break", "away_returning_from_break",
    "h2h_matches_found", "h2h_home_win_rate",
    "dc_home_win", "dc_draw", "dc_away_win", "dc_over_2_5", "dc_under_2_5",
]
CATEGORICAL_COLUMNS = ["league"]

# Exclusion explicite : tout ce qui encode ou dérive du résultat du match
# courant (buts, tirs, corners), + colonnes d'identification.
EXCLUDED_RESULT_COLUMNS = [
    "home_goals", "away_goals",
    "home_shots", "away_shots", "home_shots_target", "away_shots_target",
    "home_corners", "away_corners",
    "date", "home_team", "away_team",
]

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
    print("3. CIBLE — distribution 1X2 (0=nul, 1=domicile, 2=extérieur)")
    print("=" * 80)
    print(pd.Series(y).value_counts().sort_index().to_string())
    print()
    return pd.Series(y, index=df.index, name="target")


def select_features(df: pd.DataFrame) -> pd.DataFrame:
    print("=" * 80)
    print("2. FEATURES D'ENTRÉE (+ league en catégorie native XGBoost)")
    print("=" * 80)
    overlap = set(FEATURE_COLUMNS) & set(EXCLUDED_RESULT_COLUMNS)
    assert not overlap, f"Fuite potentielle : colonnes de résultat dans la liste de features : {overlap}"
    missing = [c for c in FEATURE_COLUMNS if c not in df.columns]
    assert not missing, f"Colonnes de features manquantes dans le fichier : {missing}"

    all_columns = FEATURE_COLUMNS + CATEGORICAL_COLUMNS
    X = df[all_columns].copy()
    X["league"] = X["league"].astype("category")

    print(f"  {len(FEATURE_COLUMNS)} features numériques pré-match + 1 catégorielle (league) :")
    for c in FEATURE_COLUMNS:
        print(f"    - {c}")
    print(f"    - league  (catégorie native, {X['league'].nunique()} modalités : "
          f"{list(X['league'].cat.categories)})")
    print(f"\n  Colonnes explicitement exclues (encodent le résultat courant ou identifient le match) :")
    print(f"    {EXCLUDED_RESULT_COLUMNS}")

    nan_counts = X.isna().sum()
    nan_counts = nan_counts[nan_counts > 0]
    if len(nan_counts):
        print(f"\n  NaN résiduels (laissés tels quels — XGBoost gère nativement les valeurs manquantes) :")
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

    print(f"  Train (fit)       : {len(X_fit)} matchs   [{df['date'].iloc[0].date()} -> {df['date'].iloc[val_start - 1].date()}]")
    print(f"  Validation (ES)   : {len(X_val)} matchs   [{df['date'].iloc[val_start].date()} -> {df['date'].iloc[test_start - 1].date()}]")
    print(f"  Test              : {len(X_test)} matchs   [{df['date'].iloc[test_start].date()} -> {df['date'].iloc[-1].date()}]")
    print(f"  Date de coupure train/validation : {df['date'].iloc[val_start].date()}")
    print(f"  Date de coupure validation/test  : {df['date'].iloc[test_start].date()}")

    df_test = df.iloc[test_start:]
    print(f"\n  Composition en ligues du test set ({N_TEST} matchs) :")
    league_counts = df_test["league"].value_counts()
    print(league_counts.to_string())
    dominant = league_counts.index[0]
    dominant_pct = 100 * league_counts.iloc[0] / N_TEST
    if dominant_pct > 40:
        print(f"  -> Test set dominé par {dominant} ({dominant_pct:.0f}%) — cohérent avec une fin de "
              f"saison plus tardive dans ce championnat. Les résultats globaux ci-dessous sont donc "
              f"tirés en majorité de cette ligue ; voir la décomposition par ligue (point 6) pour "
              f"le détail par championnat.")
    print()

    return (X_fit, y_fit), (X_val, y_val), (X_test, y_test), df_test


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
        enable_categorical=True,
        tree_method="hist",
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
    reorder = [class_order.index(c) for c in CLASS_LABELS]
    proba_xgb = proba_xgb_raw[:, reorder]
    xgb_pred = np.array(CLASS_LABELS)[np.argmax(proba_xgb, axis=1)]
    xgb_logloss = log_loss(y_test, proba_xgb, labels=CLASS_LABELS)
    xgb_acc = accuracy_score(y_test, xgb_pred)

    print(f"  --- GLOBAL (5 ligues, {len(y_test)} matchs) ---")
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

    global_verdict_gain = frac_xgb_better > 0.90 and delta > 0
    print()

    # --- Décomposition PAR LIGUE au sein du test set ---
    print(f"  --- PAR LIGUE (au sein des {len(y_test)} matchs du test set) ---")
    print(f"  {'Ligue':<15} {'n':>4} {'DC logloss':>11} {'XGB logloss':>12} {'delta':>8} {'DC acc':>8} {'XGB acc':>8}")
    y_test_arr = y_test.to_numpy()
    per_league_rows = []
    for league in df_test["league"].unique():
        mask = (df_test["league"] == league).to_numpy()
        n_league = mask.sum()
        if n_league < 10:
            print(f"  {league:<15} {n_league:>4}  (trop peu de matchs pour une log-loss fiable — ignoré)")
            continue
        dc_ll_l = log_loss(y_test_arr[mask], proba_dc[mask], labels=CLASS_LABELS)
        xgb_ll_l = log_loss(y_test_arr[mask], proba_xgb[mask], labels=CLASS_LABELS)
        dc_acc_l = accuracy_score(y_test_arr[mask], dc_pred[mask])
        xgb_acc_l = accuracy_score(y_test_arr[mask], xgb_pred[mask])
        delta_l = dc_ll_l - xgb_ll_l
        print(f"  {league:<15} {n_league:>4} {dc_ll_l:>11.4f} {xgb_ll_l:>12.4f} {delta_l:>+8.4f} "
              f"{dc_acc_l:>8.4f} {xgb_acc_l:>8.4f}")
        per_league_rows.append({"league": league, "n": n_league, "dc_logloss": dc_ll_l,
                                 "xgb_logloss": xgb_ll_l, "delta": delta_l})
    print(f"\n  NB : ces sous-échantillons par ligue sont petits (quelques dizaines de matchs au plus) — "
          f"un delta positif isolé sur une seule ligue n'est pas une confirmation statistique, "
          f"seul le résultat global + bootstrap ci-dessus l'est.")

    print()
    honest_verdict = (
        "VERDICT : le stacking XGBoost améliore la log-loss de façon crédible "
        "(gain positif et robuste au bootstrap)."
        if global_verdict_gain else
        "VERDICT : PAS DE GAIN CLAIR — l'écart de log-loss est faible et/ou instable au bootstrap. "
        "Ne pas présenter ce stacking comme une amélioration confirmée, même à 10x plus de données."
    )
    print(f">>> {honest_verdict}\n")

    return {
        "dc_logloss": dc_logloss, "dc_acc": dc_acc,
        "xgb_logloss": xgb_logloss, "xgb_acc": xgb_acc,
        "frac_xgb_better": frac_xgb_better,
        "per_league": per_league_rows,
    }


def feature_importance(model, X_fit):
    print("=" * 80)
    print("7. IMPORTANCE DES FEATURES (gain)")
    print("=" * 80)
    booster = model.get_booster()
    gain = booster.get_score(importance_type="gain")
    for c in X_fit.columns:
        gain.setdefault(c, 0.0)

    total = sum(gain.values()) or 1.0
    table = pd.DataFrame(
        [{"feature": k, "gain": v, "gain_pct": 100 * v / total} for k, v in gain.items()]
    ).sort_values("gain", ascending=False).reset_index(drop=True)
    print(table.to_string(index=False))

    top_pct = table["gain_pct"].iloc[0]
    dc_pct = table[table["feature"].str.startswith("dc_")]["gain_pct"].sum()
    league_row = table[table["feature"] == "league"]
    league_rank = table.index[table["feature"] == "league"][0] + 1
    league_pct = league_row["gain_pct"].iloc[0] if len(league_row) else 0.0

    print(f"\n  Part de gain totale des features dc_* : {dc_pct:.1f}%")
    print(f"  Position de `league` dans le classement : #{league_rank}/{len(table)}  (gain={league_pct:.1f}%)")
    if league_pct > 20:
        print(f"  ATTENTION : `league` capte une part de gain élevée ({league_pct:.1f}%) — indique que le "
              f"modèle compense surtout des différences de calibration entre championnats (niveau moyen de "
              f"buts, distribution des résultats) plutôt que d'apprendre un signal prédictif intra-match. "
              f"Cela n'aide pas à départager une confrontation particulière, seulement à ajuster un biais "
              f"de championnat que Dixon-Coles (ré-entraîné par ligue) capture déjà nativement.")
    else:
        print(f"  `league` a un poids faible à modéré — le modèle ne s'appuie pas principalement sur "
              f"l'identité du championnat.")
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
