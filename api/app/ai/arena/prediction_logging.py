"""
Prediction logging & resolution engine — Phase 6.

Mécanisme UNIQUE réutilisé par TOUS les modèles (Dixon-Coles, Elo, XGBoost,
LightGBM, futurs modèles) pour enregistrer une prédiction individuelle avant
le match, puis la résoudre une fois le résultat réel connu (§1 du ticket
Phase 6 : "ne pas créer plusieurs systèmes de logging différents").

=== Répartition prediction_log / model_predictions ===

`prediction_log` (Phase 5, INCHANGÉE) reste la source de vérité pour les
métriques Dixon-Coles dans l'Arena (voir app/ai/arena/service.py) — sa
contrainte UNIQUE sur (league, match_date, home_team, away_team), SANS
notion de modèle, empêcherait structurellement un second modèle de logguer
une prédiction pour le même match (conflit de clé) : c'est la raison
concrète, pas arbitraire, pour laquelle ce ticket crée `model_predictions`
plutôt que d'étendre `prediction_log`.

`model_predictions` (ce module) est la source pour Elo/XGBoost/LightGBM
(qui n'ont jamais eu de prédictions individuelles stockées nulle part avant
ce ticket) ET, en parallèle, pour Dixon-Coles EN DIRECT (dual-write : chaque
appel à /predictions/* continue d'écrire dans prediction_log EXACTEMENT
comme avant — zéro régression — et écrit AUSSI ici, pour que Dixon-Coles
passe par le même mécanisme commun que les autres, comme l'exige le
principe architectural §1). Les métriques Dixon-Coles de l'Arena restent
calculées depuis prediction_log uniquement (jamais les deux à la fois, pour
ne jamais compter deux fois la même prédiction ni risquer une divergence —
§7 : "conserver une source unique de vérité").

=== Deux origines pour `source` ===

  - "live"     : produite par un vrai appel HTTP à /predictions/* — le
                 résultat est inconnu au moment de l'insertion (status
                 "pending"), résolu plus tard par fetch_daily_results.py.
  - "backtest" : produite pendant l'évaluation walk-forward/split temporel
                 d'un script de backtest (scripts/backtest_elo.py,
                 scripts/train_ml_stacking_from_db.py) — le match est déjà
                 dans le passé et son résultat déjà connu du script au
                 moment même où il calcule la probabilité (c'est la
                 définition même d'un backtest). Résolue IMMÉDIATEMENT à
                 l'insertion, avec le même compute_correctness() que les
                 prédictions "live" — aucune fuite : la probabilité a été
                 produite en walk-forward/split temporel (donc sans le
                 résultat), seule sa RÉSOLUTION (déjà connue) est
                 immédiate, ce qui est la nature même d'un backtest.
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion, next_version_name


@dataclass
class PredictionRecord:
    """
    Contrat commun qu'un modèle doit produire pour être loggué (§3 du
    ticket). Les probabilités doivent être celles RÉELLEMENT produites par
    le modèle avant le match, jamais recalculées après coup. `prob_btts_*`/
    `prob_over_2_5`/`prob_under_2_5` restent None pour un modèle qui ne les
    modélise pas (Elo, XGBoost, LightGBM aujourd'hui) — jamais fabriquées.
    """
    league: str
    match_date: date
    home_team: str
    away_team: str
    model_type: str
    prob_home: float
    prob_draw: float
    prob_away: float
    prob_btts_yes: Optional[float] = None
    prob_btts_no: Optional[float] = None
    prob_over_2_5: Optional[float] = None
    prob_under_2_5: Optional[float] = None
    confidence: Optional[float] = None
    features_version: Optional[str] = None
    source: str = "live"  # "live" | "backtest"
    role: str = "active"  # "active" | "shadow" (Phase 9 — voir ModelPrediction.role)


def _pick_1x2(r: PredictionRecord) -> str:
    """Même convention que main.py::_pick_1x2 (Phase 5) — dupliquée
    volontairement ici plutôt qu'importée : main.py importerait alors ce
    module (dual-write), qui importerait main.py en retour (import
    circulaire). Logique triviale et stable, risque de divergence
    négligeable face au risque d'import circulaire."""
    if r.prob_home >= r.prob_draw and r.prob_home >= r.prob_away:
        return "home"
    if r.prob_away >= r.prob_draw:
        return "away"
    return "draw"


def _pick_btts(r: PredictionRecord) -> Optional[str]:
    if r.prob_btts_yes is None or r.prob_btts_no is None:
        return None
    return "yes" if r.prob_btts_yes >= r.prob_btts_no else "no"


def _pick_over_2_5(r: PredictionRecord) -> Optional[str]:
    if r.prob_over_2_5 is None or r.prob_under_2_5 is None:
        return None
    return "over" if r.prob_over_2_5 >= r.prob_under_2_5 else "under"


def get_or_create_active_model_version(
    session: Session, model_type: str, default_base_name: str, notes: str
) -> ModelVersion:
    """
    Réutilise la version actuellement active de `model_type` si elle existe
    déjà (typiquement créée par un script de backtest, ex.
    scripts/backtest_elo.py) — n'en crée une que si AUCUNE version active
    n'existe encore. Bootstrap nécessaire pour dixon_coles, qui n'a
    historiquement aucune ligne model_versions (voir Phase 5 : le modèle de
    production est chargé depuis model_artifact, pas depuis cette table).
    Ne duplique jamais le système de versionnage déjà en place (§4).
    """
    existing = session.exec(
        select(ModelVersion).where(ModelVersion.model_type == model_type, ModelVersion.is_active == True)  # noqa: E712
    ).first()
    if existing is not None:
        return existing

    version = ModelVersion(
        name=next_version_name(session, default_base_name),
        model_type=model_type,
        trained_at=datetime.now(timezone.utc),
        is_active=True,
        notes=notes,
    )
    session.add(version)
    session.commit()
    session.refresh(version)
    return version


def log_prediction(session: Session, record: PredictionRecord, model_version_id: int) -> ModelPrediction:
    """
    Enregistre UNE prédiction via le contrat commun. Idempotent (§13) : si
    une ligne existe déjà pour (league, match_date, home_team, away_team,
    model_type, model_version_id), la retourne telle quelle SANS la
    dupliquer ni l'écraser — une prédiction déjà enregistrée pour une
    version donnée ne change jamais.

    N'appelle PAS session.commit() : ajoute et flush seulement (pour
    obtenir .id), laisse l'appelant grouper sa transaction — même
    discipline que deactivate_other_versions() dans team_rating.py.
    """
    existing = session.exec(
        select(ModelPrediction).where(
            ModelPrediction.league == record.league,
            ModelPrediction.match_date == record.match_date,
            ModelPrediction.home_team == record.home_team,
            ModelPrediction.away_team == record.away_team,
            ModelPrediction.model_type == record.model_type,
            ModelPrediction.model_version_id == model_version_id,
        )
    ).first()
    if existing is not None:
        return existing

    row = ModelPrediction(
        league=record.league,
        match_date=record.match_date,
        home_team=record.home_team,
        away_team=record.away_team,
        model_type=record.model_type,
        model_version_id=model_version_id,
        source=record.source,
        role=record.role,
        prob_home=record.prob_home,
        prob_draw=record.prob_draw,
        prob_away=record.prob_away,
        prob_btts_yes=record.prob_btts_yes,
        prob_btts_no=record.prob_btts_no,
        prob_over_2_5=record.prob_over_2_5,
        prob_under_2_5=record.prob_under_2_5,
        pick_1x2=_pick_1x2(record),
        pick_btts=_pick_btts(record),
        pick_over_2_5=_pick_over_2_5(record),
        confidence=record.confidence,
        features_version=record.features_version,
        status="pending",
    )
    session.add(row)
    session.flush()
    return row


def compute_correctness(
    pick_1x2: Optional[str], pick_btts: Optional[str], pick_over_2_5: Optional[str],
    home_goals: int, away_goals: int,
) -> tuple[Optional[bool], Optional[bool], Optional[bool]]:
    """
    Source UNIQUE de vérité pour la correction 1X2/BTTS/O-U (§7 : "ne pas
    créer une deuxième implémentation contradictoire") — réutilisée telle
    quelle par fetch_daily_results.py (prediction_log ET model_predictions)
    et par resolve_prediction() ci-dessous.

    Un pick=None (marché non modélisé par ce modèle) renvoie correct=None,
    jamais False : on ne pénalise jamais un modèle pour un marché qu'il ne
    prédit pas. Pour prediction_log, dont les 3 picks sont TOUJOURS
    renseignés, ceci reproduit exactement le comportement historique
    (jamais None en pratique pour ce cas) — comportement inchangé.
    """
    if home_goals > away_goals:
        actual_1x2 = "home"
    elif away_goals > home_goals:
        actual_1x2 = "away"
    else:
        actual_1x2 = "draw"
    actual_btts = "yes" if home_goals > 0 and away_goals > 0 else "no"
    actual_over_2_5 = "over" if (home_goals + away_goals) > 2.5 else "under"

    correct_1x2 = (pick_1x2 == actual_1x2) if pick_1x2 is not None else None
    correct_btts = (pick_btts == actual_btts) if pick_btts is not None else None
    correct_over_2_5 = (pick_over_2_5 == actual_over_2_5) if pick_over_2_5 is not None else None
    return correct_1x2, correct_btts, correct_over_2_5


def resolve_prediction(row: ModelPrediction, home_goals: int, away_goals: int) -> None:
    """
    Résout UNE ModelPrediction avec le résultat réel — mute `row` en place
    (status, result_*, correct_*). N'appelle PAS session.commit() : laisse
    l'appelant grouper sa transaction.

    Ne DOIT jamais être appelée sur une ligne déjà résolue (§12 : une
    prédiction historique n'est jamais modifiée après résolution) — lève
    RuntimeError dans ce cas plutôt que d'écraser silencieusement un
    résultat déjà écrit.
    """
    if row.status == "resolved":
        raise RuntimeError(
            f"ModelPrediction #{row.id} déjà résolue "
            f"(résultat={row.result_home_goals}-{row.result_away_goals}) — "
            "une prédiction historique n'est jamais modifiée après résolution."
        )

    correct_1x2, correct_btts, correct_over_2_5 = compute_correctness(
        row.pick_1x2, row.pick_btts, row.pick_over_2_5, home_goals, away_goals,
    )
    row.result_home_goals = home_goals
    row.result_away_goals = away_goals
    row.result_fetched_at = datetime.now(timezone.utc)
    row.correct_1x2 = correct_1x2
    row.correct_btts = correct_btts
    row.correct_over_2_5 = correct_over_2_5
    row.status = "resolved"
