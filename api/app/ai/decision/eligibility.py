"""
api/app/ai/decision/eligibility.py — Phase 8I : Decision Eligibility & Hard
Gates (§13-§16 du prompt).

Chaque gate retourne PASS | FAIL | UNKNOWN | NOT_APPLICABLE (§14). Les 6
gates sont TOUJOURS calculés et retournés (§15 : "ne pas masquer les autres
raisons") — la liste `gates` respecte l'ordre d'évaluation demandé par §15
(Data, Model, Calibration, Temporal, Sample, Market), mais la dérivation du
statut GLOBAL (`overall`) suit une PRÉCÉDENCE DIFFÉRENTE, documentée dans
`_derive_overall_status` ci-dessous : le gate temporel est vérifié EN
PREMIER pour la décision finale, parce qu'un edge/probabilité/qualité élevés
ne doivent JAMAIS pouvoir contourner une fuite temporelle avérée (§12/§33 :
"il est interdit qu'un edge élevé contourne un hard gate"). Cette
distinction ORDRE D'AFFICHAGE vs PRÉCÉDENCE DE DÉCISION est intentionnelle,
jamais une incohérence.
"""

from __future__ import annotations

from typing import Optional

from app.ai.decision.schemas import Gate, DecisionEligibility, QualityDimensions

REJECTION_REASONS = (
    "NO_MODEL", "MODEL_UNSTABLE", "MODEL_INSUFFICIENT_DATA", "CALIBRATION_UNAVAILABLE",
    "DATA_INCOMPLETE", "DATA_STALE", "TEMPORAL_UNKNOWN", "FUTURE_INFORMATION",
    "HISTORICAL_UNVERIFIED", "INSUFFICIENT_SAMPLE", "MARKET_UNAVAILABLE",
    "MISSING_PROBABILITY", "INVALID_PROBABILITY",
)


def evaluate_gate_data(data_quality: str, feature_stale: bool = False) -> Gate:
    """HIGH/MEDIUM -> PASS ; LOW -> FAIL (DATA_STALE si `feature_stale`
    explicitement signalé par l'appelant, sinon DATA_INCOMPLETE) ; UNKNOWN -> UNKNOWN."""
    if data_quality in ("HIGH", "MEDIUM"):
        return Gate("GATE_DATA", "PASS")
    if data_quality == "LOW":
        return Gate("GATE_DATA", "FAIL", "DATA_STALE" if feature_stale else "DATA_INCOMPLETE")
    return Gate("GATE_DATA", "UNKNOWN")


def evaluate_gate_model(model_quality: str, model_provided: bool) -> Gate:
    """HIGH/MEDIUM -> PASS ; LOW -> FAIL/MODEL_UNSTABLE (signal négatif RÉEL,
    pas un manque d'information) ; UNKNOWN -> UNKNOWN, avec raison NO_MODEL
    si aucun modèle n'a même été fourni, MODEL_INSUFFICIENT_DATA sinon
    (une SelectionDecision existe mais son historique était insuffisant)."""
    if model_quality in ("HIGH", "MEDIUM"):
        return Gate("GATE_MODEL", "PASS")
    if model_quality == "LOW":
        return Gate("GATE_MODEL", "FAIL", "MODEL_UNSTABLE")
    return Gate("GATE_MODEL", "UNKNOWN", "NO_MODEL" if not model_provided else "MODEL_INSUFFICIENT_DATA")


def evaluate_gate_calibration(calibration_quality: str) -> Gate:
    """CALIBRATED/UNCALIBRATED -> PASS (l'absence de calibration bénéfique
    validée limite la CONFIANCE, voir confidence.py, mais ne bloque pas à
    elle seule l'éligibilité — un modèle brut non calibré reste utilisable
    en recherche). INSUFFICIENT_DATA/UNKNOWN -> UNKNOWN."""
    if calibration_quality in ("CALIBRATED", "UNCALIBRATED"):
        return Gate("GATE_CALIBRATION", "PASS")
    return Gate("GATE_CALIBRATION", "UNKNOWN", "CALIBRATION_UNAVAILABLE" if calibration_quality == "INSUFFICIENT_DATA" else None)


def evaluate_gate_temporal(temporal_quality: str) -> Gate:
    """§7 : FUTURE_INFORMATION -> FAIL. UNKNOWN -> UNKNOWN (jamais SAFE,
    jamais PASS). HISTORICAL_UNVERIFIED -> PASS (utilisable en RECHERCHE
    uniquement — la restriction RESEARCH_ONLY est appliquée au niveau de
    l'éligibilité globale, pas ici). TEMPORALLY_VERIFIED -> PASS."""
    if temporal_quality == "FUTURE_INFORMATION":
        return Gate("GATE_TEMPORAL", "FAIL", "FUTURE_INFORMATION")
    if temporal_quality == "UNKNOWN":
        return Gate("GATE_TEMPORAL", "UNKNOWN", "TEMPORAL_UNKNOWN")
    return Gate("GATE_TEMPORAL", "PASS")


def evaluate_gate_sample(sample_quality: str) -> Gate:
    """SUFFICIENT/LIMITED -> PASS ; INSUFFICIENT -> FAIL/INSUFFICIENT_SAMPLE ; UNKNOWN -> UNKNOWN."""
    if sample_quality in ("SUFFICIENT", "LIMITED"):
        return Gate("GATE_SAMPLE", "PASS")
    if sample_quality == "INSUFFICIENT":
        return Gate("GATE_SAMPLE", "FAIL", "INSUFFICIENT_SAMPLE")
    return Gate("GATE_SAMPLE", "UNKNOWN")


def evaluate_gate_market(market_quality: str) -> Gate:
    """§9 : NOT_AVAILABLE -> NOT_APPLICABLE (état ATTENDU sans fournisseur
    d'odds connecté, jamais bloquant à lui seul). HIGH/MEDIUM -> PASS.
    LOW -> FAIL/MARKET_UNAVAILABLE (des odds ont été fournies mais invalides
    — un vrai problème de donnée, pas une simple absence). UNKNOWN -> UNKNOWN."""
    if market_quality == "NOT_AVAILABLE":
        return Gate("GATE_MARKET", "NOT_APPLICABLE")
    if market_quality in ("HIGH", "MEDIUM"):
        return Gate("GATE_MARKET", "PASS")
    if market_quality == "LOW":
        return Gate("GATE_MARKET", "FAIL", "MARKET_UNAVAILABLE")
    return Gate("GATE_MARKET", "UNKNOWN")


def _derive_overall_status(gates_by_name: dict[str, Gate]) -> tuple[str, list[str]]:
    """
    Précédence FIXE (§12 : les gates critiques priment sur toute
    agrégation) — jamais réordonnée dynamiquement :

    1. GATE_TEMPORAL == FAIL -> INELIGIBLE (fuite avérée, priorité absolue).
    2. GATE_TEMPORAL == UNKNOWN -> INELIGIBLE (§7 : jamais un statut
       temporel inconnu accepté, quel que soit le reste).
    3. GATE_MODEL == FAIL -> INELIGIBLE (signal modèle négatif RÉEL, pas un
       manque de données).
    4. GATE_DATA == FAIL, GATE_SAMPLE == FAIL, ou GATE_MARKET == FAIL ->
       INSUFFICIENT_DATA (données incomplètes/insuffisantes — distinct
       d'INELIGIBLE : on ne SAIT pas, on ne dit pas "c'est mauvais").
    5. GATE_TEMPORAL == PASS mais dérivé d'un statut HISTORICAL_UNVERIFIED
       -> RESEARCH_ONLY (signalé par l'appelant via le 3ᵉ élément du tuple
       Gate — voir evaluate_eligibility, qui transmet le statut temporel
       brut plutôt que de le redériver ici).
    6. N'importe quel autre gate == UNKNOWN (model/calibration/data/sample) -> UNKNOWN.
    7. Sinon -> ELIGIBLE.

    Retourne (status, reasons) — `reasons` contient la raison de CHAQUE gate
    non PASS/NOT_APPLICABLE, dans l'ordre §15 (Data, Model, Calibration,
    Temporal, Sample, Market), jamais seulement celle qui a déterminé le
    statut global (§15 : ne jamais masquer les autres raisons).
    """
    order = ["GATE_DATA", "GATE_MODEL", "GATE_CALIBRATION", "GATE_TEMPORAL", "GATE_SAMPLE", "GATE_MARKET"]
    reasons = [gates_by_name[n].reason for n in order if gates_by_name[n].reason is not None]

    temporal = gates_by_name["GATE_TEMPORAL"]
    if temporal.status == "FAIL":
        return "INELIGIBLE", reasons
    if temporal.status == "UNKNOWN":
        return "INELIGIBLE", reasons
    if gates_by_name["GATE_MODEL"].status == "FAIL":
        return "INELIGIBLE", reasons
    if any(gates_by_name[n].status == "FAIL" for n in ("GATE_DATA", "GATE_SAMPLE", "GATE_MARKET")):
        return "INSUFFICIENT_DATA", reasons
    return None, reasons  # décidé par l'appelant (research-only vs unknown vs eligible), voir evaluate_eligibility


def evaluate_eligibility(dims: QualityDimensions, *, feature_stale: bool = False, model_provided: bool = True) -> DecisionEligibility:
    """§25/§14 : assess_decision() — applique les hard gates sur les 6
    dimensions déjà évaluées (quality.py), dans l'ordre d'affichage §15."""
    gates = [
        evaluate_gate_data(dims.data_quality, feature_stale),
        evaluate_gate_model(dims.model_quality, model_provided),
        evaluate_gate_calibration(dims.calibration_quality),
        evaluate_gate_temporal(dims.temporal_quality),
        evaluate_gate_sample(dims.sample_quality),
        evaluate_gate_market(dims.market_quality),
    ]
    gates_by_name = {g.name: g for g in gates}

    status, reasons = _derive_overall_status(gates_by_name)
    if status is None:
        if dims.temporal_quality == "HISTORICAL_UNVERIFIED":
            status = "RESEARCH_ONLY"
            reasons = reasons + ["HISTORICAL_UNVERIFIED"] if "HISTORICAL_UNVERIFIED" not in reasons else reasons
        elif any(gates_by_name[n].status == "UNKNOWN" for n in ("GATE_MODEL", "GATE_CALIBRATION", "GATE_DATA", "GATE_SAMPLE")):
            status = "UNKNOWN"
        else:
            status = "ELIGIBLE"

    return DecisionEligibility(status=status, gates=gates, reasons=reasons)
