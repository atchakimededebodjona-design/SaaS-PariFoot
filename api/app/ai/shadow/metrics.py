"""
api/app/ai/shadow/metrics.py — Phase 8K : Track Record shadow (§15-§19 du
prompt).

RÉUTILISE TEL QUEL (jamais réimplémenté) :
  - api/app/ai/arena/service.py::_compute_market_metrics (Phase 5) —
    accuracy/log_loss/brier_score, calculés à partir des observations
    INDIVIDUELLES, jamais une moyenne de moyennes (§16).
  - api/app/ai/arena/research.py::wilson_interval (Phase 5.7).

Ne calcule JAMAIS sur des observations PENDING/CONFLICT/UNRESOLVED/INVALID
(§15 : "les observations PENDING ne doivent jamais entrer dans accuracy/
log-loss/Brier/ROI/EV realization").
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from app.ai.arena.research import wilson_interval
from app.ai.arena.service import _compute_market_metrics

from app.ai.shadow.schemas import ShadowDecisionRecord, ShadowResolution


def _observation(record: ShadowDecisionRecord, resolution: ShadowResolution) -> Optional[dict]:
    """§15 : forme {"p_true","probs","actual","correct"} — même forme que
    partout dans l'Arena (jamais inventée). None si non RESOLVED, ou si la
    distribution complète du marché n'a jamais été capturée (record ancien/
    incomplet — jamais une probabilité fabriquée pour combler)."""
    if resolution.result_status != "RESOLVED" or resolution.actual_outcome is None:
        return None
    probs = record.market_probabilities_calibrated if record.probability_source == "CALIBRATED" else record.market_probabilities_raw
    if not probs or resolution.actual_outcome not in probs:
        return None
    pick = max(probs, key=probs.get)
    return {"p_true": probs[resolution.actual_outcome], "probs": probs, "actual": resolution.actual_outcome, "correct": pick == resolution.actual_outcome}


def compute_shadow_track_record(
    entries: list[tuple[ShadowDecisionRecord, ShadowResolution]], market: str, *,
    league: Optional[str] = None, model_type: Optional[str] = None,
    since: Optional[date] = None, until: Optional[date] = None, last_n: Optional[int] = None,
) -> dict:
    """
    §15/§16/§17/§39/§40/§41/§42 : filtre puis calcule — RESOLVED uniquement,
    jamais une moyenne de moyennes (délègue entièrement à
    service._compute_market_metrics sur la liste d'observations
    individuelles). Filtres : league/model/window (last_n/since/until) —
    aucune observation hors fenêtre n'est incluse (§42).
    """
    filtered = [
        (r, res) for r, res in entries
        if r.market == market
        and (league is None or r.league == league)
        and (model_type is None or r.model_type == model_type)
        and (since is None or (r.kickoff is not None and r.kickoff.date() >= since))
        and (until is None or (r.kickoff is not None and r.kickoff.date() <= until))
    ]
    filtered.sort(key=lambda pair: (pair[0].kickoff or pair[0].as_of, pair[0].shadow_id))  # ordre déterministe (§43), jamais l'ordre d'un set/dict
    if last_n is not None:
        filtered = filtered[-last_n:]

    resolved_only = [(r, res) for r, res in filtered if res.result_status == "RESOLVED"]
    pending_count = sum(1 for _, res in filtered if res.result_status == "PENDING")
    conflict_count = sum(1 for _, res in filtered if res.result_status == "CONFLICT")
    unresolved_count = sum(1 for _, res in filtered if res.result_status == "UNRESOLVED")
    invalid_count = sum(1 for _, res in filtered if res.result_status == "INVALID")

    if not filtered:
        return {"status": "INSUFFICIENT_DATA", "market": market, "sample_size": 0, "reason": "no_shadow_data"}

    observations = [o for o in (_observation(r, res) for r, res in resolved_only) if o is not None]
    if not observations:
        return {
            "status": "INSUFFICIENT_DATA", "market": market, "sample_size": 0,
            "pending": pending_count, "conflict": conflict_count, "unresolved": unresolved_count, "invalid": invalid_count,
            "reason": "no_resolved_observation_with_full_market_probabilities",
        }

    metrics = _compute_market_metrics(observations)
    correct = sum(1 for o in observations if o["correct"])
    ci_low, ci_high = wilson_interval(correct, metrics.sample_size)

    return {
        "status": "ok", "market": market, "league": league, "model_type": model_type,
        "sample_size": metrics.sample_size, "accuracy": metrics.accuracy, "log_loss": metrics.log_loss,
        "brier_score": metrics.brier_score, "accuracy_ci_95": [ci_low, ci_high],
        "pending": pending_count, "conflict": conflict_count, "unresolved": unresolved_count, "invalid": invalid_count,
    }


MATURITY_STATES = ("NO_DATA", "EARLY_DATA", "TRACKING", "STATISTICALLY_INFORMATIVE")
# §49 (Phase 8M/8N) : seuils OPÉRATIONNELS, explicitement documentés comme tels — jamais une conclusion
# statistique inventée. Vivent ici (jamais dans scripts/) car api/app/ ne doit jamais dépendre de scripts/
# (voir docstring de api/app/ai/arena/research.py) — l'inverse (scripts -> app) est la seule direction admise.
MATURITY_THRESHOLDS = {"EARLY_DATA": 10, "TRACKING": 30, "STATISTICALLY_INFORMATIVE": 100}


def classify_maturity(sample_size: int) -> str:
    """§23/§49 : classification opérationnelle du volume de données RESOLVED — jamais une conclusion
    statistique forcée (ex. "modèle validé")."""
    if sample_size == 0:
        return "NO_DATA"
    if sample_size < MATURITY_THRESHOLDS["EARLY_DATA"]:
        return "EARLY_DATA"
    if sample_size < MATURITY_THRESHOLDS["STATISTICALLY_INFORMATIVE"]:
        return "TRACKING"
    return "STATISTICALLY_INFORMATIVE"


def value_tracking_status(entries: list[tuple[ShadowDecisionRecord, ShadowResolution]]) -> dict:
    """
    §18 : suivi de valeur (edge/EV/outcome réalisé) — UNIQUEMENT si une odds
    RÉELLEMENT temporellement vérifiée existe (temporal_status ==
    "TEMPORALLY_VERIFIED", jamais "HISTORICAL_UNVERIFIED" — §18 : "ne pas
    utiliser football-data.co.uk comme preuve temporelle"). Aucune source
    actuellement intégrée à Xfoot ne produit ce statut (The Odds API reste
    SUPPORT_REQUIRED, Phase 8G.2) -> retourne NOT_AVAILABLE, jamais fabriqué.
    """
    verified = [r for r, _ in entries if r.temporal_status == "TEMPORALLY_VERIFIED" and r.value_status is not None]
    if not verified:
        return {"status": "NOT_AVAILABLE", "reason": "Aucune odds TEMPORALLY_VERIFIED disponible (The Odds API SUPPORT_REQUIRED, Phase 8G.2) — jamais football-data.co.uk utilisé comme preuve temporelle (§18)."}
    return {"status": "ok", "n_verified": len(verified)}
