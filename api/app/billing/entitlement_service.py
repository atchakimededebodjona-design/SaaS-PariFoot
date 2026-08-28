"""
Service centralisé de calcul de l'Entitlement Premium — voir le document
d'architecture "Coexistence Chariow + Google Play" (section Entitlement)
pour le raisonnement complet.

Règle de fusion (Phase 1, Chariow uniquement) :

    premium = OR( ProviderSubscription.is_effectively_active pour chaque
                  ligne du user, tous providers confondus )

`recompute_entitlement` est le SEUL point d'écriture sur Entitlement et
EntitlementEvent. Aucun handler de paiement (Chariow aujourd'hui, Google
Play plus tard) n'écrit directement dans ces deux tables — il modifie
ProviderSubscription, puis appelle cette fonction. Ça garantit qu'il n'y a
jamais qu'une seule implémentation de "qu'est-ce que premium veut dire",
peu importe le nombre de providers réellement branchés.
"""

import json
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app.models.entitlement import Entitlement, EntitlementEvent
from app.models.provider_subscription import ProviderSubscription


def recompute_entitlement(
    session: Session,
    user_id: int,
    *,
    provider_subscription_id: Optional[int] = None,
    reason: Optional[str] = None,
) -> Entitlement:
    """
    Relit TOUTES les ProviderSubscription de l'utilisateur (peu de lignes —
    une par provider au plus aujourd'hui) et recalcule l'Entitlement en
    entier plutôt que de faire un ajustement incrémental — plus simple à
    raisonner, et le volume de lignes par utilisateur restera faible même
    avec plusieurs providers.

    Journalise un EntitlementEvent seulement quand `premium` change de
    valeur (False<->True) — un renouvellement ou un changement de plan qui
    laisse `premium` inchangé (déjà actif avant, toujours actif après)
    n'est pas un événement d'entitlement à proprement parler ; l'historique
    de CE changement-là vit déjà dans ProviderSubscription (updated_at,
    raw_payload) et dans les Pulses Chariow eux-mêmes.
    """
    rows = session.exec(
        select(ProviderSubscription).where(ProviderSubscription.user_id == user_id)
    ).all()

    active_rows = [r for r in rows if r.is_effectively_active]
    active_sources = sorted({r.provider for r in active_rows})
    new_premium = len(active_sources) > 0
    premium_until = max(
        (r.current_period_end for r in active_rows if r.current_period_end is not None),
        default=None,
    )

    entitlement = session.get(Entitlement, user_id)
    previous_premium = entitlement.premium if entitlement is not None else False

    if entitlement is None:
        entitlement = Entitlement(user_id=user_id)

    entitlement.premium = new_premium
    entitlement.premium_until = premium_until
    entitlement.active_sources = json.dumps(active_sources)
    entitlement.computed_at = datetime.now(timezone.utc)
    session.add(entitlement)

    if previous_premium != new_premium:
        session.add(
            EntitlementEvent(
                user_id=user_id,
                provider_subscription_id=provider_subscription_id,
                event_type="granted" if new_premium else "revoked",
                previous_premium=previous_premium,
                new_premium=new_premium,
                reason=reason,
            )
        )

    session.commit()
    session.refresh(entitlement)
    return entitlement


def is_premium(session: Session, user_id: int) -> bool:
    """Lecture seule, utilisée par require_active_subscription. Un
    utilisateur sans ligne Entitlement (jamais touché à la facturation)
    est traité comme non-premium — même comportement que l'ancien
    `Subscription is None -> 402`."""
    entitlement = session.get(Entitlement, user_id)
    return entitlement.premium if entitlement is not None else False
