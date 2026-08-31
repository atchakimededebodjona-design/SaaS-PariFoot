"""
api/app/ai/decision/quality.py — Phase 8I : évaluation INDÉPENDANTE de
chaque dimension de qualité (§3-§9 du prompt).

Réutilise TELLES QUELLES (jamais réimplémentées) :
  - api/app/ai/arena/model_selection.py::SelectionDecision (Phase 6) pour
    MODEL_QUALITY.
  - api/app/ai/arena/calibration_engine.py::CalibrationResult (Phase 6)
    pour CALIBRATION_QUALITY.
  - api/app/ai/features/snapshot.py::snapshot_coverage (Phase 8A) pour
    DATA_QUALITY.
  - api/app/ai/value/quality.py::classify_temporal_status,
    TEMPORAL_STATUSES (Phase 8H) pour TEMPORAL_QUALITY — IDENTIQUE, jamais
    une deuxième classification temporelle.
  - api/app/ai/value/core.py::compute_market_probabilities (Phase 8H) pour
    MARKET_QUALITY.

Chaque fonction ci-dessous ne DEGRADE jamais silencieusement : une entrée
absente ou non concluante retourne UNKNOWN, jamais HIGH par défaut (§4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from app.ai.value.quality import classify_temporal_status, TEMPORAL_STATUSES  # noqa: F401 — réutilisé tel quel (Phase 8H)
from app.ai.value.core import compute_market_probabilities

if TYPE_CHECKING:
    from app.ai.arena.model_selection import SelectionDecision
    from app.ai.arena.calibration_engine import CalibrationResult

MODEL_QUALITY_STATUSES = ("HIGH", "MEDIUM", "LOW", "UNKNOWN")
CALIBRATION_QUALITY_STATUSES = ("CALIBRATED", "UNCALIBRATED", "INSUFFICIENT_DATA", "UNKNOWN")
DATA_QUALITY_STATUSES = ("HIGH", "MEDIUM", "LOW", "UNKNOWN")
SAMPLE_QUALITY_STATUSES = ("SUFFICIENT", "LIMITED", "INSUFFICIENT", "UNKNOWN")
MARKET_QUALITY_STATUSES = ("HIGH", "MEDIUM", "LOW", "NOT_AVAILABLE", "UNKNOWN")


# ---------------------------------------------------------------------------
# §4 : MODEL_QUALITY — réutilise SelectionDecision (Phase 6), jamais
# recalculé à partir de zéro.
# ---------------------------------------------------------------------------

def assess_model_quality(selection_decision: Optional["SelectionDecision"]) -> str:
    """
    - Aucune décision fournie -> UNKNOWN (jamais HIGH par défaut).
    - status="selected" (stable ET statistiquement crédible, portes 1-2-3
      de model_selection.select_candidate_model franchies) -> HIGH.
    - status="not_significant" (stable mais pas prouvé meilleur que son
      dauphin) -> MEDIUM — un signal réel mais pas la meilleure garantie.
    - status="unstable" (performance instable entre fenêtres) -> LOW.
    - status="insufficient_data" ou toute valeur inattendue -> UNKNOWN.
    """
    if selection_decision is None:
        return "UNKNOWN"
    status = selection_decision.status
    if status == "selected":
        return "HIGH"
    if status == "not_significant":
        return "MEDIUM"
    if status == "unstable":
        return "LOW"
    return "UNKNOWN"  # "insufficient_data" ou statut inconnu


# ---------------------------------------------------------------------------
# §5 : CALIBRATION_QUALITY — réutilise CalibrationResult (Phase 6).
# ---------------------------------------------------------------------------

def assess_calibration_quality(calibration_result: Optional["CalibrationResult"]) -> str:
    """
    - Aucun résultat fourni -> UNKNOWN.
    - verdict="INSUFFICIENT_DATA" -> INSUFFICIENT_DATA (distinct d'UNKNOWN :
      une évaluation a bien été tentée, l'échantillon était juste trop petit).
    - choice in ("platt","isotonic") ET verdict="HELPFUL" -> CALIBRATED.
    - choice="none" ou verdict in ("NEUTRAL","HARMFUL") -> UNCALIBRATED
      (aucune calibration bénéfique validée n'est appliquée — §5 : ne
      jamais considérer comme "high confidence" dans ce cas).
    """
    if calibration_result is None:
        return "UNKNOWN"
    if calibration_result.verdict == "INSUFFICIENT_DATA":
        return "INSUFFICIENT_DATA"
    if calibration_result.choice in ("platt", "isotonic") and calibration_result.verdict == "HELPFUL":
        return "CALIBRATED"
    if calibration_result.choice == "none" or calibration_result.verdict in ("NEUTRAL", "HARMFUL"):
        return "UNCALIBRATED"
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# §6 : DATA_QUALITY — réutilise snapshot_coverage (Phase 8A).
# ---------------------------------------------------------------------------

def assess_data_quality(coverage: Optional[dict], team_mapping_confident: Optional[bool] = None) -> str:
    """
    `coverage` : sortie de api.app.ai.features.snapshot.snapshot_coverage
    (dict avec `coverage_ratio`), ou None si aucun snapshot n'a pu être
    construit -> UNKNOWN.

    - coverage_ratio >= 0.9 ET team_mapping_confident n'est pas explicitement
      False -> HIGH.
    - coverage_ratio >= 0.5 -> MEDIUM.
    - coverage_ratio < 0.5 -> LOW.
    - team_mapping_confident=False (mapping équipe explicitement douteux,
      voir team_name_matching.py) dégrade HIGH -> MEDIUM, jamais plus haut
      que ce que la couverture seule indiquerait.
    """
    if coverage is None or coverage.get("coverage_ratio") is None:
        return "UNKNOWN"
    ratio = coverage["coverage_ratio"]
    if ratio >= 0.9:
        return "MEDIUM" if team_mapping_confident is False else "HIGH"
    if ratio >= 0.5:
        return "MEDIUM"
    return "LOW"


# ---------------------------------------------------------------------------
# §8 : SAMPLE_QUALITY.
# ---------------------------------------------------------------------------

def assess_sample_quality(sample_size: Optional[int], min_required: int, limited_floor: int) -> str:
    """
    §8 : ne jamais confondre un petit échantillon à excellente accuracy avec
    une haute confiance — cette fonction ne regarde QUE la taille
    d'échantillon, jamais une métrique de performance.

    - sample_size absent -> UNKNOWN.
    - sample_size >= min_required -> SUFFICIENT.
    - limited_floor <= sample_size < min_required -> LIMITED.
    - sample_size < limited_floor -> INSUFFICIENT.
    """
    if sample_size is None:
        return "UNKNOWN"
    if sample_size >= min_required:
        return "SUFFICIENT"
    if sample_size >= limited_floor:
        return "LIMITED"
    return "INSUFFICIENT"


# ---------------------------------------------------------------------------
# §9 : MARKET_QUALITY — réutilise compute_market_probabilities (Phase 8H).
# ---------------------------------------------------------------------------

def assess_market_quality(odds_by_selection: Optional[dict[str, float]], bookmaker_count: Optional[int] = None) -> str:
    """
    §9 : sans odds externes -> NOT_AVAILABLE, jamais inventé.

    - odds_by_selection absent/vide -> NOT_AVAILABLE (aucun fournisseur
      connecté — état ATTENDU tant que The Odds API reste SUPPORT_REQUIRED).
    - compute_market_probabilities (Phase 8H) échoue (cote invalide, marché
      incomplet) -> LOW.
    - marché valide ET bookmaker_count >= 2 -> HIGH (plusieurs sources).
    - marché valide, bookmaker_count inconnu ou = 1 -> MEDIUM.
    """
    if not odds_by_selection:
        return "NOT_AVAILABLE"
    result = compute_market_probabilities(odds_by_selection)
    if result is None:
        return "LOW"
    if bookmaker_count is not None and bookmaker_count >= 2:
        return "HIGH"
    return "MEDIUM"
