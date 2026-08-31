"""
api/app/ai/historical/schemas.py — Phase 8L : XFOOT HISTORICAL MODEL
SNAPSHOT & REPLAY FOUNDATION V1 — contrat de données (§2/§4/§9/§28 du prompt).

RESEARCH ONLY. Aucune dataclasse ici n'accède à la DB, au réseau, ni ne
modifie un artefact. §1 : ce module RÉUTILISE api/app/ai/shadow/replay.py
(Phase 8K, measure_data_reality/find_replay_candidates) — jamais réimplémenté
— et ÉTEND ce que Phase 8K laissait explicitement non traité : inventaire
formel des artefacts (fichiers + DB), éligibilité calibration, matrice de
replay complète, couverture dataset entier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# §4 : vocabulaire — réutilise HIGH/MEDIUM/LOW/UNKNOWN de Phase 8I où le concept est identique,
# mais ce sont ici des états de DISPONIBILITÉ POINT-IN-TIME, un concept réellement nouveau (Phase 8L).
HISTORICAL_AVAILABILITY_STATES = (
    "AVAILABLE", "PARTIALLY_AVAILABLE", "NOT_AVAILABLE", "UNKNOWN",
    "TRAINED_AFTER_AS_OF", "ARTIFACT_MISSING", "CALIBRATION_MISSING",
    "FEATURE_SET_MISSING", "METADATA_INCOMPLETE",
)

REPLAY_VERDICTS = ("REPLAYABLE", "NOT_REPLAYABLE", "PARTIAL", "UNKNOWN")

# §9 : ordre FIXE des raisons de rejet — jamais réordonné à la volée, jamais une raison masquée.
REPLAY_REJECTION_ORDER = (
    "INVALID_AS_OF", "MODEL_MISSING", "MODEL_TRAINED_AFTER_AS_OF", "ARTIFACT_MISSING",
    "FEATURE_SET_UNAVAILABLE", "FEATURE_LEAKAGE", "CALIBRATION_UNAVAILABLE",
    "CALIBRATION_LEAKAGE", "OTHER_TEMPORAL_UNCERTAINTY",
)


@dataclass(frozen=True)
class ModelVersionInventoryEntry:
    """§2 : un inventaire READ-ONLY. Un champ absent de model_versions (ex.
    `created_at` — CONFIRMÉ absent du schéma, seul `trained_at` existe, voir
    api/app/models/team_rating.py) reste None, JAMAIS déduit d'un nom de
    fichier ou d'une autre colonne (§2 : "ne jamais l'inférer")."""
    model_version_id: int
    model_type: str
    status: str
    is_active: bool
    trained_at: Optional[datetime]  # SEUL timestamp de "création" réellement présent dans le schéma
    artifact_present_in_db: bool     # ModelVersion.artifact (TEXT) non vide — xgboost/lightgbm uniquement
    artifact_length: int
    config_present: bool
    feature_version: Optional[str]   # ModelVersion.feature_version — None pour toutes les versions actuelles (constaté, pas supposé)
    training_period_start: Optional[str]
    training_period_end: Optional[str]
    team_ratings_count: int          # TeamRating liées (dixon_coles/elo) — 0 si aucune
    source: str = "model_versions (DB)"


@dataclass(frozen=True)
class ArtifactFileInventoryEntry:
    """§19/§20 : artefact FICHIER (api/model_artifacts/*.json — Dixon-Coles).
    `filesystem_mtime` n'est JAMAIS une preuve d'entraînement (§19) —
    conservée uniquement à titre informatif, `embedded_trained_at`/
    `embedded_data_up_to` (extraits du JSON lui-même, quand présents) sont
    la SEULE preuve utilisée pour l'éligibilité."""
    path: str
    league: Optional[str]
    size_bytes: int
    sha256: str
    filesystem_mtime: str
    embedded_trained_at: Optional[str]
    embedded_data_up_to: Optional[str]


@dataclass(frozen=True)
class CalibrationInventoryEntry:
    """§8/§21 : calibration RÉELLEMENT identifiable par ModelVersion. Pour
    dixon_coles : N/A (la probabilité EST le modèle, aucune étape de
    calibration séparée n'existe dans ce dépôt). Pour elo : config JSON
    {"c","scale"} par ligue, créé au MÊME instant que `trained_at` (aucun
    timestamp de calibration distinct persisté). Pour xgboost/lightgbm : la
    calibration Platt/Isotonic (Phase 6) est RECHERCHE UNIQUEMENT, jamais
    persistée sur une ModelVersion — CALIBRATION_MISSING par construction,
    constaté, jamais fabriqué."""
    model_version_id: int
    model_type: str
    method: Optional[str]           # "elo_ordered_logit" | "none_persisted" | "n/a_probability_is_the_model"
    created_at: Optional[datetime]  # None si aucune calibration n'est persistée pour ce type
    availability: str               # HISTORICAL_AVAILABILITY_STATES


@dataclass(frozen=True)
class ReplayEligibilityResult:
    """§9 : décision structurée — TOUTES les raisons trouvées sont conservées, jamais seulement la première."""
    verdict: str  # REPLAY_VERDICTS
    reasons: list[str] = field(default_factory=list)  # sous-ensemble de REPLAY_REJECTION_ORDER, dans cet ordre
    checked_at: Optional[datetime] = None
