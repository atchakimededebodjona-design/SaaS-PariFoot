"""
api/app/ai/value/quality.py — Phase 8H : classification temporelle et
Quality Gate du Value Engine.

Réutilise EXACTEMENT api/app/ai/odds_research/integrity.py::
classify_explicit_timestamp (Phase 8E, jamais réimplémentée) — ce module
traduit son résultat (SAFE|FUTURE_INFORMATION|REJECTED) vers le vocabulaire
propre au Value Engine (§10 du prompt) et ajoute le gate `has_measured_
timestamp` (§42 : une source sans mesure RÉELLE, ex. football-data.co.uk,
ne doit JAMAIS être promue TEMPORALLY_VERIFIED, même si l'arithmétique de
date semble favorable).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.ai.odds_research.integrity import classify_explicit_timestamp  # noqa: F401 — réutilisée telle quelle (§10)

TEMPORAL_STATUSES = ("TEMPORALLY_VERIFIED", "HISTORICAL_UNVERIFIED", "FUTURE_INFORMATION", "UNKNOWN")


def classify_temporal_status(
    odds_timestamp: Optional[datetime],
    cutoff_timestamp: Optional[datetime],
    match_kickoff: Optional[datetime] = None,
    has_measured_timestamp: bool = False,
) -> str:
    """
    §10 :
    - odds_timestamp OU cutoff_timestamp absent -> UNKNOWN (jamais une supposition, jamais déclaré SAFE).
    - classify_explicit_timestamp == "SAFE" ET has_measured_timestamp=True -> TEMPORALLY_VERIFIED.
    - classify_explicit_timestamp == "SAFE" ET has_measured_timestamp=False -> HISTORICAL_UNVERIFIED (§42 :
      une comparaison de dates favorable ne suffit jamais si le timestamp source n'est pas une mesure prouvée).
    - classify_explicit_timestamp in ("FUTURE_INFORMATION", "REJECTED") -> FUTURE_INFORMATION (fuite avérée
      dans les deux cas — postérieur au cutoff, ou postérieur/égal au kickoff — le Value Engine les rejette
      identiquement, §10 : "FUTURE_INFORMATION -> REJECT").
    """
    if odds_timestamp is None or cutoff_timestamp is None:
        return "UNKNOWN"
    raw = classify_explicit_timestamp(odds_timestamp, cutoff_timestamp, match_kickoff)
    if raw == "SAFE":
        return "TEMPORALLY_VERIFIED" if has_measured_timestamp else "HISTORICAL_UNVERIFIED"
    if raw in ("FUTURE_INFORMATION", "REJECTED"):
        return "FUTURE_INFORMATION"
    return "UNKNOWN"


def is_production_eligible(temporal_status: str) -> bool:
    """§10/§43 : SEUL TEMPORALLY_VERIFIED serait éligible à un usage production futur — HISTORICAL_UNVERIFIED
    reste utilisable en RECHERCHE uniquement, jamais en production (§10). Aucun appelant production n'existe
    aujourd'hui (§37) ; cette fonction documente la distinction pour un futur Phase 8I, elle n'est invoquée par
    aucun chemin de service actuel."""
    return temporal_status == "TEMPORALLY_VERIFIED"


def compute_odds_age_hours(cutoff_timestamp: Optional[datetime], odds_timestamp: Optional[datetime]) -> Optional[float]:
    """§11 : odds_age = cutoff - odds_timestamp, en HEURES. None si l'un des deux timestamps est absent —
    jamais un âge calculé à partir d'un timestamp inventé."""
    if cutoff_timestamp is None or odds_timestamp is None:
        return None
    return (cutoff_timestamp - odds_timestamp).total_seconds() / 3600.0


def evaluate_quality_gates(
    *, odds_valid: bool, model_probability_valid: bool, temporal_status: str, market_valid: bool, sample_valid: bool,
):
    """
    §20 : Quality Gate évalué AVANT tout calcul de valeur final. `temporal_status_valid` est vrai pour
    TEMPORALLY_VERIFIED ET HISTORICAL_UNVERIFIED (les deux sont utilisables en RECHERCHE, §10) — jamais pour
    FUTURE_INFORMATION (rejet obligatoire) ni UNKNOWN (jamais déclaré sûr).

    Ordre de vérification FIXE et déterministe (§35, reproductibilité) — la première condition en échec
    détermine `failure_reason` (core.REJECTION_REASONS), jamais un ordre non déterministe.
    """
    from app.ai.value.schemas import PredictionQuality  # import différé : évite un cycle schemas<->quality

    temporal_status_valid = temporal_status in ("TEMPORALLY_VERIFIED", "HISTORICAL_UNVERIFIED")
    passed = odds_valid and model_probability_valid and temporal_status_valid and market_valid and sample_valid

    reason = None
    if not odds_valid:
        reason = "INVALID_ODDS"
    elif not model_probability_valid:
        reason = "NO_MODEL_PROBABILITY"
    elif not market_valid:
        reason = "INSUFFICIENT_DATA"
    elif not temporal_status_valid:
        reason = "FUTURE_INFORMATION" if temporal_status == "FUTURE_INFORMATION" else "TEMPORAL_UNVERIFIED"
    elif not sample_valid:
        reason = "INSUFFICIENT_DATA"

    return PredictionQuality(
        odds_valid=odds_valid, model_probability_valid=model_probability_valid,
        temporal_status_valid=temporal_status_valid, market_valid=market_valid, sample_valid=sample_valid,
        passed=passed, failure_reason=reason,
    )
