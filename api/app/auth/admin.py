"""
Dépendance pour réserver un endpoint aux administrateurs — Phase 10.

Aucun palier admin n'existait dans ce dépôt avant la Phase 10 (voir
commentaire Phase 9 précédemment présent dans main.py) : `User` n'a que
`is_active`. Choix retenu (décidé explicitement avec l'utilisateur, pas
supposé) : allowlist d'emails via variable d'environnement `ADMIN_EMAILS`
(CSV, comparaison insensible à la casse) plutôt qu'une colonne DB ou une clé
API statique — zéro migration sur `User`, adapté à un projet solo/petite
équipe. Vérifiée à CHAQUE requête (jamais mise en cache), même principe que
require_active_subscription : une variable d'environnement modifiée doit
prendre effet immédiatement, sans redéploiement du token existant.
"""

import os

from fastapi import Depends, HTTPException, status

from app.auth.security import get_current_user
from app.models.user import User


def _admin_emails() -> set[str]:
    raw = os.environ.get("ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """
    À utiliser comme dépendance sur tout endpoint mutant réservé aux
    administrateurs :

        @app.post("/models/promotion/promote")
        def promote(..., user: User = Depends(require_admin)):
            ...

    403 (jamais 401, l'utilisateur est authentifié — juste pas autorisé) si
    son email n'apparaît pas dans ADMIN_EMAILS. ADMIN_EMAILS absent/vide =
    aucun admin (jamais un accès ouvert par défaut faute de configuration).
    """
    if current_user.email.strip().lower() not in _admin_emails():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Accès réservé aux administrateurs.",
        )
    return current_user
