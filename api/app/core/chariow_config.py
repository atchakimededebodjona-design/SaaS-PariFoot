"""
Configuration Chariow (plateforme de vente ouest-africaine, Mobile Money
natif — remplace Stripe pour la facturation).

Variables d'environnement requises (voir .env.example) :
    CHARIOW_API_KEY             — clé API pour créer les liens de checkout
    CHARIOW_PULSE_SECRET        — secret de signature des Pulses (webhooks Chariow)
    CHARIOW_PRODUCT_ID_MONTHLY  — ID du produit Licence mensuelle
    CHARIOW_PRODUCT_ID_YEARLY   — ID du produit Licence annuelle

IMPORTANT — modèle de licence, pas d'abonnement récurrent :
  - Les produits doivent être créés en mode "Paiement unique" avec un prix
    FIXE dans le dashboard Chariow. Le mode "Prix libre" est exclu de l'API
    de checkout (vérifié dans l'interface).
  - Chariow ne prélève PAS automatiquement de renouvellement pour ce type
    de produit : voir app/billing/router.py et README.md pour le flux de
    renouvellement manuel (l'utilisateur repasse par POST /billing/checkout).

CHARIOW_API_BASE_URL et l'appel POST /checkout dans
app/billing/router.py::_create_chariow_checkout_link sont confirmés via la
doc officielle (chariow.dev/en/guides/checkout).

NON VÉRIFIÉ (à confirmer avant mise en production, cf. rapport
d'intégration) :
  - L'existence d'un mode test (clé API distincte) : à vérifier dans
    Paramètres -> Clés API du dashboard Chariow.

Ne jamais commiter de vraie clé.
"""

import os

CHARIOW_API_KEY = os.environ.get("CHARIOW_API_KEY", "")
CHARIOW_PULSE_SECRET = os.environ.get("CHARIOW_PULSE_SECRET", "")

PRODUCT_IDS = {
    "monthly": os.environ.get("CHARIOW_PRODUCT_ID_MONTHLY", ""),
    "yearly": os.environ.get("CHARIOW_PRODUCT_ID_YEARLY", ""),
}

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")

CHARIOW_API_BASE_URL = "https://api.chariow.com/v1"

if os.environ.get("ENV") == "production":
    missing = [k for k, v in {
        "CHARIOW_API_KEY": CHARIOW_API_KEY,
        "CHARIOW_PULSE_SECRET": CHARIOW_PULSE_SECRET,
    }.items() if not v]
    if missing:
        raise RuntimeError(f"Variables Chariow manquantes en production : {missing}")
