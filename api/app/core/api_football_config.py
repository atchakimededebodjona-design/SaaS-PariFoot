"""
Configuration API-Football (api-football.com) — utilisée par :
  - fetch_daily_results.py (cron séparé) pour rapprocher les prédictions
    loguées (app/models/prediction_log.py) des scores finaux.
  - api/main.py (service web) pour GET /live-scores — scores en direct,
    appelés directement depuis le process web (voir _get_live_scores),
    contrairement aux résultats quotidiens qui restent dans le cron séparé.

Variables d'environnement requises (voir .env.example) :
    API_FOOTBALL_KEY — clé du dashboard https://dashboard.api-football.com/

IMPORTANT DÉPLOIEMENT : API_FOOTBALL_KEY doit être définie SUR LES DEUX
services Railway — le service web (pour /live-scores) ET le service cron
fetch_daily_results.py (voir RAILWAY_CRON_SETUP.md, section "Résultats
quotidiens"). Contrairement à CHARIOW_API_KEY, ce module ne lève PAS de
RuntimeError si la clé est absente en production — un service qui en a
besoin échoue simplement silencieusement (voir best-effort dans
_get_live_scores et fetch_daily_results.run()) plutôt que de bloquer le
démarrage de tout le service web pour une fonctionnalité annexe.

API_FOOTBALL_LEAGUE_IDS — vérifiés dans le dashboard API-Football
(Football → Ids → recherche par nom de championnat, filtré sur Current=True) :
Premier League 39, LaLiga 140, Serie A 135, Bundesliga 78, Ligue 1 61,
Primeira Liga (Portugal) 94, MLS 253, Saudi Pro League 307,
UEFA Champions League 2, UEFA Europa League 3,
UEFA Europa Conference League 848 (pas d'id séparé pour les tours de
qualification — inclus dans l'id de la compétition principale).
"""

import os

API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "")
API_FOOTBALL_BASE_URL = "https://v3.football.api-sports.io"

API_FOOTBALL_LEAGUE_IDS = {
    "PremierLeague": 39,
    "LaLiga": 140,
    "SerieA": 135,
    "Bundesliga": 78,
    "Ligue1": 61,
    "PrimeiraLiga": 94,
    "MLS": 253,
    "SaudiProLeague": 307,
    "ChampionsLeague": 2,
    "EuropaLeague": 3,
    "ConferenceLeague": 848,
}
