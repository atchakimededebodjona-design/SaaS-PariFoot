"""
Prédictions "shadow" du Model Selection Engine V1 (Phase 6) — une ligne par
(match, marché), totalement ISOLÉE de `model_predictions` : jamais lue par
l'Ensemble, jamais par un endpoint /predictions/*, jamais par le scheduler
de production (voir app/ai/arena/scheduler.py, inchangé). Ce mécanisme ne
doit JAMAIS influencer ce qui est réellement servi aux utilisateurs — c'est
la raison même de son isolement dans sa propre table plutôt qu'une
réutilisation de model_predictions.role="shadow" (ce rôle existant est déjà
câblé aux crons Railway de génération/évaluation shadow XGBoost/LightGBM —
voir docstring de model_selection.py — un mécanisme différent, jamais
mélangé à celui-ci).

`production_probs` est un SNAPSHOT en lecture seule de ce que le modèle de
production affichait réellement pour ce match au moment de l'écriture de
cette ligne (jamais recalculé après coup) — permet de comparer honnêtement
"ce que Shadow aurait montré" à "ce que Production a réellement montré",
même si la version active change ensuite.

`candidate_probs_raw` (Phase 7) : la probabilité AVANT calibration — ajoutée
car `candidate_probs` (déjà existant) stocke la probabilité FINALE
(calibrée si `calibration_applied != "none"`), et la comparer à la version
brute après coup est impossible une fois `candidate_probs` écrasé par la
recalibration — nécessaire pour le RAW vs CALIBRATED tracking par
prédiction individuelle (§15/§16 du prompt Phase 7), jamais dérivable a
posteriori contrairement au "candidat quasi-sélectionné" (dérivé de
ModelSelectionDecision.metrics, voir track_record.py).
"""

from datetime import date, datetime, timezone
from typing import Optional
from sqlalchemy import UniqueConstraint
from sqlmodel import SQLModel, Field


class ShadowSelectionPrediction(SQLModel, table=True):
    __tablename__ = "shadow_selection_predictions"
    __table_args__ = (
        UniqueConstraint("league", "match_date", "home_team", "away_team", "market",
                          name="uq_shadow_selection_predictions_match_market"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    selection_decision_id: int = Field(foreign_key="model_selection_decisions.id", index=True)

    league: str = Field(index=True)
    match_date: date = Field(index=True)
    home_team: str
    away_team: str
    market: str = Field(index=True)

    candidate_model_type: str = Field(index=True)
    # "none" | "platt" | "isotonic" — appliqué à CETTE prédiction précise.
    calibration_applied: str = "none"
    # JSON texte {"home_win":..,"draw":..,"away_win":..} (ou clés BTTS/O-U) —
    # la probabilité candidate FINALE (après calibration éventuelle).
    candidate_probs: str
    # Phase 7 : probabilité AVANT calibration — None si calibration_applied="none"
    # (dans ce cas candidate_probs EST déjà la version brute, jamais dupliquée).
    candidate_probs_raw: Optional[str] = None

    # Snapshot production, lecture seule au moment de l'écriture — None si
    # aucune prédiction de production n'existait encore pour ce match.
    production_model_type: Optional[str] = None
    production_model_version_id: Optional[int] = None
    production_probs: Optional[str] = None

    status: str = Field(default="pending", index=True)  # "pending" | "resolved"
    result_home_goals: Optional[int] = None
    result_away_goals: Optional[int] = None
    candidate_correct: Optional[bool] = None
    production_correct: Optional[bool] = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)
    resolved_at: Optional[datetime] = None
