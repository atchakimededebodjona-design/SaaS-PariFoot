"""
snapshot.py — Phase 8A : primitives de capture "feature snapshot" (§32/§33
du prompt Phase 8A).

NE construit PAS un pipeline d'entraînement (§32 : "ne pas encore construire
un pipeline complet") — uniquement les primitives : lire une feature,
vérifier sa disponibilité/cutoff, capturer un instantané reproductible.

`build_feature_snapshot` est un WRAPPER FIN autour de
api/app/ai/engine/live_features.py::build_live_features — la sécurité anti-
fuite (Match.date < as_of dans chaque requête) vit déjà entièrement dans
cette fonction, testée colonne par colonne par api/test_live_features.py ;
ce module ne la duplique jamais, il se contente d'étiqueter chaque valeur
retournée avec son statut de registre (voir registry.py) pour produire un
instantané auto-descriptif.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from sqlmodel import Session

from app.ai.engine.live_features import DixonColesLeagueModel, build_live_features

from .registry import get_feature


@dataclass
class FeatureSnapshot:
    league: str
    home_team: str
    away_team: str
    cutoff: date                                   # as_of_date passé à build_live_features — jamais un match à/après cette date n'est vu
    features: dict[str, Optional[float]]
    feature_status: dict[str, str]                    # feature_name -> FeatureDefinition.status au moment de la capture
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def validate_cutoff(value_date: Optional[date], cutoff: date) -> str:
    """§32/§37 du prompt : vérifie qu'une donnée datée est bien antérieure à
    `cutoff` (strictement, même convention que Match.date < as_of dans
    live_features.py). Retourne :
      - "SAFE" si value_date < cutoff ;
      - "LEAKAGE_RISK" si value_date >= cutoff (la donnée n'était pas
        encore connue au moment du cutoff — jamais silencieusement acceptée) ;
      - "UNKNOWN" si value_date est None (rien à valider, jamais supposé SAFE par défaut)."""
    if value_date is None:
        return "UNKNOWN"
    return "SAFE" if value_date < cutoff else "LEAKAGE_RISK"


def build_feature_snapshot(
    session: Session, league: str, home_team: str, away_team: str, cutoff: date,
    league_model: Optional[DixonColesLeagueModel] = None,
) -> FeatureSnapshot:
    """
    Capture les 25 features ML de production (§33 : "match_id, cutoff_timestamp,
    features") pour (league, home_team, away_team) telles qu'elles auraient
    été vues AVANT `cutoff` — réutilise build_live_features tel quel (aucune
    réimplémentation de la logique de requête/anti-fuite).

    `league_model` : optionnel — sans lui, les features dc_* reviennent NaN
    (comportement déjà documenté et testé de build_live_features, jamais un
    contournement ajouté ici). Fournir un objet dupliquant predict_1x2/
    predict_over_under (voir DixonColesLeagueModel, un Protocol) pour les
    obtenir — ex. un LeagueModel de production, ou un modèle walk-forward
    reconstruit via research.py::build_dixon_coles_walk_forward pour une
    reconstruction historique fidèle à `cutoff`.
    """
    raw_features = build_live_features(session, league_model, league, home_team, away_team, cutoff)
    feature_status = {name: (get_feature(name).status if get_feature(name) else "UNREGISTERED") for name in raw_features}
    return FeatureSnapshot(
        league=league, home_team=home_team, away_team=away_team, cutoff=cutoff,
        features=raw_features, feature_status=feature_status,
    )


def snapshot_coverage(snapshot: FeatureSnapshot) -> dict:
    """Résumé de couverture d'un instantané — combien de features ont une
    valeur réelle (non-NaN) vs manquante, jamais une moyenne de qualité
    fabriquée."""
    import math

    total = len(snapshot.features)
    missing = sum(1 for v in snapshot.features.values() if v is None or (isinstance(v, float) and math.isnan(v)))
    return {
        "total_features": total, "missing": missing, "present": total - missing,
        "coverage_ratio": round((total - missing) / total, 4) if total else None,
    }
