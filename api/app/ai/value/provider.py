"""
api/app/ai/value/provider.py — Phase 8H, §41 : interface fournisseur d'odds
GÉNÉRIQUE, CONCEPTUELLE UNIQUEMENT.

AUCUNE implémentation réseau ici. AUCUN appel à The Odds API (ni ailleurs).
Ce Protocol documente la forme qu'un futur fournisseur devrait respecter
pour alimenter le Value Engine (api/app/ai/value/core.py) SANS que ce
dernier ne dépende d'un fournisseur particulier (§4/§41 du prompt) — The
Odds API reste NON INTÉGRÉ à ce stade (statut SUPPORT_REQUIRED, Phase 8G.2).

Une implémentation réelle (ex. TheOddsApiProvider) serait un module SÉPARÉ,
créé uniquement après intégration décidée — jamais dans ce fichier.
"""

from __future__ import annotations

from typing import Protocol


class OddsProvider(Protocol):
    """Contrat conceptuel — AUCUNE classe de ce dépôt ne l'implémente
    aujourd'hui. Sert uniquement à documenter la forme attendue d'un futur
    fournisseur, pour que api/app/ai/value/core.py puisse rester indépendant
    de toute implémentation réseau."""

    def get_historical_snapshot(self, sport_key: str, iso_date: str) -> dict:
        """Retournerait un snapshot historique brut (forme fournisseur, non normalisée) — PAS implémenté ici."""
        ...

    def get_markets(self, sport_key: str) -> list[str]:
        """Retournerait les marchés disponibles pour un sport donné — PAS implémenté ici."""
        ...

    def get_bookmakers(self, sport_key: str) -> list[str]:
        """Retournerait les bookmakers disponibles pour un sport donné — PAS implémenté ici."""
        ...
