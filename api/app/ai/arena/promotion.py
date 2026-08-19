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



# =============================================================================
# Phase 10 : promotion pilotée par les performances LIVE (jamais par les
# métriques de VALIDATION offline ci-dessus, voir docstring
# evaluate_live_promotion) — moteur DISTINCT de evaluate_promotion, jamais un
# remplacement : evaluate_promotion reste la porte utilisée juste après un
# entraînement (scripts/retrain_ml_models.py --force, sur le split de
# validation) ; evaluate_live_promotion est la porte utilisée une fois qu'une
# version a tourné en SHADOW en production (voir scripts/evaluate_live_models.py).
# =============================================================================

# Échantillon LIVE minimal (résolu) requis pour CHAQUE version comparée
# (candidat ET baseline) — même ordre de grandeur que PROMOTION_MIN_VALIDATION_
# SAMPLE ci-dessus, mais une constante séparée : rien n'impose que les deux
# univers (validation offline vs LIVE réel) doivent un jour partager la même
# valeur. Valeur "bootstrap" (voir rapport final Phase 10) : à recalibrer une
# fois des cycles de shadow réels observés.
LIVE_MIN_SAMPLE_SIZE = int(os.environ.get("LIVE_MIN_SAMPLE_SIZE", "100"))

# Marge minimale d'amélioration (en log_loss, plus petit = meilleur) exigée
# pour qu'une promotion LIVE soit jugée "eligible" — DÉLIBÉRÉMENT plus strict
# que PROMOTION_LOG_LOSS_TOLERANCE (qui, elle, autorise un candidat
# légèrement MOINS bon qu'une baseline, pour un flux offline où un humain
# review déjà le résultat via --force). Ici, personne ne review
# nécessairement chaque décision (voir AUTO_PROMOTION_ENABLED) : ne jamais
# promouvoir "parce que le candidat est meilleur sur un petit écart" sans
# cette marge explicite. Valeur "bootstrap", voir rapport final Phase 10.
PROMOTION_MIN_IMPROVEMENT = float(os.environ.get("PROMOTION_MIN_IMPROVEMENT", "0.01"))

# Interrupteur global de la promotion AUTOMATIQUE (scripts/evaluate_live_models.py
# uniquement — n'affecte JAMAIS POST /models/promotion/promote, qui reste un
# acte humain explicite quel que soit ce réglage). False par défaut (§39 du
# ticket Phase 10 : "AUTO_PROMOTION_ENABLED=false par défaut").
AUTO_PROMOTION_ENABLED = os.environ.get("AUTO_PROMOTION_ENABLED", "false").strip().lower() in ("1", "true", "yes")

LivePromotionStatus = str  # "already_active" | "insufficient_data" | "rejected" | "no_clear_gain" | "eligible"


@dataclass
class LivePromotionDecision:
    status: LivePromotionStatus
    reason: str
    model_type: str
    market: str
    candidate_version_id: Optional[int]
    baseline_version_id: Optional[int] = None
    candidate_metrics: Optional[dict] = None
    baseline_metrics: Optional[dict] = None
    min_sample_size: int = LIVE_MIN_SAMPLE_SIZE
    min_improvement: float = PROMOTION_MIN_IMPROVEMENT


def get_active_version(session: Session, model_type: str) -> Optional[ModelVersion]:
    """Version actuellement active (is_active=True) pour un model_type — None
    s'il n'y en a aucune. Public (pas de préfixe `_`) : réutilisée par
    main.py (endpoints /models/promotion/*) pour retrouver la version qu'une
    promotion remplacerait, sans dupliquer cette requête."""
    return session.exec(
        select(ModelVersion).where(ModelVersion.model_type == model_type, ModelVersion.is_active == True)  # noqa: E712
    ).first()


def evaluate_live_promotion(
    session: Session, model_version_id: int, market: str = "1X2",
) -> LivePromotionDecision:
    """
    Décision de promotion basée sur les performances LIVE RÉELLES (jamais la
    VALIDATION offline, voir en-tête du bloc Phase 10 ci-dessus) d'une
    ModelVersion candidate (`status` "shadow" ou "candidate") face à la
    version ACTIVE actuelle du même `model_type`, sur les mêmes prédictions
    LIVE — deux portes indépendantes, toutes les deux doivent passer :

      1. Échantillon LIVE résolu du candidat ET de la baseline >=
         LIVE_MIN_SAMPLE_SIZE (chacun scoré par sa PROPRE model_version_id,
         voir live_validation.py — jamais mélangé avec une autre version du
         même model_type, contrairement à monitoring.py qui agrège par role).
      2. Marge d'amélioration réelle : log_loss du candidat +
         PROMOTION_MIN_IMPROVEMENT < log_loss de la baseline. Un candidat
         seulement "pas pire" (`no_clear_gain`) ou pire (`rejected`) n'est
         JAMAIS promu automatiquement.

    Fonction PURE : ne modifie jamais la base (voir apply_promotion pour
    l'application effective d'une décision déjà prise).
    """
    from . import live_validation  # import local : évite un cycle promotion.py <-> live_validation.py

    candidate = session.get(ModelVersion, model_version_id)
    if candidate is None:
        raise ValueError(f"ModelVersion #{model_version_id} introuvable.")

    if candidate.is_active:
        return LivePromotionDecision(
            status="already_active", reason="Cette version est déjà la version active servie en direct.",
            model_type=candidate.model_type, market=market, candidate_version_id=candidate.id,
        )

    baseline = get_active_version(session, candidate.model_type)

    candidate_metrics = live_validation.compute_live_model_metrics(session, candidate.model_type, candidate.id, market)

    if baseline is None:
        # Bootstrap : aucune version active pour ce model_type — seule la
        # porte d'échantillon s'applique (même logique que evaluate_promotion
        # sans baseline, ci-dessus).
        if candidate_metrics.sample_size < LIVE_MIN_SAMPLE_SIZE:
            return LivePromotionDecision(
                status="insufficient_data",
                reason=(
                    f"Échantillon LIVE résolu insuffisant : {candidate_metrics.sample_size} "
                    f"< LIVE_MIN_SAMPLE_SIZE={LIVE_MIN_SAMPLE_SIZE}."
                ),
                model_type=candidate.model_type, market=market, candidate_version_id=candidate.id,
                candidate_metrics=vars(candidate_metrics),
            )
        return LivePromotionDecision(
            status="eligible",
            reason="Aucune version active pour ce type de modèle (bootstrap) — échantillon LIVE suffisant.",
            model_type=candidate.model_type, market=market, candidate_version_id=candidate.id,
            candidate_metrics=vars(candidate_metrics),
        )

    baseline_metrics = live_validation.compute_live_model_metrics(session, baseline.model_type, baseline.id, market)

    if candidate_metrics.sample_size < LIVE_MIN_SAMPLE_SIZE or baseline_metrics.sample_size < LIVE_MIN_SAMPLE_SIZE:
        return LivePromotionDecision(
            status="insufficient_data",
            reason=(
                f"Échantillon LIVE résolu insuffisant : candidat={candidate_metrics.sample_size}, "
                f"baseline={baseline_metrics.sample_size} (seuil LIVE_MIN_SAMPLE_SIZE={LIVE_MIN_SAMPLE_SIZE})."
            ),
            model_type=candidate.model_type, market=market,
            candidate_version_id=candidate.id, baseline_version_id=baseline.id,
            candidate_metrics=vars(candidate_metrics), baseline_metrics=vars(baseline_metrics),
        )

    if candidate_metrics.log_loss is None or baseline_metrics.log_loss is None:
        return LivePromotionDecision(
            status="insufficient_data",
            reason="Log loss LIVE manquant (candidat ou baseline) — comparaison impossible, jamais devinée.",
            model_type=candidate.model_type, market=market,
            candidate_version_id=candidate.id, baseline_version_id=baseline.id,
            candidate_metrics=vars(candidate_metrics), baseline_metrics=vars(baseline_metrics),
        )

    threshold = baseline_metrics.log_loss - PROMOTION_MIN_IMPROVEMENT
    if candidate_metrics.log_loss < threshold:
        status, reason = "eligible", (
            f"Log loss LIVE du candidat ({candidate_metrics.log_loss}) < baseline "
            f"({baseline_metrics.log_loss}) - marge ({PROMOTION_MIN_IMPROVEMENT}) = {round(threshold, 4)}."
        )
    elif candidate_metrics.log_loss < baseline_metrics.log_loss:
        status, reason = "no_clear_gain", (
            f"Candidat légèrement meilleur ({candidate_metrics.log_loss} < {baseline_metrics.log_loss}) mais "
            f"sous la marge minimale d'amélioration ({PROMOTION_MIN_IMPROVEMENT}) : jamais promu sur un écart "
            "trop faible pour être jugé significatif."
        )
    else:
        status, reason = "rejected", (
            f"Log loss LIVE du candidat ({candidate_metrics.log_loss}) >= baseline ({baseline_metrics.log_loss})."
        )

    return LivePromotionDecision(
        status=status, reason=reason, model_type=candidate.model_type, market=market,
        candidate_version_id=candidate.id, baseline_version_id=baseline.id,
        candidate_metrics=vars(candidate_metrics), baseline_metrics=vars(baseline_metrics),
    )


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
