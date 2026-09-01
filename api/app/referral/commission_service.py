"""
Création/réversion des commissions (Phase 14) — SEUL point d'écriture sur
ReferralCommission, exactement comme app/billing/entitlement_service.py::
recompute_entitlement est le SEUL point d'écriture sur Entitlement (même
principe déjà établi dans ce dépôt, réutilisé ici).

§11 : appelé UNIQUEMENT depuis les handlers Pulse déjà existants
(app/billing/router.py::_handle_successful_sale pour la création,
_handle_license_status(new_status="revoked") pour la réversion) — jamais au
clic, à l'inscription, à la création du checkout, ni au choix d'un plan.

§12 : idempotence — protégée à DEUX niveaux, jamais un seul :
  1. Le routeur Pulse (ProcessedPulseDelivery, déjà existant, déjà testé)
     déduplique AVANT même d'appeler _handle_successful_sale.
  2. La contrainte UNIQUE sur ReferralCommission.source_event_id (défense en
     profondeur si ce service était un jour appelé autrement).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models.promoter import Promoter, ReferralAttribution, ReferralCommission, ReferralAuditEvent
from app.models.provider_subscription import ProviderSubscription
from app.referral.money import extract_actual_paid_amount, compute_commission_amount

logger = logging.getLogger(__name__)

UTC = timezone.utc


def _log_audit(session: Session, event_type: str, *, promoter_id: Optional[int] = None, detail: Optional[str] = None) -> None:
    session.add(ReferralAuditEvent(event_type=event_type, promoter_id=promoter_id, detail=detail))


def create_commission_for_confirmed_payment(
    session: Session, *, provider_subscription: ProviderSubscription, sale_body: dict, delivery_id: Optional[str],
) -> Optional[ReferralCommission]:
    """
    §11 : appelée UNIQUEMENT quand un paiement vient d'être confirmé (successful.sale déjà traité par
    l'appelant — cette fonction ne vérifie PAS elle-même le statut du paiement, elle fait confiance à
    l'appelant qui est le SEUL endroit du code où "PAID/CONFIRMED" est établi).

    Retourne None (jamais une exception) si : aucune attribution, promoteur non ACTIVE, self-referral,
    montant introuvable (§43 : PAYMENT_CONFIRMATION_UNAVAILABLE) — un sale non commissionné ne doit JAMAIS
    faire échouer le traitement du paiement lui-même (même discipline "best-effort" que
    api/main.py::_log_prediction).
    """
    referred_user_id = provider_subscription.user_id

    attribution = session.exec(
        select(ReferralAttribution).where(ReferralAttribution.converted_user_id == referred_user_id)
    ).first()
    if attribution is None:
        return None  # vente non attribuée — cas normal, jamais une erreur (§44 : jamais de backfill rétroactif)

    promoter = session.get(Promoter, attribution.promoter_id)
    if promoter is None:
        logger.warning("ReferralAttribution %s pointe vers un Promoter inexistant (id=%s).", attribution.id, attribution.promoter_id)
        return None

    if promoter.status != "ACTIVE":
        return None  # §5 : un promoteur INACTIVE/SUSPENDED ne génère plus de nouvelles commissions

    if promoter.user_id == referred_user_id:
        # §10 : ne devrait jamais arriver (bloqué dès l'attribution, voir router.py::attribute_referral)
        # — revérifié ici en défense en profondeur, jamais une commission créée dans ce cas.
        _log_audit(session, "SELF_REFERRAL_REJECTED", promoter_id=promoter.id,
                   detail=f"Tentative de commission self-referral bloquée au paiement (user_id={referred_user_id}).")
        session.commit()
        return None

    if delivery_id:
        existing = session.exec(
            select(ReferralCommission).where(ReferralCommission.source_event_id == delivery_id)
        ).first()
        if existing is not None:
            return existing  # §12 : idempotence — déjà créée pour cet événement précis

    amount, amount_source = extract_actual_paid_amount(sale_body, provider_subscription.plan)
    if amount is None:
        logger.warning(
            "PAYMENT_CONFIRMATION_UNAVAILABLE: montant réellement payé introuvable pour provider_subscription_id=%s "
            "(plan=%s) — aucune commission fabriquée (§43).", provider_subscription.id, provider_subscription.plan,
        )
        return None

    commission_amount = compute_commission_amount(amount, promoter.commission_rate_bp)

    commission = ReferralCommission(
        promoter_id=promoter.id, referred_user_id=referred_user_id,
        provider_subscription_id=provider_subscription.id, plan=provider_subscription.plan,
        source_event_id=delivery_id, gross_paid_amount=amount, commission_rate_bp=promoter.commission_rate_bp,
        commission_amount=commission_amount, status="ACCRUED",
    )
    session.add(commission)
    try:
        session.commit()
    except IntegrityError:
        # §12 : course improbable entre deux appels concurrents avec le même delivery_id — la contrainte
        # UNIQUE a gagné la course, on relit la ligne déjà créée par l'autre appel plutôt que d'échouer.
        session.rollback()
        if delivery_id:
            existing = session.exec(
                select(ReferralCommission).where(ReferralCommission.source_event_id == delivery_id)
            ).first()
            if existing is not None:
                return existing
        raise
    session.refresh(commission)

    _log_audit(session, "COMMISSION_CREATED", promoter_id=promoter.id,
               detail=f"commission_id={commission.id} amount={commission_amount} {commission.currency} (source={amount_source})")
    session.commit()
    return commission


def reverse_commissions_for_subscription(session: Session, provider_subscription_id: int, *, reason: str) -> list[ReferralCommission]:
    """
    §13/§35 : appelée quand une ProviderSubscription passe à "revoked" (remboursement/révocation — même
    vocabulaire déjà établi par ce dépôt, voir ProviderSubscription.revoked_or_refunded_at). Marque TOUTES
    les commissions ACCRUED de cet abonnement comme REVERSED — ne supprime JAMAIS une ligne (§13 : "NE PAS
    supprimer l'historique").
    """
    rows = session.exec(
        select(ReferralCommission).where(
            ReferralCommission.provider_subscription_id == provider_subscription_id,
            ReferralCommission.status == "ACCRUED",
        )
    ).all()
    now = datetime.now(UTC)
    for row in rows:
        row.status = "REVERSED"
        row.reversed_at = now
        row.reversed_reason = reason
        row.updated_at = now
        session.add(row)
        _log_audit(session, "COMMISSION_REVERSED", promoter_id=row.promoter_id, detail=f"commission_id={row.id} reason={reason}")
    if rows:
        session.commit()
        for row in rows:
            session.refresh(row)
    return rows
