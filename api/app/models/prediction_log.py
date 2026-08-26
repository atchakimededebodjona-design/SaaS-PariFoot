"""
Historique des prédictions générées par l'app, pour la page "Historique &
Performance" du frontend (voir GET /predictions/history dans api/main.py).

Une ligne par (league, match_date, home_team, away_team) — `match_date` est
la vraie date du coup d'envoi, transmise par l'appelant (frontend, depuis
les fixtures officielles déjà connues) via le paramètre `kickoff_date` de
GET /predictions/{league}/{home}/{away} et POST /predictions/batch (voir
main.py::_log_prediction). Sans elle (ex. match "Sur-Mesure" sans fixture
réelle associée), on retombe sur la date du jour de la requête.

AVANT ce comportement, `match_date` stockait la date de la DEMANDE de
prédiction (pas la vraie date de match), en supposant que l'utilisateur
prédit toujours "le match du jour" — hypothèse fausse en pratique (l'app
permet de prédire les matchs de toute la semaine à l'avance), ce qui
empêchait fetch_daily_results.py de jamais rapprocher un résultat (il
cherche des fixtures terminées CE jour-là). Les lignes créées avant ce
changement gardent l'ancienne sémantique et resteront probablement NULL
sur result_* pour toujours — sans conséquence, elles ne sont pas
réécrites.

`payload` reproduit la réponse MatchPrediction complète (mêmes raisons
que ModelArtifact.payload : éviter de dupliquer/faire diverger le
schéma). Les colonnes result_*/correct_* restent NULL tant que
fetch_daily_results.py n'a pas rapproché de résultat réel (API-Football).
"""

from datetime import date, datetime, timezone
from typing import Optional
from sqlalchemy import UniqueConstraint
from sqlmodel import SQLModel, Field


class PredictionLog(SQLModel, table=True):
    __tablename__ = "prediction_log"
    __table_args__ = (
        UniqueConstraint("league", "match_date", "home_team", "away_team", name="uq_predlog_league_date_teams"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    league: str = Field(index=True)
    match_date: date = Field(index=True)
    home_team: str
    away_team: str
    payload: str

    pick_1x2: str
    pick_btts: str
    pick_over_2_5: str

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    result_home_goals: Optional[int] = None
    result_away_goals: Optional[int] = None
    result_fetched_at: Optional[datetime] = None
    correct_1x2: Optional[bool] = None
    correct_btts: Optional[bool] = None
    correct_over_2_5: Optional[bool] = None
