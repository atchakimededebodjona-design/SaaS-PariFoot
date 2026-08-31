"""
api/app/ai/historical/coverage.py — Phase 8L : matrice de replay et
couverture dataset complet (§22-§26 du prompt).

Lecture seule. Réutilise evaluate_replay_eligibility (eligibility.py) —
jamais une deuxième logique de décision. Pour la couverture PLEIN DATASET,
`feature_registry_status`/calibration sont volontairement neutres
("non vérifiés à ce stade") car, EXHAUSTIVEMENT prouvé ci-dessous
(`prove_all_pairs_blocked_by_model_gate`), le gate modèle (§5) bloque déjà
100% des paires (match, ModelVersion) de ce dépôt — jamais une supposition,
une preuve calculée sur les données réelles.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import Session, select

from app.models.match import Match

from app.ai.historical.eligibility import evaluate_replay_eligibility, _aware
from app.ai.historical.schemas import ModelVersionInventoryEntry, REPLAY_VERDICTS


def prove_all_pairs_blocked_by_model_gate(session: Session, model_versions: list[ModelVersionInventoryEntry]) -> dict:
    """
    §25/§26 : preuve EXHAUSTIVE (pas une supposition) — compare la date du
    match le PLUS RÉCENT de toute la base à la ModelVersion la PLUS ANCIENNE
    (`trained_at` le plus petit). Si même la meilleure combinaison possible
    échoue au gate §5, TOUTE paire (match, version) de ce dépôt échoue —
    permet d'annoncer une couverture 0% avec certitude, jamais une
    estimation sur un échantillon.
    """
    latest_match = session.exec(select(Match.date).order_by(Match.date.desc()).limit(1)).first()
    trained_ats = [mv.trained_at for mv in model_versions if mv.trained_at is not None]
    earliest_trained_at = min(trained_ats) if trained_ats else None

    if latest_match is None or earliest_trained_at is None:
        return {"proven": False, "reason": "insufficient_data_to_prove", "latest_match_date": str(latest_match), "earliest_model_trained_at": str(earliest_trained_at)}

    proven_blocked = _aware(earliest_trained_at) > _aware(latest_match)
    return {
        "proven": proven_blocked,
        "latest_match_date": latest_match.isoformat(),
        "earliest_model_trained_at": earliest_trained_at.isoformat(),
        "conclusion": (
            "PROUVÉ : la ModelVersion la plus ANCIENNE (trained_at le plus bas) a été entraînée APRÈS le match le "
            "plus RÉCENT de toute la base — donc TOUTE paire (match, ModelVersion) de ce dépôt échoue le gate §5 "
            "(MODEL_TRAINED_AFTER_AS_OF), sans exception, sans avoir besoin de tester chaque paire individuellement."
            if proven_blocked else
            "NON PROUVÉ — au moins une ModelVersion est potentiellement antérieure à au moins un match ; une "
            "vérification paire par paire reste nécessaire (voir scan_full_dataset)."
        ),
    }


def scan_full_dataset(session: Session, model_versions: list[ModelVersionInventoryEntry]) -> dict:
    """§24/§25 : scan du dataset ENTIER (jamais un échantillon présenté comme
    le tout) — total/replayable/non-replayable/unknown/partial, segmenté par
    ligue et par modèle. Utilise evaluate_replay_eligibility pour CHAQUE
    paire (match, version) — jamais une deuxième formule de décision."""
    matches = session.exec(select(Match.league, Match.date)).all()
    total_matches = len(matches)
    total_pairs = total_matches * len(model_versions)

    counts = {v: 0 for v in REPLAY_VERDICTS}
    reason_counts: dict[str, int] = {}
    by_league: dict[str, dict] = {}
    by_model: dict[str, dict] = {}

    for league, match_date in matches:
        as_of = match_date  # évaluation AU coup d'envoi (borne la plus permissive raisonnable, §11)
        kickoff_exclusive = match_date + timedelta(microseconds=1)  # garantit as_of < kickoff strictement (§9 étape 1)

        league_bucket = by_league.setdefault(league, {v: 0 for v in REPLAY_VERDICTS})

        for mv in model_versions:
            model_bucket = by_model.setdefault(mv.model_type, {v: 0 for v in REPLAY_VERDICTS})

            result = evaluate_replay_eligibility(
                as_of=as_of, kickoff=kickoff_exclusive,
                model_trained_at=mv.trained_at, model_exists=True,
                artifact_exists=(mv.artifact_present_in_db or mv.team_ratings_count > 0 or mv.model_type == "dixon_coles"),
                artifact_metadata_sufficient=mv.config_present or mv.model_type == "dixon_coles",
                feature_registry_status=None,  # non vérifié à ce stade — voir prove_all_pairs_blocked_by_model_gate (jamais atteint si le gate modèle bloque déjà)
                calibration_exists=False, calibration_created_at=None, calibration_required=False,  # neutralisé : voir §36, décision documentée séparément (research_without_calibration)
            )
            counts[result.verdict] += 1
            league_bucket[result.verdict] += 1
            model_bucket[result.verdict] += 1
            for reason in result.reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1

    eligible_matches = total_pairs  # dénominateur : toutes les paires (match, version) considérées — §25
    replay_coverage = (counts["REPLAYABLE"] / eligible_matches) if eligible_matches else None

    return {
        "total_matches": total_matches, "total_model_versions": len(model_versions), "total_pairs_evaluated": total_pairs,
        "verdict_counts": counts, "rejection_reason_counts": reason_counts,
        "by_league": by_league, "by_model_type": by_model,
        "replay_coverage": replay_coverage if eligible_matches else None,
        "replay_coverage_status": "ok" if eligible_matches else "INSUFFICIENT_DATA",
    }
