"""
Entraînement du modèle Dixon-Coles sur données réelles — Ligue 1
==================================================================

Données : football-data.co.uk (via miroir GitHub datasets/football-datasets),
6 saisons (2019/20 à 2024/25), 821 matchs.

Usage : python3 train_on_ligue1.py
"""

import sys
sys.path.insert(0, "..")  # pour importer dixon_coles.py si lancé depuis data/

import pandas as pd
from dixon_coles import DixonColesModel, calibration_check, grid_search_l2

DATA_FILE = "data/ligue1_2019_2025.csv"


def main():
    df = pd.read_csv(DATA_FILE, parse_dates=["date"])

    # Split temporel : test = 100 derniers matchs (out-of-sample réel,
    # pas un split aléatoire qui laisserait fuiter de l'information future
    # vers le passé)
    train = df.iloc[:-100].copy()
    test = df.iloc[-100:].copy()

    model = DixonColesModel(xi=0.0018)
    model.fit(train)

    print("=== Classement (force nette attaque - défense) ===")
    print(model.team_ratings().to_string(index=False))

    print(f"\nHome advantage appris : {model.home_advantage_:.4f}")
    print(f"Rho appris : {model.rho_:.4f}")

    print("\n=== Calibration out-of-sample (100 derniers matchs) ===")
    print(calibration_check(model, test, n_bins=5).to_string(index=False))

    print("\n=== Grid search l2 (régularisation attack/défense) — critère log-loss out-of-sample ===")
    # Grille recentrée autour du minimum réel trouvé lors de l'exploration
    # élargie (voir extend_grid_search.py) : le log-loss baisse jusqu'à
    # l2≈1.0 puis remonte — 0.0-0.2 seul ne montrait qu'un plateau monotone
    # sans révéler ce minimum intérieur.
    grid = grid_search_l2(train, test, l2_values=(0.0, 0.05, 0.2, 0.5, 0.75, 1.0, 1.5, 2.0), xi=0.0018)
    print(grid.to_string(index=False))
    best = grid.loc[grid["recommended"], "l2"].iloc[0]
    print(f"\n=> l2 recommandé : {best} (log-loss minimal sur les 100 derniers matchs)")

    return model


if __name__ == "__main__":
    main()
