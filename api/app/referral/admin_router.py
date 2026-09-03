"""
API Admin du programme de promotion (Phase 14).

§18/§19/§28 : zone admin distincte, séparée de l'API promoteur (router.py).
Chaque endpoint dépend de `require_admin` (app/auth/admin.py, Phase 10,
RÉUTILISÉ TEL QUEL — allowlist ADMIN_EMAILS, vérifiée à chaque requête,
jamais mise en cache) — AUCUN nouveau mécanisme d'identification admin
n'est créé ici (§1 : "comment l'administrateur est actuellement identifié" —
déjà répondu par l'inspection : require_admin existe depuis la Phase 10).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.core.database import get_session
from app.auth.admin import require_admin
from app.models.user import User
from app.models.provider_subscription import ProviderSubscription
from app.models.promoter import Promoter, PromoterWithdrawal, ReferralAttribution, ReferralCommission, PROMOTER_STATUSES, WITHDRAWAL_STATUSES
from app.referral.promoter_service import create_promoter, set_promoter_status
from app.referral.stats import compute_promoter_stats, compute_admin_totals, compute_promoter_leaderboard
from app.referral.withdrawal_service import confirm_withdrawal_paid, reject_withdrawal

router = APIRouter(prefix="/admin", tags=["admin-referral"])


# ---------------------------------------------------------------------------
# §22 : gestion des promoteurs.
# ---------------------------------------------------------------------------

class PromoterAdminRow(BaseModel):
    id: int
    user_id: int
    email: str
    slug: str
    referral_link: str
    status: str
    created_at: datetime


@router.get("/promoters", response_model=list[PromoterAdminRow])
def list_promoters(
    admin: User = Depends(require_admin), session: Session = Depends(get_session),
    q: Optional[str] = None, status_filter: Optional[str] = None, limit: int = 50, offset: int = 0,
):
    """§22/§25 : recherche par nom/email/slug (q), filtre par statut. §26 : pagination."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    query = select(Promoter)
    if status_filter:
        query = query.where(Promoter.status == status_filter)
    promoters = session.exec(query.order_by(Promoter.created_at.desc())).all()

    out = []
    for p in promoters:
        user = session.get(User, p.user_id)
        if user is None:
            continue
        if q:
            needle = q.lower()
            if needle not in p.slug.lower() and needle not in user.email.lower() and needle not in (user.name or "").lower():
                continue
        out.append(PromoterAdminRow(
            id=p.id, user_id=p.user_id, email=user.email, slug=p.slug,
            referral_link=f"https://www.xfoot.site/{p.slug}", status=p.status, created_at=p.created_at,
        ))
    return out[offset: offset + limit]


class CreatePromoterRequest(BaseModel):
    user_id: int
    requested_slug: Optional[str] = None


@router.post("/promoters", response_model=PromoterAdminRow, status_code=201)
def admin_create_promoter(body: CreatePromoterRequest, admin: User = Depends(require_admin), session: Session = Depends(get_session)):
    user = session.get(User, body.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable.")
    try:
        promoter = create_promoter(session, user_id=user.id, display_name=user.name or user.email, actor_user_id=admin.id, requested_slug=body.requested_slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return PromoterAdminRow(
        id=promoter.id, user_id=promoter.user_id, email=user.email, slug=promoter.slug,
        referral_link=f"https://www.xfoot.site/{promoter.slug}", status=promoter.status, created_at=promoter.created_at,
    )


class SetStatusRequest(BaseModel):
    status: str


@router.post("/promoters/{promoter_id}/status", response_model=PromoterAdminRow)
def admin_set_promoter_status(promoter_id: int, body: SetStatusRequest, admin: User = Depends(require_admin), session: Session = Depends(get_session)):
    promoter = session.get(Promoter, promoter_id)
    if promoter is None:
        raise HTTPException(status_code=404, detail="Promoteur introuvable.")
    if body.status not in PROMOTER_STATUSES:
        raise HTTPException(status_code=400, detail=f"Statut invalide. Attendu un de {PROMOTER_STATUSES}.")
    promoter = set_promoter_status(session, promoter, body.status, actor_user_id=admin.id)
    user = session.get(User, promoter.user_id)
    return PromoterAdminRow(
        id=promoter.id, user_id=promoter.user_id, email=user.email, slug=promoter.slug,
        referral_link=f"https://www.xfoot.site/{promoter.slug}", status=promoter.status, created_at=promoter.created_at,
    )


@router.get("/promoters/{promoter_id}")
def admin_get_promoter_detail(promoter_id: int, admin: User = Depends(require_admin), session: Session = Depends(get_session)):
    """§23 : détail complet d'un promoteur — nom/email/slug/lien/statut/date/conversions/ventes/CA/commission."""
    promoter = session.get(Promoter, promoter_id)
    if promoter is None:
        raise HTTPException(status_code=404, detail="Promoteur introuvable.")
    user = session.get(User, promoter.user_id)
    stats = compute_promoter_stats(session, promoter.id)
    return {
        "id": promoter.id, "user_id": promoter.user_id, "email": user.email if user else None,
        "name": user.name if user else None, "slug": promoter.slug,
        "referral_link": f"https://www.xfoot.site/{promoter.slug}", "status": promoter.status,
        "commission_rate_percent": promoter.commission_rate_bp / 100, "created_at": promoter.created_at,
        **stats,
    }


# ---------------------------------------------------------------------------
# §20 : abonnés.
# ---------------------------------------------------------------------------

class SubscriberRow(BaseModel):
    user_id: int
    name: Optional[str]
    email: str
    plan: Optional[str]
    amount_paid: Optional[int]
    date: Optional[datetime]
    promoter_slug: Optional[str]
    payment_status: str


@router.get("/subscribers", response_model=list[SubscriberRow])
def list_subscribers(admin: User = Depends(require_admin), session: Session = Depends(get_session), limit: int = 50, offset: int = 0):
    """§20 : nom/prénom/email/plan/montant payé/date/promoteur attribué/statut paiement — aucune donnée
    sensible de paiement exposée (§20 : "ne pas afficher de données sensibles de paiement")."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    subs = session.exec(
        select(ProviderSubscription).where(ProviderSubscription.provider == "chariow")
        .order_by(ProviderSubscription.updated_at.desc()).offset(offset).limit(limit)
    ).all()

    # Attribution promoteur (le cas échéant) + dernier montant réellement payé, en une seule requête chacun
    # (§27 : éviter N+1) plutôt qu'une requête par ligne d'abonné.
    user_ids = [s.user_id for s in subs]
    attributions = {a.converted_user_id: a for a in session.exec(select(ReferralAttribution).where(ReferralAttribution.converted_user_id.in_(user_ids))).all()} if user_ids else {}
    promoter_ids = {a.promoter_id for a in attributions.values()}
    promoters_by_id = {p.id: p for p in session.exec(select(Promoter)).all() if p.id in promoter_ids} if promoter_ids else {}
    latest_commission_by_sub = {}
    if subs:
        sub_ids = [s.id for s in subs]
        for c in session.exec(select(ReferralCommission).where(ReferralCommission.provider_subscription_id.in_(sub_ids)).order_by(ReferralCommission.created_at.desc())).all():
            latest_commission_by_sub.setdefault(c.provider_subscription_id, c)

    out = []
    for sub in subs:
        user = session.get(User, sub.user_id)
        if user is None:
            continue
        attribution = attributions.get(sub.user_id)
        promoter = promoters_by_id.get(attribution.promoter_id) if attribution else None
        commission_row = latest_commission_by_sub.get(sub.id)
        out.append(SubscriberRow(
            user_id=user.id, name=user.name, email=user.email, plan=sub.plan,
            amount_paid=commission_row.gross_paid_amount if commission_row else None,
            date=sub.updated_at, promoter_slug=promoter.slug if promoter else None,
            payment_status=sub.status,
        ))
    return out


# ---------------------------------------------------------------------------
# §21/§24 : ventes totales / gains.
# ---------------------------------------------------------------------------

@router.get("/earnings/totals")
def admin_earnings_totals(admin: User = Depends(require_admin), session: Session = Depends(get_session)):
    """§21 : TOTAL_SUBSCRIPTIONS/TOTAL_PAID_SALES/TOTAL_REVENUE/TOTAL_COMMISSIONS/NET_AFTER_COMMISSIONS —
    toutes calculées depuis des transactions réellement confirmées (§21), jamais un COUNT d'inscriptions."""
    total_subscriptions = session.exec(select(ProviderSubscription).where(ProviderSubscription.provider == "chariow")).all()
    totals = compute_admin_totals(session)
    return {"total_subscriptions": len(total_subscriptions), **totals}


@router.get("/earnings/by-promoter")
def admin_earnings_by_promoter(admin: User = Depends(require_admin), session: Session = Depends(get_session), sort: str = "commission"):
    """§24 : tri par commission la plus élevée (défaut), ventes, ou revenus."""
    leaderboard = compute_promoter_leaderboard(session)
    sort_key = {"commission": "commission", "sales": "sales", "revenue": "revenue"}.get(sort, "commission")
    return sorted(leaderboard, key=lambda r: r[sort_key], reverse=True)


# ---------------------------------------------------------------------------
# Phase 15.14 : retrait MANUEL des commissions — §Partie H/I/J : liste +
# traitement manuel côté admin. AUCUN fournisseur de paiement contacté ici
# (§Partie V) — ces endpoints tracent une demande et permettent à un
# administrateur de confirmer un paiement DÉJÀ effectué HORS Xfoot.
# ---------------------------------------------------------------------------

class WithdrawalAdminRow(BaseModel):
    id: int
    promoter_id: int
    promoter_slug: str
    promoter_email: str
    amount: int
    currency: str
    status: str
    requested_at: datetime
    processed_at: Optional[datetime]
    processed_by_admin_email: Optional[str]
    external_reference: Optional[str]
    admin_note: Optional[str]


def _to_admin_row(session: Session, w: PromoterWithdrawal, promoters_by_id: dict, users_by_id: dict) -> WithdrawalAdminRow:
    promoter = promoters_by_id.get(w.promoter_id)
    promoter_user = users_by_id.get(promoter.user_id) if promoter else None
    processed_admin_email = None
    if w.processed_by_admin_id is not None:
        processed_admin = users_by_id.get(w.processed_by_admin_id) or session.get(User, w.processed_by_admin_id)
        processed_admin_email = processed_admin.email if processed_admin else None
    return WithdrawalAdminRow(
        id=w.id, promoter_id=w.promoter_id, promoter_slug=promoter.slug if promoter else "—",
        promoter_email=promoter_user.email if promoter_user else "—",
        amount=w.amount, currency=w.currency, status=w.status,
        requested_at=w.requested_at, processed_at=w.processed_at,
        processed_by_admin_email=processed_admin_email,
        external_reference=w.external_reference, admin_note=w.admin_note,
    )


@router.get("/withdrawals", response_model=list[WithdrawalAdminRow])
def admin_list_withdrawals(
    admin: User = Depends(require_admin), session: Session = Depends(get_session),
    status_filter: Optional[str] = None, limit: int = 50, offset: int = 0,
):
    """§Partie H : "Demandes de retrait" — filtrable par statut (PENDING/PAID/REJECTED)."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    if status_filter is not None and status_filter not in WITHDRAWAL_STATUSES:
        raise HTTPException(status_code=400, detail=f"Statut invalide. Attendu un de {WITHDRAWAL_STATUSES}.")
    query = select(PromoterWithdrawal)
    if status_filter:
        query = query.where(PromoterWithdrawal.status == status_filter)
    rows = session.exec(query.order_by(PromoterWithdrawal.requested_at.desc()).offset(offset).limit(limit)).all()

    promoter_ids = {w.promoter_id for w in rows}
    promoters_by_id = {p.id: p for p in session.exec(select(Promoter)).all() if p.id in promoter_ids} if promoter_ids else {}
    user_ids = {p.user_id for p in promoters_by_id.values()} | {w.processed_by_admin_id for w in rows if w.processed_by_admin_id}
    users_by_id = {u.id: u for u in session.exec(select(User)).all() if u.id in user_ids} if user_ids else {}

    return [_to_admin_row(session, w, promoters_by_id, users_by_id) for w in rows]


class ConfirmWithdrawalRequest(BaseModel):
    # §Partie I : confirmation EXPLICITE obligatoire — un simple clic ne suffit jamais à transitionner
    # vers PAID (le prompt : "PAID" ne doit jamais signifier "l'administrateur a simplement cliqué").
    confirm: bool
    external_reference: Optional[str] = None  # §Partie J : texte libre, aucun format imposé
    admin_note: Optional[str] = None


@router.post("/withdrawals/{withdrawal_id}/confirm-paid", response_model=WithdrawalAdminRow)
def admin_confirm_withdrawal_paid(
    withdrawal_id: int, body: ConfirmWithdrawalRequest,
    admin: User = Depends(require_admin), session: Session = Depends(get_session),
):
    """
    §Partie I : "Confirmer le versement effectué" — ce bouton ne déclenche AUCUN paiement, il confirme
    qu'un administrateur a DÉJÀ envoyé l'argent manuellement, hors Xfoot (§Partie V). `confirm=True` est
    obligatoire (la case à cocher "Je confirme avoir réellement envoyé ce montant au promoteur" côté UI) —
    sans cela, refus explicite plutôt qu'une confirmation implicite.
    """
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="Confirmation explicite requise : 'Je confirme avoir réellement envoyé ce montant au promoteur.'",
        )
    withdrawal = confirm_withdrawal_paid(
        session, withdrawal_id, admin_id=admin.id,
        external_reference=body.external_reference, admin_note=body.admin_note,
    )
    if withdrawal is None:
        existing = session.get(PromoterWithdrawal, withdrawal_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Demande de retrait introuvable.")
        # §Partie N/Q : déjà PAID/REJECTED — jamais une seconde transition, jamais une erreur 500 opaque.
        raise HTTPException(status_code=409, detail=f"Cette demande n'est plus PENDING (statut actuel : {existing.status}).")

    promoters_by_id = {withdrawal.promoter_id: session.get(Promoter, withdrawal.promoter_id)}
    promoter = promoters_by_id[withdrawal.promoter_id]
    users_by_id = {promoter.user_id: session.get(User, promoter.user_id), admin.id: admin} if promoter else {admin.id: admin}
    return _to_admin_row(session, withdrawal, promoters_by_id, users_by_id)


class RejectWithdrawalRequest(BaseModel):
    admin_note: Optional[str] = None


@router.post("/withdrawals/{withdrawal_id}/reject", response_model=WithdrawalAdminRow)
def admin_reject_withdrawal(
    withdrawal_id: int, body: RejectWithdrawalRequest,
    admin: User = Depends(require_admin), session: Session = Depends(get_session),
):
    """§Partie P : refuse une demande PENDING — le montant redevient disponible automatiquement (recalculé,
    voir withdrawal_service.py::compute_promoter_available_amount), aucune donnée financière n'est perdue."""
    withdrawal = reject_withdrawal(session, withdrawal_id, admin_id=admin.id, admin_note=body.admin_note)
    if withdrawal is None:
        existing = session.get(PromoterWithdrawal, withdrawal_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Demande de retrait introuvable.")
        raise HTTPException(status_code=409, detail=f"Cette demande n'est plus PENDING (statut actuel : {existing.status}).")

    promoters_by_id = {withdrawal.promoter_id: session.get(Promoter, withdrawal.promoter_id)}
    promoter = promoters_by_id[withdrawal.promoter_id]
    users_by_id = {promoter.user_id: session.get(User, promoter.user_id), admin.id: admin} if promoter else {admin.id: admin}
    return _to_admin_row(session, withdrawal, promoters_by_id, users_by_id)
