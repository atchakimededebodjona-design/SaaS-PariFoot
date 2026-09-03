"""
Agrégations statistiques du programme de promotion (Phase 14).

§27 : "éviter N+1 queries... utiliser les agrégations adaptées au moteur DB
existant" — chaque fonction ici fait UNE requête agrégée (func.count/func.sum
via SQLAlchemy, déjà le moteur ORM de tout ce projet — SQLModel), jamais une
boucle Python sur des lignes individuelles pour sommer un total.

§14/§45/§46 : "ne jamais calculer le dashboard à partir d'un simple COUNT
d'utilisateurs" / "les totaux doivent être recomputables" — CHAQUE nombre
retourné ici provient d'une agrégation SQL directe sur ReferralCommission/
ReferralAttribution/ReferralVisit (les tables sources), jamais d'un champ mis
en cache/incrémenté à la main.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlmodel import Session, select, func

from app.models.promoter import Promoter, ReferralAttribution, ReferralCommission, ReferralVisit
from app.referral.withdrawal_service import compute_promoter_available_amount


def compute_promoter_stats(session: Session, promoter_id: int, *, since: Optional[datetime] = None, until: Optional[datetime] = None) -> dict:
    """§15 : visiteurs / inscriptions / abonnés convertis / ventes réellement payées / revenu / commission
    totale / disponible. "Abonnés" = utilisateurs réellement attribués (ReferralAttribution). "Ventes" =
    ReferralCommission avec paiement confirmé (donc, par construction de ce module, TOUTE ligne
    ReferralCommission — elle n'existe QUE sur paiement confirmé, §11)."""
    visitors = session.exec(
        select(func.count(func.distinct(ReferralVisit.visitor_id))).where(ReferralVisit.promoter_id == promoter_id)
    ).one()

    signups = session.exec(
        select(func.count()).select_from(ReferralAttribution).where(ReferralAttribution.promoter_id == promoter_id)
    ).one()

    commission_query = select(ReferralCommission).where(ReferralCommission.promoter_id == promoter_id)
    if since is not None:
        commission_query = commission_query.where(ReferralCommission.created_at >= since)
    if until is not None:
        commission_query = commission_query.where(ReferralCommission.created_at <= until)
    rows = session.exec(commission_query).all()

    accrued = [r for r in rows if r.status == "ACCRUED"]
    reversed_rows = [r for r in rows if r.status == "REVERSED"]

    total_sales = len(rows)  # §53 : "ventes" = paiements réellement confirmés, ACCRUED + REVERSED inclus (une vente a bien eu lieu, même si depuis remboursée)
    converted_subscribers = len({r.referred_user_id for r in rows})
    gross_revenue = sum(r.gross_paid_amount for r in accrued)
    total_commission = sum(r.commission_amount for r in accrued)
    reversed_commission = sum(r.commission_amount for r in reversed_rows)

    # Phase 15.14 : système de retrait MANUEL implémenté — "disponible"/"déjà versé"/"en attente de
    # retrait" sont désormais recalculés depuis PromoterWithdrawal (le journal des demandes), jamais un
    # compteur mutable séparé (même discipline que le reste de ce fichier, §46). Le paiement lui-même reste
    # entièrement MANUEL (aucun fournisseur externe intégré, voir withdrawal_service.py).
    payout_amounts = compute_promoter_available_amount(session, promoter_id)

    return {
        "visitors_attributed": int(visitors or 0),
        "signups_attributed": int(signups or 0),
        "converted_subscribers": converted_subscribers,
        "total_sales": total_sales,
        "gross_revenue": gross_revenue,
        "currency": accrued[0].currency if accrued else "XOF",
        "total_commission_accrued": total_commission,
        "total_commission_reversed": reversed_commission,
        "commission_available": payout_amounts["commission_available"],
        "commission_pending_withdrawal": payout_amounts["commission_pending_withdrawal"],
        "commission_paid_out": payout_amounts["commission_paid_out"],
        "commission_total_requested": payout_amounts["commission_total_requested"],
        "payout_system_status": "MANUAL_WITHDRAWAL_V1",
    }


def compute_admin_totals(session: Session, *, since: Optional[datetime] = None, until: Optional[datetime] = None) -> dict:
    """§21/§45 : totaux admin — toujours recomputables depuis le ledger (jamais un compteur mutable séparé)."""
    query = select(ReferralCommission)
    if since is not None:
        query = query.where(ReferralCommission.created_at >= since)
    if until is not None:
        query = query.where(ReferralCommission.created_at <= until)
    rows = session.exec(query).all()

    accrued = [r for r in rows if r.status == "ACCRUED"]
    reversed_rows = [r for r in rows if r.status == "REVERSED"]

    total_revenue = sum(r.gross_paid_amount for r in accrued)
    total_commissions = sum(r.commission_amount for r in accrued)
    reversed_commissions = sum(r.commission_amount for r in reversed_rows)

    return {
        "total_referred_sales": len(rows),
        "total_paid_sales_accrued": len(accrued),
        "total_reversed_sales": len(reversed_rows),
        "total_revenue": total_revenue,
        "total_commissions": total_commissions,
        "reversed_commissions": reversed_commissions,
        "net_after_commissions": total_revenue - total_commissions,
        "currency": accrued[0].currency if accrued else "XOF",
    }


def compute_promoter_leaderboard(session: Session) -> list[dict]:
    """§24 : Promoteur / Ventes / CA généré / commission — une ligne par promoteur, agrégée en SQL (une
    seule requête groupée, jamais une boucle par promoteur — §27)."""
    rows = session.exec(
        select(
            ReferralCommission.promoter_id,
            func.count().label("sales"),
            func.sum(ReferralCommission.gross_paid_amount).label("revenue"),
            func.sum(ReferralCommission.commission_amount).label("commission"),
        )
        .where(ReferralCommission.status == "ACCRUED")
        .group_by(ReferralCommission.promoter_id)
    ).all()

    promoters_by_id = {p.id: p for p in session.exec(select(Promoter)).all()}
    leaderboard = []
    for promoter_id, sales, revenue, commission in rows:
        promoter = promoters_by_id.get(promoter_id)
        if promoter is None:
            continue
        leaderboard.append({
            "promoter_id": promoter_id, "slug": promoter.slug, "status": promoter.status,
            "sales": int(sales or 0), "revenue": int(revenue or 0), "commission": int(commission or 0),
        })
    return leaderboard
