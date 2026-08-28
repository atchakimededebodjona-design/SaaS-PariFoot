"""
Backfill Chariow : Subscription (ancien modèle unique) -> ProviderSubscription
-> Entitlement.

Appelé depuis la migration Alembic dédiée (voir api/alembic/versions/) ET
depuis les tests (api/test_provider_subscription_backfill.py) — jamais
dupliqué entre les deux, pour être certain que ce qui est testé est
exactement ce qui tourne en migration.

Réutilise recompute_entitlement (app/billing/entitlement_service.py) pour le
calcul de l'Entitlement plutôt que de réimplémenter la règle de fusion ici —
zéro logique métier dupliquée entre le backfill et le fonctionnement normal.

Idempotent : peut être rejoué sans dupliquer de lignes (utile si la
migration doit être relancée après une interruption).

IMPORTANT — limite connue : recompute_entitlement() fait un session.commit()
par utilisateur. Sur SQLite (Alembic en mode "non-transactional DDL", seul
usage prévu pour cette phase — voir consignes explicites de ne rien
appliquer sur Railway/production ici), c'est sans conséquence. Sur
PostgreSQL, ce commit intermédiaire interagirait avec la transaction gérée
par Alembic — à revoir avant toute exécution en production.
"""

from sqlmodel import Session, select

from app.core.chariow_config import PRODUCT_IDS
from app.models.subscription import Subscription
from app.models.provider_subscription import ProviderSubscription
from app.billing.entitlement_service import recompute_entitlement


def backfill_provider_subscriptions(session: Session) -> int:
    """Une ligne ProviderSubscription(provider='chariow') par ligne
    Subscription existante, y compris status='none' (comptes ayant
    initié un checkout sans jamais payer) — copie fidèle, aucune perte.
    Retourne le nombre de lignes réellement créées."""
    created = 0
    old_rows = session.exec(select(Subscription)).all()
    for old in old_rows:
        already = session.exec(
            select(ProviderSubscription).where(
                ProviderSubscription.user_id == old.user_id,
                ProviderSubscription.provider == "chariow",
            )
        ).first()
        if already is not None:
            continue  # idempotence : rejeu sans dupliquer

        product_id = PRODUCT_IDS.get(old.plan) or None if old.plan else None
        session.add(
            ProviderSubscription(
                user_id=old.user_id,
                provider="chariow",
                external_ref=old.chariow_license_key,
                product_id=product_id,
                plan=old.plan,
                status=old.status,
                current_period_end=old.current_period_end,
                days_until_expiry=old.days_until_expiry,
                auto_renewing=False,
                raw_status=old.status,
                created_at=old.created_at,
                updated_at=old.updated_at,
            )
        )
        created += 1
    session.commit()
    return created


def backfill_entitlements(session: Session) -> int:
    """Recalcule (via recompute_entitlement, jamais une logique séparée)
    l'Entitlement de chaque utilisateur ayant au moins une
    ProviderSubscription. Les utilisateurs n'ayant jamais touché à la
    facturation n'obtiennent aucune ligne — is_premium() les traite comme
    non-premium par défaut. Retourne le nombre d'utilisateurs traités."""
    user_ids = session.exec(select(ProviderSubscription.user_id).distinct()).all()
    for user_id in user_ids:
        recompute_entitlement(session, user_id, reason="backfill: migration Phase 1 Chariow")
    return len(user_ids)
