"""
Retrait MANUEL des commissions promoteurs — Phase 15.14.

§Partie A du prompt : audité avant écriture — aucun mécanisme de payout
n'existait dans ce dépôt (voir app/referral/stats.py::compute_promoter_stats,
qui exposait déjà PAYOUT_SYSTEM_NOT_YET_IMPLEMENTED). Ce module est le SEUL
point d'écriture sur PromoterWithdrawal, même principe déjà établi par
app/referral/commission_service.py pour ReferralCommission et
app/billing/entitlement_service.py pour Entitlement.

§Partie M : AUCUNE seconde source de vérité financière — le "disponible" est
TOUJOURS recalculé depuis ReferralCommission (grand livre existant) et
PromoterWithdrawal (journal des demandes), jamais un compteur mutable séparé.

§Partie V : ce module ne contacte AUCUN fournisseur de paiement — il trace
une demande, permet à un administrateur de confirmer un paiement DÉJÀ
EFFECTUÉ hors Xfoot, et informe le promoteur. Aucun argent ne transite par ce
code.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models.promoter import Promoter, PromoterWithdrawal, ReferralCommission, ReferralAuditEvent

UTC = timezone.utc


def _log_audit(session: Session, event_type: str, *, promoter_id: Optional[int] = None,
                actor_user_id: Optional[int] = None, detail: Optional[str] = None) -> None:
    session.add(ReferralAuditEvent(event_type=event_type, promoter_id=promoter_id, actor_user_id=actor_user_id, detail=detail))


def compute_promoter_available_amount(session: Session, promoter_id: int) -> dict:
    """
    §Partie F/L/M : dérive TOUT depuis les tables sources, jamais un champ
    mutable. `available` est CLAMPÉ à 0 au minimum pour l'affichage/la
    décision (§Partie S : un remboursement après paiement peut en théorie
    rendre le calcul brut négatif — voir la documentation explicite de cette
    limite dans withdrawal_service.py::REFUND_AFTER_PAYOUT_LIMITATION
    ci-dessous ; ce module n'invente AUCUN mécanisme de récupération d'argent
    pour ce cas).
    """
    accrued_rows = session.exec(
        select(ReferralCommission).where(ReferralCommission.promoter_id == promoter_id, ReferralCommission.status == "ACCRUED")
    ).all()
    total_accrued = sum(r.commission_amount for r in accrued_rows)

    withdrawals = session.exec(select(PromoterWithdrawal).where(PromoterWithdrawal.promoter_id == promoter_id)).all()
    total_paid_out = sum(w.amount for w in withdrawals if w.status == "PAID")
    total_pending = sum(w.amount for w in withdrawals if w.status == "PENDING")

    raw_available = total_accrued - total_paid_out - total_pending
    return {
        "commission_accrued": total_accrued,
        "commission_paid_out": total_paid_out,
        "commission_pending_withdrawal": total_pending,
        "commission_available": max(0, raw_available),
        "_raw_available_unclamped": raw_available,  # usage interne (détection du cas §Partie S), jamais exposé tel quel à l'API
    }


class WithdrawalRequestError(ValueError):
    """§Partie D/E : refus métier explicite (jamais une exception générique opaque)."""

    def __init__(self, code: str, message: str, *, available: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.available = available


# §Partie E : "Si le métier ne définit pas encore si les retraits doivent
# être obligatoirement totaux ou peuvent être partiels : NE PAS inventer la
# règle." — recherche explicite (Partie A) : aucune règle de ce type
# n'existe nulle part dans ce dépôt avant cette phase. Convention retenue
# pour cette V1, suivant la PRÉFÉRENCE RECOMMANDÉE du prompt lui-même :
# retrait UNIQUEMENT du montant disponible en totalité (réduit fortement le
# risque de fragmentation/doublons). Documentée explicitement plutôt
# qu'implicite — une phase future pourra introduire des retraits partiels
# sans casser l'historique (chaque ligne PromoterWithdrawal garde son propre
# montant, jamais réécrit).
WITHDRAWAL_AMOUNT_POLICY = "FULL_AVAILABLE_ONLY"


def create_withdrawal_request(session: Session, promoter: Promoter, *, requested_amount: Optional[int] = None) -> PromoterWithdrawal:
    """
    §Partie E : `requested_amount`, si fourni par le client, n'est JAMAIS
    utilisé tel quel comme montant de la demande — il est seulement comparé
    au montant réellement disponible (recalculé serveur) pour décider
    d'accepter ou de refuser ; le montant réellement écrit en base est
    TOUJOURS `available`, jamais `requested_amount`.

    §Partie G/N : idempotence à deux niveaux (même discipline que
    commission_service.py::create_commission_for_confirmed_payment) : 1) une
    demande PENDING déjà existante est retournée telle quelle (no-op, jamais
    une deuxième ligne) ; 2) la contrainte UNIQUE PARTIELLE en base
    (PromoterWithdrawal.__table_args__) protège contre une vraie course
    (deux requêtes HTTP simultanées) — IntegrityError rattrapée ci-dessous,
    jamais laissée remonter comme une erreur 500 générique.
    """
    existing_pending = session.exec(
        select(PromoterWithdrawal).where(PromoterWithdrawal.promoter_id == promoter.id, PromoterWithdrawal.status == "PENDING")
    ).first()
    if existing_pending is not None:
        return existing_pending  # double-clic/retry réseau -> no-op, jamais une deuxième demande

    amounts = compute_promoter_available_amount(session, promoter.id)
    available = amounts["commission_available"]

    if available <= 0:
        raise WithdrawalRequestError("NO_AMOUNT_AVAILABLE", "Aucune commission disponible pour un retrait.", available=available)

    if requested_amount is not None:
        if requested_amount <= 0:
            raise WithdrawalRequestError("INVALID_AMOUNT", "Le montant demandé doit être positif.", available=available)
        if requested_amount != available:
            raise WithdrawalRequestError(
                "PARTIAL_WITHDRAWAL_NOT_ALLOWED",
                f"Retraits partiels non autorisés en V1 (WITHDRAWAL_AMOUNT_POLICY={WITHDRAWAL_AMOUNT_POLICY}) — "
                f"le montant demandé doit être exactement le montant disponible ({available}).",
                available=available,
            )

    withdrawal = PromoterWithdrawal(promoter_id=promoter.id, amount=available, currency="XOF", status="PENDING")
    session.add(withdrawal)
    try:
        session.commit()
    except IntegrityError:
        # §Partie G/O : course réelle entre deux requêtes simultanées — la contrainte UNIQUE partielle a
        # gagné la course, on relit la demande déjà créée par l'autre appel plutôt que d'échouer.
        session.rollback()
        existing = session.exec(
            select(PromoterWithdrawal).where(PromoterWithdrawal.promoter_id == promoter.id, PromoterWithdrawal.status == "PENDING")
        ).first()
        if existing is not None:
            return existing
        raise
    session.refresh(withdrawal)

    _log_audit(session, "WITHDRAWAL_REQUESTED", promoter_id=promoter.id,
               detail=f"withdrawal_id={withdrawal.id} amount={withdrawal.amount} {withdrawal.currency}")
    session.commit()
    return withdrawal


def confirm_withdrawal_paid(
    session: Session, withdrawal_id: int, *, admin_id: int,
    external_reference: Optional[str] = None, admin_note: Optional[str] = None,
) -> Optional[PromoterWithdrawal]:
    """
    §Partie C/I/N/O/Q : transition PENDING -> PAID via un UPDATE atomique
    conditionné sur `status = 'PENDING'` (exécuté par le moteur DB en une
    seule instruction, jamais un pattern lire-puis-écrire côté application) —
    correct sous SQLite ET PostgreSQL sans verrouillage explicite
    supplémentaire : si deux confirmations arrivent en même temps, une seule
    instruction UPDATE peut matcher `status='PENDING'` (la seconde ne trouve
    plus de ligne PENDING et échoue proprement, `rowcount == 0`).

    Retourne None si la demande n'existe pas OU n'est plus PENDING (déjà
    PAID/REJECTED) — l'appelant traduit ceci en 404/409, jamais une seconde
    transition silencieuse (§Partie Q : "un retrait PAID ne doit jamais
    pouvoir redevenir payable").
    """
    now = datetime.now(UTC)
    result = session.exec(
        update(PromoterWithdrawal)
        .where(PromoterWithdrawal.id == withdrawal_id, PromoterWithdrawal.status == "PENDING")
        .values(status="PAID", processed_at=now, processed_by_admin_id=admin_id,
                external_reference=external_reference, admin_note=admin_note, updated_at=now)
    )
    session.commit()
    if result.rowcount == 0:
        return None

    withdrawal = session.get(PromoterWithdrawal, withdrawal_id)
    _log_audit(session, "WITHDRAWAL_PAID", promoter_id=withdrawal.promoter_id, actor_user_id=admin_id,
               detail=f"withdrawal_id={withdrawal.id} amount={withdrawal.amount} {withdrawal.currency} ref={external_reference or '—'}")
    session.commit()
    return withdrawal


def reject_withdrawal(session: Session, withdrawal_id: int, *, admin_id: int, admin_note: Optional[str] = None) -> Optional[PromoterWithdrawal]:
    """
    §Partie P : refuse une demande PENDING — le montant redevient
    automatiquement disponible car `compute_promoter_available_amount` ne
    compte plus les demandes REJECTED dans `total_pending` (aucune donnée
    financière n'est modifiée, seul le statut change — le calcul de
    disponibilité se corrige de lui-même au prochain calcul, §Partie M).
    Même pattern d'UPDATE atomique conditionnel que confirm_withdrawal_paid.
    """
    now = datetime.now(UTC)
    result = session.exec(
        update(PromoterWithdrawal)
        .where(PromoterWithdrawal.id == withdrawal_id, PromoterWithdrawal.status == "PENDING")
        .values(status="REJECTED", processed_at=now, processed_by_admin_id=admin_id, admin_note=admin_note, updated_at=now)
    )
    session.commit()
    if result.rowcount == 0:
        return None

    withdrawal = session.get(PromoterWithdrawal, withdrawal_id)
    _log_audit(session, "WITHDRAWAL_REJECTED", promoter_id=withdrawal.promoter_id, actor_user_id=admin_id,
               detail=f"withdrawal_id={withdrawal.id} amount={withdrawal.amount} {withdrawal.currency}")
    session.commit()
    return withdrawal


# §Partie S : limite documentée, jamais un système de récupération d'argent
# inventé. Trois cas audités :
#   1. Commission ACCRUED, jamais retirée (aucune ligne PromoterWithdrawal) :
#      un remboursement (reverse_commissions_for_subscription, déjà
#      existant) passe la commission à REVERSED -> elle sort immédiatement
#      de `total_accrued` -> `available` diminue correctement. Cas déjà géré
#      SANS aucun changement ici.
#   2. Une demande PENDING existe pour ce promoteur au moment du
#      remboursement : le `total_pending` de CETTE demande n'est pas touché
#      (elle reste PENDING pour son plein montant), alors que
#      `total_accrued` vient de baisser -> `raw_available` (non clampé) peut
#      devenir négatif -> `commission_available` affiché reste 0 (clampé),
#      mais la demande PENDING existante n'est PAS automatiquement annulée.
#      Décision explicite de cette phase : NE PAS annuler automatiquement une
#      demande humaine en cours sur la seule base d'un recalcul — un
#      administrateur doit trancher au cas par cas (REJECTED manuel si
#      jugé nécessaire). Documenté ici, jamais implémenté silencieusement.
#   3. Une demande est déjà PAID lorsque le remboursement survient : AUCUN
#      mécanisme de reversement/reprise de fonds n'existe (le prompt
#      l'interdit explicitement : "NE PAS inventer un système de
#      récupération d'argent"). `raw_available` peut devenir négatif ;
#      l'admin en est informé uniquement en lisant `commission_accrued` vs
#      `commission_paid_out` (recalculés, jamais masqués) — aucune action
#      automatique n'est prise. Limite connue et acceptée pour la V1.
REFUND_AFTER_PAYOUT_LIMITATION = (
    "Un remboursement Chariow (reverse_commissions_for_subscription) après qu'une commission a déjà été "
    "incluse dans un retrait PENDING ou PAID ne déclenche AUCUNE récupération automatique d'argent. Le "
    "calcul de disponibilité peut devenir négatif en interne (clampé à 0 pour l'affichage) ; l'administrateur "
    "doit trancher manuellement (REJECTED d'une demande PENDING si jugé nécessaire) — aucun système de "
    "recouvrement n'est implémenté en V1, conformément au prompt Phase 15.14 Partie S."
)
