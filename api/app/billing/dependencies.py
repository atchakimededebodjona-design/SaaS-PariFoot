"""Dépendance pour réserver un endpoint aux utilisateurs avec un abonnement actif."""

from fastapi import Depends, HTTPException, status
from sqlmodel import Session, select

from app.core.database import get_session
from app.auth.security import get_current_user
from app.models.user import User
from app.models.subscription import Subscription


def require_active_subscription(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> User:
    """
    À utiliser comme dépendance sur tout endpoint premium :

        @app.get("/predictions/premium/...")
        def premium_endpoint(user: User = Depends(require_active_subscription)):
            ...

    Vérifie en base (jamais sur la seule foi d'un champ dans le token JWT,
    qui pourrait devenir obsolète entre deux renouvellements) que
    l'utilisateur a un abonnement actif au moment de la requête.
    """
    sub = session.exec(select(Subscription).where(Subscription.user_id == current_user.id)).first()
    if sub is None or not sub.is_active:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Abonnement actif requis pour accéder à cette fonctionnalité",
        )
    return current_user
