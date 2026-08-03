"""
Réimplémentation vectorisée (numpy) du modèle Dixon-Coles de dixon_coles.py
=============================================================================

Même formule exacte (tau, decay temporel, prior L2) que DixonColesModel,
mais vectorisée avec numpy au lieu d'une boucle Python pur sur chaque match.
Utilité : la classe officielle prend ~110s par fit sur 821 matchs (boucle
Python + gradient SLSQP par différences finies) — beaucoup trop lent dès
qu'on a besoin de dizaines/centaines de fits (bootstrap, grid search, ou
walk-forward feature engineering match par match). Cette version fait le
même fit en ~0.3-0.5s.

Équivalence vérifiée dans validate_l2_regularization.sanity_check() :
écart maximal de 1e-5 sur les net_ratings et les paramètres appris (home
advantage, rho) par rapport à la classe officielle, sur les mêmes données.
Ce module n'est PAS la classe de production — dixon_coles.DixonColesModel
reste la référence — mais un raccourci numériquement équivalent pour tout
ce qui demande un grand nombre de fits.
"""

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import poisson


def _tau_vec(x, y, lam, mu, rho):
    tau = np.ones_like(lam)
    m00 = (x == 0) & (y == 0)
    m01 = (x == 0) & (y == 1)
    m10 = (x == 1) & (y == 0)
    m11 = (x == 1) & (y == 1)
    tau = np.where(m00, 1 - lam * mu * rho, tau)
    tau = np.where(m01, 1 + lam * rho, tau)
    tau = np.where(m10, 1 + mu * rho, tau)
    tau = np.where(m11, 1 - rho, tau)
    return tau


def fast_fit(matches: pd.DataFrame, xi: float, l2: float, reference_date=None, teams=None):
    matches = matches.copy()
    matches["date"] = pd.to_datetime(matches["date"])
    reference_date = reference_date or matches["date"].max()

    if teams is None:
        teams = sorted(set(matches["home_team"]) | set(matches["away_team"]))
    n = len(teams)
    idx = {t: i for i, t in enumerate(teams)}

    home_idx = matches["home_team"].map(idx).to_numpy()
    away_idx = matches["away_team"].map(idx).to_numpy()
    hg = matches["home_goals"].to_numpy(dtype=float)
    ag = matches["away_goals"].to_numpy(dtype=float)
    days_diff = (reference_date - matches["date"]).dt.days.clip(lower=0).to_numpy()
    weights = np.exp(-xi * days_diff)

    def nll(params):
        attack = params[:n]
        defense = params[n:2 * n]
        home_adv = params[2 * n]
        rho = params[2 * n + 1]

        lam = np.exp(attack[home_idx] - defense[away_idx] + home_adv)
        mu = np.exp(attack[away_idx] - defense[home_idx])

        p_home = np.maximum(poisson.pmf(hg, lam), 1e-10)
        p_away = np.maximum(poisson.pmf(ag, mu), 1e-10)
        tau = np.maximum(_tau_vec(hg, ag, lam, mu, rho), 1e-10)

        log_lik = np.sum(weights * (np.log(p_home) + np.log(p_away) + np.log(tau)))
        l2_penalty = l2 * np.sum(params[:2 * n] ** 2)
        return -log_lik + l2_penalty

    init_params = np.concatenate([np.zeros(n), np.zeros(n), [0.2], [-0.05]])
    # Contrainte d'identifiabilité — voir la note détaillée dans
    # dixon_coles.DixonColesModel.fit(). En résumé : seule la transformation
    # attack_i += c, defense_i += c (même c pour toutes les équipes,
    # simultanément) laisse lam/mu invariants, donc une seule contrainte
    # linéaire (ici mean(attack)=0) identifie déjà complètement le modèle,
    # attack ET defense. mean(defense) qui en résulte n'est PAS 0 en général
    # (c'est mean(attack)-mean(defense), invariant le long de cette unique
    # direction, qui fixe sa valeur) — ce n'est pas un défaut d'identifiabilité
    # restant. Ajouter une deuxième contrainte mean(defense)=0 sur-détermine
    # le système (testé : dégrade l'objectif régularisé et déplace les net
    # ratings de ~3 unités log sur Ligue 1) et n'a donc PAS été ajoutée.
    constraints = [{"type": "eq", "fun": lambda p, n=n: np.mean(p[:n])}]

    result = minimize(
        nll, init_params, constraints=constraints,
        method="SLSQP", options={"maxiter": 200, "ftol": 1e-8},
    )
    if not result.success:
        raise RuntimeError(f"Optimisation non convergée : {result.message}")

    attack = dict(zip(teams, result.x[:n]))
    defense = dict(zip(teams, result.x[n:2 * n]))
    net = {t: attack[t] - defense[t] for t in teams}
    return {
        "teams": teams,
        "attack": attack,
        "defense": defense,
        "net": net,
        "home_advantage": result.x[2 * n],
        "rho": result.x[2 * n + 1],
    }


def team_ratings_from_fast(fit_result):
    df = pd.DataFrame({
        "team": fit_result["teams"],
        "attack": [fit_result["attack"][t] for t in fit_result["teams"]],
        "defense": [fit_result["defense"][t] for t in fit_result["teams"]],
    })
    df["net_rating"] = df["attack"] - df["defense"]
    return df.sort_values("net_rating", ascending=False).reset_index(drop=True)


def fast_score_matrix(fit_result, home_team: str, away_team: str, max_goals: int = 8):
    """Même calcul que DixonColesModel.predict_score_matrix, à partir d'un fit fast_fit()."""
    lam = np.exp(fit_result["attack"][home_team] - fit_result["defense"][away_team] + fit_result["home_advantage"])
    mu = np.exp(fit_result["attack"][away_team] - fit_result["defense"][home_team])
    x = np.arange(max_goals + 1)
    ph = poisson.pmf(x, lam)
    pa = poisson.pmf(x, mu)
    matrix = np.outer(ph, pa)
    X, Y = np.meshgrid(x, x, indexing="ij")
    matrix *= _tau_vec(X, Y, lam, mu, fit_result["rho"])
    matrix = np.clip(matrix, 0, None)
    matrix /= matrix.sum()
    return matrix


def fast_predict_1x2(fit_result, home_team: str, away_team: str, max_goals: int = 8) -> dict:
    matrix = fast_score_matrix(fit_result, home_team, away_team, max_goals)
    return {
        "home_win": float(np.tril(matrix, -1).sum()),
        "draw": float(np.trace(matrix)),
        "away_win": float(np.triu(matrix, 1).sum()),
    }


def fast_predict_over_under(fit_result, home_team: str, away_team: str, line: float = 2.5,
                             max_goals: int = 8) -> dict:
    matrix = fast_score_matrix(fit_result, home_team, away_team, max_goals)
    total_goals = np.add.outer(np.arange(max_goals + 1), np.arange(max_goals + 1))
    return {
        "over": float(matrix[total_goals > line].sum()),
        "under": float(matrix[total_goals < line].sum()),
    }
