"""
Validation de la régularisation L2 sur le modèle Dixon-Coles — Ligue 1
========================================================================

Objectif : vérifier que le prior gaussien (l2) ajouté sur attack/defense
stabilise le classement, en particulier pour les équipes à faible échantillon
pondéré par le decay temporel (typiquement les reléguées : Nîmes, mais on
regarde aussi Montpellier car le sujet le mentionne explicitement).

Deux vérifications :
  1. Rétrécissement direct : rating net (attack - defense) avant/après L2,
     sur le fit complet (821 matchs).
  2. Stabilité sous ré-échantillonnage : on bootstrap les matchs (tirage avec
     remise) et on mesure l'écart-type du rating net obtenu, avec et sans L2.
     Un prior qui marche doit réduire cette variance, surtout pour les
     équipes à faible poids effectif (peu de matchs, ou matchs anciens donc
     fortement décotés par le decay).

Remarque technique : la classe DixonColesModel de dixon_coles.py boucle en
Python pur sur chaque match à chaque évaluation de la NLL (~110s par fit sur
821 matchs avec SLSQP + gradient par différences finies). C'est trop lent
pour faire des dizaines de fits bootstrap. Ce script réimplémente donc la
MÊME formule (tau, decay, prior L2) de façon vectorisée avec numpy, unique-
ment pour ce test de validation — la classe de production n'est pas touchée.
On vérifie d'abord que les deux implémentations convergent vers le même
résultat, pour garantir que le test porte bien sur le même modèle.
"""

import time
import numpy as np
import pandas as pd

from dixon_coles import DixonColesModel
from dixon_coles_fast import fast_fit, team_ratings_from_fast

DATA_FILE = "data/ligue1_2019_2025.csv"


def effective_sample_size(df: pd.DataFrame, xi: float, reference_date) -> pd.Series:
    """Somme des poids de decay des matchs joués par chaque équipe (proxy de
    la quantité d'information réellement disponible sur elle)."""
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    days = (reference_date - df["date"]).dt.days.clip(lower=0)
    df["w"] = np.exp(-xi * days)
    home = df.groupby("home_team")["w"].sum()
    away = df.groupby("away_team")["w"].sum()
    return home.add(away, fill_value=0).sort_values()


# ---------------------------------------------------------------------------
# 1. Sanity check : la version vectorisée reproduit-elle bien la classe officielle ?
# ---------------------------------------------------------------------------

def sanity_check(df, xi=0.0018, l2=0.05):
    print("### Sanity check : implémentation vectorisée vs DixonColesModel officiel ###\n")
    t0 = time.time()
    official = DixonColesModel(xi=xi, l2=l2)
    official.fit(df)
    t_official = time.time() - t0
    off_ratings = official.team_ratings().set_index("team")["net_rating"]

    t0 = time.time()
    fast = fast_fit(df, xi=xi, l2=l2, teams=official.teams_)
    t_fast = time.time() - t0
    fast_ratings = team_ratings_from_fast(fast).set_index("team")["net_rating"]

    diff = (off_ratings - fast_ratings).abs()
    print(f"Temps fit officiel (boucle python) : {t_official:.1f}s")
    print(f"Temps fit vectorisé (numpy)        : {t_fast:.1f}s")
    print(f"Écart max net_rating entre les deux fits : {diff.max():.6f}")
    print(f"Écart moyen                              : {diff.mean():.6f}")
    print(f"home_advantage officiel={official.home_advantage_:.4f} vs vectorisé={fast['home_advantage']:.4f}")
    print(f"rho officiel={official.rho_:.4f} vs vectorisé={fast['rho']:.4f}")
    ok = diff.max() < 0.01
    print("=> " + ("OK, implémentations équivalentes.\n" if ok else "ATTENTION : divergence significative.\n"))
    return ok


# ---------------------------------------------------------------------------
# 2. Rétrécissement direct des ratings sur le fit complet
# ---------------------------------------------------------------------------

def shrinkage_comparison(df, xi=0.0018, l2_values=(0.0, 0.02, 0.05, 0.1)):
    print("### Rétrécissement des ratings selon l2 (fit complet, 821 matchs) ###\n")
    reference_date = pd.to_datetime(df["date"]).max()
    eff_n = effective_sample_size(df, xi, reference_date)

    results = {}
    for l2 in l2_values:
        fit = fast_fit(df, xi=xi, l2=l2)
        results[l2] = team_ratings_from_fast(fit).set_index("team")["net_rating"]

    table = pd.DataFrame(results)
    table.columns = [f"l2={l2}" for l2 in l2_values]
    table["poids_effectif"] = eff_n
    table = table.sort_values("poids_effectif")

    focus = ["Nimes", "Montpellier"]
    print("--- Équipes suivies (Nîmes, Montpellier) + poids effectif (somme des decay) ---")
    print(table.loc[[t for t in focus if t in table.index]].to_string())

    print("\n--- Toutes les équipes, triées par poids effectif (les plus fragiles en premier) ---")
    print(table.to_string())

    print("\n--- Extremité globale du classement (écart-type des net_rating sur toutes les équipes) ---")
    for col in table.columns[:-1]:
        print(f"  {col:10s} : std = {table[col].std():.4f}, max(|net_rating|) = {table[col].abs().max():.4f}")

    return table


# ---------------------------------------------------------------------------
# 3. Stabilité sous bootstrap (ré-échantillonnage des matchs avec remise)
# ---------------------------------------------------------------------------

def bootstrap_stability(df, xi=0.0018, l2_values=(0.0, 0.05), n_boot=15, seed=42):
    print(f"\n### Stabilité bootstrap (n_boot={n_boot} ré-échantillonnages) ###\n")
    rng = np.random.default_rng(seed)
    all_teams = sorted(set(df["home_team"]) | set(df["away_team"]))
    reference_date = pd.to_datetime(df["date"]).max()
    n = len(df)

    focus = ["Nimes", "Montpellier"]
    records = {l2: {t: [] for t in all_teams} for l2 in l2_values}

    t_start = time.time()
    for b in range(n_boot):
        sample_idx = rng.integers(0, n, size=n)
        sample = df.iloc[sample_idx]
        # garantit que toutes les équipes sont présentes dans le tirage
        present = set(sample["home_team"]) | set(sample["away_team"])
        if present != set(all_teams):
            continue
        for l2 in l2_values:
            fit = fast_fit(sample, xi=xi, l2=l2, reference_date=reference_date, teams=all_teams)
            for t in all_teams:
                records[l2][t].append(fit["net"][t])
        print(f"  bootstrap {b + 1}/{n_boot} terminé ({time.time() - t_start:.0f}s écoulées)")

    print()
    summary_rows = []
    for t in all_teams:
        row = {"team": t}
        for l2 in l2_values:
            vals = records[l2][t]
            row[f"std_l2={l2}"] = np.std(vals) if len(vals) > 1 else np.nan
        summary_rows.append(row)
    summary = pd.DataFrame(summary_rows).set_index("team")

    print("--- Écart-type du net_rating à travers les tirages bootstrap ---")
    print("--- (équipes suivies : Nîmes, Montpellier) ---")
    print(summary.loc[[t for t in focus if t in summary.index]].to_string())

    print("\n--- Toutes les équipes ---")
    print(summary.sort_values(summary.columns[0], ascending=False).to_string())

    print("\n--- Moyenne de l'écart-type sur toutes les équipes (mesure globale de stabilité) ---")
    for col in summary.columns:
        print(f"  {col} : {summary[col].mean():.4f}")

    return summary


if __name__ == "__main__":
    df = pd.read_csv(DATA_FILE, parse_dates=["date"])

    sanity_check(df)
    shrinkage_comparison(df)
    bootstrap_stability(df, l2_values=(0.0, 0.02, 0.05, 0.1), n_boot=40)
