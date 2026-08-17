"""
Configuration API-Football (api-football.com) — source des résultats réels
utilisée par fetch_daily_results.py pour rapprocher les prédictions loguées
(app/models/prediction_log.py) des scores finaux.

Variables d'environnement requises (voir .env.example) :
    API_FOOTBALL_KEY — clé du dashboard https://dashboard.api-football.com/

Ce module n'est importé QUE par fetch_daily_results.py (le service web
api/main.py n'a jamais besoin d'appeler API-Football directement) — pas de
vérification RuntimeError au démarrage de l'API, contrairement à
chariow_config.py qui est bien utilisé par le service web.

API_FOOTBALL_LEAGUE_IDS — NON VÉRIFIÉ : ids v3 usuels (Premier League 39,
LaLiga 140, Serie A 135, Bundesliga 78, Ligue 1 61), à confirmer dans le
dashboard API-Football avant la première exécution en production.
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
}
