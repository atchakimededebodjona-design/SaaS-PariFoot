"""
core.py — Phase 8D : XFOOT ODDS RESEARCH PROTOTYPE V1.

Fonctions PURES (aucun accès réseau/DB) : validation de cotes décimales,
probabilité implicite, overround, retrait de marge, correspondance
ligue/équipe football-data.co.uk -> Xfoot, classification de la qualité du
timestamp. Aucune de ces fonctions n'écrit en base, ne fait d'appel HTTP, ni
ne modifie une table de production — voir scripts/odds_research_walkforward.py
pour l'orchestration (téléchargement, fusion, walk-forward).

=== Pourquoi le mapping équipe est un rapprochement EXACT, pas fuzzy (§26) ===

Xfoot utilise DÉJÀ la convention de nommage football-data.co.uk comme
référence canonique interne — voir api/app/core/team_name_matching.py,
docstring : "Rapprochement des noms d'équipe renvoyés par API-Football avec
les noms canoniques internes (football-data.co.uk, cf. api/model_artifacts/
*.json)". Vérifié empiriquement (Phase 8D) : les noms d'équipe Premier League
en base (`Man City`, `Man United`, `Nott'm Forest`, ...) sont identiques aux
noms retournés par football-data.co.uk. Un rapprochement EXACT (après
normalisation triviale espace/casse) suffit donc — aucun fuzzy matching,
jamais une correspondance approximative silencieuse.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Optional

# ---------------------------------------------------------------------------
# §10-§13 : normalisation des cotes, probabilité implicite, overround, marge
# ---------------------------------------------------------------------------

def is_valid_decimal_odds(value) -> bool:
    """Une cote décimale valide est un nombre fini > 1.0 (§25 : odds <= 1,
    null, infinie sont invalides — rejetées, jamais corrigées)."""
    if value is None:
        return False
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    if math.isnan(v) or math.isinf(v):
        return False
    return v > 1.0


def implied_probability(odds: float) -> float:
    """raw_implied_probability = 1 / odds (§11) — JAMAIS appelée "probabilité
    vraie", uniquement la probabilité implicite BRUTE (marge incluse)."""
    if not is_valid_decimal_odds(odds):
        raise ValueError(f"cote invalide : {odds!r}")
    return 1.0 / float(odds)


def overround(raw_probs: list[float]) -> float:
    """sum(implied probabilities) - 1 (§12) — la marge du bookmaker."""
    return sum(raw_probs) - 1.0


def normalize_margin(raw_probs: list[float]) -> list[float]:
    """p_normalized_i = p_i / sum(p_j) (§13) — méthode proportionnelle simple
    et documentée. Pas de méthode plus complexe (ex. Shin) sans nécessité
    démontrée — aucune autre méthode de retrait de marge n'est déjà utilisée
    ailleurs dans le dépôt (grep : aucune occurrence avant cette phase)."""
    total = sum(raw_probs)
    if total <= 0:
        raise ValueError("somme des probabilités implicites <= 0 — retrait de marge impossible")
    return [p / total for p in raw_probs]


def compute_1x2_odds_features(odds_home, odds_draw, odds_away) -> Optional[dict]:
    """§14 : implied probability 1X2 (brute + normalisée) + overround.
    None si UNE SEULE des 3 cotes est invalide — jamais imputée (§24)."""
    values = (odds_home, odds_draw, odds_away)
    if not all(is_valid_decimal_odds(v) for v in values):
        return None
    raw = [implied_probability(v) for v in values]
    norm = normalize_margin(raw)
    return {
        "raw_home": raw[0], "raw_draw": raw[1], "raw_away": raw[2],
        "norm_home": norm[0], "norm_draw": norm[1], "norm_away": norm[2],
        "overround": overround(raw),
    }


def compute_ou25_odds_features(odds_over, odds_under) -> Optional[dict]:
    """§14 : implied probability Over/Under 2.5 (brute + normalisée) + overround."""
    values = (odds_over, odds_under)
    if not all(is_valid_decimal_odds(v) for v in values):
        return None
    raw = [implied_probability(v) for v in values]
    norm = normalize_margin(raw)
    return {"raw_over": raw[0], "raw_under": raw[1], "norm_over": norm[0], "norm_under": norm[1], "overround": overround(raw)}


# ---------------------------------------------------------------------------
# §26-§28 : mapping ligue / équipe (déterministe, jamais fuzzy)
# ---------------------------------------------------------------------------

DIV_TO_LEAGUE = {
    "E0": "PremierLeague", "SP1": "LaLiga", "D1": "Bundesliga", "I1": "SerieA", "F1": "Ligue1",
}
LEAGUE_TO_DIV = {v: k for k, v in DIV_TO_LEAGUE.items()}


def normalize_team_name(name: str) -> str:
    """Normalisation TRIVIALE (espaces/casse) uniquement — jamais un
    rapprochement fuzzy. Voir docstring module : Xfoot et football-data.co.uk
    partagent déjà la même convention de nommage."""
    return " ".join(str(name).strip().split())


def map_league(div: str) -> Optional[str]:
    return DIV_TO_LEAGUE.get(div)


def match_key(league: str, match_date: date, home_team: str, away_team: str) -> tuple:
    """Clé de rapprochement DÉTERMINISTE provider -> Xfoot (§26) : (league,
    date, home_team normalisé, away_team normalisé). Toute clé ne trouvant
    aucune correspondance exacte dans `match` est REJECTED — jamais forcée
    par une correspondance approximative."""
    return (league, match_date, normalize_team_name(home_team), normalize_team_name(away_team))


# ---------------------------------------------------------------------------
# §5, §6, §29 : qualité du timestamp
# ---------------------------------------------------------------------------

TIMESTAMP_QUALITIES = ("SAFE", "CAUTION", "LEAKAGE_RISK", "REJECTED", "UNKNOWN")

# football-data.co.uk NE fournit PAS de timestamp exact par ligne — seule une
# règle de collecte DOCUMENTÉE (notes.txt, vérifiée Phase 8C) est connue :
# cotes "pré-clôture" collectées le vendredi après-midi (matchs week-end) ou
# le mardi après-midi (matchs en semaine) ; cotes de "clôture" juste avant le
# coup d'envoi. C'est une INFÉRENCE méthodologique, JAMAIS un timestamp
# mesuré -> CAUTION systématique, jamais SAFE, pour les deux snapshots.
PRE_CLOSING_SOURCE = "pre_closing"
CLOSING_SOURCE = "closing"


def classify_source_timestamp_quality(source: str) -> str:
    """§29 : classification par snapshot. football-data.co.uk n'expose aucun
    timestamp par ligne mesuré -> ni pre_closing ni closing ne peuvent être
    SAFE. Les deux restent CAUTION (méthodologie documentée par le
    fournisseur, jamais un timestamp exact)."""
    if source in (PRE_CLOSING_SOURCE, CLOSING_SOURCE):
        return "CAUTION"
    return "UNKNOWN"


def validate_explicit_timestamp(odds_timestamp, kickoff) -> str:
    """§5/§6/§67 : pour une observation qui EXPOSE un timestamp explicite
    (données synthétiques de test, ou une future source qui en fournirait un) :
    SAFE si odds_timestamp < kickoff strictement ; LEAKAGE_RISK si
    odds_timestamp >= kickoff (fuite avérée, y compris égalité) ; REJECTED si
    le timestamp est manquant."""
    if odds_timestamp is None:
        return "REJECTED"
    if odds_timestamp >= kickoff:
        return "LEAKAGE_RISK"
    return "SAFE"


# ---------------------------------------------------------------------------
# §25 : contrôle qualité d'une ligne brute football-data.co.uk
# ---------------------------------------------------------------------------

def check_row_quality(row: dict) -> Optional[str]:
    """Retourne la raison de rejet (§25) si la ligne est structurellement
    invalide, None si elle est exploitable pour au moins un marché.
    - "missing_date" : Date absente/invalide.
    - "future_date" : Date postérieure à aujourd'hui (contrôle sanité, jamais
      un vrai cas football-data.co.uk mais testé — §67).
    - "missing_teams" : HomeTeam/AwayTeam absent.
    Les cotes elles-mêmes sont validées séparément par marché
    (compute_1x2_odds_features/compute_ou25_odds_features), une ligne peut
    être valide pour 1X2 mais invalide pour O/U (jamais rejetée en bloc)."""
    if not row.get("Date"):
        return "missing_date"
    if not row.get("HomeTeam") or not row.get("AwayTeam"):
        return "missing_teams"
    return None
