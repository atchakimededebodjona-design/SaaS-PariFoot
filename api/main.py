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
from datetime import datetime, timezone

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

class MatchPrediction(BaseModel):
    league: str
    home_team: str
    away_team: str
    home_win: float = Field(..., description="Probabilité de victoire à domicile")
    draw: float
    away_win: float
    over_2_5: float
    under_2_5: float
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
    scores = model.most_likely_scores(home_team, away_team)

    return MatchPrediction(
        league=league,
        home_team=home_team,
        away_team=away_team,
        home_win=probs_1x2["home_win"],
        draw=probs_1x2["draw"],
        away_win=probs_1x2["away_win"],
        over_2_5=probs_ou["over"],
        under_2_5=probs_ou["under"],
        most_likely_scores=scores,
        model_trained_at=model.trained_at,
        model_data_up_to=model.data_up_to,
        home_team_resolution=home_res["method"],
        away_team_resolution=away_res["method"],
    )


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


@app.get("/ratings/{league}", response_model=list[TeamRating])
def get_ratings(league: str):
    if league not in LEAGUE_MODELS:
        raise HTTPException(
            status_code=404,
            detail=f"Ligue inconnue : '{league}'. Ligues disponibles : {list(LEAGUE_MODELS.keys())}",
        )
    return LEAGUE_MODELS[league].ratings()
