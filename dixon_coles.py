"""
Dixon-Coles Model — Implémentation professionnelle
====================================================

Contexte : moteur de prédiction pour SaaS d'analyse statistique football.

Ce module implémente le modèle Dixon & Coles (1997) tel qu'utilisé en production
en analyse quantitative sportive, avec les 3 briques essentielles que les
implémentations "jouet" oublient presque toujours :

1. Pondération temporelle (xi/decay) — un match d'il y a 2 ans ne doit PAS peser
   autant qu'un match d'il y a 2 semaines. Sans ça, le modèle est structurellement
   en retard sur la forme actuelle des équipes.

2. Correction rho (tau) — Poisson bivariée nue suppose indépendance buts dom/ext,
   ce qui est FAUX empiriquement sur les scores bas (0-0, 1-0, 0-1, 1-1). Sans la
   correction, le modèle sur-estime systématiquement ces scores exacts et les
   probabilités de nul.

3. Calibration — un modèle qui a un bon "hit rate" mais n'est pas calibré (proba
   annoncée à 60% qui ne se réalise que 45% du temps) est statistiquement peu
   fiable, même s'il "devine" souvent juste. C'est LE point que les
   implémentations académiques ignorent le plus souvent.

4. Régularisation L2 (prior bayésien) — sans elle, une équipe avec peu de matchs
   pondérés par le decay temporel (ex : reléguée, ou juste montée) peut recevoir
   un rating extrême sur la seule foi de 2-3 résultats surprenants. Le prior
   gaussien centré sur 0 tire ces ratings peu fiables vers la moyenne de la
   ligue, proportionnellement à l'incertitude (peu de matchs pondérés => prior
   qui domine ; beaucoup de matchs => vraisemblance qui domine).

Auteur : assistant technique — usage : projet SaaS d'analyse statistique football (Aimé)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import poisson
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ---------------------------------------------------------------------------
# 1. Structure des données attendues
# ---------------------------------------------------------------------------
#
# Le modèle attend un DataFrame avec au minimum les colonnes :
#   date         (datetime)   — date du match
#   home_team    (str)
#   away_team    (str)
#   home_goals   (int)
#   away_goals   (int)
#
# Exemple minimal fourni en bas de fichier (__main__) pour test immédiat.


@dataclass
class DixonColesModel:
    """
    Modèle Dixon-Coles avec decay temporel, correction rho et prior L2.

    Paramètres appris :
        attack[team]   : force offensive (alpha)
        defense[team]  : force défensive (beta) — plus bas = meilleure défense
        home_advantage : avantage du terrain (gamma), constant sur toute la ligue
        rho            : correction de dépendance basse-marque

    xi : demi-vie de la pondération temporelle, en "1/jours". Valeur typique
         observée en pratique : 0.0018 à 0.0025 (≈ demi-vie de 300-450 jours).
         Plus xi est élevé, plus le modèle "oublie" vite les vieux matchs.

    l2 : force du prior gaussien centré sur 0 appliqué aux paramètres
         attack/defense (PAS à home_advantage ni rho, qui sont des paramètres
         de ligue partagés par tous les matchs et donc déjà bien contraints).
         Équivaut à supposer attack[team], defense[team] ~ N(0, 1/(2*l2)) a
         priori. l2=0 retombe exactement sur le MLE non régularisé d'origine.
         Une équipe avec beaucoup de matchs pondérés (poids de decay élevés)
         voit sa vraisemblance dominer le prior et son rating bouge peu ; une
         équipe avec peu de matchs pondérés (reléguée depuis longtemps, ou
         promue récemment) voit son rating tiré vers 0 proportionnellement à
         son manque de données — exactement le comportement recherché pour
         éviter les ratings extrêmes non fiables.
    """

    xi: float = 0.0018
    l2: float = 0.05
    teams_: list = field(default_factory=list)
    attack_: dict = field(default_factory=dict)
    defense_: dict = field(default_factory=dict)
    home_advantage_: float = 0.0
    rho_: float = 0.0
    fit_date_: Optional[datetime] = None

    # -- Fonctions de base -------------------------------------------------

    @staticmethod
    def _tau(x: int, y: int, lambda_x: float, mu_y: float, rho: float) -> float:
        """
        Correction de Dixon-Coles pour les scores bas.
        Cette fonction ajuste la probabilité jointe uniquement pour
        les combinaisons (0,0), (0,1), (1,0), (1,1) — partout ailleurs
        la correction vaut 1 (pas d'ajustement).
        """
        if x == 0 and y == 0:
            return 1 - lambda_x * mu_y * rho
        elif x == 0 and y == 1:
            return 1 + lambda_x * rho
        elif x == 1 and y == 0:
            return 1 + mu_y * rho
        elif x == 1 and y == 1:
            return 1 - rho
        else:
            return 1.0

    def _time_weight(self, match_date, reference_date) -> float:
        """Poids exponentiel décroissant : matchs récents comptent plus."""
        days_diff = (reference_date - match_date).days
        days_diff = max(days_diff, 0)
        return np.exp(-self.xi * days_diff)

    # -- Log-vraisemblance à maximiser --------------------------------------

    def _negative_log_likelihood(self, params, data, teams, reference_date):
        n = len(teams)
        attack = dict(zip(teams, params[:n]))
        defense = dict(zip(teams, params[n:2 * n]))
        home_adv = params[2 * n]
        rho = params[2 * n + 1]

        log_lik = 0.0
        for row in data.itertuples():
            lam = np.exp(attack[row.home_team] - defense[row.away_team] + home_adv)
            mu = np.exp(attack[row.away_team] - defense[row.home_team])

            p_home_goals = max(poisson.pmf(row.home_goals, lam), 1e-10)
            p_away_goals = max(poisson.pmf(row.away_goals, mu), 1e-10)
            tau = self._tau(row.home_goals, row.away_goals, lam, mu, rho)

            # Garde-fou numérique : tau peut devenir négatif ou nul pour des
            # rho mal calibrés en cours d'optimisation, et les probas Poisson
            # peuvent s'écraser à 0 en float pour des lambda/mu extrêmes
            # explorés temporairement par l'optimiseur -> on plafonne les
            # trois quantités pour éviter log(0) ou log(négatif).
            tau = max(tau, 1e-10)

            weight = self._time_weight(row.date, reference_date)
            log_lik += weight * (np.log(p_home_goals) + np.log(p_away_goals) + np.log(tau))

        # Régularisation L2 (prior gaussien centré sur 0) sur attack/defense
        # uniquement — home_advantage et rho ne sont pas régularisés car ce
        # sont des paramètres de ligue déjà contraints par l'ensemble des
        # matchs, pas par équipe (pas de risque de sur-ajustement par manque
        # d'échantillon comme pour une équipe isolée).
        l2_penalty = self.l2 * np.sum(params[:2 * n] ** 2)

        return -log_lik + l2_penalty

    # -- Entraînement --------------------------------------------------------

    def fit(self, matches: pd.DataFrame, reference_date: Optional[datetime] = None):
        """
        Entraîne le modèle sur un historique de matchs.

        matches : DataFrame avec colonnes date, home_team, away_team,
                  home_goals, away_goals
        reference_date : date à partir de laquelle le decay est calculé
                          (par défaut : date la plus récente du dataset,
                          typiquement "aujourd'hui" en usage réel)
        """
        matches = matches.copy()
        matches["date"] = pd.to_datetime(matches["date"])
        reference_date = reference_date or matches["date"].max()

        teams = sorted(set(matches["home_team"]) | set(matches["away_team"]))
        n = len(teams)

        # Initialisation : attack/defense à 0, home_advantage à 0.2 (valeur
        # de départ raisonnable, l'avantage du terrain historique tourne
        # généralement autour de +30% à +40% de buts en plus à domicile),
        # rho à -0.05 (valeur de départ typique, sera réestimée).
        init_params = np.concatenate([
            np.zeros(n),       # attack
            np.zeros(n),       # defense
            [0.2],              # home_advantage
            [-0.05],            # rho
        ])

        # Contrainte d'identifiabilité : la SEULE transformation qui laisse
        # la vraisemblance inchangée est de décaler TOUS les attack ET TOUS
        # les defense de la MÊME constante c (lam = exp(attack_h - defense_a
        # + home_adv) et mu = exp(attack_a - defense_h) sont tous deux
        # invariants sous ce décalage simultané ; décaler attack seul,
        # defense seul, ou les deux de constantes différentes change lam ou
        # mu). Avec ≥3 équipes, c'est l'unique degré de liberté non identifié
        # du modèle — une seule contrainte linéaire suffit donc à le fixer
        # complètement, y compris pour les valeurs individuelles de defense.
        #
        # Vérifié empiriquement (voir historique du projet) : sous cette
        # seule contrainte, mean(defense) ne tombe PAS à 0 (ex. ≈ -0.20 sur
        # Ligue 1) — mais ce n'est pas un signe de sous-identification. La
        # quantité mean(attack) - mean(defense) est invariante le long de
        # l'unique direction de jauge (elle ne dépend pas de c), donc dès
        # que mean(attack)=0 est imposé, mean(defense) est entièrement
        # déterminé par les données, pas laissé libre. Ajouter une DEUXIÈME
        # contrainte indépendante mean(defense)=0 sur-détermine le système
        # (il n'y a qu'un seul degré de liberté à fixer, pas deux) : testé
        # empiriquement, ça dégrade l'objectif régularisé (+2.8 sur Ligue 1)
        # et déplace les net ratings (attack-defense) de chaque équipe de
        # jusqu'à ~3 unités log — un biais réel, pas un simple recentrage.
        constraints = [{
            "type": "eq",
            "fun": lambda p, n=n: np.mean(p[:n]),
        }]

        result = minimize(
            self._negative_log_likelihood,
            init_params,
            args=(matches, teams, reference_date),
            constraints=constraints,
            method="SLSQP",
            options={"maxiter": 200, "ftol": 1e-8},
        )

        if not result.success:
            raise RuntimeError(f"Optimisation non convergée : {result.message}")

        self.teams_ = teams
        self.attack_ = dict(zip(teams, result.x[:n]))
        self.defense_ = dict(zip(teams, result.x[n:2 * n]))
        self.home_advantage_ = result.x[2 * n]
        self.rho_ = result.x[2 * n + 1]
        self.fit_date_ = reference_date

        return self

    # -- Prédiction ------------------------------------------------------

    def predict_score_matrix(self, home_team: str, away_team: str, max_goals: int = 8) -> np.ndarray:
        """
        Retourne une matrice (max_goals+1) x (max_goals+1) des probabilités
        de chaque score exact, avec correction rho appliquée.
        """
        self._check_fitted()
        self._check_teams(home_team, away_team)

        lam = np.exp(self.attack_[home_team] - self.defense_[away_team] + self.home_advantage_)
        mu = np.exp(self.attack_[away_team] - self.defense_[home_team])

        matrix = np.zeros((max_goals + 1, max_goals + 1))
        for x in range(max_goals + 1):
            for y in range(max_goals + 1):
                p = poisson.pmf(x, lam) * poisson.pmf(y, mu)
                p *= self._tau(x, y, lam, mu, self.rho_)
                matrix[x, y] = max(p, 0)  # garde-fou : pas de proba négative

        matrix /= matrix.sum()  # renormalisation après correction rho
        return matrix

    def predict_1x2(self, home_team: str, away_team: str, max_goals: int = 8) -> dict:
        """Probabilités Victoire domicile / Nul / Victoire extérieur."""
        matrix = self.predict_score_matrix(home_team, away_team, max_goals)
        home_win = np.tril(matrix, -1).sum()
        draw = np.trace(matrix)
        away_win = np.triu(matrix, 1).sum()
        return {"home_win": home_win, "draw": draw, "away_win": away_win}

    def predict_over_under(self, home_team: str, away_team: str, line: float = 2.5,
                            max_goals: int = 8) -> dict:
        """Probabilité que le total de buts du match soit au-dessus / en-dessous d'un seuil donné (ex: 2.5)."""
        matrix = self.predict_score_matrix(home_team, away_team, max_goals)
        total_goals = np.add.outer(np.arange(max_goals + 1), np.arange(max_goals + 1))
        over = matrix[total_goals > line].sum()
        under = matrix[total_goals < line].sum()
        return {"over": over, "under": under}

    def team_ratings(self) -> pd.DataFrame:
        """Classement des équipes par force nette (attaque - défense)."""
        self._check_fitted()
        df = pd.DataFrame({
            "team": self.teams_,
            "attack": [self.attack_[t] for t in self.teams_],
            "defense": [self.defense_[t] for t in self.teams_],
        })
        df["net_rating"] = df["attack"] - df["defense"]
        return df.sort_values("net_rating", ascending=False).reset_index(drop=True)

    # -- Garde-fous --------------------------------------------------------

    def _check_fitted(self):
        if not self.teams_:
            raise RuntimeError("Le modèle n'est pas encore entraîné. Appelle .fit() d'abord.")

    def _check_teams(self, home_team, away_team):
        for t in (home_team, away_team):
            if t not in self.attack_:
                raise ValueError(
                    f"Équipe inconnue du modèle : '{t}'. "
                    f"Équipes disponibles : {self.teams_}"
                )


# ---------------------------------------------------------------------------
# 2. Validation par calibration — INDISPENSABLE avant toute mise en prod
# ---------------------------------------------------------------------------

def calibration_check(model: DixonColesModel, test_matches: pd.DataFrame, n_bins: int = 10) -> pd.DataFrame:
    """
    Vérifie que les probabilités prédites correspondent aux fréquences réelles.

    Un modèle "bien classé" (bon AUC) peut quand même être mal calibré.
    C'est la calibration — pas le pouvoir discriminant seul — qui détermine
    si les probabilités annoncées sont fiables : si le modèle prédit 60% de
    victoire domicile et que ça n'arrive que 45% du temps, les probabilités
    affichées sont trompeuses même si le modèle "devine" juste plus souvent
    que le hasard.

    Retourne un tableau bin par bin : proba prédite moyenne vs fréquence réelle.
    """
    predicted_probs = []
    outcomes = []

    for row in test_matches.itertuples():
        try:
            probs = model.predict_1x2(row.home_team, row.away_team)
        except ValueError:
            continue  # équipe absente du set d'entraînement, on skip

        predicted_probs.append(probs["home_win"])
        outcomes.append(1 if row.home_goals > row.away_goals else 0)

    df = pd.DataFrame({"predicted": predicted_probs, "actual": outcomes})
    df["bin"] = pd.cut(df["predicted"], bins=n_bins)
    summary = df.groupby("bin", observed=True).agg(
        predicted_mean=("predicted", "mean"),
        actual_freq=("actual", "mean"),
        n_matches=("actual", "count"),
    ).reset_index()

    return summary


# ---------------------------------------------------------------------------
# 3. Sélection de l2 par grid search — critère : log-loss out-of-sample
# ---------------------------------------------------------------------------

def grid_search_l2(train_matches: pd.DataFrame, test_matches: pd.DataFrame,
                    l2_values=(0.0, 0.01, 0.02, 0.05, 0.1, 0.2),
                    xi: float = 0.0018,
                    reference_date: Optional[datetime] = None) -> pd.DataFrame:
    """
    Grid search sur la force du prior L2 (attack/defense), évaluée par
    log-loss 1X2 (victoire domicile / nul / victoire extérieur) sur un split
    temporel out-of-sample — pas un split aléatoire, pour ne pas laisser
    fuiter d'information future vers le passé (même logique que le split
    train/test de train_on_ligue1.py).

    Pourquoi le log-loss et pas le hit-rate : le hit-rate récompense un
    modèle qui prédit souvent l'issue la plus probable, même avec des
    probabilités mal calibrées (ex: toujours 90% pour le favori). Le
    log-loss pénalise lourdement une probabilité annoncée haute sur l'issue
    qui ne se réalise pas — c'est la métrique qui reflète directement la
    fiabilité statistique des probabilités, comme pour calibration_check
    ci-dessus.

    train_matches, test_matches : DataFrames au format attendu par .fit()
                                   (typiquement df.iloc[:-100] / df.iloc[-100:])
    l2_values : valeurs de l2 à tester
    xi        : decay temporel, fixé pendant la recherche (on ne grid-search
                que l2 ici — croiser xi et l2 est possible mais pas demandé)
    reference_date : passée telle quelle à .fit() pour chaque modèle

    Retourne un DataFrame trié par log-loss croissant (meilleur en premier),
    avec une colonne 'recommended' marquant la ligne au log-loss minimal.
    """
    rows = []
    for l2 in l2_values:
        model = DixonColesModel(xi=xi, l2=l2)
        model.fit(train_matches, reference_date=reference_date)

        neg_log_probs = []
        for row in test_matches.itertuples():
            try:
                probs = model.predict_1x2(row.home_team, row.away_team)
            except ValueError:
                continue  # équipe absente du set d'entraînement, on skip

            if row.home_goals > row.away_goals:
                p = probs["home_win"]
            elif row.home_goals == row.away_goals:
                p = probs["draw"]
            else:
                p = probs["away_win"]
            neg_log_probs.append(-np.log(max(p, 1e-10)))

        rows.append({
            "l2": l2,
            "logloss": float(np.mean(neg_log_probs)) if neg_log_probs else np.nan,
            "n_matches": len(neg_log_probs),
        })

    results = pd.DataFrame(rows).sort_values("logloss", ascending=True).reset_index(drop=True)
    results["recommended"] = False
    if results["logloss"].notna().any():
        results.loc[results["logloss"].idxmin(), "recommended"] = True

    return results


# ---------------------------------------------------------------------------
# 3. Exemple exécutable — jeu de données jouet pour valider que tout tourne
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Jeu de données minimal pour vérifier que le pipeline tourne de bout
    # en bout. À remplacer par un historique réel (Sportmonks) pour un
    # entraînement significatif — 3-5 saisons par ligue est le minimum
    # raisonnable en pratique.
    np.random.seed(42)
    teams = ["PSG", "Marseille", "Lyon", "Monaco", "Lille", "Rennes"]
    rows = []
    base_date = datetime(2024, 8, 1)
    for i in range(300):
        home, away = np.random.choice(teams, 2, replace=False)
        rows.append({
            "date": base_date + pd.Timedelta(days=i),
            "home_team": home,
            "away_team": away,
            "home_goals": np.random.poisson(1.5),
            "away_goals": np.random.poisson(1.1),
        })
    df = pd.DataFrame(rows)

    train = df.iloc[:250]
    test = df.iloc[250:]

    model = DixonColesModel(xi=0.0018)
    model.fit(train)

    print("=== Classement (force nette attaque - défense) ===")
    print(model.team_ratings().to_string(index=False))

    print("\n=== Exemple de prédiction : PSG (domicile) vs Marseille ===")
    print("1X2       :", model.predict_1x2("PSG", "Marseille"))
    print("Plus/Moins de 2.5 buts :", model.predict_over_under("PSG", "Marseille", line=2.5))

    print("\n=== Vérification de calibration (sur données de test) ===")
    print(calibration_check(model, test, n_bins=5).to_string(index=False))

    print(f"\nParamètre home_advantage appris : {model.home_advantage_:.4f}")
    print(f"Paramètre rho (correction basse-marque) appris : {model.rho_:.4f}")
