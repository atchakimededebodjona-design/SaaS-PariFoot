"""
team_name_matching.py — Rapprochement des noms d'équipe renvoyés par
API-Football avec les noms canoniques internes (football-data.co.uk, cf.
api/model_artifacts/*.json).

Extrait de fetch_daily_results.py (Phase 6) pour être réutilisé tel quel par
le scheduler de prédictions live (Phase 9, api/app/ai/arena/scheduler.py) —
même logique, mêmes deux sens d'utilisation (nom canonique déjà connu ->
vérifier une fixture API-Football ; ou l'inverse pour choisir une équipe
dans une liste), aucune réimplémentation.
"""

import difflib
import unicodedata

# Nommage API-Football -> nommage canonique interne (football-data.co.uk,
# cf. api/model_artifacts/*.json) pour les cas où normalisation+fuzzy ne
# suffisent pas seuls (abréviations/noms officiels trop différents). Liste
# non exhaustive au démarrage — à enrichir empiriquement au fil des
# exécutions réelles (les cas non rapprochés sont loggés en warning avec
# les deux noms, pour repérage facile).
API_FOOTBALL_TEAM_ALIASES = {
    "manchester united": "Man United",
    "manchester city": "Man City",
    "nottingham forest": "Nott'm Forest",
    "west bromwich albion": "West Brom",
    "wolverhampton wanderers": "Wolves",
    "newcastle united": "Newcastle",
    "leicester city": "Leicester",
    "sheffield utd": "Sheffield United",
    "brighton & hove albion": "Brighton",
    "tottenham hotspur": "Tottenham",
    "paris saint germain": "Paris SG",
    "saint-etienne": "St Etienne",
    "olympique de marseille": "Marseille",
    "olympique lyonnais": "Lyon",
    "borussia monchengladbach": "M'gladbach",
    "bayer leverkusen": "Leverkusen",
    "internazionale": "Inter",
    "ac milan": "Milan",
}


def _normalize(name: str) -> str:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return n.lower().strip()


def names_match(api_football_name: str, canonical_name: str, cutoff: float = 0.6) -> bool:
    """Le nom canonique est déjà connu (celui stocké au moment de la
    prédiction) — on vérifie juste que le nom renvoyé par API-Football
    désigne la même équipe, jamais l'inverse (pas besoin de choisir parmi
    toute la ligue)."""
    na, nb = _normalize(api_football_name), _normalize(canonical_name)
    if na == nb:
        return True
    alias = API_FOOTBALL_TEAM_ALIASES.get(na)
    if alias is not None and _normalize(alias) == nb:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= cutoff
