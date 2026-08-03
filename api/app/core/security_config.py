"""
Configuration de sécurité.

IMPORTANT — avant la mise en production :
- SECRET_KEY DOIT être définie via la variable d'environnement JWT_SECRET_KEY,
  jamais laissée à la valeur par défaut ci-dessous (qui n'est là que pour
  permettre de lancer le projet en local sans configuration).
  Génère une vraie clé avec : python -c "import secrets; print(secrets.token_hex(32))"
"""

import os

SECRET_KEY = os.environ.get(
    "JWT_SECRET_KEY",
    "dev-only-insecure-key-CHANGE-ME-IN-PRODUCTION",
)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 jours

if SECRET_KEY == "dev-only-insecure-key-CHANGE-ME-IN-PRODUCTION" and os.environ.get("ENV") == "production":
    raise RuntimeError(
        "JWT_SECRET_KEY doit être définie explicitement en production. "
        "Génère-en une avec : python -c \"import secrets; print(secrets.token_hex(32))\""
    )
