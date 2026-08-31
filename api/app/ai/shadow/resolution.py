"""
api/app/ai/shadow/resolution.py — Phase 8K : résolution des Shadow Decision
Records (§12-§14, §36/§37 du prompt).

Réutilise l'ORDRE de résolution de Phase 7 (scripts/model_selection_shadow.py
::_find_result_for_shadow_row : model_predictions -> prediction_log ->
match) — mais l'ÉTEND pour détecter un DÉSACCORD entre sources (§13/§37),
ce que la fonction Phase 7 ne fait PAS (elle s'arrête à la première source
trouvée). Cette extension est nécessaire et documentée (§12 : "réutiliser
l'ordre de résolution Phase 7" — l'ORDRE est réutilisé, la détection de
conflit est un besoin réellement nouveau de cette phase).

RÈGLE ABSOLUE : lecture seule sur match/model_predictions/prediction_log —
aucune écriture ici. Ne modifie JAMAIS une ShadowResolution déjà non-PENDING.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Optional

from sqlmodel import Session, select

from app.models.match import Match
from app.models.model_prediction import ModelPrediction
from app.models.prediction_log import PredictionLog

from app.ai.arena import research  # réutilise research.actual_outcome (Phase 5.7), jamais réimplémenté
from app.ai.shadow.schemas import ShadowDecisionRecord, ShadowResolution

RESOLUTION_SOURCE_ORDER = ("model_predictions", "prediction_log", "match")  # §12 : même ordre que Phase 7


def find_candidate_results(session: Session, league: str, match_date: date, home_team: str, away_team: str) -> dict[str, tuple[int, int]]:
    """
    §12/§13/§37 : interroge les TROIS sources, dans l'ordre Phase 7, mais
    SANS s'arrêter à la première trouvée (contrairement à _find_result_for_
    shadow_row) — nécessaire pour détecter un désaccord entre sources
    (§37 : "Source A: HOME, Source B: AWAY -> Expected: CONFLICT").
    """
    results: dict[str, tuple[int, int]] = {}

    mp = session.exec(
        select(ModelPrediction).where(
            ModelPrediction.league == league, ModelPrediction.match_date == match_date,
            ModelPrediction.home_team == home_team, ModelPrediction.away_team == away_team,
            ModelPrediction.status == "resolved",
        )
    ).first()
    if mp is not None and mp.result_home_goals is not None and mp.result_away_goals is not None:
        results["model_predictions"] = (mp.result_home_goals, mp.result_away_goals)

    pl = session.exec(
        select(PredictionLog).where(
            PredictionLog.league == league, PredictionLog.match_date == match_date,
            PredictionLog.home_team == home_team, PredictionLog.away_team == away_team,
            PredictionLog.result_fetched_at.is_not(None),
        )
    ).first()
    if pl is not None and pl.result_home_goals is not None and pl.result_away_goals is not None:
        results["prediction_log"] = (pl.result_home_goals, pl.result_away_goals)

    m = session.exec(
        select(Match).where(
            Match.league == league, Match.date == datetime.combine(match_date, time.min),
            Match.home_team == home_team, Match.away_team == away_team,
        )
    ).first()
    if m is not None:
        results["match"] = (m.home_goals, m.away_goals)

    return results


def resolve_record(
    session: Session, record: ShadowDecisionRecord, current: ShadowResolution,
) -> ShadowResolution:
    """
    §12/§13/§14/§36/§37 : retourne une NOUVELLE ShadowResolution — n'écrit
    JAMAIS en base, ne mute JAMAIS `current` en place.

    - `current.result_status != "PENDING"` -> retournée TELLE QUELLE, sans
      aucune requête (§12 : "ne jamais modifier une décision déjà résolue").
    - Aucune source n'a de résultat -> reste PENDING.
    - Une seule valeur distincte parmi les sources trouvées -> RESOLVED.
    - Au moins deux sources EN DÉSACCORD -> CONFLICT, `conflict_sources`
      liste TOUTES les valeurs trouvées (§13 : "ne pas choisir arbitrairement").
    - `record.league`/`home_team`/`away_team`/`kickoff` manquants (identité
      insuffisante pour chercher) -> UNRESOLVED (jamais une recherche
      approximative).
    """
    if current.result_status != "PENDING":
        return current

    if not record.league or not record.home_team or not record.away_team or record.kickoff is None:
        return ShadowResolution(result_status="UNRESOLVED")

    match_date = record.kickoff.date()
    candidates = find_candidate_results(session, record.league, match_date, record.home_team, record.away_team)

    if not candidates:
        return current  # toujours PENDING — aucun résultat disponible pour l'instant

    distinct_values = set(candidates.values())
    if len(distinct_values) > 1:
        return ShadowResolution(
            result_status="CONFLICT",
            conflict_sources={src: list(v) for src, v in candidates.items()},
            resolved_at=datetime.now(timezone.utc),
        )

    hg, ag = next(iter(distinct_values))
    try:
        actual = research.actual_outcome(record.market, hg, ag)
    except ValueError:
        return ShadowResolution(result_status="INVALID", resolved_at=datetime.now(timezone.utc))

    candidate_correct = None
    if record.selection:
        candidate_correct = (record.selection == actual)

    return ShadowResolution(
        result_status="RESOLVED", actual_home_goals=hg, actual_away_goals=ag,
        actual_outcome=actual, candidate_correct=candidate_correct,
        resolved_at=datetime.now(timezone.utc),
    )
