"""
API Promoteur + résolution publique du référent (Phase 14).

§28 : "Séparer PROMOTER API / ADMIN API." — ce routeur ne contient QUE les
endpoints publics (résolution/attribution d'un lien de parrainage) et ceux
réservés à un promoteur authentifié sur SES PROPRES données (jamais un
promoter_id transmis par le client, §29 — toujours dérivé via
get_current_promoter, qui dérive lui-même depuis current_user).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session, select

from app.core.database import get_session
from app.core.rate_limit import limiter
from app.auth.security import get_current_user
from app.models.user import User
from app.models.promoter import Promoter, PromoterWithdrawal, ReferralAttribution, ReferralCommission, ReferralVisit, ReferralAuditEvent
from app.referral.dependencies import get_current_promoter
from app.referral.stats import compute_promoter_stats
from app.referral.withdrawal_service import WithdrawalRequestError, create_withdrawal_request

router = APIRouter(tags=["referral"])

# §9 : fenêtre d'attribution — AUCUNE convention existante dans ce dépôt
# avant cette phase (recherche explicite, §1) : 30 jours est le standard de
# facto de l'industrie de l'affiliation (fenêtre de cookie la plus
# fréquemment utilisée) — retenu ici comme PREMIÈRE convention du projet,
# documentée explicitement plutôt qu'implicite, jamais présentée comme une
# règle préexistante. Une future Phase pourra l'ajuster sans casser
# l'historique (le calcul n'est appliqué qu'à la CRÉATION de l'attribution).
ATTRIBUTION_WINDOW_DAYS = 30

UTC = timezone.utc


def _mask_email_or_name(user: User) -> str:
    """§16 : privacy-safe — jamais l'email complet. Préfère le prénom/nom si connu (moins sensible qu'un
    email), sinon email masqué (2 premiers caractères + domaine)."""
    if user.name:
        return user.name
    local, _, domain = user.email.partition("@")
    masked_local = (local[:2] + "***") if len(local) > 2 else "***"
    return f"{masked_local}@{domain}" if domain else "***"


# ---------------------------------------------------------------------------
# §31/§32 : résolution publique du slug — AUCUNE authentification requise
# (appelée dès l'arrivée sur la landing page, avant toute connexion).
# ---------------------------------------------------------------------------

class ResolveSlugRequest(BaseModel):
    visitor_id: Optional[str] = None  # UUID anonyme généré côté client — jamais une donnée personnelle (§8)


class ResolveSlugResponse(BaseModel):
    valid: bool


@router.post("/referral/resolve/{slug}", response_model=ResolveSlugResponse)
@limiter.limit("60/minute")
def resolve_referral_slug(request: Request, slug: str, body: ResolveSlugRequest, session: Session = Depends(get_session)):
    """
    §32 : un slug inexistant OU un promoteur suspendu/inactif renvoient EXACTEMENT la même réponse
    ({"valid": false}) — jamais d'information permettant de distinguer les deux cas (§32 : "ne pas exposer
    d'informations administratives"). Enregistre une visite anonyme (§15) UNIQUEMENT si valid=true.
    """
    promoter = session.exec(select(Promoter).where(Promoter.slug == slug.lower())).first()
    if promoter is None or promoter.status != "ACTIVE":
        return ResolveSlugResponse(valid=False)

    if body.visitor_id:
        session.add(ReferralVisit(promoter_id=promoter.id, visitor_id=body.visitor_id))
        session.commit()

    return ResolveSlugResponse(valid=True)


# ---------------------------------------------------------------------------
# §8/§9/§10 : attribution — appelée juste après inscription/connexion, une
# fois l'utilisateur authentifié (le SEUL moment où une ligne serveur est créée).
# ---------------------------------------------------------------------------

class AttributeReferralRequest(BaseModel):
    slug: str
    captured_at: Optional[datetime] = None  # horodatage de la capture cliente originale (§9, fenêtre d'attribution)
    visitor_id: Optional[str] = None


class AttributeReferralResponse(BaseModel):
    attributed: bool
    reason: Optional[str] = None


@router.post("/referral/attribute", response_model=AttributeReferralResponse)
@limiter.limit("20/minute")
def attribute_referral(
    request: Request, body: AttributeReferralRequest,
    current_user: User = Depends(get_current_user), session: Session = Depends(get_session),
):
    """
    §9 : LAST VALID REFERRER — un utilisateur ne peut être attribué qu'UNE SEULE fois (contrainte UNIQUE
    sur converted_user_id) ; un appel répété (ex. reconnexions successives avec un référent en
    localStorage) est un no-op silencieux, jamais une erreur, jamais une ré-attribution.
    §10 : self-referral explicitement rejeté et journalisé (SELF_REFERRAL_REJECTED), jamais de commission
    possible dans ce cas (revérifié aussi en aval, voir commission_service.py).
    """
    already = session.exec(select(ReferralAttribution).where(ReferralAttribution.converted_user_id == current_user.id)).first()
    if already is not None:
        return AttributeReferralResponse(attributed=False, reason="ALREADY_ATTRIBUTED")

    promoter = session.exec(select(Promoter).where(Promoter.slug == body.slug.lower())).first()
    if promoter is None or promoter.status != "ACTIVE":
        return AttributeReferralResponse(attributed=False, reason="INVALID_OR_INACTIVE_PROMOTER")

    if promoter.user_id == current_user.id:
        session.add(ReferralAuditEvent(event_type="SELF_REFERRAL_REJECTED", promoter_id=promoter.id,
                                        actor_user_id=current_user.id, detail="Tentative d'auto-parrainage à l'attribution."))
        session.commit()
        return AttributeReferralResponse(attributed=False, reason="SELF_REFERRAL_REJECTED")

    if body.captured_at is not None:
        captured_at = body.captured_at if body.captured_at.tzinfo else body.captured_at.replace(tzinfo=UTC)
        if datetime.now(UTC) - captured_at > timedelta(days=ATTRIBUTION_WINDOW_DAYS):
            return AttributeReferralResponse(attributed=False, reason="ATTRIBUTION_WINDOW_EXPIRED")

    attribution = ReferralAttribution(
        promoter_id=promoter.id, converted_user_id=current_user.id, visitor_id=body.visitor_id,
        captured_at=body.captured_at,
    )
    session.add(attribution)
    session.commit()
    return AttributeReferralResponse(attributed=True)


# ---------------------------------------------------------------------------
# §15/§16/§17 : tableau de bord promoteur — réservé au promoteur authentifié,
# sur SES PROPRES données uniquement (§29 : jamais un promoter_id fourni par le client).
# ---------------------------------------------------------------------------

class PromoterMeResponse(BaseModel):
    slug: str
    referral_link: str
    status: str
    commission_rate_percent: float
    created_at: datetime


@router.get("/promoter/me", response_model=PromoterMeResponse)
def get_my_promoter_profile(promoter: Promoter = Depends(get_current_promoter)):
    return PromoterMeResponse(
        slug=promoter.slug, referral_link=f"https://www.xfoot.site/{promoter.slug}",
        status=promoter.status, commission_rate_percent=promoter.commission_rate_bp / 100,
        created_at=promoter.created_at,
    )


@router.get("/promoter/me/stats")
def get_my_promoter_stats(promoter: Promoter = Depends(get_current_promoter), session: Session = Depends(get_session)):
    return compute_promoter_stats(session, promoter.id)


class SaleRow(BaseModel):
    date: datetime
    client: str
    plan: Optional[str]
    amount_paid: int
    commission: int
    currency: str
    status: str


@router.get("/promoter/me/sales", response_model=list[SaleRow])
def get_my_promoter_sales(
    promoter: Promoter = Depends(get_current_promoter), session: Session = Depends(get_session),
    limit: int = 20, offset: int = 0,
):
    """§16 : "Client (privacy-safe)" — jamais l'email complet, jamais les détails de paiement (§16 : "pas
    d'informations de paiement, pas de transaction details inutiles"). §26 : pagination obligatoire."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    rows = session.exec(
        select(ReferralCommission)
        .where(ReferralCommission.promoter_id == promoter.id)
        .order_by(ReferralCommission.created_at.desc())
        .offset(offset).limit(limit)
    ).all()
    out = []
    for r in rows:
        client = session.get(User, r.referred_user_id)
        out.append(SaleRow(
            date=r.created_at, client=_mask_email_or_name(client) if client else "—",
            plan=r.plan, amount_paid=r.gross_paid_amount, commission=r.commission_amount,
            currency=r.currency, status=r.status,
        ))
    return out


# ---------------------------------------------------------------------------
# Phase 15.14 : retrait MANUEL des commissions — endpoints promoteur.
# §Partie D/K : le promoteur ne voit et n'agit que sur SES PROPRES demandes,
# jamais via un promoter_id fourni par le client (même discipline que le
# reste de ce fichier, get_current_promoter dérive du token authentifié).
# ---------------------------------------------------------------------------

class CreateWithdrawalRequest(BaseModel):
    # §Partie E : optionnel — si fourni, DOIT correspondre exactement au montant
    # disponible recalculé côté serveur (WITHDRAWAL_AMOUNT_POLICY=FULL_AVAILABLE_ONLY,
    # voir withdrawal_service.py) ; jamais utilisé tel quel comme montant écrit en base.
    amount: Optional[int] = None


class WithdrawalRow(BaseModel):
    id: int
    amount: int
    currency: str
    status: str
    requested_at: datetime
    processed_at: Optional[datetime]
    external_reference: Optional[str]


def _to_withdrawal_row(w: PromoterWithdrawal) -> WithdrawalRow:
    return WithdrawalRow(
        id=w.id, amount=w.amount, currency=w.currency, status=w.status,
        requested_at=w.requested_at, processed_at=w.processed_at, external_reference=w.external_reference,
    )


@router.post("/promoter/me/withdrawals", response_model=WithdrawalRow, status_code=201)
@limiter.limit("10/minute")
def request_withdrawal(
    request: Request, body: CreateWithdrawalRequest,
    promoter: Promoter = Depends(get_current_promoter), session: Session = Depends(get_session),
):
    """
    §Partie D : "Demander un retrait." Le montant réellement disponible est
    TOUJOURS recalculé côté serveur (jamais confié au frontend, §Partie E) —
    voir create_withdrawal_request. §Partie G/N : un double-clic ou un retry
    réseau sur cet endpoint ne crée jamais une deuxième demande PENDING.
    """
    try:
        withdrawal = create_withdrawal_request(session, promoter, requested_amount=body.amount)
    except WithdrawalRequestError as e:
        raise HTTPException(status_code=400, detail={"code": e.code, "message": str(e), "available": e.available})
    return _to_withdrawal_row(withdrawal)


@router.get("/promoter/me/withdrawals", response_model=list[WithdrawalRow])
def list_my_withdrawals(
    promoter: Promoter = Depends(get_current_promoter), session: Session = Depends(get_session),
    limit: int = 20, offset: int = 0,
):
    """§Partie K : le promoteur voit SES propres demandes uniquement, jamais celles d'un autre promoteur
    (filtré par promoter.id dérivé du token, jamais un identifiant transmis par le client)."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    rows = session.exec(
        select(PromoterWithdrawal)
        .where(PromoterWithdrawal.promoter_id == promoter.id)
        .order_by(PromoterWithdrawal.requested_at.desc())
        .offset(offset).limit(limit)
    ).all()
    return [_to_withdrawal_row(w) for w in rows]
