"""
integrity.py — Phase 8E : XFOOT ODDS TIMESTAMP & HISTORICAL INTEGRITY AUDIT V1.

Fonctions PURES (aucun accès réseau/DB) : hiérarchie temporelle T_ODDS/
T_CUTOFF/T_MATCH (§2-§4), classification à 5 voies d'une observation odds
(§8), retrait de marge SAFE au niveau consensus (§17-§20), audit
opening/closing (§15/§16). Aucune de ces fonctions n'écrit en base, ne fait
d'appel réseau, ni ne modifie une table de production.

=== Constat central de cette phase (voir docstring de
    scripts/odds_integrity_audit.py pour l'audit complet) ===

football-data.co.uk (source Phase 8D) NE fournit AUCUN timestamp par
observation de cote — seule une colonne `Time` existe, et c'est l'heure de
COUP D'ENVOI du match (T_MATCH), jamais un T_ODDS mesuré. La classification
"pré-clôture"/"clôture" de Phase 8D repose sur une méthodologie DOCUMENTÉE
par le fournisseur (notes.txt), jamais un timestamp mesuré ligne par ligne.
Conséquence directe (§7/§51) : AUCUNE observation de ce dataset ne peut
recevoir la classification TEMPORALLY_VERIFIED — toutes celles dont les
cotes sont valides tombent dans HISTORICAL_BUT_UNTIMESTAMPED.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

# ---------------------------------------------------------------------------
# §3-§4 : hiérarchie temporelle et cas limites (comparaison EXPLICITE —
# nécessite un T_ODDS réellement mesuré ; voir classify_observation pour le
# cas, très majoritaire dans ce dataset, où T_ODDS n'existe pas du tout).
# ---------------------------------------------------------------------------

TEMPORAL_CLASSES = ("SAFE", "FUTURE_INFORMATION", "REJECTED", "UNKNOWN")


def classify_explicit_timestamp(t_odds: Optional[datetime], t_cutoff: datetime, t_match: Optional[datetime]) -> str:
    """§4 — cas limites, EXACTEMENT dans l'ordre du prompt :
    - T_ODDS = NULL -> REJECTED (donnée manquante, jamais un UNKNOWN optimiste
      par défaut — cohérent avec core.py::validate_explicit_timestamp).
    - T_ODDS >= T_MATCH -> REJECTED (fuite avérée, y compris égalité).
    - T_CUTOFF < T_ODDS < T_MATCH -> FUTURE_INFORMATION (connue après le
      cutoff théorique, mais avant le coup d'envoi).
    - T_ODDS <= T_CUTOFF -> SAFE (inclut l'égalité, "convention documentée" §4).
    `t_match` peut être None (kickoff inconnu) — dans ce cas seule la
    comparaison à t_cutoff est faite, jamais une supposition sur t_match.
    """
    if t_odds is None:
        return "REJECTED"
    if t_match is not None and t_odds >= t_match:
        return "REJECTED"
    if t_odds > t_cutoff:
        return "FUTURE_INFORMATION"
    return "SAFE"


# ---------------------------------------------------------------------------
# §8 : classification à 5 voies d'une observation (le cœur de l'audit).
# ---------------------------------------------------------------------------

OBSERVATION_CLASSES = (
    "TEMPORALLY_VERIFIED", "HISTORICAL_BUT_UNTIMESTAMPED",
    "TIMESTAMPED_BUT_AFTER_CUTOFF", "UNKNOWN", "REJECTED",
)


def classify_observation(
    *, is_valid_odds: bool, has_measured_timestamp: bool,
    t_odds: Optional[datetime] = None, t_cutoff: Optional[datetime] = None, t_match: Optional[datetime] = None,
) -> str:
    """§8 :
    E. REJECTED — cote structurellement invalide (§25, réutilise core.py::
       is_valid_decimal_odds en amont — cette fonction reçoit déjà le
       booléen, jamais recalculé ici).
    A. TEMPORALLY_VERIFIED — timestamp RÉELLEMENT mesuré ET classify_explicit_
       timestamp(...) == "SAFE".
    C. TIMESTAMPED_BUT_AFTER_CUTOFF — timestamp mesuré mais FUTURE_INFORMATION
       ou REJECTED (fuite avérée par rapport au cutoff/kickoff).
    B. HISTORICAL_BUT_UNTIMESTAMPED — cote valide, historique réel (le match
       a bien eu lieu, le résultat est réel), mais AUCUN timestamp mesuré
       n'existe pour cette observation — le cas football-data.co.uk (§7/§51).
    D. UNKNOWN — ne devrait normalement pas être atteint si has_measured_
       timestamp est correctement renseigné ; filet de sécurité si
       classify_explicit_timestamp retourne une valeur imprévue.
    """
    if not is_valid_odds:
        return "REJECTED"
    if not has_measured_timestamp:
        return "HISTORICAL_BUT_UNTIMESTAMPED"
    if t_cutoff is None:
        return "UNKNOWN"
    temporal = classify_explicit_timestamp(t_odds, t_cutoff, t_match)
    if temporal == "SAFE":
        return "TEMPORALLY_VERIFIED"
    if temporal in ("FUTURE_INFORMATION", "REJECTED"):
        return "TIMESTAMPED_BUT_AFTER_CUTOFF"
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# §15-§16 : audit opening / closing — distinction entre "colonne nommée
# opening/closing par la source" et "timestamp réellement prouvé".
# ---------------------------------------------------------------------------

def audit_source_label(label: str, has_measured_timestamp: bool) -> str:
    """§16 : une valeur étiquetée "closing" (ou "opening") par la source SANS
    timestamp mesuré doit rester HISTORICAL_BUT_UNTIMESTAMPED — jamais
    promue en TEMPORALLY_VERIFIED sur la seule foi de son étiquette."""
    if has_measured_timestamp:
        return "TEMPORALLY_VERIFIED"  # timestamp réel prouvé -> le label seul ne suffit plus à classer, mais ceci ne se produit jamais avec football-data.co.uk (voir docstring module)
    return "HISTORICAL_BUT_UNTIMESTAMPED"


# ---------------------------------------------------------------------------
# §17-§20, §42 : consensus SAFE — jamais le consensus pré-calculé par la
# source (provenance temporelle inconnue), toujours reconstruit à partir des
# SEULES observations dont le timestamp est mesuré et <= cutoff.
# ---------------------------------------------------------------------------

def safe_consensus(observations: list[dict], cutoff: datetime) -> Optional[dict]:
    """
    `observations` : liste de {"bookmaker": str, "timestamp": datetime|None,
    "implied_probs": {"home":float,"draw":float,"away":float}}.

    §19 : inclut UNIQUEMENT les observations avec timestamp mesuré ET
    timestamp <= cutoff. §20 : ne comble jamais un bookmaker absent — le
    consensus retourné indique explicitement bookmaker_count et la liste des
    bookmakers inclus (provenance, §21). None si aucun bookmaker ne qualifie
    (jamais un consensus fabriqué à partir de zéro observation valide).
    """
    included = [o for o in observations if o.get("timestamp") is not None and o["timestamp"] <= cutoff]
    if not included:
        return None
    n = len(included)
    return {
        "bookmaker_count": n,
        "bookmakers": sorted(o["bookmaker"] for o in included),
        "consensus_home": sum(o["implied_probs"]["home"] for o in included) / n,
        "consensus_draw": sum(o["implied_probs"]["draw"] for o in included) / n,
        "consensus_away": sum(o["implied_probs"]["away"] for o in included) / n,
        "excluded_bookmakers": sorted(o["bookmaker"] for o in observations if o not in included),
    }


# ---------------------------------------------------------------------------
# §24-§26 : reconstruction du kickoff (Date+Time football-data.co.uk) —
# TIMEZONE NON VÉRIFIÉE (documenté, jamais supposée UTC/CET par défaut).
# ---------------------------------------------------------------------------

def combine_date_time(date_str: str, time_str: Optional[str]) -> Optional[datetime]:
    """Reconstruit un datetime NAÏF (sans timezone — voir docstring module :
    football-data.co.uk ne documente pas la timezone de sa colonne `Time`,
    jamais supposée UTC/CET/heure locale du stade sans preuve). Retourne None
    si `Time` est absent/malformé — le match reste alors daté au jour près
    uniquement (comme Match.date actuellement en base Xfoot)."""
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            d = datetime.strptime(date_str, fmt)
            break
        except (ValueError, TypeError):
            d = None
    if d is None:
        return None
    if not time_str or not isinstance(time_str, str) or ":" not in time_str:
        return d
    try:
        h, m = time_str.strip().split(":")[:2]
        return d.replace(hour=int(h), minute=int(m))
    except (ValueError, IndexError):
        return d
