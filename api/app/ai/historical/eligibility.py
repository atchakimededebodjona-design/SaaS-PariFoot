"""
api/app/ai/historical/eligibility.py — Phase 8L : règles pures de point-in-
time eligibility (§5-§10 du prompt).

Fonctions PURES uniquement — aucun accès DB/réseau/filesystem. Réutilise le
vocabulaire de api/app/ai/historical/schemas.py (§4), jamais un second
vocabulaire. Règle centrale (§10, jamais violée) : UNKNOWN n'est JAMAIS
promu à SAFE/AVAILABLE — l'absence de preuve n'est jamais une preuve
d'absence de risque.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.ai.historical.schemas import ReplayEligibilityResult


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalise en timezone-aware (UTC) si naïf — évite un TypeError silencieux sur une comparaison naïf/aware,
    jamais une supposition sur la timezone réelle au-delà d'UTC (convention déjà utilisée partout ailleurs, Phase 8E)."""
    if dt is None:
        return None
    from datetime import timezone
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def is_model_available_at(trained_at: Optional[datetime], as_of: datetime) -> str:
    """§5 : trained_at <= as_of -> AVAILABLE. trained_at > as_of -> TRAINED_AFTER_AS_OF.
    trained_at absent -> UNKNOWN (JAMAIS SAFE/AVAILABLE, §5/§10)."""
    if trained_at is None:
        return "UNKNOWN"
    t, a = _aware(trained_at), _aware(as_of)
    return "AVAILABLE" if t <= a else "TRAINED_AFTER_AS_OF"


def is_artifact_available(artifact_exists: bool, metadata_sufficient: bool) -> str:
    """§6 : artifact_exists ET metadata_sufficient -> AVAILABLE. artifact_exists mais metadata insuffisante
    -> METADATA_INCOMPLETE. artifact absent -> ARTIFACT_MISSING."""
    if not artifact_exists:
        return "ARTIFACT_MISSING"
    return "AVAILABLE" if metadata_sufficient else "METADATA_INCOMPLETE"


def is_feature_set_reconstructible(feature_registry_status: Optional[str]) -> str:
    """§7 : réutilise le statut Phase 8A (registry.py::FeatureDefinition.status) — PRODUCTION -> AVAILABLE
    (reconstructible via build_feature_snapshot, Phase 8A, anti-fuite déjà garanti). EXPERIMENTAL ->
    PARTIALLY_AVAILABLE. MISSING/REJECTED/None -> FEATURE_SET_MISSING. Jamais substitué silencieusement par le
    feature set actuel (§7) — ce module ne fait QUE lire le statut, jamais recalculer une feature."""
    if feature_registry_status == "PRODUCTION":
        return "AVAILABLE"
    if feature_registry_status == "EXPERIMENTAL":
        return "PARTIALLY_AVAILABLE"
    return "FEATURE_SET_MISSING"


def is_calibration_available_at(calibration_created_at: Optional[datetime], as_of: datetime, calibration_exists: bool) -> str:
    """§8 : calibration_exists=False -> CALIBRATION_MISSING (jamais fabriquée). calibration_created_at absent
    malgré calibration_exists=True -> UNKNOWN (jamais SAFE). calibration_created_at > as_of -> TRAINED_AFTER_AS_OF
    (fuite de calibration, §14/§31). Sinon -> AVAILABLE."""
    if not calibration_exists:
        return "CALIBRATION_MISSING"
    if calibration_created_at is None:
        return "UNKNOWN"
    c, a = _aware(calibration_created_at), _aware(as_of)
    return "AVAILABLE" if c <= a else "TRAINED_AFTER_AS_OF"


def evaluate_replay_eligibility(
    *, as_of: Optional[datetime], kickoff: Optional[datetime],
    model_trained_at: Optional[datetime], model_exists: bool,
    artifact_exists: bool, artifact_metadata_sufficient: bool,
    feature_registry_status: Optional[str], feature_leakage_detected: bool = False,
    calibration_exists: bool, calibration_created_at: Optional[datetime], calibration_required: bool = True,
) -> ReplayEligibilityResult:
    """
    §9 : décision structurée, ordre de vérification FIXE (jamais réordonné,
    déterminisme §30) — TOUTES les raisons applicables sont conservées dans
    `reasons`, le VERDICT s'arrête à la première condition bloquante
    rencontrée dans cet ordre précis :

    1. invalid as_of (absent, ou as_of >= kickoff quand kickoff est connu — une prédiction pré-match exige as_of < kickoff, §11)
    2. model missing
    3. model trained after as_of
    4. artifact missing / metadata incomplete
    5. feature set unavailable
    6. feature leakage (signalé explicitement par l'appelant, §15/§37)
    7. calibration unavailable (uniquement si calibration_required=True)
    8. calibration leakage (calibration créée après as_of)
    9. other temporal uncertainty (timestamp UNKNOWN quelque part, jamais promu SAFE)
    """
    reasons: list[str] = []

    if as_of is None or (kickoff is not None and _aware(as_of) >= _aware(kickoff)):
        reasons.append("INVALID_AS_OF")
        return ReplayEligibilityResult(verdict="NOT_REPLAYABLE", reasons=reasons, checked_at=as_of)

    if not model_exists:
        reasons.append("MODEL_MISSING")
        return ReplayEligibilityResult(verdict="NOT_REPLAYABLE", reasons=reasons, checked_at=as_of)

    model_status = is_model_available_at(model_trained_at, as_of)
    if model_status == "TRAINED_AFTER_AS_OF":
        reasons.append("MODEL_TRAINED_AFTER_AS_OF")
        return ReplayEligibilityResult(verdict="NOT_REPLAYABLE", reasons=reasons, checked_at=as_of)
    if model_status == "UNKNOWN":
        reasons.append("OTHER_TEMPORAL_UNCERTAINTY")
        return ReplayEligibilityResult(verdict="UNKNOWN", reasons=reasons, checked_at=as_of)

    artifact_status = is_artifact_available(artifact_exists, artifact_metadata_sufficient)
    if artifact_status in ("ARTIFACT_MISSING", "METADATA_INCOMPLETE"):
        reasons.append("ARTIFACT_MISSING")
        return ReplayEligibilityResult(verdict="NOT_REPLAYABLE", reasons=reasons, checked_at=as_of)

    feature_status = is_feature_set_reconstructible(feature_registry_status)
    if feature_status == "FEATURE_SET_MISSING":
        reasons.append("FEATURE_SET_UNAVAILABLE")
        return ReplayEligibilityResult(verdict="NOT_REPLAYABLE", reasons=reasons, checked_at=as_of)

    if feature_leakage_detected:
        reasons.append("FEATURE_LEAKAGE")
        return ReplayEligibilityResult(verdict="NOT_REPLAYABLE", reasons=reasons, checked_at=as_of)

    calib_status = is_calibration_available_at(calibration_created_at, as_of, calibration_exists)
    if calibration_required:
        if calib_status == "CALIBRATION_MISSING":
            reasons.append("CALIBRATION_UNAVAILABLE")
            return ReplayEligibilityResult(verdict="NOT_REPLAYABLE", reasons=reasons, checked_at=as_of)
        if calib_status == "TRAINED_AFTER_AS_OF":
            reasons.append("CALIBRATION_LEAKAGE")
            return ReplayEligibilityResult(verdict="NOT_REPLAYABLE", reasons=reasons, checked_at=as_of)
        if calib_status == "UNKNOWN":
            reasons.append("OTHER_TEMPORAL_UNCERTAINTY")
            return ReplayEligibilityResult(verdict="UNKNOWN", reasons=reasons, checked_at=as_of)

    # feature_status == PARTIALLY_AVAILABLE (EXPERIMENTAL) -> replay possible mais dégradé, jamais bloquant seul.
    verdict = "PARTIAL" if feature_status == "PARTIALLY_AVAILABLE" else "REPLAYABLE"
    return ReplayEligibilityResult(verdict=verdict, reasons=reasons, checked_at=as_of)
