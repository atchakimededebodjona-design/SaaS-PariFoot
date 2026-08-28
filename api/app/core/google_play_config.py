"""
Configuration Google Play Billing (Phase 2 — backend uniquement, voir
app/billing/google_play_service.py).

Variables d'environnement requises :
    GOOGLE_PLAY_PACKAGE_NAME              — applicationId Android (site.xfoot.app,
                                             cf. mobile/capacitor.config.json)
    GOOGLE_PLAY_SERVICE_ACCOUNT_JSON      — contenu JSON complet du compte de
                                             service (pas un chemin de fichier —
                                             pattern Railway habituel pour les
                                             secrets), scope androidpublisher
    GOOGLE_PLAY_SERVICE_ACCOUNT_JSON_PATH — confort dev local uniquement :
                                             chemin vers un fichier .json
                                             gitignored, utilisé seulement si
                                             la variable ci-dessus est absente
    GOOGLE_PLAY_PRODUCT_ID                — Product ID Google Play UNIQUE (xfoot_premium) —
                                             modèle Play Console 2022+ : un abonnement =
                                             un Product ID contenant plusieurs Base Plans,
                                             PAS un Product ID par période (confirmé contre
                                             la doc officielle "Create and manage
                                             subscriptions" avant d'écrire ce module — voir
                                             échanges Phase 3, correction de la Phase 2
                                             initiale qui supposait à tort deux Product ID)
    GOOGLE_PLAY_BASE_PLAN_ID_MONTHLY      — Base Plan ID Google Play pour le plan mensuel
    GOOGLE_PLAY_BASE_PLAN_ID_YEARLY       — Base Plan ID Google Play pour le plan annuel
    GOOGLE_RTDN_SHARED_SECRET             — protection intérimaire de
                                             POST /billing/google/rtdn (query
                                             param ?secret=...) tant que la
                                             vérification OIDC des messages
                                             Pub/Sub n'est pas branchée sur un
                                             vrai projet Google Cloud

IMPORTANT — aucun secret réel ici : ce module ne fait que lire des variables
d'environnement, jamais de valeur en dur. Ne jamais commiter de vraie clé de
compte de service ni de vrai secret RTDN.

Compte de service (à créer manuellement dans Google Cloud Console, puis
inviter dans Play Console -> Utilisateurs et autorisations, avec les
permissions couvrant au moins "Voir les données financières, commandes et
réponses aux enquêtes d'annulation" (lecture, purchases.subscriptionsv2.get)
et une permission couvrant purchases.subscriptions.acknowledge (écriture) —
noms exacts des cases à cocher à confirmer dans l'interface Play Console au
moment de la configuration réelle, non vérifiable sans accès à une vraie
Play Console).

Scope OAuth2 unique nécessaire : https://www.googleapis.com/auth/androidpublisher
(confirmé via la documentation officielle Google Play Developer API).
"""

import os

GOOGLE_PLAY_PACKAGE_NAME = os.environ.get("GOOGLE_PLAY_PACKAGE_NAME", "")

GOOGLE_PLAY_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON", "")
GOOGLE_PLAY_SERVICE_ACCOUNT_JSON_PATH = os.environ.get("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON_PATH", "")

GOOGLE_PLAY_PRODUCT_ID = os.environ.get("GOOGLE_PLAY_PRODUCT_ID", "")

GOOGLE_PLAY_BASE_PLAN_IDS = {
    "monthly": os.environ.get("GOOGLE_PLAY_BASE_PLAN_ID_MONTHLY", ""),
    "yearly": os.environ.get("GOOGLE_PLAY_BASE_PLAN_ID_YEARLY", ""),
}

GOOGLE_RTDN_SHARED_SECRET = os.environ.get("GOOGLE_RTDN_SHARED_SECRET", "")

GOOGLE_PLAY_API_BASE_URL = "https://androidpublisher.googleapis.com/androidpublisher/v3"
GOOGLE_PLAY_OAUTH_SCOPE = "https://www.googleapis.com/auth/androidpublisher"

if os.environ.get("ENV") == "production":
    missing = [k for k, v in {
        "GOOGLE_PLAY_PACKAGE_NAME": GOOGLE_PLAY_PACKAGE_NAME,
    }.items() if not v]
    if missing:
        raise RuntimeError(f"Variables Google Play manquantes en production : {missing}")
