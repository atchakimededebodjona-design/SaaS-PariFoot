"""
Calcul monétaire du programme de promotion (Phase 14).

§4 du prompt : "NE JAMAIS utiliser des calculs monétaires flottants naïfs.
Utiliser le mécanisme monétaire déjà présent dans le projet." — INSPECTION
PRÉALABLE (§1) : aucun mécanisme de précision monétaire n'existe nulle part
dans ce dépôt avant cette phase (aucun usage de `Decimal`, aucune constante
de prix en code — les prix Chariow vivent entièrement dans le dashboard
Chariow, hors de ce code, seuls les Product ID sont configurés côté serveur,
voir app/core/chariow_config.py). Cette phase introduit donc la PREMIÈRE
convention monétaire du projet — documentée ici, jamais implicite.

Convention retenue :
  - XOF (FCFA) est une devise SANS sous-unité (contrairement à EUR/USD) —
    les montants sont des ENTIERS dans leur unité courante, jamais des
    "minor units" façon centimes.
  - Le taux de commission est un ENTIER en points de base (1/100 de %,
    ex. 4000 = 40.00%) — jamais un float (0.40), pour que le calcul reste
    100% arithmétique entière, déterministe bit-à-bit.
  - Arrondi : FLOOR (division entière vers le bas) — jamais un montant de
    commission supérieur à ce que floor((gross * rate_bp) / 10000) donne,
    donc jamais un centime de commission "offert" par arrondi supérieur.
    Règle documentée, testée (voir api/test_referral_promoter_platform.py),
    identique quel que soit l'appelant (backend uniquement — aucun calcul
    de commission n'est jamais dupliqué côté frontend, qui ne fait
    qu'AFFICHER les montants déjà calculés et stockés par le backend).
"""

from __future__ import annotations

import os
from typing import Optional

# §33 : XOF par défaut (audience ouest-africaine du projet, cohérent avec
# Chariow/Mobile Money déjà en place) — configurable si un jour multi-devise.
DEFAULT_CURRENCY = os.environ.get("REFERRAL_DEFAULT_CURRENCY", "XOF")

# §43 : repli EXPLICITE si le webhook Chariow ne porte pas le montant réel
# payé (voir extract_actual_paid_amount ci-dessous) — un prix FIXE configuré
# par plan, PAS un "prix catalogue" utilisé à la place du montant payé
# (interdit par §33), mais littéralement le SEUL montant qu'il est possible
# de payer pour ce plan : les produits Chariow de ce projet sont documentés
# comme "Paiement unique, prix FIXE" (mode "Prix libre" explicitement exclu
# de l'API de checkout — voir app/core/chariow_config.py) — pour ce système
# précis, "prix configuré du plan" et "montant réellement payé" sont donc
# IDENTIQUES par construction, jamais une supposition sur un provider non
# vérifié. Utilisé UNIQUEMENT si le webhook lui-même ne porte aucun montant.
PLAN_LIST_PRICES: dict[str, int] = {}
for _plan_key, _env_name in (("monthly", "REFERRAL_PLAN_PRICE_MONTHLY"), ("yearly", "REFERRAL_PLAN_PRICE_YEARLY")):
    _raw = os.environ.get(_env_name, "")
    if _raw.strip():
        try:
            PLAN_LIST_PRICES[_plan_key] = int(_raw.strip())
        except ValueError:
            pass  # valeur non numérique -> traité comme non configuré, jamais une valeur devinée.

# §43 : clés candidates pour le montant réel dans l'objet "sale" du Pulse
# Chariow successful.sale — la doc du projet (voir app/billing/router.py::
# _handle_successful_sale) ne documente QUE custom_metadata pour cet objet ;
# aucune preuve dans ce dépôt qu'un champ montant y soit présent. Vérifié
# défensivement contre plusieurs noms plausibles plutôt que supposé absent
# purement par hypothèse — si aucune de ces clés n'est présente, on ne
# fabrique jamais un montant (voir extract_actual_paid_amount).
_AMOUNT_CANDIDATE_KEYS = ("amount", "amount_paid", "paid_amount", "total_amount", "total", "price")


def extract_actual_paid_amount(sale_body: dict, plan: Optional[str]) -> tuple[Optional[int], str]:
    """
    §33 : retourne (montant, source) où source in {"webhook", "configured_fixed_price", "unavailable"}.
    Ne fabrique JAMAIS un montant : "unavailable" -> l'appelant NE DOIT PAS créer de commission
    (voir commission_service.py, marque PAYMENT_CONFIRMATION_UNAVAILABLE, §43).
    """
    for key in _AMOUNT_CANDIDATE_KEYS:
        value = sale_body.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value), "webhook"
        if isinstance(value, str) and value.strip():
            try:
                parsed = int(float(value.strip()))
                if parsed > 0:
                    return parsed, "webhook"
            except ValueError:
                continue

    if plan and plan in PLAN_LIST_PRICES:
        return PLAN_LIST_PRICES[plan], "configured_fixed_price"

    return None, "unavailable"


def compute_commission_amount(gross_paid_amount: int, commission_rate_bp: int) -> int:
    """§3/§4/§33 : commission = floor(gross_paid_amount * commission_rate_bp / 10000) — arithmétique
    entière uniquement. Exemples testés (§40) : 10000 × 4000bp -> 4000 ; 15000 × 4000bp -> 6000."""
    if gross_paid_amount < 0 or commission_rate_bp < 0:
        raise ValueError("gross_paid_amount et commission_rate_bp doivent être >= 0.")
    return (gross_paid_amount * commission_rate_bp) // 10000
