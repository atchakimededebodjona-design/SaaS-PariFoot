"""
Cycle de vie du compte promoteur (Phase 14) — création, attribution de slug,
changement de statut. Réutilise la même discipline d'audit que
commission_service.py (ReferralAuditEvent, jamais un second système).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app.models.promoter import Promoter, ReferralAuditEvent, PROMOTER_STATUSES, DEFAULT_COMMISSION_RATE_BP
from app.referral.slug import generate_base_slug, next_slug_candidate, is_reserved_slug, is_valid_slug_format

UTC = timezone.utc


def _log_audit(session: Session, event_type: str, *, promoter_id: Optional[int], actor_user_id: Optional[int] = None, detail: Optional[str] = None) -> None:
    session.add(ReferralAuditEvent(event_type=event_type, promoter_id=promoter_id, actor_user_id=actor_user_id, detail=detail))


def slug_is_available(session: Session, slug: str) -> bool:
    if is_reserved_slug(slug):
        return False
    existing = session.exec(select(Promoter).where(Promoter.slug == slug)).first()
    return existing is None


def allocate_slug(session: Session, display_name: str) -> str:
    """§7 : "jean-dupont" -> "jean-dupont-2" si collision — déterministe (jamais aléatoire), borné pour
    éviter une boucle infinie en cas de pathologie (jamais rencontrée en pratique, garde-fou explicite)."""
    base = generate_base_slug(display_name)
    for attempt in range(1, 1000):
        candidate = next_slug_candidate(base, attempt)
        if slug_is_available(session, candidate):
            return candidate
    raise RuntimeError(f"Impossible d'allouer un slug disponible pour '{display_name}' après 999 tentatives.")


def create_promoter(session: Session, *, user_id: int, display_name: str, actor_user_id: Optional[int] = None,
                     requested_slug: Optional[str] = None) -> Promoter:
    """
    Crée un Promoter pour `user_id`. Si `requested_slug` est fourni, il DOIT être valide/disponible
    (levée ValueError sinon — jamais un repli silencieux sur un autre slug que celui demandé) ; sinon un
    slug est généré depuis `display_name` (§7).
    """
    existing = session.exec(select(Promoter).where(Promoter.user_id == user_id)).first()
    if existing is not None:
        raise ValueError(f"Un compte promoteur existe déjà pour user_id={user_id} (slug='{existing.slug}').")

    if requested_slug:
        if not is_valid_slug_format(requested_slug):
            raise ValueError(f"Format de slug invalide : '{requested_slug}'.")
        if not slug_is_available(session, requested_slug):
            raise ValueError(f"Slug déjà pris ou réservé : '{requested_slug}'.")
        slug = requested_slug
    else:
        slug = allocate_slug(session, display_name)

    promoter = Promoter(user_id=user_id, slug=slug, status="ACTIVE", commission_rate_bp=DEFAULT_COMMISSION_RATE_BP)
    session.add(promoter)
    session.commit()
    session.refresh(promoter)
    _log_audit(session, "PROMOTER_CREATED", promoter_id=promoter.id, actor_user_id=actor_user_id, detail=f"slug={slug}")
    session.commit()
    return promoter


def set_promoter_status(session: Session, promoter: Promoter, new_status: str, *, actor_user_id: Optional[int] = None) -> Promoter:
    """§5 : INACTIVE/SUSPENDED bloquent les FUTURES commissions (voir commission_service.py) — l'historique
    (ReferralCommission déjà créées) n'est jamais modifié ni supprimé par ce changement de statut."""
    if new_status not in PROMOTER_STATUSES:
        raise ValueError(f"Statut promoteur invalide : '{new_status}'. Attendu un de {PROMOTER_STATUSES}.")
    promoter.status = new_status
    promoter.updated_at = datetime.now(UTC)
    session.add(promoter)
    session.commit()
    session.refresh(promoter)
    event_type = {"ACTIVE": "PROMOTER_ACTIVATED", "INACTIVE": "PROMOTER_DEACTIVATED", "SUSPENDED": "PROMOTER_SUSPENDED"}[new_status]
    _log_audit(session, event_type, promoter_id=promoter.id, actor_user_id=actor_user_id)
    session.commit()
    return promoter
