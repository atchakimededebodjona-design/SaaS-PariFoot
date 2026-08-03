"""
Configuration Stripe.

Variables d'environnement requises (voir .env.example) :
    STRIPE_SECRET_KEY       — clé API secrète (sk_test_... en dev, sk_live_... en prod)
    STRIPE_WEBHOOK_SECRET   — secret de signature du endpoint webhook (whsec_...)
    STRIPE_PRICE_ID_MONTHLY — Price ID Stripe du plan mensuel (price_...)
    STRIPE_PRICE_ID_YEARLY  — Price ID Stripe du plan annuel (price_...)
    FRONTEND_URL            — utilisé pour les URLs de succès/annulation de Checkout

IMPORTANT : ne jamais commiter de vraie clé secrète. En développement,
utilise les clés de test Stripe (préfixe sk_test_/whsec_ issu du CLI Stripe
`stripe listen`), jamais les clés live.
"""

import os

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

PRICE_IDS = {
    "monthly": os.environ.get("STRIPE_PRICE_ID_MONTHLY", ""),
    "yearly": os.environ.get("STRIPE_PRICE_ID_YEARLY", ""),
}

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")

if os.environ.get("ENV") == "production":
    missing = [k for k, v in {
        "STRIPE_SECRET_KEY": STRIPE_SECRET_KEY,
        "STRIPE_WEBHOOK_SECRET": STRIPE_WEBHOOK_SECRET,
    }.items() if not v]
    if missing:
        raise RuntimeError(f"Variables Stripe manquantes en production : {missing}")
