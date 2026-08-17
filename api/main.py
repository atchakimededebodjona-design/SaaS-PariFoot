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

import json
import logging
import os
import unicodedata
import difflib
from pathlib import Path
from datetime import datetime, timezone, timedelta

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
# sources). Couvre les 5 ligues du dataset (Ligue1, PremierLeague, LaLiga,
# Bundesliga, SerieA) — vérifié contre la liste réelle des équipes dans
# model_artifacts/*.json (cf. export_model_artifacts.py).
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


# ---------------------------------------------------------------------------
# Schémas de réponse (Pydantic — documentation auto-générée par FastAPI)
# ---------------------------------------------------------------------------

class OverUnderLine(BaseModel):
    line: float
    over: float
    under: float


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
        most_likely_scores=scores,
        model_trained_at=model.trained_at,
        model_data_up_to=model.data_up_to,
        home_team_resolution=home_res["method"],
        away_team_resolution=away_res["method"],
    )
    _log_prediction(prediction)
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
