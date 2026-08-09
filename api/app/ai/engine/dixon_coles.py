"""
Wrapper Dixon-Coles — entraîne à partir des tables match/match_stats
(Phase 2) plutôt que depuis data/all_leagues_raw_with_stats.csv.

Réutilise TEL QUEL le moteur d'entraînement de production
(export_model_artifacts._FastDixonColesL2, la version vectorisée numpy —
numériquement équivalente à 1e-5 près à dixon_coles.DixonColesModel, la
classe de référence, voir dixon_coles_fast.py) : ce ticket change
uniquement la SOURCE des données, jamais les mathématiques. On appelle
directement export_model_artifacts.export_league() (même fonction que
celle utilisée par le job de ré-entraînement en prod) pour garantir que
toute divergence de résultat vienne de la donnée, jamais d'une
réimplémentation légèrement différente.

Import inter-dossier volontaire (racine du dépôt, hors de api/) : ce
wrapper n'est PAS appelé par api/main.py à ce stade (voir Phase 3, §5 —
« aucune exposition prématurée ») et reste un outil d'entraînement/backtest
hors-ligne, au même titre qu'export_model_artifacts.py lui-même — jamais
exécuté dans le service web en production (dont le Root Directory Railway
est api/, qui n'inclut pas la racine du dépôt). Si une phase future
branche ce wrapper sur un endpoint servi en direct, il faudra alors
rapatrier _FastDixonColesL2 à l'intérieur de api/ pour lever cette
dépendance croisée — pas un problème pour ce ticket, mais à ne pas
oublier (voir RAPPORT_PHASE3 pour le détail).
"""

import sys
from pathlib import Path

import pandas as pd
from sqlmodel import Session, select

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from export_model_artifacts import export_league  # noqa: E402

from app.core.database import engine  # noqa: E402
from app.models.match import Match  # noqa: E402


def load_matches_from_db(league: str | None = None) -> pd.DataFrame:
    """
    Reconstruit un DataFrame de même forme que celui lu depuis
    data/all_leagues_raw_with_stats.csv (colonnes date/home_team/away_team/
    home_goals/away_goals/league) à partir de la table `match`. Les
    colonnes de `match_stats` (tirs, corners) ne sont pas jointes : Dixon-
    Coles n'utilise que le score, exactement comme le pipeline CSV actuel.
    """
    with Session(engine) as session:
        stmt = select(Match)
        if league is not None:
            stmt = stmt.where(Match.league == league)
        rows = session.exec(stmt).all()

    return pd.DataFrame([{
        "date": r.date, "home_team": r.home_team, "away_team": r.away_team,
        "home_goals": r.home_goals, "away_goals": r.away_goals, "league": r.league,
    } for r in rows])


def train_all_leagues_from_db() -> dict:
    """
    Équivalent, sourcé en base, de export_model_artifacts.train_all_leagues()
    (qui lit le CSV) : même boucle par ligue (tri chronologique, puis
    export_league()), même dict de sortie par ligue.
    """
    df = load_matches_from_db()
    artifacts = {}
    for league_name, sub in df.groupby("league"):
        sub = sub.sort_values("date").reset_index(drop=True)
        artifacts[league_name] = export_league(league_name, sub)
    return artifacts
