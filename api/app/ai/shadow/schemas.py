"""
api/app/ai/shadow/schemas.py — Phase 8K : XFOOT SHADOW DECISION TRACKING &
OPERATIONAL VALIDATION V1 — contrat de données (§4/§5/§14/§24/§26 du prompt).

=== §3 : pourquoi AUCUNE nouvelle table SQL n'est créée dans cette phase ===

Inspection PRÉALABLE (§3, avant toute décision) des 4 tables candidates :

  - `shadow_selection_predictions` (Phase 6/7) : UniqueConstraint(league,
    match_date, home_team, away_team, market) — AU PLUS UNE ligne par
    (match, marché), point final. Incompatible avec la clé de déduplication
    exigée ici (§7 : match_id + market + model_version + as_of) — deux
    model_version ou deux as_of légitimement différents pour le MÊME match/
    marché doivent produire deux ShadowDecisionRecord distincts (§7 : "deux
    cutoffs différents -> autorisés si le système les supporte"), ce que
    cette contrainte UNIQUE interdit structurellement. Ses colonnes sont en
    outre spécifiques au vocabulaire du Model Selection Engine
    (candidate_model_type, calibration_applied) — aucune colonne pour les 6
    dimensions de qualité (Phase 8I), l'éligibilité, le value_status ou la
    provenance détaillée (Phase 8H/8I/8J).
  - `model_predictions` (Phase 6) : liée à `model_versions` par FK, `role`
    déjà réservé à un mécanisme de comparaison shadow-vs-actif DIFFÉRENT
    (Phase 9, promotion) — mêmes lacunes de colonnes que ci-dessus.
  - `prediction_log` : spécifique à Dixon-Coles production, `payload`
    reproduit la réponse API — aucune notion de marché multiple/qualité/
    éligibilité.

Colonnes RÉELLEMENT manquantes dans les 4 tables existantes pour porter UN
ShadowDecisionRecord complet (§4) : model_quality, calibration_quality,
data_quality, temporal_quality, sample_quality, market_quality (6),
confidence.overall_status, eligibility, gates/reasons, value_status,
provenance (7 champs), probability_source — une vingtaine de colonnes,
aucune ne pouvant être détournée d'un champ existant sans confondre son
sens documenté (ex. `candidate_probs` de shadow_selection_predictions
signifie précisément "probabilité candidate du Model Selection Engine",
jamais un blob générique).

DÉCISION (§3 : "STOP avant migration") : une migration ajoutant ~20
colonnes (ou une nouvelle table) serait TECHNIQUEMENT justifiable, mais la
règle absolue de TOUTES les phases précédentes ("aucune migration DB sauf
nécessité absolue démontrée", répétée §2/§55 de cette phase) et le
caractère RECHERCHE + SHADOW de cette V1 (jamais consommée par la
production) rendent une migration disproportionnée à ce stade. Cette V1
persiste donc les ShadowDecisionRecord dans un FICHIER JSON local
(reports/shadow/shadow_decision_store.json, voir tracking.py::
ShadowDecisionStore) — réversible, supprimable, jamais une table SQL,
jamais lu par la production. Si une Phase 8L ultérieure a besoin d'un
stockage durable/interrogeable en SQL, une migration dédiée (sur le modèle
de shadow_selection_predictions, étendue) serait alors le choix naturel —
explicitement PAS construite ici.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# ---------------------------------------------------------------------------
# §14 : vocabulaire de résolution — PENDING/RESOLVED/INVALID reprennent
# littéralement les statuts déjà utilisés en base (ModelPrediction.status,
# ShadowSelectionPrediction.status : "pending"/"resolved"/"invalid", ici en
# MAJUSCULES pour rester cohérent avec le vocabulaire Phase 8H/8I/8J déjà
# établi). CONFLICT et UNRESOLVED sont de VRAIS ajouts : aucune table
# existante ne modélise "deux sources de résultat en désaccord" (§13/§37) —
# le mécanisme existant (_find_result_for_shadow_row, Phase 7) s'arrête à la
# PREMIÈRE source trouvée, sans jamais vérifier l'accord entre sources.
# ---------------------------------------------------------------------------

RESOLUTION_STATES = ("PENDING", "RESOLVED", "CONFLICT", "UNRESOLVED", "INVALID")

# §26 : catégories d'erreur — un match en erreur ne doit JAMAIS arrêter le batch (§25).
ERROR_CATEGORIES = (
    "INPUT_INVALID", "MODEL_MISSING", "FEATURES_MISSING", "CALIBRATION_MISSING",
    "TEMPORAL_UNKNOWN", "ODDS_MISSING", "PIPELINE_EXCEPTION", "DUPLICATE", "RESOLUTION_CONFLICT",
)


@dataclass(frozen=True)
class ShadowDecisionRecord:
    """
    §4/§5 : SNAPSHOT IMMUTABLE — une fois créé, AUCUN champ ci-dessous n'est
    jamais réassigné (dataclass frozen). Le "vocabulaire déjà existant" de
    Phase 8H/8I/8J est réutilisé tel quel partout où le concept existe déjà
    (quality/eligibility/value_status/temporal_status/provenance) — ce
    module ne réévalue RIEN, il capture ce que api/app/ai/pipeline (Phase
    8J) a déjà produit.
    """
    shadow_id: str  # = tracking.dedup_key(match_id, market, model_version, as_of) — identité stable, §7
    match_id: Optional[int]
    league: Optional[str]
    home_team: Optional[str]  # requis pour la résolution (§12) — model_predictions/prediction_log/match sont indexées par clé naturelle, pas par match_id (voir leurs docstrings) ; absent de PipelineInput (Phase 8J, jamais modifié), donc porté ici
    away_team: Optional[str]
    kickoff: Optional[datetime]
    as_of: datetime
    model_type: Optional[str]
    model_version: Optional[str]
    calibration_source: Optional[str]
    market: str
    selection: str
    raw_probability: Optional[float]
    calibrated_probability: Optional[float]
    # Distribution COMPLÈTE du marché (toutes les issues, ex. {"home_win":..,
    # "draw":..,"away_win":..}) — au-delà du champ scalaire "raw_probability"
    # du §4 du prompt (qui ne porte que la sélection suivie) : nécessaire pour
    # calculer honnêtement log_loss/Brier (§17) une fois résolu, qui exigent
    # la probabilité assignée à CHAQUE issue, pas seulement celle suivie —
    # sans elle, log_loss/Brier seraient soit impossibles soit fabriqués sur
    # un raté (§53 : jamais de performance fabriquée). None si non disponible.
    market_probabilities_raw: Optional[dict]
    market_probabilities_calibrated: Optional[dict]
    probability_source: str  # "RAW" | "CALIBRATED" — Phase 8I, réutilisé
    quality: dict            # QualityDimensions.__dict__ (Phase 8I), snapshot figé
    confidence: str          # PredictionConfidence.overall_status (Phase 8I)
    eligibility: str         # DecisionEligibility.status (Phase 8I)
    value_status: Optional[str]  # ValueSignal.status (Phase 8H) ou None si non évalué
    odds_source: Optional[str]
    odds_timestamp: Optional[datetime]
    temporal_status: str
    provenance: dict         # Provenance.__dict__ (Phase 8J), snapshot figé
    status: str              # PipelineAssessment.final_status (Phase 8J) au moment T — jamais recalculé après
    created_at: datetime
    data_marking: str = "REAL"  # "REAL" | "SYNTHETIC" (§44/§53) — jamais ambigu


@dataclass(frozen=True)
class ShadowResolution:
    """
    §5/§13 : SEULE partie mutable d'un enregistrement shadow — mais même
    ici, "mutable" signifie "remplacée UNE FOIS par une nouvelle instance
    frozen", jamais un champ modifié en place. Une résolution existante
    (result_status != PENDING) n'est PLUS JAMAIS remplacée (§12/§13 :
    "ne jamais modifier une décision déjà résolue", "ne jamais remplacer un
    résultat existant").
    """
    result_status: str  # RESOLUTION_STATES
    actual_home_goals: Optional[int] = None
    actual_away_goals: Optional[int] = None
    actual_outcome: Optional[str] = None
    candidate_correct: Optional[bool] = None
    conflict_sources: Optional[dict] = None  # {"model_predictions": [hg, ag], "prediction_log": [hg, ag], ...} si CONFLICT — jamais un choix arbitraire
    resolved_at: Optional[datetime] = None


def pending_resolution() -> ShadowResolution:
    return ShadowResolution(result_status="PENDING")


@dataclass(frozen=True)
class ShadowOperationalHealth:
    """§24 : synthèse opérationnelle d'un run (capture + résolution)."""
    records_created: int = 0
    duplicates_prevented: int = 0
    records_resolved: int = 0
    unresolved: int = 0
    conflicts: int = 0
    invalid: int = 0
    pipeline_errors: int = 0
    provenance_missing: int = 0
    temporal_unknown: int = 0
    no_odds: int = 0
    no_fixtures: int = 0
    error_categories: dict = field(default_factory=dict)  # {ERROR_CATEGORIES: count}
