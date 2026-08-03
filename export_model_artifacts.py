"""
export_model_artifacts.py — Entraîne Dixon-Coles par ligue et exporte les
paramètres en JSON, séparément de l'API.
=============================================================================

Pourquoi séparer entraînement et API :
- L'entraînement dépend de scipy (optimisation SLSQP) — lourd, pas besoin
  en production pour SERVIR des prédictions.
- L'API n'a besoin que des paramètres déjà appris (attack, defense,
  home_advantage, rho) pour calculer une matrice de Poisson — ça ne
  dépend que de numpy/scipy.stats, beaucoup plus léger à déployer, et
  la prédiction devient quasi instantanée (pas de ré-optimisation à
  la volée).

Ce script est celui qu'un job planifié (cron, ou déclenché après chaque
journée de championnat) doit exécuter pour rafraîchir les paramètres.

Contrainte d'identifiabilité — un seul mean(attack)=0, PAS de contrainte
supplémentaire mean(defense)=0 : testé empiriquement (voir dixon_coles.py
et dixon_coles_fast.py) qu'avec ≥3 équipes, l'unique degré de liberté non
identifié du modèle est le décalage simultané attack_i += c, defense_i += c
pour toutes les équipes — la seule contrainte mean(attack)=0 le fixe déjà
complètement (attack ET defense). Ajouter une deuxième contrainte
indépendante mean(defense)=0 sur-détermine le système : ça dégrade
l'objectif régularisé (+2.8 mesuré sur Ligue 1) et déplace les net ratings
de chaque équipe de plusieurs unités log — un biais réel, pas un simple
recentrage cosmétique. Ce script n'implémente donc volontairement qu'une
seule contrainte.

Usage : python3 export_model_artifacts.py
Produit : model_artifacts/<league>.json pour chaque ligue présente dans
          le CSV multi-ligues.
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd

import numpy as np
from scipy.stats import poisson
from scipy.optimize import minimize

RAW_FEATURES_FILE = "data/all_leagues_raw_with_stats.csv"
OUTPUT_DIR = Path("model_artifacts")
XI = 0.0018
L2_REG = 0.05  # valeur validée précédemment (cf. grid search sur Ligue 1)


def _tau_vec(x, y, lam, mu, rho):
    out = np.ones_like(x, dtype=float)
    m00 = (x == 0) & (y == 0)
    out[m00] = 1 - lam[m00] * mu[m00] * rho
    m01 = (x == 0) & (y == 1)
    out[m01] = 1 + lam[m01] * rho
    m10 = (x == 1) & (y == 0)
    out[m10] = 1 + mu[m10] * rho
    m11 = (x == 1) & (y == 1)
    out[m11] = 1 - rho
    return out


class _FastDixonColesL2:
    """
    Version vectorisée numpy de Dixon-Coles, avec régularisation L2 sur
    attack/defense uniquement (home_advantage et rho ne sont jamais
    régularisés, car estimés sur l'intégralité de l'historique, jamais en
    régime faible-échantillon).

    Nécessaire ici (plutôt que la classe de référence DixonColesModel) car
    celle-ci boucle en Python pur match par match : un entraînement complet
    sur une ligue (~2000 matchs) dépasse largement le temps raisonnable
    pour un job de rafraîchissement périodique. Cette version fait le même
    calcul mathématique en une fraction de seconde.
    """

    def __init__(self, xi: float = XI, l2_reg: float = L2_REG):
        self.xi = xi
        self.l2_reg = l2_reg
        self.teams_, self.attack_, self.defense_ = [], {}, {}
        self.home_advantage_, self.rho_ = 0.0, 0.0

    def fit(self, matches: pd.DataFrame, reference_date=None):
        matches = matches.copy()
        matches["date"] = pd.to_datetime(matches["date"])
        reference_date = reference_date or matches["date"].max()

        teams = sorted(set(matches["home_team"]) | set(matches["away_team"]))
        team_idx = {t: i for i, t in enumerate(teams)}
        n = len(teams)

        home_idx = matches["home_team"].map(team_idx).values
        away_idx = matches["away_team"].map(team_idx).values
        hg = matches["home_goals"].values.astype(float)
        ag = matches["away_goals"].values.astype(float)
        days = (reference_date - matches["date"]).dt.days.clip(lower=0).values
        weights = np.exp(-self.xi * days)

        def negative_log_likelihood(params):
            attack, defense = params[:n], params[n:2 * n]
            gamma, rho = params[2 * n], params[2 * n + 1]
            lam = np.exp(attack[home_idx] - defense[away_idx] + gamma)
            mu = np.exp(attack[away_idx] - defense[home_idx])
            p1 = np.clip(poisson.pmf(hg, lam), 1e-10, None)
            p2 = np.clip(poisson.pmf(ag, mu), 1e-10, None)
            t = np.clip(_tau_vec(hg, ag, lam, mu, rho), 1e-10, None)
            log_lik = np.sum(weights * (np.log(p1) + np.log(p2) + np.log(t)))
            penalty = self.l2_reg * (np.sum(attack ** 2) + np.sum(defense ** 2))
            return -log_lik + penalty

        init_params = np.concatenate([np.zeros(n), np.zeros(n), [0.2], [-0.05]])
        # Contrainte d'identifiabilité unique — voir note en tête de fichier.
        constraints = [{"type": "eq", "fun": lambda p: np.mean(p[:n])}]

        result = minimize(
            negative_log_likelihood, init_params, constraints=constraints,
            method="SLSQP", options={"maxiter": 200, "ftol": 1e-8},
        )
        if not result.success:
            raise RuntimeError(f"Optimisation non convergée : {result.message}")

        self.teams_ = teams
        self.attack_ = dict(zip(teams, result.x[:n]))
        self.defense_ = dict(zip(teams, result.x[n:2 * n]))
        self.home_advantage_ = result.x[2 * n]
        self.rho_ = result.x[2 * n + 1]
        return self


def export_league(league_name: str, matches: pd.DataFrame) -> dict:
    """Entraîne le modèle sur tout l'historique disponible d'une ligue et
    retourne un dict directement sérialisable en JSON."""
    model = _FastDixonColesL2(xi=XI, l2_reg=L2_REG)
    model.fit(matches)

    return {
        "league": league_name,
        "xi": model.xi,
        "l2_reg": model.l2_reg,
        "home_advantage": model.home_advantage_,
        "rho": model.rho_,
        "attack": model.attack_,
        "defense": model.defense_,
        "teams": model.teams_,
        "trained_on_matches": len(matches),
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "data_up_to": matches["date"].max().isoformat(),
    }


def train_all_leagues(raw_file: str = RAW_FEATURES_FILE) -> dict:
    """
    Entraîne les 5 ligues et retourne {league_name: artifact_dict} EN
    MÉMOIRE, sans rien écrire sur disque. Séparé de main() pour que
    refresh_and_retrain.py puisse valider chaque artefact AVANT de
    remplacer les fichiers JSON existants (jamais d'écriture partielle
    d'un lot potentiellement invalide).
    """
    df = pd.read_csv(raw_file, parse_dates=["date"])

    artifacts = {}
    for league_name, sub in df.groupby("league"):
        sub = sub.sort_values("date").reset_index(drop=True)
        artifacts[league_name] = export_league(league_name, sub)
    return artifacts


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    artifacts = train_all_leagues()

    for league_name, artifact in artifacts.items():
        print(f"Entraînement {league_name} ({artifact['trained_on_matches']} matchs)...")
        out_path = OUTPUT_DIR / f"{league_name}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(artifact, f, ensure_ascii=False, indent=2)
        print(f"  -> {out_path} "
              f"(home_advantage={artifact['home_advantage']:.4f}, "
              f"rho={artifact['rho']:.4f}, {len(artifact['teams'])} équipes)")

    print(f"\nTerminé. {len(list(OUTPUT_DIR.glob('*.json')))} fichiers dans {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
