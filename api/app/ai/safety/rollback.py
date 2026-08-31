"""
api/app/ai/safety/rollback.py — Phase 9.1 : §18-23 — rollback réellement
testable.

RÉUTILISE TEL QUEL (jamais réimplémenté, §18 : "ne pas supposer qu'il
fonctionne") : api/app/ai/arena/promotion.py::apply_promotion/
get_active_version (mécanisme de production EXISTANT, déjà utilisé par
POST /models/promotion/promote, require_admin). Ce module ne fait
qu'IDENTIFIER une cible de rollback valide et appeler ce mécanisme — jamais
une deuxième logique de promotion/désactivation.

§39 (prompt) : `execute_rollback` ÉCRIT en DB (session.add/commit, via
apply_promotion) — n'est donc JAMAIS appelé par un script contre
api/app.db dans cette phase. Testé UNIQUEMENT sur une base isolée
(api/test_safety_controls.py, jamais api/app.db).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app.models.team_rating import ModelVersion
from app.models.model_promotion_event import ModelPromotionEvent

from app.ai.arena.promotion import apply_promotion, get_active_version

from app.ai.safety.kill_switch import KillSwitchStore
from app.ai.safety.schemas import AuditEvent, RollbackReadiness, RollbackResult

UTC = timezone.utc


def find_rollback_target(session: Session, model_type: str) -> "ModelVersion | None":
    """§18/§23 : par défaut (aucune cible explicite fournie), la version
    RETIRED la plus récemment désactivée pour ce model_type — jamais
    inventée, jamais une version 'candidate'/'shadow' (celles-ci n'ont
    jamais été 'active', un rollback n'en fait donc jamais une cible — voir
    promotion.py, cycle de vie ci-dessus). Représente "annuler la dernière
    promotion" — PAS nécessairement la même cible d'un appel à l'autre
    (voir execute_rollback : passer `target_version_id` explicitement pour
    un rollback idempotent vers UNE version précise, §22)."""
    return session.exec(
        select(ModelVersion)
        .where(ModelVersion.model_type == model_type, ModelVersion.status == "retired", ModelVersion.deactivated_at.is_not(None))
        .order_by(ModelVersion.deactivated_at.desc())
    ).first()


def evaluate_rollback_readiness(session: Session, model_type: str, *, target_version_id: Optional[int] = None) -> RollbackReadiness:
    """§18/§23 : détermine si un rollback est possible SANS l'exécuter — lecture seule.
    `target_version_id` : si fourni, valide CETTE version précise (doit être une ModelVersion réelle du bon
    model_type, déjà passée par 'active' au moins une fois — status in (active, retired), jamais candidate/shadow)."""
    if target_version_id is not None:
        target = session.get(ModelVersion, target_version_id)
        if target is None or target.model_type != model_type or target.status not in ("active", "retired"):
            return RollbackReadiness(status="ROLLBACK_NOT_AVAILABLE", model_type=model_type,
                                      reason=f"target_version_id={target_version_id} invalide pour model_type='{model_type}' (introuvable, mauvais type, ou jamais passée par 'active' — §23 : jamais inventée).")
        return RollbackReadiness(status="ROLLBACK_AVAILABLE", model_type=model_type, target_version_id=target.id, target_version_name=target.name)

    target = find_rollback_target(session, model_type)
    if target is None:
        return RollbackReadiness(status="ROLLBACK_NOT_AVAILABLE", model_type=model_type,
                                  reason="Aucune ModelVersion retired trouvée pour ce model_type — pas de version précédente valide (§23 : jamais inventée).")
    return RollbackReadiness(status="ROLLBACK_AVAILABLE", model_type=model_type, target_version_id=target.id, target_version_name=target.name)


def execute_rollback(session: Session, model_type: str, *, actor: str, target_version_id: Optional[int] = None,
                      audit_store: Optional[KillSwitchStore] = None, market: str = "ALL") -> RollbackResult:
    """
    §19-§22 : exécute un rollback réel — ÉCRIT en DB (jamais appelé contre
    api/app.db par un script de cette phase, §39).

    §21 : cible introuvable -> DENIED, jamais un "best effort" sur une
    version arbitraire.
    §22 : IDEMPOTENT pour une cible EXPLICITE (`target_version_id` fourni) —
    si cette version est DÉJÀ active (deuxième rollback vers la MÊME
    cible), retourne NOOP_ALREADY_ACTIVE SANS ré-appeler apply_promotion
    (évite un ModelPromotionEvent dupliqué et un activated_at ré-écrit
    inutilement pour un état déjà cohérent). Sans `target_version_id`
    (comportement par défaut = "annuler la dernière promotion"), deux
    appels successifs alternent délibérément entre les deux dernières
    versions — ce n'est PAS le scénario d'idempotence testé par §22, qui
    porte sur "rollback A -> A" avec une cible fixe.

    §20 : un rollback EXÉCUTÉ écrit aussi un ModelPromotionEvent
    (decision="promoted", reason préfixé "ROLLBACK:") — CONTRAIREMENT à
    main.py::_record_promotion_event (qui exige un LivePromotionDecision
    issu de evaluate_live_promotion, absent ici par construction : un
    rollback restaure une version déjà connue, il ne réévalue pas une
    candidate), ce module construit directement la ligne. Sans cela, un
    futur rollback réel resterait invisible au gate ROLLBACK (Phase 9,
    basé sur le COMPTE de model_promotion_events) — limitation identifiée
    et corrigée ici plutôt que documentée sans être traitée.
    `market` : ModelPromotionEvent.market est NOT NULL mais un rollback
    restaure une VERSION (pas une décision par marché) — "ALL" par défaut,
    jamais une valeur de ARENA_MARKETS inventée sans qu'elle soit fournie
    explicitement par l'appelant.

    §27 : `audit_store` optionnel — si fourni, journalise ROLLBACK_EXECUTED/
    ROLLBACK_NOOP/ROLLBACK_DENIED (même fichier d'audit que le Kill Switch,
    un seul journal d'événements de sécurité, jamais deux journaux
    concurrents). `actor` y figure toujours, même quand `audit_store` est
    omis (traçabilité de l'appelant dans le RollbackResult, voir tests).
    """
    def _audit(event_type: str, reason: str) -> None:
        if audit_store is not None:
            audit_store.append_audit(AuditEvent(event_type=event_type, scope="MODEL_PROMOTION", code=None,
                                                  reason=reason, actor=actor, timestamp=datetime.now(UTC), model_version=str(target_version_id) if target_version_id else None))

    readiness = evaluate_rollback_readiness(session, model_type, target_version_id=target_version_id)
    if readiness.status == "ROLLBACK_NOT_AVAILABLE":
        _audit("ROLLBACK_DENIED", readiness.reason)
        return RollbackResult(status="DENIED", model_type=model_type, reason=readiness.reason)

    current_active = get_active_version(session, model_type)
    if current_active is not None and current_active.id == readiness.target_version_id:
        _audit("ROLLBACK_NOOP", f"target {readiness.target_version_id} déjà active — aucune écriture.")
        return RollbackResult(status="NOOP_ALREADY_ACTIVE", model_type=model_type,
                               restored_version_id=readiness.target_version_id, previous_active_version_id=current_active.id)

    target = session.get(ModelVersion, readiness.target_version_id)
    previous_active_id = current_active.id if current_active is not None else None
    apply_promotion(session, target)  # RÉUTILISÉ TEL QUEL — flush seulement, appelant committe (même discipline que promotion.py).
    event = ModelPromotionEvent(
        model_version_id=target.id, previous_model_version_id=previous_active_id, model_type=model_type, market=market,
        decision="promoted", reason=f"ROLLBACK: restored {target.name} (id={target.id}) by actor={actor}",
        actor=actor,
    )
    session.add(event)
    session.commit()
    _audit("ROLLBACK_EXECUTED", f"restored={target.id} previous_active={previous_active_id} actor={actor}")

    return RollbackResult(status="EXECUTED", model_type=model_type, restored_version_id=target.id, previous_active_version_id=previous_active_id)
