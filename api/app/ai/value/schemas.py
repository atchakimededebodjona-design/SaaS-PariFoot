"""
api/app/ai/value/schemas.py — Phase 8H : XFOOT VALUE ENGINE & MARKET
INTELLIGENCE FOUNDATION V1.

Contrat d'entrée/sortie du Value Engine (§2 du prompt). Dataclasses PURES,
aucune n'accède à la DB, au réseau, ni à un fournisseur d'odds. Réutilise le
vocabulaire marché/issue DÉJÀ établi en production (api/app/ai/arena/
ensemble.py::MARKETS/MARKET_OUTCOME_KEYS) — jamais un second vocabulaire
inventé pour ce module.

AUCUNE fonction ni classe de ce package (api/app/ai/value/) n'est importée
par la production (main.py, scheduler.py, orchestrator.py, service.py,
ensemble.py, models_common.py, promotion.py) — RESEARCH + SHADOW ONLY (§37
du prompt Phase 8H).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.ai.arena.ensemble import MARKET_OUTCOME_KEYS  # noqa: F401 — réutilisé tel quel, jamais réinventé
from app.ai.arena.service import MARKETS  # noqa: F401 — réutilisé tel quel, jamais réinventé

# ---------------------------------------------------------------------------
# §3 : probabilité Xfoot — le Value Engine ne choisit JAMAIS le modèle
# source (Dixon-Coles/XGBoost/LightGBM/Ensemble/calibré), il accepte p_model
# tel quel, quelle que soit son origine.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelProbability:
    market: str
    selection: str
    raw_probability: float
    calibrated_probability: Optional[float] = None
    source_model_type: Optional[str] = None  # informatif uniquement (ex. "ensemble", "xgboost") — jamais utilisé pour un choix de logique

    def effective_probability(self) -> float:
        """§17 : calibrated_probability si explicitement fournie, sinon raw_probability — jamais l'inverse,
        jamais une moyenne des deux."""
        return self.calibrated_probability if self.calibrated_probability is not None else self.raw_probability


# ---------------------------------------------------------------------------
# §4 : odds — indépendant de tout fournisseur. §11 : timestamp jamais
# fabriqué ; has_measured_timestamp distingue une mesure RÉELLE d'une
# simple présence de champ (§42 : football-data.co.uk n'a jamais de mesure
# réelle, seulement une méthodologie de collecte documentée).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OddsSnapshot:
    market: str
    selection: str
    decimal_odds: float
    bookmaker: Optional[str] = None
    odds_timestamp: Optional[datetime] = None
    has_measured_timestamp: bool = False


@dataclass(frozen=True)
class MarketProbability:
    """§5/§6 : raw et normalized gardées STRICTEMENT séparées, jamais mélangées."""
    market: str
    selection: str
    raw_implied_probability: float
    normalized_probability: Optional[float] = None
    overround: Optional[float] = None


@dataclass(frozen=True)
class TemporalMetadata:
    """§10/§11 : statut temporel d'une observation odds vis-à-vis d'un cutoff donné."""
    odds_timestamp: Optional[datetime]
    cutoff_timestamp: Optional[datetime]
    match_kickoff: Optional[datetime]
    has_measured_timestamp: bool
    status: str  # quality.TEMPORAL_STATUSES
    odds_age_hours: Optional[float] = None


@dataclass(frozen=True)
class PredictionQuality:
    """§20 : résultat du Quality Gate, évalué AVANT tout calcul de valeur final."""
    odds_valid: bool
    model_probability_valid: bool
    temporal_status_valid: bool
    market_valid: bool
    sample_valid: bool
    passed: bool
    failure_reason: Optional[str] = None  # core.REJECTION_REASONS, None si passed=True


@dataclass
class ValueSignal:
    """§18 — objet de sortie unique du Value Engine, RESEARCH/SHADOW UNIQUEMENT."""
    match_id: Optional[int]
    market: str
    selection: str
    model_probability: Optional[float]
    market_probability_raw: Optional[float]
    market_probability_normalized: Optional[float]
    odds: Optional[float]
    edge: Optional[float]
    expected_value: Optional[float]
    temporal_status: str
    odds_timestamp: Optional[datetime]
    cutoff_timestamp: Optional[datetime]
    confidence: Optional[float]
    status: str  # core.VALUE_TYPES
    reason: Optional[str] = None  # core.REJECTION_REASONS, None si status n'est pas un rejet


# ---------------------------------------------------------------------------
# §9 : seuils de décision — PARAMÈTRES, jamais des constantes de production.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ValueThresholds:
    min_edge: float
    min_ev: float
    min_probability: float
    min_confidence: float
    max_odds_age_hours: float


# RESEARCH_DEFAULT : seuils permissifs (0 = tout ce qui a des chiffres valides passe), documentés comme JAMAIS
# validés statistiquement (§40 : STATISTICAL_VALUE_VALIDATION = NOT_AVAILABLE) — ne représentent aucune
# recommandation de production. Utiliser evaluate_threshold_grid() (core.py, §28) pour explorer des candidats.
RESEARCH_DEFAULT_THRESHOLDS = ValueThresholds(
    min_edge=0.0, min_ev=0.0, min_probability=0.0, min_confidence=0.0, max_odds_age_hours=float("inf"),
)
