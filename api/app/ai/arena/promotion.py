"""
api/app/ai/arena/promotion.py — Phase 9, Partie F/G : décision de promotion
d'une ModelVersion CANDIDATE, et mode SHADOW.

=== Cycle de vie (ModelVersion.status, voir app/models/team_rating.py) ===

  candidate -> (evaluate_promotion) -> active   (promue)
                                     -> reste candidate (rejetée, aucune
                                        trace négative ailleurs que le log/
                                        retour de evaluate_promotion — on ne
                                        supprime jamais un candidat rejeté,
                                        §22 : conserver l'historique complet)
  active    -> (set_shadow)         -> shadow   (mise en observation, sans
                                        jamais entrer dans l'Ensemble/les
                                        décisions — voir docstring set_shadow)
  active    -> (apply_promotion sur une AUTRE version) -> retired

Aucune des fonctions de ce module ne modifie `is_active` d'une version
CANDIDATE avant promotion effective (§22 : "Une nouvelle version ne devient
pas automatiquement active parce que l'entraînement réussit").
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app.models.team_rating import ModelVersion, deactivate_other_versions

# Tolérance de log_loss (§23 du ticket Phase 9) : un candidat n'est jamais
# rejeté pour être marginalement moins bon que la baseline — mais jamais non
# plus promu "à tout prix" (§23 : "ne pas utiliser une règle qui garantit
# artificiellement la promotion"). 0.02 est un point de départ opérationnel
# (comparable à l'écart observé entre XGBoost/LightGBM/Ensemble en Phase 8,
# voir RAPPORT_PHASE8 : 1.0311 vs 1.0307 vs 1.0332), PAS une valeur validée
# scientifiquement — à ajuster une fois des cycles de retraining réels
# observés (voir rapport final Phase 9, section Limitations).
PROMOTION_LOG_LOSS_TOLERANCE = float(os.environ.get("PROMOTION_LOG_LOSS_TOLERANCE", "0.02"))

# Échantillon minimal de VALIDATION (jamais de test, voir docstring module
# retraining.py) pour qu'une décision de promotion ait un sens statistique —
# même ordre de grandeur que MIN_BENCHMARK_SAMPLE_SIZE (Phase 5, 100), pas
# une coïncidence : les deux répondent à la même question ("cet échantillon
# est-il assez grand pour qu'une métrique soit digne de confiance ?").
PROMOTION_MIN_VALIDATION_SAMPLE = int(os.environ.get("PROMOTION_MIN_VALIDATION_SAMPLE", "100"))


@dataclass
class PromotionDecision:
    promote: bool
    reason: str
    candidate_version_id: Optional[int]
    baseline_version_id: Optional[int]
    candidate_log_loss: Optional[float] = None
    baseline_log_loss: Optional[float] = None
    tolerance: float = PROMOTION_LOG_LOSS_TOLERANCE
    min_validation_sample: int = PROMOTION_MIN_VALIDATION_SAMPLE


def _validation_log_loss(version: ModelVersion) -> Optional[float]:
    """Lit UNIQUEMENT la partie "validation" de ModelVersion.metrics (JSON
    {"validation": {...}, "test": {...}}) — jamais "test" (§22-23 : le test
    set ne doit jamais influencer une décision de promotion, seulement être
    rapporté pour audit)."""
    if not version.metrics:
        return None
    try:
        parsed = json.loads(version.metrics)
    except (json.JSONDecodeError, TypeError):
        return None
    validation = parsed.get("validation") or {}
    return validation.get("log_loss")


def evaluate_promotion(candidate: ModelVersion, baseline: Optional[ModelVersion]) -> PromotionDecision:
    """
    Décision de promotion (§22-23 du ticket) — DEUX portes indépendantes,
    toutes les deux doivent passer :

      1. `candidate.sample_size` (taille de l'échantillon de VALIDATION,
         jamais de test) >= PROMOTION_MIN_VALIDATION_SAMPLE.
      2. Si une baseline existe : log_loss de validation du candidat <=
         log_loss de la baseline + PROMOTION_LOG_LOSS_TOLERANCE. Sans
         baseline (bootstrap d'un model_type qui n'a encore aucune version
         "active" comparable), seule la porte 1 s'applique.

    Un entraînement "réussi" (le candidat existe, avec un artefact valide)
    mais moins bon que la tolérance est TOUJOURS rejeté — cette fonction ne
    contient aucun chemin qui promeut inconditionnellement.
    """
    if candidate.sample_size is None or candidate.sample_size < PROMOTION_MIN_VALIDATION_SAMPLE:
        return PromotionDecision(
            promote=False,
            reason=(
                f"Échantillon de validation insuffisant : {candidate.sample_size} "
                f"< PROMOTION_MIN_VALIDATION_SAMPLE={PROMOTION_MIN_VALIDATION_SAMPLE}."
            ),
            candidate_version_id=candidate.id,
            baseline_version_id=baseline.id if baseline is not None else None,
        )

    candidate_log_loss = _validation_log_loss(candidate)

    if baseline is None:
        return PromotionDecision(
            promote=True,
            reason="Aucune baseline existante pour ce model_type (bootstrap) — échantillon de validation suffisant.",
            candidate_version_id=candidate.id,
            baseline_version_id=None,
            candidate_log_loss=candidate_log_loss,
        )

    baseline_log_loss = _validation_log_loss(baseline)
    if candidate_log_loss is None or baseline_log_loss is None:
        return PromotionDecision(
            promote=False,
            reason="Métriques de validation manquantes (candidat ou baseline) — comparaison impossible, jamais devinée.",
            candidate_version_id=candidate.id,
            baseline_version_id=baseline.id,
            candidate_log_loss=candidate_log_loss,
            baseline_log_loss=baseline_log_loss,
        )

    threshold = baseline_log_loss + PROMOTION_LOG_LOSS_TOLERANCE
    if candidate_log_loss <= threshold:
        return PromotionDecision(
            promote=True,
            reason=(
                f"Log loss de validation du candidat ({candidate_log_loss}) <= baseline "
                f"({baseline_log_loss}) + tolérance ({PROMOTION_LOG_LOSS_TOLERANCE}) = {threshold}."
            ),
            candidate_version_id=candidate.id, baseline_version_id=baseline.id,
            candidate_log_loss=candidate_log_loss, baseline_log_loss=baseline_log_loss,
        )

    return PromotionDecision(
        promote=False,
        reason=(
            f"Log loss de validation du candidat ({candidate_log_loss}) dépasse la baseline "
            f"({baseline_log_loss}) + tolérance ({PROMOTION_LOG_LOSS_TOLERANCE}) = {threshold}."
        ),
        candidate_version_id=candidate.id, baseline_version_id=baseline.id,
        candidate_log_loss=candidate_log_loss, baseline_log_loss=baseline_log_loss,
    )


def apply_promotion(session: Session, candidate: ModelVersion) -> None:
    """
    Applique une promotion déjà DÉCIDÉE (par evaluate_promotion, jamais
    appelée seule sans décision préalable) : réutilise TELLE QUELLE
    `deactivate_other_versions` (Phase 3, inchangée) pour le flip
    `is_active`, puis ajoute la bascule bookkeeping Phase 9 (`status`,
    `deactivated_at` sur les anciennes versions actives). Flush seulement —
    laisse l'appelant committer (même discipline que le reste du module
    prediction_logging.py).
    """
    now = datetime.now(timezone.utc)

    # deactivate_other_versions gère déjà is_active=False sur toutes les
    # AUTRES versions du même model_type — on complète juste le bookkeeping
    # Phase 9 (status/deactivated_at) sur celles qui étaient actives.
    previously_active = session.exec(
        select(ModelVersion).where(
            ModelVersion.model_type == candidate.model_type,
            ModelVersion.id != candidate.id,
            ModelVersion.is_active == True,  # noqa: E712
        )
    ).all()

    deactivate_other_versions(session, candidate.model_type)

    for old in previously_active:
        old.status = "retired"
        old.deactivated_at = now
        session.add(old)

    candidate.status = "active"
    candidate.is_active = True
    candidate.activated_at = now
    session.add(candidate)
    session.flush()


def set_shadow(session: Session, model_version_id: int) -> ModelVersion:
    """
    Place une ModelVersion en mode SHADOW (§24-25) : `status="shadow"`
    UNIQUEMENT — `is_active` reste inchangé (jamais mis à True). C'est cette
    seule garantie qui empêche une version shadow d'entrer dans un chemin
    Phase 5-8 existant : `default_models`/`get_or_create_active_model_version`
    /`compute_market_weights` sont tous conditionnés sur `is_active`, jamais
    sur `status`.
    """
    version = session.get(ModelVersion, model_version_id)
    if version is None:
        raise ValueError(f"ModelVersion #{model_version_id} introuvable.")
    version.status = "shadow"
    session.add(version)
    session.flush()
    return version
