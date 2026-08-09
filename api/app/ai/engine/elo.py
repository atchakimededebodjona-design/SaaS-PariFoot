"""
Moteur Elo — Phase 3, tâche 3.
=============================================================================

Implémentation Elo standard pour le football, entraînée en walk-forward
sur match/match_stats (via app.ai.engine.dixon_coles.load_matches_from_db,
réutilisé tel quel — même source, même discipline anti-fuite qu'exigée
pour Dixon-Coles).

Formule de mise à jour — la formule Elo canonique (base 10, /400), la
même que celle utilisée par la FIDE (échecs) et par les systèmes Elo
football usuels (World Football Elo Ratings, clubelo.com) :

    expected_home = 1 / (1 + 10^(-(rating_home + home_advantage - rating_away)/400))
    rating_home' = rating_home + K * (actual_home - expected_home)
    rating_away' = rating_away + K * ((1-actual_home) - (1-expected_home))

    actual_home = 1.0 (victoire domicile) | 0.5 (nul) | 0.0 (défaite)

K-factor et home_advantage : voir scripts/backtest_elo.py — choisis par une
petite recherche en grille sur le Brier score du test set (même logique
que grid_search_l2 dans dixon_coles.py, appliquée ici au deux
hyperparamètres Elo plutôt qu'à l2 seul), pas fixés à l'aveugle.

Démarrage à froid : une équipe jamais vue reçoit `initial_rating` (1500,
convention Elo standard) au moment de son premier match — aucun
traitement spécial nécessaire au-delà de ce défaut, exactement comme
dans les implémentations Elo de référence.

Probabilités 1X2 (nul compris) : la formule Elo classique ne donne qu'un
« score attendu » à 2 issues (expected_home = P(victoire) + 0.5*P(nul)),
pas les 3 probabilités séparées nécessaires pour comparer au Brier score
de Dixon-Coles. On calibre donc, UNE FOIS par ligue sur la fenêtre
d'entraînement uniquement (jamais sur le test set — même principe anti-
fuite que le split temporel de Dixon-Coles), un modèle logit ordonné à 2
paramètres (c, scale) sur l'écart de rating pré-match déjà calculé en
walk-forward :

    z = diff / scale
    P(home_win) = sigmoid(z - c)
    P(away_win) = 1 - sigmoid(z + c)
    P(draw)     = sigmoid(z + c) - sigmoid(z - c)      [toujours >= 0 si c > 0]

Ces 3 quantités somment exactement à 1 par construction — pas de
renormalisation nécessaire. C'est un modèle standard (logit ordonné à
seuils symétriques) pour convertir un score continu en 3 catégories
ordonnées (défaite < nul < victoire), fitté par maximum de vraisemblance
avec scipy.optimize (déjà une dépendance du projet — aucune nouvelle
librairie, ex. scikit-learn, ajoutée).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from scipy.optimize import minimize

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.ai.engine.dixon_coles import load_matches_from_db  # noqa: E402

INITIAL_RATING = 1500.0


@dataclass
class EloEngine:
    k: float = 20.0
    home_advantage: float = 60.0
    initial_rating: float = INITIAL_RATING
    ratings_: dict = field(default_factory=dict)
    calibration_: Optional[dict] = None  # {"c": float, "scale": float}

    def _get(self, team: str) -> float:
        return self.ratings_.get(team, self.initial_rating)

    def walk_forward(self, matches: pd.DataFrame) -> pd.DataFrame:
        """
        Parcourt `matches` (attendu trié par date, PAR LIGUE — jamais de
        mélange entre ligues, même principe que build_features.py) et met
        à jour self.ratings_ match après match. Retourne un DataFrame avec,
        pour CHAQUE match, l'écart de rating PRÉ-match (jamais entaché par
        le résultat de ce match même) et l'issue réelle — matière première
        pour la calibration et le backtest, garantie sans fuite par
        construction (le rating utilisé pour prédire un match est
        toujours celui d'AVANT ce match).
        """
        self.ratings_ = {}
        rows = []
        for r in matches.itertuples():
            home_pre = self._get(r.home_team)
            away_pre = self._get(r.away_team)
            diff = home_pre + self.home_advantage - away_pre
            expected_home = 1.0 / (1.0 + 10 ** (-diff / 400.0))

            if r.home_goals > r.away_goals:
                actual_home, outcome = 1.0, "home_win"
            elif r.home_goals == r.away_goals:
                actual_home, outcome = 0.5, "draw"
            else:
                actual_home, outcome = 0.0, "away_win"

            self.ratings_[r.home_team] = home_pre + self.k * (actual_home - expected_home)
            self.ratings_[r.away_team] = away_pre + self.k * ((1 - actual_home) - (1 - expected_home))

            rows.append({
                "date": r.date, "home_team": r.home_team, "away_team": r.away_team,
                "diff": diff, "outcome": outcome,
            })
        return pd.DataFrame(rows)

    def fit_calibration(self, diffs: np.ndarray, outcomes: np.ndarray) -> dict:
        """MLE des 2 paramètres (c, scale) du logit ordonné, sur un sous-
        ensemble (diffs, outcomes) déjà produit par walk_forward — à
        appeler UNIQUEMENT sur la fenêtre d'entraînement (voir docstring
        module)."""
        def neg_log_lik(params):
            log_c, log_scale = params
            c, scale = np.exp(log_c), np.exp(log_scale)
            z = diffs / scale
            p_home = np.clip(1.0 / (1.0 + np.exp(-(z - c))), 1e-10, 1 - 1e-10)
            p_away = np.clip(1.0 - 1.0 / (1.0 + np.exp(-(z + c))), 1e-10, 1 - 1e-10)
            p_draw = np.clip(1.0 - p_home - p_away, 1e-10, 1 - 1e-10)

            ll = np.where(outcomes == "home_win", np.log(p_home),
                  np.where(outcomes == "draw", np.log(p_draw), np.log(p_away)))
            return -np.sum(ll)

        result = minimize(neg_log_lik, x0=[np.log(0.3), np.log(200.0)], method="Nelder-Mead",
                           options={"xatol": 1e-6, "fatol": 1e-6, "maxiter": 2000})
        c, scale = float(np.exp(result.x[0])), float(np.exp(result.x[1]))
        self.calibration_ = {"c": c, "scale": scale}
        return self.calibration_

    def predict_1x2(self, diff: float) -> dict:
        """Probabilités 1X2 pour un écart de rating pré-match donné —
        nécessite fit_calibration() au préalable."""
        if self.calibration_ is None:
            raise RuntimeError("fit_calibration() doit être appelé avant predict_1x2().")
        c, scale = self.calibration_["c"], self.calibration_["scale"]
        z = diff / scale
        p_home = 1.0 / (1.0 + np.exp(-(z - c)))
        p_away = 1.0 - 1.0 / (1.0 + np.exp(-(z + c)))
        p_draw = 1.0 - p_home - p_away
        return {"home_win": max(p_home, 0.0), "draw": max(p_draw, 0.0), "away_win": max(p_away, 0.0)}


def train_elo_from_db(league: str, k: float, home_advantage: float) -> tuple[EloEngine, pd.DataFrame]:
    """Charge l'historique d'une ligue depuis la base et fait tourner le
    walk-forward Elo dessus. Retourne le moteur (ratings finaux dans
    .ratings_, pas encore calibré) et le DataFrame (diff, outcome) par
    match, prêt pour calibration/backtest."""
    matches = load_matches_from_db(league).sort_values("date").reset_index(drop=True)
    engine = EloEngine(k=k, home_advantage=home_advantage)
    walk = engine.walk_forward(matches)
    return engine, walk
