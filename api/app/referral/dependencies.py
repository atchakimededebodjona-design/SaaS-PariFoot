"""
§29 du prompt Phase 14 : "current_user -> current_promoter... Ne jamais
accepter promoter_id comme unique mécanisme d'autorisation." Même principe
exact que app/billing/dependencies.py::require_active_subscription et
app/auth/admin.py::require_admin — dérivé du token authentifié, jamais d'un
paramètre transmis par le client.
"""

from fastapi import Depends, HTTPException, status
from sqlmodel import Session, select

from app.core.database import get_session
from app.auth.security import get_current_user
from app.models.user import User
from app.models.promoter import Promoter


def get_current_promoter(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> Promoter:
    """
    403 si l'utilisateur authentifié n'a pas de compte promoteur — jamais 404 (on ne révèle pas si le
    slug/l'id existe, on indique juste que CE compte n'a pas accès). Un promoteur INACTIVE/SUSPENDED
    garde l'accès en LECTURE à son propre tableau de bord (§5 : "son historique doit rester intact") —
    seule la CRÉATION de nouvelles commissions est bloquée (voir commission_service.py), jamais la
    consultation.
    """
    promoter = session.exec(select(Promoter).where(Promoter.user_id == current_user.id)).first()
    if promoter is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Aucun compte promoteur associé à cet utilisateur.")
    return promoter
