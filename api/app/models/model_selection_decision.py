"""
Historique append-only des décisions du Model Selection Engine V1 (Phase 6) —
chaque exécution de scripts/model_selection_research.py ou
scripts/model_selection_shadow.py écrit EXACTEMENT une ligne ici par marché
évalué, y compris pour un refus ("insufficient_data"/"unstable"/
"not_significant") — même discipline que ModelPromotionEvent (Phase 10) :
"aucune décision ne doit être silencieuse". Aucune ligne n'est jamais mise à
jour ni supprimée après écriture.

Table VOLONTAIREMENT séparée de `model_promotion_events` (schéma pensé pour
le vocabulaire de promotion LIVE, déjà câblé aux crons Railway) : ce moteur
reste RECHERCHE + SHADOW UNIQUEMENT, jamais une promotion — ne jamais
mélanger les deux historiques (voir app/ai/arena/model_selection.py).
"""

from datetime import date, datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field


class ModelSelectionDecision(SQLModel, table=True):
    __tablename__ = "model_selection_decisions"

    id: Optional[int] = Field(default=None, primary_key=True)

    # Identifiant partagé par toutes les lignes écrites par une même
    # exécution de script (recherche ou shadow) — permet de regrouper les
    # décisions d'un même run sans dépendre de created_at (résolution DB).
    run_id: str = Field(index=True)
    mode: str = Field(index=True)  # "research" | "shadow"

    market: str = Field(index=True)
    league: Optional[str] = None
    as_of: date = Field(index=True)

    # "selected" | "insufficient_data" | "unstable" | "not_significant" —
    # voir app/ai/arena/model_selection.py::SelectionDecision.
    status: str = Field(index=True)
    selected_model_type: Optional[str] = Field(default=None, index=True)
    runner_up_model_type: Optional[str] = None
    windows_evaluated: int = 0

    # JSON texte {"windows": [...], "per_model": {...}} — snapshot des
    # métriques par fenêtre au moment de la décision (même convention texte
    # libre non reparsé que ModelVersion.metrics/config).
    metrics: Optional[str] = None
    reason: str

    # "none" | "platt" | "isotonic" — choix retenu par le Calibration Engine
    # pour CE candidat, "none" si non calibré (candidat non sélectionné, ou
    # calibration jugée non utile/insuffisante — voir calibration_engine.py).
    calibration_choice: str = "none"
    # "HELPFUL" | "NEUTRAL" | "HARMFUL" | "INSUFFICIENT_DATA"
    calibration_verdict: Optional[str] = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)
