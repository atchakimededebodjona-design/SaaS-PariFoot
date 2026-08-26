"""
API FastAPI — Prédictions Dixon-Coles
=======================================

Sert des prédictions à partir des paramètres DÉJÀ APPRIS (attack, defense,
home_advantage, rho par ligue), stockés dans model_artifacts/<league>.json
et produits par export_model_artifacts.py.

Volontairement SANS dépendance à scipy.optimize : l'API ne fait que du
calcul de matrice de Poisson (numpy + scipy.stats), donc démarre vite et
répond en quelques millisecondes. Le ré-entraînement (coûteux) est un
processus séparé, déclenché par un job planifié — jamais à la volée dans
une requête HTTP.

Lancement local :
    uvicorn api.main:app --reload --port 8000

Puis : http://localhost:8000/docs pour la documentation interactive.
"""

import dataclasses
import json
import logging
import os
import unicodedata
import difflib
from pathlib import Path
from datetime import date, datetime, timezone, timedelta

import httpx
import numpy as np
from scipy.stats import poisson
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler
from sqlmodel import Session, select

from app.core.database import init_db, engine
from app.core.rate_limit import limiter
from app.auth.router import router as auth_router
from app.auth.security import get_current_user
from app.billing.router import router as billing_router
from app.billing.dependencies import require_active_subscription
from app.models.user import User
from app.models.model_artifact import ModelArtifact
from app.models.prediction_log import PredictionLog
from app.core.api_football_config import API_FOOTBALL_KEY, API_FOOTBALL_BASE_URL, API_FOOTBALL_LEAGUE_IDS
from app.ai.arena.schemas import (
    ArenaPerformanceResponse,
    ArenaBenchmarkResponse,
    ModelPerformanceEntry,
    ModelPredictionRead,
    ModelPredictionListResponse,
)
from app.ai.arena.service import (
    get_models_performance,
    get_models_benchmark,
    get_model_version_detail,
    list_model_predictions,
    get_model_prediction,
    MARKETS as ARENA_MARKETS,
)
from app.ai.arena.prediction_logging import (
    PredictionRecord,
    get_or_create_active_model_version,
    log_prediction,
)
from app.ai.arena.models_common import MatchContext
from app.ai.arena.orchestrator import ModelOrchestrator, default_models
from app.ai.arena.ensemble import build_live_ensemble, WEIGHT_STRATEGIES, DEFAULT_STRATEGY, KNOWN_MODEL_TYPES
from app.ai.arena.availability import compute_model_availability
from app.ai.arena import monitoring as arena_monitoring
from app.ai.arena import retraining as arena_retraining
from app.ai.arena import promotion as arena_promotion
from app.ai.arena import live_validation as arena_live_validation
from app.ai.arena import shadow_comparison as arena_shadow_comparison
from app.models.team_rating import ModelVersion as ArenaModelVersion
from app.models.model_promotion_event import ModelPromotionEvent
from app.auth.admin import require_admin

logger = logging.getLogger("uvicorn.error")

ARTIFACTS_DIR = Path(__file__).parent / "model_artifacts"
MAX_GOALS = 8  # troncature de la matrice de Poisson — au-delà, probabilité négligeable


# ---------------------------------------------------------------------------
# Résolution de noms d'équipes ambigus
# ---------------------------------------------------------------------------
#
# Trois niveaux de résolution, dans l'ordre :
#   1. Correspondance EXACTE (le cas normal, ~0 coût)
#   2. Normalisation (accents, casse, espaces) — "st etienne" -> "St Etienne"
#   3. Alias connus — "PSG" -> "Paris SG", "OM" -> "Marseille"
#   4. Correspondance floue (difflib) — en dernier recours, pour les fautes
#      de frappe ; retourne une suggestion plutôt que de résoudre en
#      silence si la confiance n'est pas suffisante (évite de deviner
#      la mauvaise équipe sans que l'utilisateur s'en rende compte).

# Alias courants observés dans l'usage réel (abréviations médiatiques,
# noms officiels complets vs noms courts utilisés dans les données
# sources). Couvre les 6 ligues du dataset (Ligue1, PremierLeague, LaLiga,
# Bundesliga, SerieA, PrimeiraLiga) — vérifié contre la liste réelle des
# équipes dans model_artifacts/*.json (cf. export_model_artifacts.py).
TEAM_ALIASES = {
    # --- Ligue 1 ---
    "psg": "Paris SG",
    "paris saint germain": "Paris SG",
    "paris saint-germain": "Paris SG",
    "om": "Marseille",
    "marseille om": "Marseille",
    "olympique de marseille": "Marseille",
    "ol": "Lyon",
    "olympique lyonnais": "Lyon",
    "asse": "St Etienne",
    "saint etienne": "St Etienne",
    "saint-etienne": "St Etienne",
    "as saint etienne": "St Etienne",
    "losc": "Lille",
    "losc lille": "Lille",
    "as monaco": "Monaco",
    "stade rennais": "Rennes",
    "ogc nice": "Nice",
    "fc nantes": "Nantes",
    "girondins de bordeaux": "Bordeaux",
    "girondins bordeaux": "Bordeaux",
    "rc strasbourg": "Strasbourg",
    "rc strasbourg alsace": "Strasbourg",
    "toulouse fc": "Toulouse",
    "tfc": "Toulouse",
    "montpellier hsc": "Montpellier",
    "stade de reims": "Reims",
    "rc lens": "Lens",
    "stade brestois": "Brest",
    "paris football club": "Paris FC",
    "fc metz": "Metz",
    "angers sco": "Angers",
    "aj auxerre": "Auxerre",
    "le havre ac": "Le Havre",
    "clermont foot": "Clermont",
    "dijon fco": "Dijon",
    "ac ajaccio": "Ajaccio",
    "amiens sc": "Amiens",
    "nimes olympique": "Nimes",
    "es troyes ac": "Troyes",

    # --- Premier League ---
    "man utd": "Man United",
    "manchester united": "Man United",
    "manchester city": "Man City",
    "man city": "Man City",
    "spurs": "Tottenham",
    "tottenham hotspur": "Tottenham",
    "newcastle united": "Newcastle",
    "wolverhampton": "Wolves",
    "wolverhampton wanderers": "Wolves",
    "west ham united": "West Ham",
    "west bromwich albion": "West Brom",
    "wba": "West Brom",
    "nottingham forest": "Nott'm Forest",
    "notts forest": "Nott'm Forest",
    "brighton and hove albion": "Brighton",
    "brighton & hove albion": "Brighton",
    "leicester city": "Leicester",
    "leeds united": "Leeds",
    "sheffield utd": "Sheffield United",
    "afc bournemouth": "Bournemouth",
    "crystal palace fc": "Crystal Palace",
    "aston villa fc": "Aston Villa",
    "norwich city": "Norwich",
    "ipswich town": "Ipswich",
    "luton town": "Luton",

    # --- LaLiga ---
    "atletico madrid": "Ath Madrid",
    "atletico de madrid": "Ath Madrid",
    "atletico": "Ath Madrid",
    "atleti": "Ath Madrid",
    "real madrid": "Real Madrid",
    "barcelone": "Barcelona",
    "barca": "Barcelona",
    "fc barcelona": "Barcelona",
    "athletic bilbao": "Ath Bilbao",
    "athletic club": "Ath Bilbao",
    "real sociedad": "Sociedad",
    "real betis": "Betis",
    "rayo vallecano": "Vallecano",
    "real valladolid": "Valladolid",
    "real oviedo": "Oviedo",
    "celta vigo": "Celta",
    "celta de vigo": "Celta",
    "espanyol": "Espanol",
    "rcd espanyol": "Espanol",
    "deportivo alaves": "Alaves",
    "ca osasuna": "Osasuna",
    "girona fc": "Girona",
    "rcd mallorca": "Mallorca",
    "villarreal cf": "Villarreal",
    "ud almeria": "Almeria",
    "cadiz cf": "Cadiz",
    "sd eibar": "Eibar",
    "elche cf": "Elche",
    "getafe cf": "Getafe",
    "granada cf": "Granada",
    "sd huesca": "Huesca",
    "ud las palmas": "Las Palmas",
    "cd leganes": "Leganes",
    "levante ud": "Levante",
    "sevilla fc": "Sevilla",
    "valencia cf": "Valencia",

    # --- Bundesliga ---
    "bayern": "Bayern Munich",
    "bayern munchen": "Bayern Munich",
    "bayern münchen": "Bayern Munich",
    "fc bayern": "Bayern Munich",
    "fc bayern munich": "Bayern Munich",
    "dortmund": "Dortmund",
    "borussia dortmund": "Dortmund",
    "bvb": "Dortmund",
    "gladbach": "M'gladbach",
    "monchengladbach": "M'gladbach",
    "mönchengladbach": "M'gladbach",
    "borussia monchengladbach": "M'gladbach",
    "borussia mgladbach": "M'gladbach",
    "leverkusen": "Leverkusen",
    "bayer leverkusen": "Leverkusen",
    "bayer 04 leverkusen": "Leverkusen",
    "leipzig": "RB Leipzig",
    "rb leipzig": "RB Leipzig",
    "eintracht frankfurt": "Ein Frankfurt",
    "frankfurt": "Ein Frankfurt",
    "koln": "FC Koln",
    "köln": "FC Koln",
    "cologne": "FC Koln",
    "1 fc koln": "FC Koln",
    "fc cologne": "FC Koln",
    "hertha berlin": "Hertha",
    "hertha bsc": "Hertha",
    "union berlin": "Union Berlin",
    "1 fc union berlin": "Union Berlin",
    "schalke": "Schalke 04",
    "fc schalke 04": "Schalke 04",
    "vfl wolfsburg": "Wolfsburg",
    "werder": "Werder Bremen",
    "sv werder bremen": "Werder Bremen",
    "vfb stuttgart": "Stuttgart",
    "sc freiburg": "Freiburg",
    "fc augsburg": "Augsburg",
    "hoffenheim": "Hoffenheim",
    "tsg hoffenheim": "Hoffenheim",
    "1899 hoffenheim": "Hoffenheim",
    "mainz 05": "Mainz",
    "fsv mainz": "Mainz",
    "fsv mainz 05": "Mainz",
    "fortuna dusseldorf": "Fortuna Dusseldorf",
    "fortuna düsseldorf": "Fortuna Dusseldorf",
    "dusseldorf": "Fortuna Dusseldorf",
    "arminia bielefeld": "Bielefeld",
    "sc paderborn": "Paderborn",
    "sc paderborn 07": "Paderborn",
    "fc st pauli": "St Pauli",
    "sankt pauli": "St Pauli",
    "vfl bochum": "Bochum",
    "darmstadt 98": "Darmstadt",
    "sv darmstadt 98": "Darmstadt",
    "greuther furth": "Greuther Furth",
    "greuther fürth": "Greuther Furth",
    "spvgg greuther furth": "Greuther Furth",
    "1 fc heidenheim": "Heidenheim",
    "fc heidenheim": "Heidenheim",
    "holstein kiel": "Holstein Kiel",
    "kiel": "Holstein Kiel",
    "hamburger sv": "Hamburg",
    "hsv": "Hamburg",

    # --- Serie A ---
    "juve": "Juventus",
    "inter": "Inter",
    "inter milan": "Inter",
    "internazionale": "Inter",
    "fc internazionale": "Inter",
    "milan ac": "Milan",
    "ac milan": "Milan",
    "as roma": "Roma",
    "roma fc": "Roma",
    "ssc napoli": "Napoli",
    "ss lazio": "Lazio",
    "acf fiorentina": "Fiorentina",
    "viola": "Fiorentina",
    "atalanta bc": "Atalanta",
    "torino fc": "Torino",
    "genoa cfc": "Genoa",
    "uc sampdoria": "Sampdoria",
    "us sassuolo": "Sassuolo",
    "udinese calcio": "Udinese",
    "bologna fc": "Bologna",
    "hellas verona": "Verona",
    "cagliari calcio": "Cagliari",
    "empoli fc": "Empoli",
    "us lecce": "Lecce",
    "parma calcio": "Parma",
    "spezia calcio": "Spezia",
    "venezia fc": "Venezia",
    "ac monza": "Monza",
    "us salernitana": "Salernitana",
    "frosinone calcio": "Frosinone",
    "us cremonese": "Cremonese",
    "fc crotone": "Crotone",
    "benevento calcio": "Benevento",
    "brescia calcio": "Brescia",
    "spal 2013": "Spal",
    "como 1907": "Como",
    "pisa sc": "Pisa",
    "ac pisa": "Pisa",

    # --- Premier League (ajouts promotion 2025-26) ---
    "sunderland afc": "Sunderland",

    # --- Liga Portugal (Primeira Liga) ---
    "sporting": "Sp Lisbon",
    "sporting cp": "Sp Lisbon",
    "sporting clube de portugal": "Sp Lisbon",
    "sporting lisbonne": "Sp Lisbon",
    "sporting lisbon": "Sp Lisbon",
    "scp": "Sp Lisbon",
    "fc porto": "Porto",
    "fcp": "Porto",
    "sl benfica": "Benfica",
    "slb": "Benfica",
    "sc braga": "Sp Braga",
    "braga": "Sp Braga",
    "vitoria de guimaraes": "Guimaraes",
    "vitória de guimarães": "Guimaraes",
    "vitoria guimaraes": "Guimaraes",
    "cd santa clara": "Santa Clara",
    "cd nacional": "Nacional",
    "gil vicente fc": "Gil Vicente",
    "cd tondela": "Tondela",
    "fc famalicao": "Famalicao",
    "fc famalicão": "Famalicao",
    "moreirense fc": "Moreirense",
    "rio ave fc": "Rio Ave",
    "sc farense": "Farense",
    "boavista fc": "Boavista",
    "fc arouca": "Arouca",
    "fc pacos de ferreira": "Pacos Ferreira",
    "pacos de ferreira": "Pacos Ferreira",
    "gd estoril praia": "Estoril",
    "estoril praia": "Estoril",
    "cf estrela da amadora": "Estrela",
    "estrela da amadora": "Estrela",
    "portimonense sc": "Portimonense",
    "cd aves": "Aves",
    "desportivo aves": "Aves",
    "fc vizela": "Vizela",
    "casa pia ac": "Casa Pia",
    "cf belenenses": "Belenenses",
    "os belenenses": "Belenenses",
    "vitoria de setubal": "Setubal",
    "vitória de setúbal": "Setubal",

    # --- MLS ---
    "inter miami cf": "Inter Miami",
    "la galaxy": "Los Angeles Galaxy",
    "lafc": "Los Angeles FC",
    "la fc": "Los Angeles FC",
    "atlanta united": "Atlanta United FC",
    "montreal impact": "CF Montreal",
    "chicago fire fc": "Chicago Fire",
    "dc utd": "DC United",
    "new york city fc": "New York City FC",
    "nyc fc": "New York City FC",
    "nycfc": "New York City FC",
    "red bulls": "New York Red Bulls",
    "ny red bulls": "New York Red Bulls",
    "st louis city": "St. Louis City",
    "saint louis city": "St. Louis City",
    "sporting kc": "Sporting Kansas City",
    "houston dynamo fc": "Houston Dynamo",

    # --- Saudi Pro League ---
    "al hilal": "Al-Hilal Saudi FC",
    "al-hilal": "Al-Hilal Saudi FC",
    "al nassr": "Al-Nassr",
    "al nassr fc": "Al-Nassr",
    "al ittihad": "Al-Ittihad FC",
    "al ahli": "Al-Ahli Jeddah",
    "al ahli saudi": "Al-Ahli Jeddah",
    "al ettifaq": "Al-Ettifaq",
    "al fateh": "Al-Fateh",
    "al shabab fc": "Al Shabab",
    "al taawoun": "Al Taawon",
    "al wehda": "Al Wehda Club",

    # --- Champions/Europa/Conference League ---
    # NB : "psg"/"paris saint germain"/"paris saint-germain" sont déjà alias
    # de "Paris SG" (section Ligue 1 ci-dessus) — ne PAS les redéfinir ici,
    # un alias ne peut viser qu'une seule cible. Le nom exact utilisé par
    # API-Football dans ces 3 coupes est "Paris Saint Germain" (sans tiret),
    # qui se résout déjà tel quel en saisie exacte, sans alias nécessaire.
}


def _normalize(name: str) -> str:
    """Minuscules, accents retirés, espaces normalisés — pour la
    correspondance insensible à la casse/aux accents."""
    nfkd = unicodedata.normalize("NFKD", name)
    without_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(without_accents.lower().split())


def resolve_team_name(input_name: str, known_teams: list) -> dict:
    """
    Tente de résoudre `input_name` vers un nom exact de `known_teams`.

    Retourne toujours un dict avec :
      - resolved (str | None) : le nom exact trouvé, ou None si aucune
        résolution fiable
      - method (str) : "exact" | "normalized" | "alias" | "fuzzy" | "none"
      - suggestions (list[str]) : candidats proches, utile pour un message
        d'erreur actionnable si resolved est None ou si method == "fuzzy"
        avec une confiance jugée insuffisante pour résoudre en silence
    """
    if input_name in known_teams:
        return {"resolved": input_name, "method": "exact", "suggestions": []}

    normalized_input = _normalize(input_name)
    normalized_map = {_normalize(t): t for t in known_teams}
    if normalized_input in normalized_map:
        return {"resolved": normalized_map[normalized_input], "method": "normalized", "suggestions": []}

    if normalized_input in TEAM_ALIASES:
        alias_target = TEAM_ALIASES[normalized_input]
        if alias_target in known_teams:
            return {"resolved": alias_target, "method": "alias", "suggestions": []}

    # Correspondance floue : on ne résout JAMAIS silencieusement en fuzzy
    # (contrairement à normalized/alias qui sont sans ambiguïté) — on
    # retourne toujours les candidats pour que l'appelant décide/affiche
    # une erreur explicite plutôt que de prédire un match sur une mauvaise
    # équipe sans que l'utilisateur s'en aperçoive.
    close = difflib.get_close_matches(input_name, known_teams, n=3, cutoff=0.6)
    return {"resolved": None, "method": "fuzzy" if close else "none", "suggestions": close}


# ---------------------------------------------------------------------------
# Chargement des modèles par ligue au démarrage (une fois, pas par requête)
# ---------------------------------------------------------------------------

class LeagueModel:
    """Wrapper léger autour des paramètres appris d'une ligue — ne fait que
    du calcul, aucune dépendance à l'entraînement."""

    def __init__(self, artifact: dict):
        self.league = artifact["league"]
        self.attack = artifact["attack"]
        self.defense = artifact["defense"]
        self.home_advantage = artifact["home_advantage"]
        self.rho = artifact["rho"]
        self.teams = artifact["teams"]
        self.trained_at = artifact["trained_at"]
        self.data_up_to = artifact["data_up_to"]

    def _tau(self, x: int, y: int, lam: float, mu: float) -> float:
        if x == 0 and y == 0:
            return 1 - lam * mu * self.rho
        elif x == 0 and y == 1:
            return 1 + lam * self.rho
        elif x == 1 and y == 0:
            return 1 + mu * self.rho
        elif x == 1 and y == 1:
            return 1 - self.rho
        return 1.0

    def score_matrix(self, home_team: str, away_team: str) -> np.ndarray:
        if home_team not in self.attack:
            raise KeyError(home_team)
        if away_team not in self.attack:
            raise KeyError(away_team)

        lam = np.exp(self.attack[home_team] - self.defense[away_team] + self.home_advantage)
        mu = np.exp(self.attack[away_team] - self.defense[home_team])

        matrix = np.zeros((MAX_GOALS + 1, MAX_GOALS + 1))
        for x in range(MAX_GOALS + 1):
            for y in range(MAX_GOALS + 1):
                p = poisson.pmf(x, lam) * poisson.pmf(y, mu) * self._tau(x, y, lam, mu)
                matrix[x, y] = max(p, 0)
        matrix /= matrix.sum()
        return matrix

    def predict_1x2(self, home_team: str, away_team: str) -> dict:
        m = self.score_matrix(home_team, away_team)
        return {
            "home_win": round(float(np.tril(m, -1).sum()), 4),
            "draw": round(float(np.trace(m)), 4),
            "away_win": round(float(np.triu(m, 1).sum()), 4),
        }

    def predict_over_under(self, home_team: str, away_team: str, line: float = 2.5) -> dict:
        m = self.score_matrix(home_team, away_team)
        total = np.add.outer(np.arange(MAX_GOALS + 1), np.arange(MAX_GOALS + 1))
        return {
            "line": line,
            "over": round(float(m[total > line].sum()), 4),
            "under": round(float(m[total < line].sum()), 4),
        }

    def predict_btts(self, home_team: str, away_team: str) -> dict:
        """Both Teams To Score — dérivé de la même matrice de score que
        predict_1x2/predict_over_under, aucun nouveau paramètre appris
        nécessaire : "oui" = P(buts domicile >= 1 ET buts extérieur >= 1)."""
        m = self.score_matrix(home_team, away_team)
        yes = float(m[1:, 1:].sum())
        return {"yes": round(yes, 4), "no": round(1 - yes, 4)}

    def predict_double_chance(self, home_team: str, away_team: str) -> dict:
        """Simple recombinaison des probabilités 1X2 déjà calculées — pas
        un nouveau marché au sens statistique, juste une autre façon de
        lire les mêmes probabilités (1X = domicile ou nul, etc.)."""
        probs = self.predict_1x2(home_team, away_team)
        return {
            "home_or_draw": round(probs["home_win"] + probs["draw"], 4),
            "home_or_away": round(probs["home_win"] + probs["away_win"], 4),
            "draw_or_away": round(probs["draw"] + probs["away_win"], 4),
        }

    def most_likely_scores(self, home_team: str, away_team: str, top_n: int = 5) -> list:
        m = self.score_matrix(home_team, away_team)
        flat_idx = np.argsort(m.ravel())[::-1][:top_n]
        results = []
        for idx in flat_idx:
            x, y = np.unravel_index(idx, m.shape)
            results.append({"home_goals": int(x), "away_goals": int(y), "probability": round(float(m[x, y]), 4)})
        return results

    def predict_match_stats(self, home_team: str, away_team: str) -> dict:
        """Estime les statistiques de match (corners, fautes, pénaltys,
        cartons jaunes/rouges) à partir de la force relative des équipes.

        Le modèle Dixon-Coles fournit les intensités offensives/défensives
        (lambda_home, mu_away). On s'en sert comme facteurs de pondération
        par rapport aux moyennes typiques de ligue :
        - Plus l'intensité offensive est élevée → plus de corners, plus de
          cartons défensifs adverses, plus de pénaltys potentiels.
        - Plus l'intensité défensive adversaire est élevée → plus de fautes.

        Ce n'est pas un modèle séparé entraîné sur ces statistiques, mais
        une estimation cohérente et intelligente dérivée du même socle.
        """
        lam = np.exp(self.attack[home_team] - self.defense[away_team] + self.home_advantage)
        mu = np.exp(self.attack[away_team] - self.defense[home_team])
        total_intensity = lam + mu  # intensité totale du match (~2.5 en moyenne)

        # Moyennes typiques de ligue européenne par match
        AVG_CORNERS = 10.2
        AVG_FOULS = 22.0
        AVG_YELLOW = 3.8
        AVG_RED = 0.18
        AVG_PENALTIES = 0.22
        AVG_GOALS = 2.65  # moyenne typique buts/match

        # Facteur d'intensité (1.0 = match moyen)
        intensity_factor = total_intensity / AVG_GOALS

        # Corners : corrélés positivement avec l'intensité offensive
        corners_home = round(AVG_CORNERS * 0.53 * (lam / (AVG_GOALS / 2)), 1)
        corners_away = round(AVG_CORNERS * 0.47 * (mu / (AVG_GOALS / 2)), 1)
        corners_total = round(corners_home + corners_away, 1)

        # Fautes : plus de fautes dans les matchs intenses / défensifs
        defensive_factor = 1 + 0.15 * (intensity_factor - 1)
        fouls_home = round(AVG_FOULS * 0.48 * defensive_factor, 1)
        fouls_away = round(AVG_FOULS * 0.52 * defensive_factor, 1)
        fouls_total = round(fouls_home + fouls_away, 1)

        # Cartons jaunes : corrélés aux fautes
        yellows_home = round(AVG_YELLOW * 0.47 * defensive_factor, 1)
        yellows_away = round(AVG_YELLOW * 0.53 * defensive_factor, 1)
        yellows_total = round(yellows_home + yellows_away, 1)

        # Cartons rouges : très rares, léger biais dans les matchs tendus
        red_factor = min(intensity_factor * 1.1, 2.0)  # plafonné
        reds_total = round(AVG_RED * red_factor, 2)

        # Pénaltys : corrélés avec les occasions offensives
        penalties_prob = round(min(AVG_PENALTIES * intensity_factor, 0.55), 2)

        # Over/Under pour corners et cartons
        from scipy.stats import poisson as poisson_dist
        corners_over_8_5 = round(1 - float(poisson_dist.cdf(8, corners_total)), 4)
        corners_over_10_5 = round(1 - float(poisson_dist.cdf(10, corners_total)), 4)
        yellows_over_3_5 = round(1 - float(poisson_dist.cdf(3, yellows_total)), 4)

        return {
            "corners": {
                "home": corners_home,
                "away": corners_away,
                "total": corners_total,
                "over_8_5": corners_over_8_5,
                "over_10_5": corners_over_10_5,
            },
            "fouls": {
                "home": fouls_home,
                "away": fouls_away,
                "total": fouls_total,
            },
            "cards": {
                "yellow_home": yellows_home,
                "yellow_away": yellows_away,
                "yellow_total": yellows_total,
                "red_total": reds_total,
                "over_3_5_yellows": yellows_over_3_5,
            },
            "penalties": {
                "probability": penalties_prob,
            },
        }

    def ratings(self) -> list:
        rows = [
            {"team": t, "attack": round(self.attack[t], 4), "defense": round(self.defense[t], 4),
             "net_rating": round(self.attack[t] - self.defense[t], 4)}
            for t in self.teams
        ]
        return sorted(rows, key=lambda r: r["net_rating"], reverse=True)


def _load_all_leagues() -> dict:
    models = {}
    if not ARTIFACTS_DIR.exists():
        return models
    for path in ARTIFACTS_DIR.glob("*.json"):
        with open(path, encoding="utf-8") as f:
            artifact = json.load(f)
        models[artifact["league"]] = LeagueModel(artifact)
    return models


LEAGUE_MODELS: dict[str, LeagueModel] = _load_all_leagues()

# Instances de PredictionModel construites UNE SEULE FOIS au chargement du
# module (Phase 8, §31) — jamais reconstruites par requête : XGBoostPredictionModel/
# LightGBMPredictionModel mettent en cache leur dernier booster désérialisé
# PAR INSTANCE (voir models_common.py::_MLPredictionModel._get_booster), un
# cache reconstruit à chaque appel serait sans effet. Chaque instance garde
# une RÉFÉRENCE vers LEAGUE_MODELS (pas une copie) : la mise à jour en place
# faite par on_startup() (LEAGUE_MODELS.update(...)) reste donc visible sans
# avoir à reconstruire ces instances.
_PREDICTION_MODELS = default_models(LEAGUE_MODELS)


def _load_leagues_from_db() -> dict[str, LeagueModel]:
    """
    Complète/actualise LEAGUE_MODELS depuis la table `model_artifact`, seule
    source de vérité partagée avec le Cron Job Railway de ré-entraînement
    (celui-ci tourne dans un service séparé, sans accès au système de
    fichiers de ce service web — voir RAILWAY_CRON_SETUP.md).

    Appelée au démarrage, APRÈS init_db() (la table peut ne pas encore
    exister avant la première migration). Best-effort : toute erreur
    (table absente, base injoignable) est loggée et on retombe silencieusement
    sur les fichiers api/model_artifacts/*.json chargés par _load_all_leagues()
    ci-dessus — jamais fatal pour le démarrage de l'API.
    """
    models: dict[str, LeagueModel] = {}
    try:
        with Session(engine) as session:
            rows = session.exec(select(ModelArtifact)).all()
        for row in rows:
            artifact = json.loads(row.payload)
            models[artifact["league"]] = LeagueModel(artifact)
    except Exception as e:
        logger.warning(f"Chargement des artefacts depuis la base impossible, fallback fichiers : {e}")
    return models


def load_league_models() -> dict[str, "LeagueModel"]:
    """
    Reconstruit un LEAGUE_MODELS complet (fichiers PUIS écrasés par la table
    `model_artifact` si elle contient des lignes plus fraîches — mêmes deux
    sources, même ordre que le démarrage FastAPI ci-dessous) pour un
    contexte HORS service web (Phase 9, scripts/generate_live_predictions.py)
    qui ne passe jamais par `on_startup()` (jamais servi par uvicorn/
    TestClient). Fonction pure, ne touche jamais au LEAGUE_MODELS du module
    web — n'existe QUE pour être appelée depuis un script séparé.
    """
    models = _load_all_leagues()
    from_db = _load_leagues_from_db()
    if from_db:
        models.update(from_db)
    return models


# ---------------------------------------------------------------------------
# Schémas de réponse (Pydantic — documentation auto-générée par FastAPI)
# ---------------------------------------------------------------------------

class OverUnderLine(BaseModel):
    line: float
    over: float
    under: float


class CornersStats(BaseModel):
    home: float = Field(..., description="Corners estimés pour l'équipe à domicile")
    away: float = Field(..., description="Corners estimés pour l'équipe extérieure")
    total: float = Field(..., description="Total corners estimés dans le match")
    over_8_5: float = Field(..., description="Probabilité de plus de 8.5 corners")
    over_10_5: float = Field(..., description="Probabilité de plus de 10.5 corners")


class FoulsStats(BaseModel):
    home: float = Field(..., description="Fautes estimées pour l'équipe à domicile")
    away: float = Field(..., description="Fautes estimées pour l'équipe extérieure")
    total: float = Field(..., description="Total fautes estimées dans le match")


class CardsStats(BaseModel):
    yellow_home: float = Field(..., description="Cartons jaunes estimés — domicile")
    yellow_away: float = Field(..., description="Cartons jaunes estimés — extérieur")
    yellow_total: float = Field(..., description="Total cartons jaunes estimés")
    red_total: float = Field(..., description="Estimation cartons rouges dans le match")
    over_3_5_yellows: float = Field(..., description="Probabilité de plus de 3.5 cartons jaunes")


class PenaltiesStats(BaseModel):
    probability: float = Field(..., description="Probabilité qu'au moins un pénalty soit sifflé")


class MatchStats(BaseModel):
    """Statistiques prédites du match : corners, fautes, cartons, pénaltys."""
    corners: CornersStats
    fouls: FoulsStats
    cards: CardsStats
    penalties: PenaltiesStats


class MatchPrediction(BaseModel):
    league: str
    home_team: str
    away_team: str
    home_win: float = Field(..., description="Probabilité de victoire à domicile")
    draw: float
    away_win: float
    over_2_5: float
    under_2_5: float
    btts_yes: float = Field(..., description="Both Teams To Score — probabilité que les deux équipes marquent")
    btts_no: float
    double_chance_1x: float = Field(..., description="Probabilité domicile OU nul")
    double_chance_12: float = Field(..., description="Probabilité domicile OU extérieur (pas de nul)")
    double_chance_x2: float = Field(..., description="Probabilité nul OU extérieur")
    over_under_lines: list[OverUnderLine] = Field(
        ..., description="Probabilités +/- pour plusieurs lignes de buts (0.5, 1.5, 2.5, 3.5) — over_2_5/under_2_5 ci-dessus restent la ligne de référence"
    )
    match_stats: MatchStats = Field(..., description="Statistiques estimées : corners, fautes, cartons, pénaltys")
    most_likely_scores: list
    model_trained_at: str
    model_data_up_to: str
    home_team_resolution: str = Field(..., description="exact | normalized | alias — comment le nom fourni a été résolu")
    away_team_resolution: str


class TeamRating(BaseModel):
    team: str
    attack: float
    defense: float
    net_rating: float


class BatchMatchRequest(BaseModel):
    league: str
    home_team: str
    away_team: str


class BatchPredictionResult(BaseModel):
    league: str
    home_team_input: str
    away_team_input: str
    ok: bool
    prediction: MatchPrediction | None = None
    error: str | None = None
    suggestions: list[str] = []


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Foot Prediction API — Dixon-Coles",
    description="Analyse statistique football : probabilités 1X2 et probabilité de plus/moins de 2.5 buts, basées sur un modèle Dixon-Coles régularisé, par ligue.",
    version="1.0.0",
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ALLOWED_ORIGINS : liste d'origines séparées par des virgules (ex.
# "https://xfoot.vercel.app,https://xfoot.com"). Par défaut, seul le
# serveur de dev Next.js local est autorisé — indispensable de la définir
# en production, sinon le vrai frontend sera bloqué par le navigateur.
_allowed_origins = os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _allowed_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(billing_router)


@app.on_event("startup")
def on_startup():
    init_db()
    from_db = _load_leagues_from_db()
    if from_db:
        LEAGUE_MODELS.update(from_db)
        logger.info(f"Artefacts chargés depuis la base pour : {sorted(from_db.keys())} (ligues restantes sur fichier : {sorted(set(LEAGUE_MODELS) - set(from_db))})")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "leagues_loaded": list(LEAGUE_MODELS.keys()),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/leagues")
def list_leagues():
    """Liste les ligues disponibles et leurs équipes."""
    return {
        league: {"teams": model.teams, "trained_at": model.trained_at, "data_up_to": model.data_up_to}
        for league, model in LEAGUE_MODELS.items()
    }


def _pick_1x2(prediction: "MatchPrediction") -> str:
    if prediction.home_win >= prediction.draw and prediction.home_win >= prediction.away_win:
        return "home"
    if prediction.away_win >= prediction.draw:
        return "away"
    return "draw"


def _log_prediction(prediction: "MatchPrediction") -> None:
    """
    Enregistre la prédiction pour la page Historique & Performance (voir
    app/models/prediction_log.py) — best effort, ne doit jamais faire
    échouer la réponse de prédiction elle-même (même philosophie que
    _write_artifact_to_db dans refresh_and_retrain.py). Un seul
    enregistrement par (league, jour, home_team, away_team) : les relances
    du même match le même jour sont ignorées silencieusement.
    """
    try:
        today = datetime.now(timezone.utc).date()
        with Session(engine) as session:
            existing = session.exec(
                select(PredictionLog).where(
                    PredictionLog.league == prediction.league,
                    PredictionLog.match_date == today,
                    PredictionLog.home_team == prediction.home_team,
                    PredictionLog.away_team == prediction.away_team,
                )
            ).first()
            if existing is not None:
                return
            session.add(PredictionLog(
                league=prediction.league,
                match_date=today,
                home_team=prediction.home_team,
                away_team=prediction.away_team,
                payload=prediction.model_dump_json(),
                pick_1x2=_pick_1x2(prediction),
                pick_btts="yes" if prediction.btts_yes >= prediction.btts_no else "no",
                pick_over_2_5="over" if prediction.over_2_5 >= prediction.under_2_5 else "under",
            ))
            session.commit()
    except Exception as e:
        logger.warning(f"Enregistrement de l'historique de prédiction impossible (non bloquant) : {e}")


def _log_model_prediction(prediction: "MatchPrediction") -> None:
    """
    Dual-write vers model_predictions (Phase 6, source de vérité
    multi-modèles pour Xfoot AI Arena) — EN PLUS de _log_prediction
    (prediction_log, ci-dessus, totalement inchangée). Best-effort comme
    elle : ne doit jamais faire échouer la réponse de prédiction.

    Les métriques Dixon-Coles de l'Arena continuent de se calculer depuis
    prediction_log uniquement (voir app/ai/arena/service.py) — cette
    écriture-ci sert à faire passer Dixon-Coles par le même mécanisme
    commun que Elo/XGBoost/LightGBM (voir prediction_logging.py), pas à
    doubler le calcul des métriques.
    """
    try:
        with Session(engine) as session:
            version = get_or_create_active_model_version(
                session, "dixon_coles", "xfoot-dixon-coles",
                notes="Version de production — auto-créée au premier appel loggué via le logger commun (Phase 6).",
            )
            record = PredictionRecord(
                league=prediction.league,
                match_date=datetime.now(timezone.utc).date(),
                home_team=prediction.home_team,
                away_team=prediction.away_team,
                model_type="dixon_coles",
                prob_home=prediction.home_win,
                prob_draw=prediction.draw,
                prob_away=prediction.away_win,
                prob_btts_yes=prediction.btts_yes,
                prob_btts_no=prediction.btts_no,
                prob_over_2_5=prediction.over_2_5,
                prob_under_2_5=prediction.under_2_5,
                source="live",
            )
            log_prediction(session, record, version.id)
            session.commit()
    except Exception as e:
        logger.warning(f"Enregistrement model_predictions impossible (non bloquant) : {e}")


def _resolve_and_predict(league: str, home_team_input: str, away_team_input: str) -> MatchPrediction:
    """
    Résout les noms d'équipes (exact -> normalisé -> alias) puis calcule la
    prédiction. Lève ValueError avec un message actionnable (incluant des
    suggestions le cas échéant) si la ligue ou l'une des deux équipes ne
    peut pas être résolue de façon fiable — la résolution floue (fuzzy)
    n'est JAMAIS appliquée silencieusement, elle remonte comme une erreur
    avec suggestions.
    """
    if league not in LEAGUE_MODELS:
        raise ValueError(f"Ligue inconnue : '{league}'. Ligues disponibles : {list(LEAGUE_MODELS.keys())}")

    model = LEAGUE_MODELS[league]

    home_res = resolve_team_name(home_team_input, model.teams)
    away_res = resolve_team_name(away_team_input, model.teams)

    if home_res["resolved"] is None:
        msg = f"Équipe domicile non reconnue : '{home_team_input}'."
        if home_res["suggestions"]:
            msg += f" Vouliez-vous dire : {home_res['suggestions']} ?"
        raise ValueError(msg)

    if away_res["resolved"] is None:
        msg = f"Équipe extérieure non reconnue : '{away_team_input}'."
        if away_res["suggestions"]:
            msg += f" Vouliez-vous dire : {away_res['suggestions']} ?"
        raise ValueError(msg)

    home_team = home_res["resolved"]
    away_team = away_res["resolved"]

    probs_1x2 = model.predict_1x2(home_team, away_team)
    probs_ou = model.predict_over_under(home_team, away_team, line=2.5)
    probs_btts = model.predict_btts(home_team, away_team)
    probs_dc = model.predict_double_chance(home_team, away_team)
    # Autres lignes que 2.5, mêmes probabilités déjà exposées sous un autre
    # découpage — over_2_5/under_2_5 ci-dessus restent calculées séparément
    # pour ne pas casser les clients existants qui les lisent directement.
    over_under_lines = [model.predict_over_under(home_team, away_team, line=l) for l in (0.5, 1.5, 2.5, 3.5)]
    scores = model.most_likely_scores(home_team, away_team)
    match_stats = model.predict_match_stats(home_team, away_team)

    prediction = MatchPrediction(
        league=league,
        home_team=home_team,
        away_team=away_team,
        home_win=probs_1x2["home_win"],
        draw=probs_1x2["draw"],
        away_win=probs_1x2["away_win"],
        over_2_5=probs_ou["over"],
        under_2_5=probs_ou["under"],
        btts_yes=probs_btts["yes"],
        btts_no=probs_btts["no"],
        double_chance_1x=probs_dc["home_or_draw"],
        double_chance_12=probs_dc["home_or_away"],
        double_chance_x2=probs_dc["draw_or_away"],
        over_under_lines=over_under_lines,
        match_stats=match_stats,
        most_likely_scores=scores,
        model_trained_at=model.trained_at,
        model_data_up_to=model.data_up_to,
        home_team_resolution=home_res["method"],
        away_team_resolution=away_res["method"],
    )
    _log_prediction(prediction)
    _log_model_prediction(prediction)
    return prediction


@app.get("/predictions/{league}/{home_team}/{away_team}", response_model=MatchPrediction)
def get_prediction(league: str, home_team: str, away_team: str,
                    user: User = Depends(require_active_subscription)):
    try:
        return _resolve_and_predict(league, home_team, away_team)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/predictions/batch", response_model=list[BatchPredictionResult])
def get_predictions_batch(matches: list[BatchMatchRequest],
                           user: User = Depends(require_active_subscription)):
    """
    Prédictions pour plusieurs matchs en un seul appel — utile pour une
    journée complète de championnat. Chaque match est traité
    indépendamment : un nom d'équipe non résolu sur UN match ne fait pas
    échouer les autres (ok=False + suggestions pour celui-là, le reste
    continue normalement).
    """
    results = []
    for m in matches:
        try:
            prediction = _resolve_and_predict(m.league, m.home_team, m.away_team)
            results.append(BatchPredictionResult(
                league=m.league, home_team_input=m.home_team, away_team_input=m.away_team,
                ok=True, prediction=prediction,
            ))
        except ValueError as e:
            error_msg = str(e)
            suggestions = []
            # Récupère les suggestions si l'erreur vient d'une équipe non résolue
            if m.league in LEAGUE_MODELS:
                model = LEAGUE_MODELS[m.league]
                for name in (m.home_team, m.away_team):
                    res = resolve_team_name(name, model.teams)
                    if res["resolved"] is None:
                        suggestions.extend(res["suggestions"])
            results.append(BatchPredictionResult(
                league=m.league, home_team_input=m.home_team, away_team_input=m.away_team,
                ok=False, error=error_msg, suggestions=suggestions,
            ))
    return results


class PredictionHistoryResult(BaseModel):
    home_goals: int
    away_goals: int


class PredictionHistoryMatch(BaseModel):
    league: str
    home_team: str
    away_team: str
    pick_1x2: str
    pick_btts: str
    pick_over_2_5: str
    result: PredictionHistoryResult | None = None
    correct_1x2: bool | None = None
    correct_btts: bool | None = None
    correct_over_2_5: bool | None = None


class PredictionHistoryDay(BaseModel):
    date: str
    matches: list[PredictionHistoryMatch]


class PredictionHistoryAccuracy(BaseModel):
    sample_size: int
    overall_1x2: float | None = None
    btts: float | None = None
    over_under_2_5: float | None = None


class PredictionHistoryResponse(BaseModel):
    accuracy: PredictionHistoryAccuracy
    days: list[PredictionHistoryDay]


@app.get("/predictions/history", response_model=PredictionHistoryResponse)
def get_predictions_history(days: int = 14, user: User = Depends(get_current_user)):
    """
    Historique des prédictions loguées (voir _log_prediction) et de leur
    résultat une fois connu (rempli par fetch_daily_results.py, séparé de
    ce service web — voir app/core/api_football_config.py). Accessible à
    tout compte connecté (pas réservé aux abonnés) : sert de preuve
    sociale du taux de réussite du modèle.

    `accuracy` est calculée uniquement sur les prédictions déjà résolues
    (result_fetched_at renseigné) — les matchs sans résultat encore connu
    apparaissent dans `days` avec result=null mais ne comptent pas dans le
    pourcentage.
    """
    since = datetime.now(timezone.utc).date() - timedelta(days=max(days, 0))
    with Session(engine) as session:
        logs = session.exec(
            select(PredictionLog)
            .where(PredictionLog.match_date >= since)
            .order_by(PredictionLog.match_date.desc(), PredictionLog.id.desc())
        ).all()

    resolved = [log for log in logs if log.result_fetched_at is not None]
    accuracy = PredictionHistoryAccuracy(
        sample_size=len(resolved),
        overall_1x2=(sum(1 for log in resolved if log.correct_1x2) / len(resolved)) if resolved else None,
        btts=(sum(1 for log in resolved if log.correct_btts) / len(resolved)) if resolved else None,
        over_under_2_5=(sum(1 for log in resolved if log.correct_over_2_5) / len(resolved)) if resolved else None,
    )

    days_map: dict[str, list[PredictionHistoryMatch]] = {}
    for log in logs:
        result = None
        if log.result_home_goals is not None and log.result_away_goals is not None:
            result = PredictionHistoryResult(home_goals=log.result_home_goals, away_goals=log.result_away_goals)
        match = PredictionHistoryMatch(
            league=log.league,
            home_team=log.home_team,
            away_team=log.away_team,
            pick_1x2=log.pick_1x2,
            pick_btts=log.pick_btts,
            pick_over_2_5=log.pick_over_2_5,
            result=result,
            correct_1x2=log.correct_1x2,
            correct_btts=log.correct_btts,
            correct_over_2_5=log.correct_over_2_5,
        )
        days_map.setdefault(log.match_date.isoformat(), []).append(match)

    return PredictionHistoryResponse(
        accuracy=accuracy,
        days=[PredictionHistoryDay(date=d, matches=m) for d, m in days_map.items()],
    )


class LiveMatch(BaseModel):
    league: str
    home_team: str
    away_team: str
    home_goals: int
    away_goals: int
    status_short: str
    elapsed: int | None = None
    home_logo: str | None = None
    away_logo: str | None = None


# Cache en mémoire, PARTAGÉ entre tous les utilisateurs — pas une table,
# même philosophie que LEAGUE_MODELS (état du process, pas de
# persistance nécessaire). Indispensable : le plan Free d'API-Football
# est limité à 100 requêtes/jour au total sur la clé (déjà partagée avec
# fetch_daily_results.py) — sans ce cache, chaque visite de /live-scores
# viderait le quota en quelques minutes un jour de match.
_live_scores_cache: dict = {"matches": [], "next_fetch_at": None}

_LIVE_SCORES_LEAGUE_IDS_TO_NAME = {v: k for k, v in API_FOOTBALL_LEAGUE_IDS.items()}

# TTL adaptatif : court quand il y a effectivement des matchs en cours
# dans nos ligues (l'utilisateur veut du frais), long sinon (pas la peine
# de rappeler l'API toutes les 3 minutes un jour sans match — les
# fenêtres de matchs en direct sont limitées dans le temps, pas 24h/24).
_LIVE_TTL_WITH_MATCHES = timedelta(seconds=180)
_LIVE_TTL_NO_MATCHES = timedelta(seconds=300)


def _get_live_scores() -> list[LiveMatch]:
    now = datetime.now(timezone.utc)
    next_fetch_at = _live_scores_cache["next_fetch_at"]
    if next_fetch_at is not None and now < next_fetch_at:
        return _live_scores_cache["matches"]

    try:
        resp = httpx.get(
            f"{API_FOOTBALL_BASE_URL}/fixtures",
            params={"live": "all"},
            headers={"x-apisports-key": API_FOOTBALL_KEY},
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(f"réponse API-Football en erreur : {data['errors']}")

        matches = []
        for fixture in data.get("response", []):
            league_id = fixture.get("league", {}).get("id")
            league_name = _LIVE_SCORES_LEAGUE_IDS_TO_NAME.get(league_id)
            if league_name is None:
                continue
            matches.append(LiveMatch(
                league=league_name,
                home_team=fixture.get("teams", {}).get("home", {}).get("name", "?"),
                away_team=fixture.get("teams", {}).get("away", {}).get("name", "?"),
                home_goals=fixture.get("goals", {}).get("home") or 0,
                away_goals=fixture.get("goals", {}).get("away") or 0,
                status_short=fixture.get("fixture", {}).get("status", {}).get("short", "?"),
                elapsed=fixture.get("fixture", {}).get("status", {}).get("elapsed"),
                home_logo=fixture.get("teams", {}).get("home", {}).get("logo"),
                away_logo=fixture.get("teams", {}).get("away", {}).get("logo"),
            ))

        _live_scores_cache["matches"] = matches
        _live_scores_cache["next_fetch_at"] = now + (
            _LIVE_TTL_WITH_MATCHES if matches else _LIVE_TTL_NO_MATCHES
        )
    except Exception as e:
        logger.warning(f"Rafraîchissement des scores en direct impossible (cache précédent conservé) : {e}")
        # Best effort : on ne remonte jamais l'erreur à l'utilisateur, on
        # garde le dernier cache connu (vide au tout premier appel) — et on
        # évite de retenter à chaque requête tant que l'erreur persiste.
        _live_scores_cache["next_fetch_at"] = now + _LIVE_TTL_NO_MATCHES

    return _live_scores_cache["matches"]


@app.get("/live-scores", response_model=list[LiveMatch])
def get_live_scores(user: User = Depends(get_current_user)):
    """
    Scores en direct des matchs en cours sur nos 5 championnats, sans
    prédiction associée (contrairement à /predictions/*) — accessible à
    tout compte connecté. Voir _get_live_scores pour le cache serveur
    partagé, obligatoire vu le quota du plan API-Football Free.
    """
    return _get_live_scores()


@app.get("/ratings/{league}", response_model=list[TeamRating])
def get_ratings(league: str):
    if league not in LEAGUE_MODELS:
        raise HTTPException(
            status_code=404,
            detail=f"Ligue inconnue : '{league}'. Ligues disponibles : {list(LEAGUE_MODELS.keys())}",
        )
    return LEAGUE_MODELS[league].ratings()


# ---------------------------------------------------------------------------
# Matchs de la Semaine par Ligue (/fixtures/week) — Semaine du 17 au 23 Août 2026
# ---------------------------------------------------------------------------

_OFFICIAL_WEEKLY_SCHEDULE_2026 = {
    "LaLiga": [
        {"home": "Athletic Club", "away": "Getafe", "kickoff": "2026-08-18T19:00:00+00:00"},
        {"home": "Betis", "away": "Girona", "kickoff": "2026-08-18T21:30:00+00:00"},
        {"home": "Celta", "away": "Alaves", "kickoff": "2026-08-19T19:00:00+00:00"},
        {"home": "Las Palmas", "away": "Sevilla", "kickoff": "2026-08-19T21:30:00+00:00"},
        {"home": "Osasuna", "away": "Leganes", "kickoff": "2026-08-20T19:00:00+00:00"},
        {"home": "Valencia", "away": "Barcelona", "kickoff": "2026-08-20T21:30:00+00:00"},
        {"home": "Real Sociedad", "away": "Rayo Vallecano", "kickoff": "2026-08-21T19:00:00+00:00"},
        {"home": "Mallorca", "away": "Real Madrid", "kickoff": "2026-08-21T21:30:00+00:00"},
        {"home": "Valladolid", "away": "Espanol", "kickoff": "2026-08-22T19:00:00+00:00"},
        {"home": "Villarreal", "away": "Atletico Madrid", "kickoff": "2026-08-22T21:30:00+00:00"},
    ],
    "PremierLeague": [
        {"home": "Man United", "away": "Fulham", "kickoff": "2026-08-18T20:00:00+00:00"},
        {"home": "Ipswich", "away": "Liverpool", "kickoff": "2026-08-19T12:30:00+00:00"},
        {"home": "Arsenal", "away": "Wolves", "kickoff": "2026-08-19T15:00:00+00:00"},
        {"home": "Everton", "away": "Brighton", "kickoff": "2026-08-19T15:00:00+00:00"},
        {"home": "Newcastle", "away": "Southampton", "kickoff": "2026-08-19T15:00:00+00:00"},
        {"home": "Nott'm Forest", "away": "Bournemouth", "kickoff": "2026-08-19T15:00:00+00:00"},
        {"home": "West Ham", "away": "Aston Villa", "kickoff": "2026-08-19T17:30:00+00:00"},
        {"home": "Brentford", "away": "Crystal Palace", "kickoff": "2026-08-20T14:00:00+00:00"},
        {"home": "Chelsea", "away": "Man City", "kickoff": "2026-08-20T16:30:00+00:00"},
        {"home": "Leicester", "away": "Tottenham", "kickoff": "2026-08-21T20:00:00+00:00"},
    ],
    "Ligue1": [
        {"home": "Le Havre", "away": "Paris SG", "kickoff": "2026-08-18T20:45:00+00:00"},
        {"home": "Brest", "away": "Marseille", "kickoff": "2026-08-19T17:00:00+00:00"},
        {"home": "Reims", "away": "Lille", "kickoff": "2026-08-19T19:00:00+00:00"},
        {"home": "Monaco", "away": "St Etienne", "kickoff": "2026-08-19T21:00:00+00:00"},
        {"home": "Auxerre", "away": "Nice", "kickoff": "2026-08-20T15:00:00+00:00"},
        {"home": "Angers", "away": "Lens", "kickoff": "2026-08-20T17:00:00+00:00"},
        {"home": "Montpellier", "away": "Strasbourg", "kickoff": "2026-08-20T17:00:00+00:00"},
        {"home": "Toulouse", "away": "Nantes", "kickoff": "2026-08-20T17:00:00+00:00"},
        {"home": "Rennes", "away": "Lyon", "kickoff": "2026-08-20T20:45:00+00:00"},
    ],
    "SerieA": [
        {"home": "Genoa", "away": "Inter", "kickoff": "2026-08-18T18:30:00+00:00"},
        {"home": "Parma", "away": "Fiorentina", "kickoff": "2026-08-18T18:30:00+00:00"},
        {"home": "Milan", "away": "Torino", "kickoff": "2026-08-18T20:45:00+00:00"},
        {"home": "Empoli", "away": "Monza", "kickoff": "2026-08-18T20:45:00+00:00"},
        {"home": "Bologna", "away": "Udinese", "kickoff": "2026-08-19T18:30:00+00:00"},
        {"home": "Verona", "away": "Napoli", "kickoff": "2026-08-19T18:30:00+00:00"},
        {"home": "Cagliari", "away": "Roma", "kickoff": "2026-08-19T20:45:00+00:00"},
        {"home": "Lazio", "away": "Venezia", "kickoff": "2026-08-19T20:45:00+00:00"},
        {"home": "Lecce", "away": "Atalanta", "kickoff": "2026-08-20T18:30:00+00:00"},
        {"home": "Juventus", "away": "Como", "kickoff": "2026-08-20T20:45:00+00:00"},
    ],
    "Bundesliga": [
        {"home": "M'gladbach", "away": "Leverkusen", "kickoff": "2026-08-18T20:30:00+00:00"},
        {"home": "RB Leipzig", "away": "Bochum", "kickoff": "2026-08-19T15:30:00+00:00"},
        {"home": "Hoffenheim", "away": "Holstein Kiel", "kickoff": "2026-08-19T15:30:00+00:00"},
        {"home": "Mainz", "away": "Union Berlin", "kickoff": "2026-08-19T15:30:00+00:00"},
        {"home": "Augsburg", "away": "Werder Bremen", "kickoff": "2026-08-19T15:30:00+00:00"},
        {"home": "Freiburg", "away": "Stuttgart", "kickoff": "2026-08-19T15:30:00+00:00"},
        {"home": "Dortmund", "away": "Ein Frankfurt", "kickoff": "2026-08-19T18:30:00+00:00"},
        {"home": "Wolfsburg", "away": "Bayern Munich", "kickoff": "2026-08-20T15:30:00+00:00"},
        {"home": "St Pauli", "away": "Heidenheim", "kickoff": "2026-08-20T17:30:00+00:00"},
    ],
}

_weekly_fixtures_cache = {
    "fixtures_by_league": {},
    "expires_at": None,
}


def _get_weekly_fixtures():
    now = datetime.now(timezone.utc)
    if _weekly_fixtures_cache["expires_at"] is not None and now < _weekly_fixtures_cache["expires_at"]:
        return _weekly_fixtures_cache["fixtures_by_league"]

    by_league = {l: [] for l in LEAGUE_MODELS.keys()}
    try:
        from app.core.api_football_client import fetch_upcoming_fixtures
        raw = fetch_upcoming_fixtures(168, now=now)
        league_ids_to_name = {v: k for k, v in API_FOOTBALL_LEAGUE_IDS.items()}
        for f in raw:
            lid = f.get("league", {}).get("id")
            lname = league_ids_to_name.get(lid)
            if not lname or lname not in by_league:
                continue
            teams = f.get("teams", {})
            home = teams.get("home", {})
            away = teams.get("away", {})
            fixture_info = f.get("fixture", {})
            by_league[lname].append({
                "id": str(fixture_info.get("id") or f"{home.get('name')}_{away.get('name')}"),
                "league": lname,
                "home_team": home.get("name", "Domicile"),
                "away_team": away.get("name", "Extérieur"),
                "home_logo": home.get("logo"),
                "away_logo": away.get("logo"),
                "kickoff": fixture_info.get("date"),
                "status": fixture_info.get("status", {}).get("short", "NS"),
            })
    except Exception as e:
        logger.warning(f"Erreur lors de la récupération des fixtures hebdo API-Football : {e}")

    # Charge les affiches officielles réelles de la semaine du 17 au 23 Août 2026
    for lname in LEAGUE_MODELS.keys():
        if not by_league[lname] and lname in _OFFICIAL_WEEKLY_SCHEDULE_2026:
            for idx, match in enumerate(_OFFICIAL_WEEKLY_SCHEDULE_2026[lname]):
                by_league[lname].append({
                    "id": f"real_{lname}_{idx}",
                    "league": lname,
                    "home_team": match["home"],
                    "away_team": match["away"],
                    "home_logo": None,
                    "away_logo": None,
                    "kickoff": match["kickoff"],
                    "status": "NS",
                })

    _weekly_fixtures_cache["fixtures_by_league"] = by_league
    _weekly_fixtures_cache["expires_at"] = now + timedelta(hours=2)
    return _weekly_fixtures_cache["fixtures_by_league"]


@app.get("/fixtures/week")
def get_weekly_fixtures(league: str | None = None):
    """
    Renvoie les matchs de la semaine (lundi à dimanche) pour les ligues suivies.
    """
    fixtures_by_league = _get_weekly_fixtures()
    if league:
        if league not in LEAGUE_MODELS:
            raise HTTPException(
                status_code=400,
                detail=f"Ligue inconnue : '{league}'. Ligues disponibles : {list(LEAGUE_MODELS.keys())}",
            )
        return {
            "league": league,
            "fixtures": fixtures_by_league.get(league, []),
            "count": len(fixtures_by_league.get(league, [])),
        }
    return {
        "leagues": list(LEAGUE_MODELS.keys()),
        "fixtures_by_league": fixtures_by_league,
        "total_count": sum(len(v) for v in fixtures_by_league.values()),
    }



# ---------------------------------------------------------------------------
# Xfoot AI Arena (Phase 5) — voir app/ai/arena/service.py pour le détail.
# Public comme /health et /ratings : données de mesure, jamais de secrets/PII.
# /models/performance et /models/benchmark déclarés AVANT /models/{model_version_id} :
# les chemins littéraux doivent être essayés en premier, sinon "performance"/
# "benchmark" seraient tentés comme model_version_id.
# ---------------------------------------------------------------------------

def _validate_arena_filters(league: str | None, market: str | None = None, prediction_source: str | None = None) -> None:
    if league is not None and league not in LEAGUE_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Ligue inconnue : '{league}'. Ligues disponibles : {list(LEAGUE_MODELS.keys())}",
        )
    if market is not None and market not in ARENA_MARKETS:
        raise HTTPException(
            status_code=400,
            detail=f"Marché inconnu : '{market}'. Marchés disponibles : {list(ARENA_MARKETS)}",
        )
    if prediction_source is not None and prediction_source not in ("live", "backtest"):
        raise HTTPException(
            status_code=400,
            detail=f"prediction_source inconnu : '{prediction_source}'. Valeurs possibles : live, backtest.",
        )


@app.get("/models/performance", response_model=ArenaPerformanceResponse)
def get_models_performance_endpoint(
    league: str | None = None,
    since: date | None = None,
    until: date | None = None,
    prediction_source: str | None = None,
):
    """
    Filtres optionnels appliqués aux métriques par marché : `league`
    restreint à une ligue, `since`/`until` (dates ISO) restreignent la
    période. Sans filtre : all_time, toutes ligues confondues (comportement
    V1 inchangé). `prediction_source` (Phase 8, §21) : "live" ou "backtest"
    pour ne JAMAIS mélanger silencieusement les deux à l'affichage — omis
    (défaut), les deux restent confondues (comportement Phase 5/6/7 inchangé).
    """
    _validate_arena_filters(league, prediction_source=prediction_source)
    with Session(engine) as session:
        return get_models_performance(session, LEAGUE_MODELS, league=league, since=since, until=until,
                                       prediction_source=prediction_source)


@app.get("/models/benchmark", response_model=ArenaBenchmarkResponse)
def get_models_benchmark_endpoint(
    market: str | None = None,
    league: str | None = None,
    since: date | None = None,
    until: date | None = None,
    prediction_source: str | None = None,
):
    """
    Comparaison croisée des modèles, marché par marché — ne désigne un
    "meilleur modèle" (`best_model.status="ok"`) que lorsqu'au moins 2
    modèles ont, sur le même marché, la même métrique disponible avec un
    échantillon suffisant (voir app/ai/arena/service.py::_pick_best_model).
    `market` limite la réponse à un seul marché parmi 1X2/BTTS/OVER_UNDER_2_5 ;
    `league`/`since`/`until`/`prediction_source` mêmes filtres que GET
    /models/performance.
    """
    _validate_arena_filters(league, market, prediction_source=prediction_source)
    with Session(engine) as session:
        return get_models_benchmark(session, LEAGUE_MODELS, market=market, league=league, since=since, until=until,
                                     prediction_source=prediction_source)


@app.get("/models/predictions", response_model=ModelPredictionListResponse)
def list_model_predictions_endpoint(
    model_type: str | None = None,
    status: str | None = None,
    league: str | None = None,
    limit: int = 100,
):
    """
    Consultation brute des prédictions individuelles multi-modèles (Phase
    6, table model_predictions) — observabilité/audit, distincte des vues
    agrégées de GET /models/performance|benchmark. Plus récentes d'abord,
    plafonnée à `limit` (défaut 100, max 500) pour rester légère.
    """
    if status is not None and status not in ("pending", "resolved", "invalid"):
        raise HTTPException(status_code=400, detail=f"Statut inconnu : '{status}'. Valeurs possibles : pending, resolved, invalid")
    _validate_arena_filters(league)
    limit = max(1, min(limit, 500))
    with Session(engine) as session:
        predictions, total = list_model_predictions(session, model_type=model_type, status=status, league=league, limit=limit)
    return ModelPredictionListResponse(predictions=predictions, count=total, limit=limit)


@app.get("/models/predictions/{prediction_id}", response_model=ModelPredictionRead)
def get_model_prediction_endpoint(prediction_id: int):
    with Session(engine) as session:
        prediction = get_model_prediction(session, prediction_id)
    if prediction is None:
        raise HTTPException(status_code=404, detail=f"Aucune prédiction avec id={prediction_id}.")
    return prediction


class EnsemblePredictRequest(BaseModel):
    league: str
    home_team: str
    away_team: str
    # Phase 8, §14 : stratégie de pondération (voir GET /models/ensemble/strategies
    # pour la liste). None (défaut) = InverseLogLossStrategy, la baseline Phase 7.
    strategy: str | None = None


def _model_outcome_response(outcome) -> dict:
    if outcome.status == "ok":
        r = outcome.record
        return {
            "status": "ok",
            "model_version_id": outcome.model_version_id,
            "markets": {
                "1X2": {"home": r.prob_home, "draw": r.prob_draw, "away": r.prob_away},
                "BTTS": {"yes": r.prob_btts_yes, "no": r.prob_btts_no} if r.prob_btts_yes is not None else None,
                "OVER_UNDER_2_5": (
                    {"over": r.prob_over_2_5, "under": r.prob_under_2_5} if r.prob_over_2_5 is not None else None
                ),
            },
        }
    return {"status": outcome.status, "model_version_id": outcome.model_version_id, "reason": outcome.reason}


def _ensemble_market_response(market_result) -> dict:
    if market_result.status != "ok":
        return {"status": "not_available", "reason": market_result.reason}
    c = market_result.combined
    return {
        "status": "ok",
        "probabilities": c.probs,
        "weights": c.weights_used,
        "models_used": c.models_used,
        "degraded": c.degraded,
    }


@app.post("/models/ensemble/predict")
def post_ensemble_predict(payload: EnsemblePredictRequest, user: User = Depends(require_active_subscription)):
    """
    Prédiction LIVE multi-modèles (Phase 7) : appelle chaque modèle connu
    (Dixon-Coles, Elo, XGBoost, LightGBM) via ModelOrchestrator — un modèle
    indisponible (voir app/ai/arena/models_common.py) n'empêche jamais les
    autres de répondre —, journalise chaque prédiction individuelle réussie
    dans model_predictions (source="live", même mécanisme commun que
    Phase 6), puis combine les prédictions disponibles via EnsembleEngine
    (poids dérivés du log_loss historique par marché, jamais arbitraires —
    voir app/ai/arena/ensemble.py) et journalise l'Ensemble lui-même
    (model_type="ensemble"). Réutilise la résolution de noms d'équipes déjà
    en place pour /predictions/* (resolve_team_name, via l'artefact
    Dixon-Coles) : aucun modèle ne reçoit jamais un nom d'équipe non résolu.
    """
    if payload.league not in LEAGUE_MODELS:
        raise HTTPException(
            status_code=404,
            detail=f"Ligue inconnue : '{payload.league}'. Ligues disponibles : {list(LEAGUE_MODELS.keys())}",
        )
    if payload.strategy is not None and payload.strategy not in WEIGHT_STRATEGIES:
        raise HTTPException(
            status_code=400,
            detail=f"Stratégie inconnue : '{payload.strategy}'. Voir GET /models/ensemble/strategies. "
                    f"Valeurs possibles : {list(WEIGHT_STRATEGIES.keys())}",
        )
    strategy = WEIGHT_STRATEGIES[payload.strategy] if payload.strategy else DEFAULT_STRATEGY

    dc_model = LEAGUE_MODELS[payload.league]
    home_res = resolve_team_name(payload.home_team, dc_model.teams)
    away_res = resolve_team_name(payload.away_team, dc_model.teams)

    if home_res["resolved"] is None:
        msg = f"Équipe domicile non reconnue : '{payload.home_team}'."
        if home_res["suggestions"]:
            msg += f" Vouliez-vous dire : {home_res['suggestions']} ?"
        raise HTTPException(status_code=404, detail=msg)
    if away_res["resolved"] is None:
        msg = f"Équipe extérieure non reconnue : '{payload.away_team}'."
        if away_res["suggestions"]:
            msg += f" Vouliez-vous dire : {away_res['suggestions']} ?"
        raise HTTPException(status_code=404, detail=msg)

    home_team, away_team = home_res["resolved"], away_res["resolved"]
    match_date = datetime.now(timezone.utc).date()
    ctx = MatchContext(league=payload.league, home_team=home_team, away_team=away_team, match_date=match_date)

    with Session(engine) as session:
        orchestrator = ModelOrchestrator(_PREDICTION_MODELS)
        outcomes = orchestrator.predict_all(session, ctx)

        model_records = {mt: o.record for mt, o in outcomes.items() if o.status == "ok" and o.record is not None}
        ensemble_result = build_live_ensemble(
            session, model_records, payload.league, home_team, away_team, match_date, strategy=strategy,
        )

    return {
        "status": "ok",
        "match": {
            "league": payload.league, "home_team": home_team, "away_team": away_team,
            "match_date": match_date.isoformat(),
        },
        "models": {mt: _model_outcome_response(o) for mt, o in outcomes.items()},
        "ensemble": {
            "model_version_id": ensemble_result.model_version_id,
            "markets": {m: _ensemble_market_response(r) for m, r in ensemble_result.markets.items()},
            "confidence": ensemble_result.confidence,
            "weighting_metric": strategy.name,
        },
    }


@app.get("/models/ensemble/strategies")
def get_ensemble_strategies():
    """
    Liste les stratégies de pondération disponibles pour POST
    /models/ensemble/predict (§14-§18, §34 : ajouté car réellement utile —
    évite de coder en dur cette liste côté frontend). InverseLogLossStrategy
    reste la valeur par défaut si `strategy` est omis.
    """
    return {
        "default": DEFAULT_STRATEGY.name,
        "strategies": [
            {
                "name": s.name,
                "params": {k: v for k, v in vars(s).items() if not k.startswith("_")},
            }
            for s in WEIGHT_STRATEGIES.values()
        ],
    }


@app.get("/models/availability")
def get_models_availability_endpoint():
    """
    Matrice de disponibilité par model_type — `active` (version déployée),
    `live_available` (peut réellement prédire maintenant), et par marché
    `benchmark_eligible`/`ensemble_eligible` (§12-§13 du ticket Phase 8).
    Public comme /models/performance : données de mesure, jamais de
    secrets/PII.
    """
    with Session(engine) as session:
        availability = compute_model_availability(session, _PREDICTION_MODELS)

    return {
        model_type: {
            "active": a.active,
            "model_version_id": a.model_version_id,
            "version_name": a.version_name,
            "live_available": a.live_available,
            "live_reason": a.live_reason,
            "markets": {
                market: {
                    "benchmark_eligible": ma.benchmark_eligible,
                    "ensemble_eligible": ma.ensemble_eligible,
                    "sample_size": ma.sample_size,
                    "reason": ma.reason,
                }
                for market, ma in a.markets.items()
            },
        }
        for model_type, a in availability.items()
    }


# ---------------------------------------------------------------------------
# Phase 9 — LIVE monitoring / model health / versioning / training status
# (GET uniquement — retrain/shadow restent CLI-only, voir
# scripts/retrain_ml_models.py). La promotion, elle, a un palier admin
# depuis la Phase 10 (voir app/auth/admin.py::require_admin, ADMIN_EMAILS) —
# section "Phase 10" plus bas pour les endpoints /models/promotion/*.
# ---------------------------------------------------------------------------

_LIVE_MONITORING_MODEL_TYPES = (*KNOWN_MODEL_TYPES, "ensemble")


def _window_stats_response(ws) -> dict:
    return {
        "status": ws.status,
        "sample_size": ws.sample_size,
        "metrics": (
            None if ws.metrics is None else {
                "accuracy": ws.metrics.accuracy, "log_loss": ws.metrics.log_loss,
                "brier_score": ws.metrics.brier_score, "sample_size": ws.metrics.sample_size,
                "correct_predictions": ws.metrics.correct_predictions,
            }
        ),
    }


@app.get("/models/live-performance")
def get_models_live_performance_endpoint(
    model_type: str | None = None, market: str | None = None, window: str | None = None,
):
    """
    Métriques LIVE (source="live", role="active" par défaut — jamais BACKTEST
    ni SHADOW mélangés silencieusement, Phase 9 §12-13), par fenêtre
    (ALL_TIME/LAST_100/LAST_50/LAST_30_DAYS), chacune honnêtement
    INSUFFICIENT_DATA si trop peu de lignes résolues. Sans filtre : tous les
    model_type connus (dixon_coles/elo/xgboost/lightgbm/ensemble) et les 3
    marchés.
    """
    if model_type is not None and model_type not in _LIVE_MONITORING_MODEL_TYPES:
        raise HTTPException(status_code=400, detail=f"model_type inconnu : '{model_type}'. Valeurs possibles : {list(_LIVE_MONITORING_MODEL_TYPES)}")
    if market is not None and market not in ARENA_MARKETS:
        raise HTTPException(status_code=400, detail=f"Marché inconnu : '{market}'. Marchés disponibles : {list(ARENA_MARKETS)}")
    if window is not None and window not in arena_monitoring.DEFAULT_WINDOWS:
        raise HTTPException(status_code=400, detail=f"Fenêtre inconnue : '{window}'. Valeurs possibles : {list(arena_monitoring.DEFAULT_WINDOWS)}")

    model_types = [model_type] if model_type else list(_LIVE_MONITORING_MODEL_TYPES)
    markets = [market] if market else list(ARENA_MARKETS)
    windows = (window,) if window else arena_monitoring.DEFAULT_WINDOWS

    result: dict = {}
    with Session(engine) as session:
        for mt in model_types:
            summary = arena_monitoring.get_live_summary(session, mt)
            result[mt] = {
                "summary": {
                    "predictions_total": summary.predictions_total,
                    "predictions_resolved": summary.predictions_resolved,
                    "predictions_pending": summary.predictions_pending,
                    "last_prediction_at": summary.last_prediction_at.isoformat() if summary.last_prediction_at else None,
                    "last_resolved_at": summary.last_resolved_at.isoformat() if summary.last_resolved_at else None,
                },
                "markets": {},
            }
            for mk in markets:
                stats = arena_monitoring.get_live_monitoring(session, mt, mk, windows=windows)
                result[mt]["markets"][mk] = {w: _window_stats_response(ws) for w, ws in stats.items()}

        # §33-35 : Ensemble vs meilleur modèle individuel, sur données LIVE
        # uniquement — inclus systématiquement (coût négligeable, quelques
        # requêtes de plus), jamais "Ensemble meilleur" affirmé sur un petit
        # échantillon (voir monitoring.py::compute_ensemble_delta).
        result["ensemble_delta"] = {}
        for mk in markets:
            d = arena_monitoring.compute_ensemble_delta(session, mk)
            result["ensemble_delta"][mk] = {
                "benchmark_status": d.benchmark_status, "sample_size": d.sample_size,
                "ensemble_log_loss": d.ensemble_log_loss,
                "best_individual_model": d.best_individual_model, "best_individual_log_loss": d.best_individual_log_loss,
                "delta_log_loss": d.delta_log_loss, "delta_brier": d.delta_brier, "delta_accuracy": d.delta_accuracy,
                "reason": d.reason,
            }
    return result


@app.get("/models/health")
def get_models_health_endpoint(model_type: str | None = None, market: str | None = None):
    """
    État de santé LIVE par model_type/marché (§16-18) — HEALTHY/WARNING/
    DEGRADED/INSUFFICIENT_DATA/UNAVAILABLE, jamais un simple booléen. Compare
    la fenêtre ALL_TIME (baseline) à LAST_30_DAYS (récente) ; jamais de
    désactivation automatique, purement informatif.
    """
    if model_type is not None and model_type not in KNOWN_MODEL_TYPES:
        raise HTTPException(status_code=400, detail=f"model_type inconnu : '{model_type}'. Valeurs possibles : {list(KNOWN_MODEL_TYPES)}")
    if market is not None and market not in ARENA_MARKETS:
        raise HTTPException(status_code=400, detail=f"Marché inconnu : '{market}'. Marchés disponibles : {list(ARENA_MARKETS)}")

    model_types = [model_type] if model_type else list(KNOWN_MODEL_TYPES)
    markets = [market] if market else list(ARENA_MARKETS)

    result: dict = {}
    with Session(engine) as session:
        for mt in model_types:
            result[mt] = {}
            for mk in markets:
                health = arena_monitoring.compute_model_health(session, mt, mk, models=_PREDICTION_MODELS)
                result[mt][mk] = {
                    "status": health.status, "reason": health.reason,
                    "baseline_window": health.baseline_window, "recent_window": health.recent_window,
                    "baseline_log_loss": health.baseline_log_loss, "recent_log_loss": health.recent_log_loss,
                    "delta": health.delta, "min_monitoring_sample": health.min_monitoring_sample,
                    "warning_delta": health.warning_delta, "critical_delta": health.critical_delta,
                }
    return result


@app.get("/models/versions")
def list_model_versions_endpoint(model_type: str | None = None, status: str | None = None):
    """
    Historique des ModelVersion avec les champs de versioning Phase 9
    (status/activated_at/deactivated_at/périodes/feature_version/sample_size)
    — permet de comparer v1/v2/v3 (§19). `status` filtre sur
    active/shadow/candidate/retired.
    """
    if status is not None and status not in ("active", "shadow", "candidate", "retired"):
        raise HTTPException(status_code=400, detail=f"status inconnu : '{status}'. Valeurs possibles : active, shadow, candidate, retired.")

    with Session(engine) as session:
        stmt = select(ArenaModelVersion)
        if model_type is not None:
            stmt = stmt.where(ArenaModelVersion.model_type == model_type)
        if status is not None:
            stmt = stmt.where(ArenaModelVersion.status == status)
        versions = session.exec(stmt.order_by(ArenaModelVersion.model_type, ArenaModelVersion.id)).all()

    return {
        "versions": [
            {
                "id": v.id, "name": v.name, "model_type": v.model_type,
                "is_active": v.is_active, "status": v.status,
                "trained_at": v.trained_at.isoformat(), "activated_at": v.activated_at.isoformat() if v.activated_at else None,
                "deactivated_at": v.deactivated_at.isoformat() if v.deactivated_at else None,
                "training_period": (
                    {"start": v.training_period_start.isoformat(), "end": v.training_period_end.isoformat()}
                    if v.training_period_start and v.training_period_end else None
                ),
                "validation_period": (
                    {"start": v.validation_period_start.isoformat(), "end": v.validation_period_end.isoformat()}
                    if v.validation_period_start and v.validation_period_end else None
                ),
                "test_period": (
                    {"start": v.test_period_start.isoformat(), "end": v.test_period_end.isoformat()}
                    if v.test_period_start and v.test_period_end else None
                ),
                "sample_size": v.sample_size,
                "feature_version": v.feature_version,
                "metrics": json.loads(v.metrics) if v.metrics else None,
                "baseline_version_id": v.baseline_version_id,
                "notes": v.notes,
            }
            for v in versions
        ],
        "count": len(versions),
    }


@app.get("/models/training/status")
def get_models_training_status_endpoint(model_type: str | None = None):
    """
    Aperçu en LECTURE SEULE de la disponibilité des données d'entraînement
    (§28) et de ce qu'un `python scripts/retrain_ml_models.py --model ... --dry-run`
    afficherait — jamais un déclenchement d'entraînement (voir Partie F du
    rapport final Phase 9 : retrain/promote/shadow restent CLI-only).
    """
    supported = arena_retraining.SUPPORTED_MODEL_TYPES
    if model_type is not None and model_type not in supported:
        raise HTTPException(status_code=400, detail=f"model_type inconnu : '{model_type}'. Valeurs possibles : {list(supported)}")

    model_types = [model_type] if model_type else list(supported)
    result: dict = {}
    with Session(engine) as session:
        for mt in model_types:
            r = arena_retraining.run_retrain(session, mt, dry_run=True)
            result[mt] = {
                "status": r.status,
                "message": r.message,
                "readiness": None if r.readiness is None else {
                    "ready": r.readiness.ready, "reason": r.readiness.reason,
                    "match_count": r.readiness.match_count, "league_count": r.readiness.league_count,
                    "duplicate_count": r.readiness.duplicate_count,
                    "missing_value_columns": r.readiness.missing_value_columns,
                    "period_start": r.readiness.period_start.isoformat() if r.readiness.period_start else None,
                    "period_end": r.readiness.period_end.isoformat() if r.readiness.period_end else None,
                    "leakage_detected": r.readiness.leakage_detected,
                },
            }
    return result


# ---------------------------------------------------------------------------
# Phase 10 — Promotion pilotée par les performances LIVE (voir app/ai/arena/
# promotion.py::evaluate_live_promotion / app/ai/arena/live_validation.py).
# GET (status/history) restent publics, comme le reste de l'Arena (données de
# mesure, jamais de secrets/PII) ; POST (evaluate/promote) exigent
# require_admin (ADMIN_EMAILS) — endpoints MUTANTS, jamais publics.
# ---------------------------------------------------------------------------

_PROMOTABLE_STATUSES = ("shadow", "candidate")


def _live_promotion_decision_response(d) -> dict:
    return {
        "status": d.status, "reason": d.reason, "model_type": d.model_type, "market": d.market,
        "candidate_version_id": d.candidate_version_id, "baseline_version_id": d.baseline_version_id,
        "candidate_metrics": _live_metrics_response(d.candidate_metrics),
        "baseline_metrics": _live_metrics_response(d.baseline_metrics),
        "min_sample_size": d.min_sample_size, "min_improvement": d.min_improvement,
    }


def _live_metrics_response(m: dict | None) -> dict | None:
    if m is None:
        return None
    return {
        **{k: v for k, v in m.items() if k not in ("period_start", "period_end")},
        "period_start": m["period_start"].isoformat() if m.get("period_start") else None,
        "period_end": m["period_end"].isoformat() if m.get("period_end") else None,
    }


def _record_promotion_event(
    session: Session, decision, *, actor: str, automatic: bool, previous_model_version_id: int | None,
) -> ModelPromotionEvent:
    metrics_snapshot = json.dumps({
        "candidate": _live_metrics_response(decision.candidate_metrics),
        "baseline": _live_metrics_response(decision.baseline_metrics),
    })
    sample_size = None
    if decision.candidate_metrics is not None:
        sample_size = decision.candidate_metrics.get("sample_size")
    event = ModelPromotionEvent(
        model_version_id=decision.candidate_version_id,
        previous_model_version_id=previous_model_version_id,
        model_type=decision.model_type, market=decision.market,
        decision=decision.status, reason=decision.reason,
        metrics=metrics_snapshot, sample_size=sample_size,
        actor=actor, automatic=automatic,
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


@app.get("/models/promotion/status")
def get_models_promotion_status_endpoint(model_type: str | None = None, market: str = "1X2"):
    """
    Pour chaque ModelVersion candidate à la promotion (status shadow ou
    candidate) du/des model_type demandé(s) : décision LIVE actuelle
    (voir evaluate_live_promotion), SANS rien écrire dans l'historique — un
    simple GET de consultation n'est jamais une "décision" tracée (voir POST
    /models/promotion/evaluate pour ça).
    """
    if model_type is not None and model_type not in KNOWN_MODEL_TYPES:
        raise HTTPException(status_code=400, detail=f"model_type inconnu : '{model_type}'. Valeurs possibles : {list(KNOWN_MODEL_TYPES)}")
    if market not in ARENA_MARKETS:
        raise HTTPException(status_code=400, detail=f"Marché inconnu : '{market}'. Marchés disponibles : {list(ARENA_MARKETS)}")

    model_types = [model_type] if model_type else list(KNOWN_MODEL_TYPES)
    candidates: list[dict] = []
    with Session(engine) as session:
        for mt in model_types:
            versions = session.exec(
                select(ArenaModelVersion).where(
                    ArenaModelVersion.model_type == mt, ArenaModelVersion.status.in_(_PROMOTABLE_STATUSES),
                )
            ).all()
            for v in versions:
                decision = arena_promotion.evaluate_live_promotion(session, v.id, market)
                candidates.append({
                    "model_version_id": v.id, "version_name": v.name, "status": v.status,
                    **_live_promotion_decision_response(decision),
                })
    return {"market": market, "candidates": candidates, "count": len(candidates)}


@app.get("/models/promotion/history")
def get_models_promotion_history_endpoint(
    model_type: str | None = None, decision: str | None = None, limit: int = 50,
):
    """Historique append-only des décisions de promotion (§9/règle 11 du
    ticket Phase 10 : rien de silencieux), plus récentes d'abord."""
    limit = max(1, min(limit, 200))
    with Session(engine) as session:
        stmt = select(ModelPromotionEvent)
        if model_type is not None:
            stmt = stmt.where(ModelPromotionEvent.model_type == model_type)
        if decision is not None:
            stmt = stmt.where(ModelPromotionEvent.decision == decision)
        events = session.exec(stmt.order_by(ModelPromotionEvent.created_at.desc()).limit(limit)).all()

    return {
        "events": [
            {
                "id": e.id, "model_version_id": e.model_version_id,
                "previous_model_version_id": e.previous_model_version_id,
                "model_type": e.model_type, "market": e.market, "decision": e.decision, "reason": e.reason,
                "metrics": json.loads(e.metrics) if e.metrics else None, "sample_size": e.sample_size,
                "created_at": e.created_at.isoformat(), "actor": e.actor, "automatic": e.automatic,
            }
            for e in events
        ],
        "count": len(events),
    }


class PromotionEvaluateRequest(BaseModel):
    model_version_id: int
    market: str = "1X2"


@app.post("/models/promotion/evaluate")
def post_models_promotion_evaluate(payload: PromotionEvaluateRequest, user: User = Depends(require_admin)):
    """
    Évalue une version candidate SANS la promouvoir (§10 : "évalue une
    version candidate sans forcément la promouvoir") — écrit tout de même
    une ligne d'historique (jamais une évaluation silencieuse, admin ou
    non) avec `automatic=False`, `actor=<email admin>`.
    """
    if payload.market not in ARENA_MARKETS:
        raise HTTPException(status_code=400, detail=f"Marché inconnu : '{payload.market}'. Marchés disponibles : {list(ARENA_MARKETS)}")
    with Session(engine) as session:
        version = session.get(ArenaModelVersion, payload.model_version_id)
        if version is None:
            raise HTTPException(status_code=404, detail=f"Aucune ModelVersion avec id={payload.model_version_id}.")
        decision = arena_promotion.evaluate_live_promotion(session, payload.model_version_id, payload.market)
        _record_promotion_event(
            session, decision, actor=user.email, automatic=False,
            previous_model_version_id=decision.baseline_version_id,
        )
    return _live_promotion_decision_response(decision)


class PromotionPromoteRequest(BaseModel):
    model_version_id: int
    market: str = "1X2"


@app.post("/models/promotion/promote")
def post_models_promotion_promote(payload: PromotionPromoteRequest, user: User = Depends(require_admin)):
    """
    Applique une promotion — ré-évalue TOUJOURS côté serveur avant
    d'appliquer (§ : "ne jamais faire confiance à une décision envoyée par
    le client"). Rejette (400) et journalise quand même si la décision
    n'est pas "eligible" — une tentative de promotion refusée reste une
    décision tracée, jamais un échec silencieux.
    """
    if payload.market not in ARENA_MARKETS:
        raise HTTPException(status_code=400, detail=f"Marché inconnu : '{payload.market}'. Marchés disponibles : {list(ARENA_MARKETS)}")
    with Session(engine) as session:
        version = session.get(ArenaModelVersion, payload.model_version_id)
        if version is None:
            raise HTTPException(status_code=404, detail=f"Aucune ModelVersion avec id={payload.model_version_id}.")

        decision = arena_promotion.evaluate_live_promotion(session, payload.model_version_id, payload.market)
        previous = arena_promotion.get_active_version(session, decision.model_type)
        previous_id = previous.id if previous is not None else None

        if decision.status != "eligible":
            _record_promotion_event(
                session, decision, actor=user.email, automatic=False, previous_model_version_id=previous_id,
            )
            raise HTTPException(
                status_code=400,
                detail={"message": "Promotion refusée.", "decision": _live_promotion_decision_response(decision)},
            )

        arena_promotion.apply_promotion(session, version)
        _record_promotion_event(
            session, dataclasses.replace(decision, status="promoted"),
            actor=user.email, automatic=False, previous_model_version_id=previous_id,
        )

    return {"status": "promoted", "model_version_id": payload.model_version_id, "previous_model_version_id": previous_id}


# ---------------------------------------------------------------------------
# Phase 11 — Live Shadow Comparison (voir app/ai/arena/shadow_comparison.py).
# Publics comme le reste de l'observabilité Arena (mesure pure, aucune
# décision appliquée ici — pour la décision de promotion, voir
# /models/promotion/* ci-dessus).
# ---------------------------------------------------------------------------

@app.get("/models/shadow/status")
def get_models_shadow_status_endpoint(model_type: str | None = None):
    """
    Vue d'ensemble ACTIVE vs SHADOW par model_type : version(s) en place,
    disponibilité LIVE, compteurs pending/resolved de chaque rôle (§10 du
    ticket Phase 11) — réutilise compute_model_availability (Phase 8) et
    get_live_summary (Phase 9), aucun calcul dupliqué.
    """
    if model_type is not None and model_type not in KNOWN_MODEL_TYPES:
        raise HTTPException(status_code=400, detail=f"model_type inconnu : '{model_type}'. Valeurs possibles : {list(KNOWN_MODEL_TYPES)}")
    model_types = [model_type] if model_type else list(KNOWN_MODEL_TYPES)

    result: dict = {}
    with Session(engine) as session:
        availability = compute_model_availability(session, _PREDICTION_MODELS)
        for mt in model_types:
            active_version = arena_promotion.get_active_version(session, mt)
            shadow_versions = session.exec(
                select(ArenaModelVersion).where(ArenaModelVersion.model_type == mt, ArenaModelVersion.status == "shadow")
            ).all()
            active_summary = arena_monitoring.get_live_summary(session, mt, role="active")
            avail = availability.get(mt)

            result[mt] = {
                "active": {
                    "model_version_id": active_version.id if active_version else None,
                    "version_name": active_version.name if active_version else None,
                    "live_available": avail.live_available if avail else False,
                    "predictions_pending": active_summary.predictions_pending,
                    "predictions_resolved": active_summary.predictions_resolved,
                },
                "shadow": [
                    {
                        "model_version_id": sv.id, "version_name": sv.name,
                        "predictions_pending": sv_metrics.predictions_pending,
                        "predictions_resolved": sv_metrics.predictions_resolved,
                    }
                    for sv in shadow_versions
                    # Scopé par model_version_id (comme live_validation.py, jamais par role
                    # seul) : si plusieurs versions shadow coexistent pour ce model_type, chacune
                    # affiche SES PROPRES compteurs, jamais un total mélangé (§8 du ticket Phase 11).
                    for sv_metrics in [arena_live_validation.compute_live_model_metrics(session, mt, sv.id, "1X2")]
                ],
            }
    return {"status": "ok", "models": result}


@app.get("/models/shadow/comparison")
def get_models_shadow_comparison_endpoint(model_type: str, market: str = "1X2", shadow_version_id: int | None = None):
    """
    Comparaison "matched" ACTIVE vs SHADOW — UNIQUEMENT sur les matchs
    réellement prédits ET résolus par les DEUX (§9 du ticket Phase 11),
    jamais deux échantillons indépendants présentés comme comparables.
    """
    if model_type not in KNOWN_MODEL_TYPES:
        raise HTTPException(status_code=400, detail=f"model_type inconnu : '{model_type}'. Valeurs possibles : {list(KNOWN_MODEL_TYPES)}")
    if market not in ARENA_MARKETS:
        raise HTTPException(status_code=400, detail=f"Marché inconnu : '{market}'. Marchés disponibles : {list(ARENA_MARKETS)}")

    with Session(engine) as session:
        comparison = arena_shadow_comparison.compute_matched_comparison(
            session, model_type, market, shadow_version_id=shadow_version_id,
        )

    logger.info(
        f"[LIVE_SHADOW_EVALUATION_COMPLETED] model_type={model_type} market={market} "
        f"status={comparison.status} matched_sample_size={comparison.matched_sample_size}"
    )
    return {
        "status": comparison.status, "reason": comparison.reason,
        "model_type": comparison.model_type, "market": comparison.market,
        "active_version_id": comparison.active_version_id, "shadow_version_id": comparison.shadow_version_id,
        "matched_sample_size": comparison.matched_sample_size,
        "min_matched_sample_size": comparison.min_matched_sample_size,
        "active": {
            "accuracy": comparison.active_accuracy, "log_loss": comparison.active_log_loss, "brier_score": comparison.active_brier,
        },
        "shadow": {
            "accuracy": comparison.shadow_accuracy, "log_loss": comparison.shadow_log_loss, "brier_score": comparison.shadow_brier,
        },
        "deltas": {
            "log_loss": comparison.delta_log_loss, "brier_score": comparison.delta_brier, "accuracy": comparison.delta_accuracy,
        },
        "period_start": comparison.period_start.isoformat() if comparison.period_start else None,
        "period_end": comparison.period_end.isoformat() if comparison.period_end else None,
    }


@app.get("/models/{model_version_id}", response_model=ModelPerformanceEntry)
def get_model_version_detail_endpoint(model_version_id: int):
    """
    Détail d'une version de modèle BACKTESTÉE (ligne réelle de
    `model_versions`, id auto-incrémenté — jamais de schéma d'id fabriqué).
    Le modèle de production Dixon-Coles n'a pas de ModelVersion associée :
    voir GET /models/performance, entrée source="model_artifact".
    """
    with Session(engine) as session:
        entry = get_model_version_detail(session, model_version_id)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Aucune version de modèle backtestée avec id={model_version_id}. "
                "Le modèle de production Dixon-Coles n'a pas d'id ici — voir GET /models/performance."
            ),
        )
    return entry
