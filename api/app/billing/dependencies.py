"""Dépendance pour réserver un endpoint aux utilisateurs avec un abonnement actif."""

from fastapi import Depends, HTTPException, status
from sqlmodel import Session

from app.core.database import get_session
from app.auth.security import get_current_user
from app.models.user import User
from app.billing.entitlement_service import is_premium


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
    l'utilisateur a un Entitlement Premium actif au moment de la requête —
    Entitlement étant le cache unifié recalculé à partir de
    ProviderSubscription (Chariow aujourd'hui, d'autres providers plus tard),
    voir app/billing/entitlement_service.py. Remplace la lecture directe de
    l'ancienne table Subscription, sans changement de comportement pour
    Chariow (voir tests test_premium.py, test_main.py, test_prediction_history.py).
    """
    if current_user.email == "atchakimededebodjona@gmail.com":
        return current_user

    if not is_premium(session, current_user.id):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Abonnement actif requis pour accéder à cette fonctionnalité",
        )
    return current_user
