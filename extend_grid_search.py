"""
Grille étendue pour l2 — recherche de log-loss out-of-sample
==============================================================

La grille initiale (0, 0.01, 0.02, 0.05, 0.1, 0.2) montrait un log-loss qui
diminue de façon monotone jusqu'au bord supérieur (0.2), sans minimum
intérieur visible et avec des écarts minuscules (4e décimale) — donc rien
ne prouvait que 0.2 soit vraiment optimal plutôt que "la plus grande valeur
testée". Ce script élargit la grille très au-delà (jusqu'à l2=10) pour voir
si le log-loss finit par remonter (signe d'un vrai optimum) ou continue de
baisser indéfiniment (signe qu'on écrase le signal utile).

Pour aller vite sur ~15 valeurs de l2, on réutilise fast_fit (réimplémentation
vectorisée numpy de dixon_coles.py, validée équivalente à la classe officielle
à 1e-5 près dans validate_l2_regularization.py) plutôt que DixonColesModel,
qui prendrait ~100s par fit avec sa boucle Python pure.
"""

import numpy as np
import pandas as pd

from dixon_coles_fast import fast_fit, fast_predict_1x2

DATA_FILE = "data/ligue1_2019_2025.csv"


def logloss_for_l2(train, test, l2, xi=0.0018):
    fit = fast_fit(train, xi=xi, l2=l2)
    neg_log_probs = []
    for row in test.itertuples():
        if row.home_team not in fit["attack"] or row.away_team not in fit["attack"]:
            continue
        probs = fast_predict_1x2(fit, row.home_team, row.away_team)
        if row.home_goals > row.away_goals:
            p = probs["home_win"]
        elif row.home_goals == row.away_goals:
            p = probs["draw"]
        else:
            p = probs["away_win"]
        neg_log_probs.append(-np.log(max(p, 1e-10)))
    return float(np.mean(neg_log_probs)), len(neg_log_probs)


if __name__ == "__main__":
    df = pd.read_csv(DATA_FILE, parse_dates=["date"])
    train = df.iloc[:-100].copy()
    test = df.iloc[-100:].copy()

    l2_values = [0.0, 0.01, 0.02, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 8.0, 10.0]

    rows = []
    for l2 in l2_values:
        logloss, n = logloss_for_l2(train, test, l2)
        rows.append({"l2": l2, "logloss": logloss, "n_matches": n})

    results = pd.DataFrame(rows)
    results["recommended"] = results["logloss"] == results["logloss"].min()
    print(results.to_string(index=False))

    best = results.loc[results["logloss"].idxmin(), "l2"]
    print(f"\n=> l2 recommandé (grille élargie) : {best}")
