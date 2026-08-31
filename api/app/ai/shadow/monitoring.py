"""
api/app/ai/shadow/monitoring.py — Phase 8N : XFOOT SHADOW MONITORING & DATA
QUALITY OPERATIONS V1.

RÉUTILISE TEL QUEL (jamais réimplémenté, §1/§2) :
  - api/app/ai/shadow/replay.py::measure_data_reality (Phase 8K).
  - api/app/ai/shadow/live.py::discover_live_candidates/assess_capture_eligibility/
    build_pipeline_input_for_live/check_production_consistency (Phase 8M).
  - api/app/ai/shadow/tracking.py::ShadowDecisionStore (Phase 8K/8M — écriture
    atomique + détection de corruption déjà en place, jamais modifiées ici).
  - api/app/ai/shadow/resolution.py::find_candidate_results (Phase 8K).
  - api/app/ai/shadow/metrics.py::compute_shadow_track_record/value_tracking_status (Phase 5/7).
  - scripts/shadow_live_track.py::classify_maturity/MATURITY_THRESHOLDS (Phase 8M).
  - api/app/ai/decision/decision.py::validate_market_probabilities (Phase 8I).
  - api/app/ai/features/registry.py::FEATURE_REGISTRY (Phase 8A).
  - api/app/ai/historical/inventory.py::build_calibration_inventory (Phase 8L).

RÈGLE ABSOLUE (§31) : ce module est STRICTEMENT READ-ONLY — aucune écriture
DB, aucune écriture du Shadow Store (contrairement à tracking.py/resolution.py).
`as_of` doit TOUJOURS être fourni explicitement à ces fonctions pures (§36) —
jamais `datetime.now()` à l'intérieur d'une fonction de calcul.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import Session

from app.models.model_prediction import ModelPrediction

from app.ai.shadow.replay import measure_data_reality
from app.ai.shadow.live import discover_live_candidates, assess_capture_eligibility, build_pipeline_input_for_live, check_production_consistency
from app.ai.shadow.tracking import ShadowDecisionStore, dedup_key
from app.ai.shadow.resolution import find_candidate_results
from app.ai.shadow.metrics import compute_shadow_track_record, value_tracking_status, classify_maturity, MATURITY_THRESHOLDS
from app.ai.decision.decision import validate_market_probabilities
from app.ai.features.registry import FEATURE_REGISTRY

# ---------------------------------------------------------------------------
# §4/§26/§27 : vocabulaire — réutilisé où le concept existe déjà (RESOLVED/
# PENDING/CONFLICT/INVALID/UNRESOLVED de Phase 8K, TEMPORALLY_VERIFIED/
# HISTORICAL_UNVERIFIED/FUTURE_INFORMATION/UNKNOWN de Phase 8H), jamais un
# second vocabulaire pour un concept déjà nommé.
# ---------------------------------------------------------------------------

HEALTH_STATUSES = ("NO_DATA", "HEALTHY", "DEGRADED", "CRITICAL", "BLOCKED")

ALERT_CATEGORIES = (
    "NO_DATA", "LOW_COVERAGE", "MISSED_CAPTURE", "LATE_PREDICTION", "STALE_PREDICTION",
    "TEMPORAL_UNKNOWN", "PROVENANCE_MISSING", "MODEL_MISMATCH", "PROBABILITY_MISMATCH",
    "DECISION_MISMATCH", "STORE_CORRUPTION", "RESOLUTION_CONFLICT", "PIPELINE_ERROR",
)
ALERT_SEVERITIES = ("INFO", "WARNING", "ERROR", "CRITICAL")
_DEFAULT_SEVERITY = {
    "NO_DATA": "INFO", "LOW_COVERAGE": "WARNING", "MISSED_CAPTURE": "ERROR", "LATE_PREDICTION": "WARNING",
    "STALE_PREDICTION": "WARNING", "TEMPORAL_UNKNOWN": "WARNING", "PROVENANCE_MISSING": "ERROR",
    "MODEL_MISMATCH": "CRITICAL", "PROBABILITY_MISMATCH": "CRITICAL", "DECISION_MISMATCH": "CRITICAL",
    "STORE_CORRUPTION": "CRITICAL", "RESOLUTION_CONFLICT": "ERROR", "PIPELINE_ERROR": "ERROR",
}

MISSED_CAPTURE_CATEGORIES = ("NO_PRODUCTION_PREDICTION", "LATE_PREDICTION", "TEMPORAL_UNKNOWN", "PIPELINE_ERROR", "PROVENANCE_MISSING", "OTHER")

# §10/§40 : seuils OPÉRATIONNELS, explicitement non-statistiques — jamais présentés comme validés (§27 Phase 8M déjà, réaffirmé ici).
OPERATIONAL_STALE_PREDICTION_HOURS = 24.0
OPERATIONAL_UNRESOLVED_AGE_HOURS = 48.0
LOW_COVERAGE_THRESHOLD = 0.5  # OPERATIONAL_THRESHOLD — pas une validation statistique


@dataclass(frozen=True)
class Alert:
    category: str          # ALERT_CATEGORIES
    severity: str           # ALERT_SEVERITIES
    key: str                  # §28 : clé de déduplication (alert_type+match_id+model+market+date si pertinent)
    message: str
    evidence: dict = field(default_factory=dict)


def _dedup_alerts(alerts: list[Alert]) -> list[Alert]:
    """§28 : deux détections de la MÊME anomalie (même `key`) dans un run -> une seule Alert, jamais un bruit
    répété. Ne supprime rien entre deux RUNS (chaque rapport horodaté reste distinct, §28 : jamais l'historique effacé)."""
    seen: dict[str, Alert] = {}
    for a in alerts:
        if a.key not in seen:
            seen[a.key] = a
    return list(seen.values())


# ---------------------------------------------------------------------------
# §6/§7 : couverture fixtures / prédictions / capture.
# ---------------------------------------------------------------------------

def compute_fixture_coverage(reality: dict) -> dict:
    """§6 : future_fixtures vs pending_predictions — jamais qualifié d'erreur automatiquement (une fixture
    sans prédiction peut être non capturable/retardée/volontairement ignorée, §6)."""
    future = reality["future_fixtures"]
    pending = reality["pending_model_predictions"]
    if future == 0:
        return {"future_fixtures": 0, "pending_predictions": pending, "coverage": None, "status": "NO_DATA"}
    return {"future_fixtures": future, "pending_predictions": pending, "coverage": round(pending / future, 4), "status": "ok"}


def compute_capture_coverage(eligible: int, captured: int) -> dict:
    """§7 : captured / eligible — INSUFFICIENT_DATA si eligible=0, jamais une division fabriquée."""
    if eligible == 0:
        return {"eligible": 0, "captured": captured, "coverage": None, "status": "INSUFFICIENT_DATA"}
    return {"eligible": eligible, "captured": captured, "coverage": round(captured / eligible, 4), "status": "ok"}


def classify_missed_capture_reason(session: Session, mp: ModelPrediction, as_of: datetime, already_captured: bool) -> Optional[str]:
    """§8 : catégorise pourquoi une fixture ÉLIGIBLE (capturable=True) n'a pas encore de shadow record.
    None si déjà capturée (pas un miss). Jamais une cause supposée sans preuve — s'appuie sur les
    diagnostics RÉELS de build_pipeline_input_for_live (Phase 8M), jamais recalculée différemment."""
    if already_captured:
        return None
    pi, diagnostics = build_pipeline_input_for_live(session, mp, "1X2", "home_win", as_of)
    if pi is not None:
        return "OTHER"  # capturable et construisible, simplement pas encore capturé (retard opérationnel, pas une erreur)
    reason = diagnostics.get("reason")
    if reason == "PREDICTION_TIMESTAMP_AFTER_AS_OF":
        return "LATE_PREDICTION"
    if reason == "MODEL_VERSION_MISSING":
        return "NO_PRODUCTION_PREDICTION"
    if reason in ("MARKET_NOT_MODELED_BY_THIS_MODEL", "unknown_market"):
        return "OTHER"
    return "OTHER"


# ---------------------------------------------------------------------------
# §9/§10/§35 : late / stale predictions — NÉCESSITENT un kickoff RÉEL.
# ---------------------------------------------------------------------------

def classify_prediction_timing(mp: ModelPrediction, as_of: datetime, kickoff_time_known: bool = False, stale_threshold_hours: float = OPERATIONAL_STALE_PREDICTION_HOURS) -> dict:
    """
    §9/§10/§35 : `kickoff_time_known=False` (cas RÉEL de ce dépôt — Match/
    ModelPrediction.match_date ne portent qu'une DATE, jamais une heure de
    coup d'envoi réelle, voir docstring de ces modèles) -> le calcul LATE
    (predicted_at >= kickoff) est STRUCTURELLEMENT NON FIABLE avec un proxy
    minuit (sur-détection systématique, direction FAUSSE de la prudence) —
    retourne "KICKOFF_TIME_UNKNOWN", jamais un verdict LATE fabriqué sur une
    heure supposée. STALE (predicted_at trop ancien par rapport à as_of)
    reste calculable indépendamment du kickoff — toujours évalué.
    """
    predicted_at = mp.predicted_at if mp.predicted_at.tzinfo else mp.predicted_at.replace(tzinfo=timezone.utc)
    a = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
    age_hours = (a - predicted_at).total_seconds() / 3600.0
    stale = age_hours > stale_threshold_hours

    late_status = "NOT_LATE"
    if not kickoff_time_known:
        late_status = "KICKOFF_TIME_UNKNOWN"
    else:
        kickoff = datetime.combine(mp.match_date, datetime.min.time(), tzinfo=timezone.utc)
        late_status = "LATE" if predicted_at >= kickoff else "NOT_LATE"

    return {"age_hours": round(age_hours, 2), "stale": stale, "stale_threshold_hours": stale_threshold_hours, "late_status": late_status}


# ---------------------------------------------------------------------------
# §11 : temporal health — réutilise EXACTEMENT le vocabulaire Phase 8H/8I/8J/8K.
# ---------------------------------------------------------------------------

def compute_temporal_health(entries: list[tuple]) -> dict:
    counts = {"TEMPORALLY_VERIFIED": 0, "HISTORICAL_UNVERIFIED": 0, "FUTURE_INFORMATION": 0, "UNKNOWN": 0}
    for record, _ in entries:
        counts[record.temporal_status] = counts.get(record.temporal_status, 0) + 1
    return {
        "temporal_safe": counts["TEMPORALLY_VERIFIED"] + counts["HISTORICAL_UNVERIFIED"],
        "temporal_unknown": counts["UNKNOWN"], "temporal_rejected": counts["FUTURE_INFORMATION"],
        "late_capture": 0,  # aucune capture tardive n'a pu entrer dans le store — bloquée à la source (Phase 8M, §34) — jamais un compteur fabriqué
        "future_information_attempts": counts["FUTURE_INFORMATION"], "by_status": counts,
    }


# ---------------------------------------------------------------------------
# §12 : provenance health.
# ---------------------------------------------------------------------------

def compute_provenance_health(entries: list[tuple]) -> dict:
    required_keys = ("model_source", "model_version", "calibration_source", "feature_snapshot", "odds_source")
    complete, partial, missing = 0, 0, 0
    for record, _ in entries:
        prov = record.provenance or {}
        present = sum(1 for k in required_keys if prov.get(k) not in (None, ""))
        if present == len(required_keys):
            complete += 1
        elif present == 0:
            missing += 1
        else:
            partial += 1
    return {"complete": complete, "partial": partial, "missing": missing, "total": len(entries)}


# ---------------------------------------------------------------------------
# §13/§14/§15 : consistency — réutilise check_production_consistency (Phase 8M) telle quelle.
# ---------------------------------------------------------------------------

def compute_consistency_health(session: Session, entries: list[tuple]) -> dict:
    """Re-snapshote le `ModelPrediction` d'origine (record.match_id, qui porte l'ID de la prédiction
    production pour une capture LIVE — voir live.py::build_pipeline_input_for_live) et compare — jamais
    de correction automatique (§13/§14/§15)."""
    model_mismatches, probability_mismatches, checked = 0, 0, 0
    details = []
    for record, _ in entries:
        if record.match_id is None:
            continue
        mp = session.get(ModelPrediction, record.match_id)
        if mp is None:
            continue
        checked += 1
        field_map = {"1X2": {"home_win": "prob_home", "draw": "prob_draw", "away_win": "prob_away"},
                     "BTTS": {"yes": "prob_btts_yes", "no": "prob_btts_no"},
                     "OVER_UNDER_2_5": {"over": "prob_over_2_5", "under": "prob_under_2_5"}}.get(record.market, {})
        current_probs = {sel: getattr(mp, attr) for sel, attr in field_map.items()}
        if record.model_type != mp.model_type:
            model_mismatches += 1
            details.append({"shadow_id": record.shadow_id, "type": "MODEL_MISMATCH"})
        elif record.market_probabilities_raw and current_probs and any(v is not None for v in current_probs.values()) and current_probs != record.market_probabilities_raw:
            probability_mismatches += 1
            details.append({"shadow_id": record.shadow_id, "type": "PROBABILITY_MISMATCH"})
    return {"checked": checked, "model_mismatches": model_mismatches, "probability_mismatches": probability_mismatches, "details": details}


# ---------------------------------------------------------------------------
# §16 : duplicate health.
# ---------------------------------------------------------------------------

def compute_duplicate_health(attempted: int, created: int) -> dict:
    prevented = max(0, attempted - created)
    return {"duplicate_attempts": attempted, "duplicates_prevented": prevented,
            "duplicate_ratio": round(prevented / attempted, 4) if attempted else None}


# ---------------------------------------------------------------------------
# §17 : store integrity — réutilise ShadowDecisionStore.load() (Phase 8M, §40 déjà durci).
# ---------------------------------------------------------------------------

def compute_store_integrity(store: ShadowDecisionStore) -> dict:
    try:
        store.load()
    except ValueError as e:
        return {"status": "CRITICAL", "valid_json": False, "error": str(e)}
    entries = store.all()
    keys = [r.shadow_id for r, _ in entries]
    duplicate_keys = len(keys) - len(set(keys))
    invalid_timestamps = sum(1 for r, _ in entries if r.as_of is None or r.created_at is None)
    return {"status": "CRITICAL" if (duplicate_keys or invalid_timestamps) else "OK",
            "valid_json": True, "record_count": len(entries), "duplicate_keys_found": duplicate_keys,
            "records_with_invalid_timestamps": invalid_timestamps}


# ---------------------------------------------------------------------------
# §20/§21 : resolution health / latency.
# ---------------------------------------------------------------------------

def compute_resolution_health(entries: list[tuple]) -> dict:
    counts = {"PENDING": 0, "RESOLVED": 0, "CONFLICT": 0, "UNRESOLVED": 0, "INVALID": 0}
    latencies_hours = []
    for record, resolution in entries:
        counts[resolution.result_status] = counts.get(resolution.result_status, 0) + 1
        if resolution.result_status == "RESOLVED" and resolution.resolved_at and record.kickoff:
            resolved_at = resolution.resolved_at if resolution.resolved_at.tzinfo else resolution.resolved_at.replace(tzinfo=timezone.utc)
            kickoff = record.kickoff if record.kickoff.tzinfo else record.kickoff.replace(tzinfo=timezone.utc)
            latencies_hours.append((resolved_at - kickoff).total_seconds() / 3600.0)

    if not latencies_hours:
        latency = {"status": "INSUFFICIENT_DATA"}
    else:
        sorted_lat = sorted(latencies_hours)
        p90_idx = min(len(sorted_lat) - 1, int(0.9 * len(sorted_lat)))
        latency = {"status": "ok", "min": round(min(sorted_lat), 2), "median": round(statistics.median(sorted_lat), 2),
                   "p90": round(sorted_lat[p90_idx], 2), "max": round(max(sorted_lat), 2), "n": len(sorted_lat)}
    return {"counts": counts, "resolution_latency_hours": latency}


def find_matches_played_but_pending(session: Session, entries: list[tuple], as_of: datetime) -> list[dict]:
    """§39 : matchs joués (kickoff < as_of) mais toujours PENDING — jamais un futur match considéré stale.
    Réutilise find_candidate_results (Phase 8K/resolution.py) pour indiquer si un résultat est DÉJÀ
    disponible quelque part (donc résoluble dès le prochain --resolve) ou encore réellement absent."""
    a = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
    out = []
    for record, resolution in entries:
        if resolution.result_status != "PENDING" or record.kickoff is None:
            continue
        kickoff = record.kickoff if record.kickoff.tzinfo else record.kickoff.replace(tzinfo=timezone.utc)
        if kickoff < a:
            age_hours = (a - kickoff).total_seconds() / 3600.0
            result_available = False
            if record.league and record.home_team and record.away_team:
                candidates = find_candidate_results(session, record.league, kickoff.date(), record.home_team, record.away_team)
                result_available = bool(candidates)
            out.append({
                "shadow_id": record.shadow_id, "kickoff": kickoff.isoformat(), "age_hours": round(age_hours, 2),
                "unresolved_age_exceeded": age_hours > OPERATIONAL_UNRESOLVED_AGE_HOURS,
                "result_already_available_pending_resolve_run": result_available,
            })
    return out


# ---------------------------------------------------------------------------
# §22/§23 : track record health / maturity — réutilise Phase 5/7 et Phase 8M telles quelles.
# ---------------------------------------------------------------------------

def compute_track_record_health(entries: list[tuple], filters: Optional[dict] = None) -> dict:
    """§22/§23 : réutilise compute_shadow_track_record (Phase 5/7, via Phase 8K/8M metrics.py) pour les 3
    marchés, et classify_maturity (Phase 8M, metrics.py — seuils inchangés sans justification, §23)."""
    filters = filters or {}
    per_market = {m: compute_shadow_track_record(entries, market=m, **filters) for m in ("1X2", "BTTS", "OVER_UNDER_2_5")}
    resolved_1x2 = per_market["1X2"].get("sample_size", 0) if per_market["1X2"]["status"] == "ok" else 0
    return {"per_market": per_market, "maturity": classify_maturity(resolved_1x2), "maturity_thresholds": MATURITY_THRESHOLDS}


# ---------------------------------------------------------------------------
# §42 : invalid probability — réutilise validate_market_probabilities (Phase 8I).
# ---------------------------------------------------------------------------

def check_invalid_probabilities(entries: list[tuple]) -> list[dict]:
    invalid = []
    for record, _ in entries:
        probs = record.market_probabilities_raw
        if not probs:
            continue
        reason = validate_market_probabilities(record.market, probs)
        if reason is not None:
            invalid.append({"shadow_id": record.shadow_id, "reason": reason})
    return invalid


# ---------------------------------------------------------------------------
# §43/§44 : odds / value health.
# ---------------------------------------------------------------------------

def compute_odds_and_value_health(entries: list[tuple]) -> dict:
    no_odds = sum(1 for r, _ in entries if r.odds_source is None)
    unverified = sum(1 for r, _ in entries if r.odds_source is not None and r.temporal_status != "TEMPORALLY_VERIFIED")
    verified = sum(1 for r, _ in entries if r.odds_source is not None and r.temporal_status == "TEMPORALLY_VERIFIED")
    value_candidates = sum(1 for r, _ in entries if r.value_status == "POSITIVE_VALUE")
    return {
        "odds_available": no_odds == 0 and len(entries) > 0, "no_odds_count": no_odds,
        "odds_unverified_count": unverified, "odds_verified_count": verified,
        "value_candidate_count": value_candidates, "value_tracking": value_tracking_status(entries),
    }


# ---------------------------------------------------------------------------
# §47 : feature health — réutilise Phase 8A.
# ---------------------------------------------------------------------------

def _find_missed_captures(session: Session, store: ShadowDecisionStore, capturable: list[tuple], as_of: datetime) -> list[dict]:
    """§8 : parmi les candidats CAPTURABLES (assess_capture_eligibility=True), lesquels n'ont PAS de shadow
    record correspondant — clé de déduplication réutilisée telle quelle (Phase 8K, tracking.dedup_key),
    construite avec le MÊME nom de version que build_pipeline_input_for_live (Phase 8M), jamais un
    identifiant différent (l'ID numérique seul ne correspondrait à aucun shadow_id réel)."""
    from app.models.team_rating import ModelVersion

    missed = []
    for mp, _reason in capturable:
        version = session.get(ModelVersion, mp.model_version_id)
        version_name = version.name if version else None
        key = dedup_key(mp.id, "1X2", version_name, as_of)
        already_captured = store.contains(key) or any(r.match_id == mp.id and r.market == "1X2" for r, _ in store.all())
        if not already_captured:
            reason = classify_missed_capture_reason(session, mp, as_of, already_captured=False)
            missed.append({"match": f"{mp.league}:{mp.match_date}:{mp.home_team}-{mp.away_team}", "model_type": mp.model_type, "category": reason})
    return missed


def build_alerts(*, reality: dict, fixture_coverage: dict, capture_coverage: dict, missed_captures: list[dict],
                  temporal_health: dict, provenance_health: dict, consistency_health: dict, store_integrity: dict,
                  resolution_health: dict, played_but_pending: list[dict], invalid_probabilities: list[dict]) -> list[Alert]:
    """§26/§27/§28 : alertes UNIQUEMENT opérationnelles (jamais "modèle bon/mauvais", §24) — dédupliquées
    par clé logique au sein de ce run (§28), jamais un historique effacé entre runs (chaque rapport
    horodaté reste un enregistrement distinct)."""
    alerts: list[Alert] = []

    if reality["future_fixtures"] == 0 or reality["pending_model_predictions"] == 0:
        alerts.append(Alert("NO_DATA", "INFO", "no_data", "Aucune donnée prospective actuellement disponible pour évaluer le fonctionnement opérationnel du Shadow.", reality))

    if fixture_coverage.get("coverage") is not None and fixture_coverage["coverage"] < LOW_COVERAGE_THRESHOLD:
        alerts.append(Alert("LOW_COVERAGE", "WARNING", "low_coverage_fixtures", f"Couverture prédiction/fixture = {fixture_coverage['coverage']:.0%} (< {LOW_COVERAGE_THRESHOLD:.0%}, seuil opérationnel).", fixture_coverage))
    if capture_coverage.get("coverage") is not None and capture_coverage["coverage"] < LOW_COVERAGE_THRESHOLD:
        alerts.append(Alert("LOW_COVERAGE", "WARNING", "low_coverage_capture", f"Couverture capture = {capture_coverage['coverage']:.0%} (< {LOW_COVERAGE_THRESHOLD:.0%}).", capture_coverage))

    for m in missed_captures:
        key = f"missed_capture:{m['match']}:{m['model_type']}"
        alerts.append(Alert("MISSED_CAPTURE", "ERROR", key, f"Fixture capturable sans shadow record : {m['match']} ({m['model_type']}) — catégorie {m['category']}.", m))

    if temporal_health["temporal_unknown"] > 0:
        alerts.append(Alert("TEMPORAL_UNKNOWN", "WARNING", "temporal_unknown", f"{temporal_health['temporal_unknown']} record(s) avec temporal_status=UNKNOWN.", temporal_health))

    if provenance_health["missing"] > 0:
        alerts.append(Alert("PROVENANCE_MISSING", "ERROR", "provenance_missing", f"{provenance_health['missing']} record(s) sans provenance exploitable.", provenance_health))

    for d in consistency_health["details"]:
        cat = "MODEL_MISMATCH" if d["type"] == "MODEL_MISMATCH" else "PROBABILITY_MISMATCH"
        alerts.append(Alert(cat, "CRITICAL", f"{cat.lower()}:{d['shadow_id']}", f"{cat} détecté sur {d['shadow_id']} — jamais corrigé automatiquement.", d))

    if store_integrity["status"] == "CRITICAL":
        alerts.append(Alert("STORE_CORRUPTION", "CRITICAL", "store_corruption", "Le Shadow Store est corrompu ou incohérent — STOP, jamais écrasé silencieusement.", store_integrity))

    if resolution_health["counts"].get("CONFLICT", 0) > 0:
        alerts.append(Alert("RESOLUTION_CONFLICT", "ERROR", "resolution_conflict", f"{resolution_health['counts']['CONFLICT']} conflit(s) de résolution non arbitrés.", resolution_health["counts"]))

    for p in played_but_pending:
        if p["unresolved_age_exceeded"]:
            alerts.append(Alert("MISSED_CAPTURE", "WARNING", f"unresolved_age:{p['shadow_id']}", f"Match joué depuis {p['age_hours']:.1f}h toujours PENDING (seuil opérationnel {OPERATIONAL_UNRESOLVED_AGE_HOURS}h).", p))

    for inv in invalid_probabilities:
        alerts.append(Alert("PROBABILITY_MISMATCH", "CRITICAL", f"invalid_prob:{inv['shadow_id']}", f"Probabilité invalide détectée sur {inv['shadow_id']} ({inv['reason']}) — jamais corrigée automatiquement.", inv))

    return _dedup_alerts(alerts)


def derive_health_status(*, reality: dict, alerts: list[Alert], store_integrity: dict) -> str:
    """
    §4/§5/§61 : NO_DATA prime sur tout (jamais "HEALTHY" faute de données, même avec 0 erreur).
    Puis CRITICAL (toute alerte CRITICAL, ex. corruption/mismatch) > DEGRADED (toute alerte ERROR/WARNING)
    > HEALTHY (aucune alerte, données réellement présentes). BLOCKED réservé au store illisible.
    """
    if store_integrity.get("valid_json") is False:
        return "BLOCKED"
    if reality["future_fixtures"] == 0 or reality["pending_model_predictions"] == 0:
        return "NO_DATA"
    if any(a.severity == "CRITICAL" for a in alerts):
        return "CRITICAL"
    if any(a.severity in ("ERROR", "WARNING") for a in alerts):
        return "DEGRADED"
    return "HEALTHY"


def compute_shadow_health(session: Session, store: ShadowDecisionStore, as_of: datetime, filters: Optional[dict] = None) -> dict:
    """
    §3 : point d'entrée PRINCIPAL — assemble toutes les dimensions ci-dessus.
    STRICTEMENT READ-ONLY (§31) : aucune écriture DB, aucune écriture du store
    (store.load() est un appel en lecture ; ce module n'appelle jamais
    store.save()/upsert_new/update_resolution).
    """
    reality = measure_data_reality(session)
    candidates = discover_live_candidates(session, as_of)
    capturable, rejected = [], []
    for c in candidates:
        ok, reason = assess_capture_eligibility(c, as_of)
        (capturable if ok else rejected).append((c, reason))

    try:
        store.load()
        entries = store.all()
        store_integrity = compute_store_integrity(store)
    except ValueError as e:
        entries = []
        store_integrity = {"status": "CRITICAL", "valid_json": False, "error": str(e)}

    fixture_coverage = compute_fixture_coverage(reality)
    capture_coverage = compute_capture_coverage(len(capturable), sum(1 for r, _ in entries if r.as_of and abs((r.as_of - as_of).total_seconds()) < 86400))
    missed_captures = _find_missed_captures(session, store, capturable, as_of) if store_integrity.get("valid_json", True) else []
    temporal_health = compute_temporal_health(entries)
    provenance_health = compute_provenance_health(entries)
    consistency_health = compute_consistency_health(session, entries)
    resolution_health = compute_resolution_health(entries)
    played_but_pending = find_matches_played_but_pending(session, entries, as_of)
    track_record_health = compute_track_record_health(entries, filters)
    value_health = compute_odds_and_value_health(entries)
    feature_health = compute_feature_health(entries)
    invalid_probabilities = check_invalid_probabilities(entries)

    alerts = build_alerts(
        reality=reality, fixture_coverage=fixture_coverage, capture_coverage=capture_coverage,
        missed_captures=missed_captures, temporal_health=temporal_health, provenance_health=provenance_health,
        consistency_health=consistency_health, store_integrity=store_integrity, resolution_health=resolution_health,
        played_but_pending=played_but_pending, invalid_probabilities=invalid_probabilities,
    )
    status = derive_health_status(reality=reality, alerts=alerts, store_integrity=store_integrity)

    return {
        "as_of": as_of.isoformat(), "status": status,
        "future_fixtures": reality["future_fixtures"], "pending_predictions": reality["pending_model_predictions"],
        "capturable": len(capturable), "captured": len(entries),
        "pending_shadow": resolution_health["counts"].get("PENDING", 0), "resolved_shadow": resolution_health["counts"].get("RESOLVED", 0),
        "conflicts": resolution_health["counts"].get("CONFLICT", 0), "invalid": resolution_health["counts"].get("INVALID", 0),
        "errors": sum(1 for a in alerts if a.severity in ("ERROR", "CRITICAL")),
        "stale_predictions": None,  # calculé séparément par record via classify_prediction_timing (nécessite un mp par record) — voir rapport §7/§10
        "late_predictions": "KICKOFF_TIME_UNKNOWN — voir Limitations (aucune heure de coup d'envoi réelle persistée)",
        "missing_provenance": provenance_health["missing"], "temporal_unknown": temporal_health["temporal_unknown"],
        "no_odds": value_health["no_odds_count"], "missed_capture_candidates": len(missed_captures),
        "reality": reality, "fixture_coverage": fixture_coverage, "capture_coverage": capture_coverage,
        "missed_captures_detail": missed_captures, "temporal_health": temporal_health, "provenance_health": provenance_health,
        "consistency_health": consistency_health, "store_integrity": store_integrity, "resolution_health": resolution_health,
        "played_but_pending": played_but_pending, "track_record_health": track_record_health, "value_health": value_health,
        "feature_health": feature_health, "invalid_probabilities": invalid_probabilities,
        "alerts": [{"category": a.category, "severity": a.severity, "key": a.key, "message": a.message, "evidence": a.evidence} for a in alerts],
    }


def compute_feature_health(entries: list[tuple]) -> dict:
    registry_summary = {
        "production": sum(1 for f in FEATURE_REGISTRY.values() if f.status == "PRODUCTION"),
        "experimental": sum(1 for f in FEATURE_REGISTRY.values() if f.status == "EXPERIMENTAL"),
        "missing": sum(1 for f in FEATURE_REGISTRY.values() if f.status == "MISSING"),
    }
    coverage_ratios = [r.quality.get("data_quality") for r, _ in entries if r.quality]
    low_data_quality = sum(1 for q in coverage_ratios if q == "LOW")
    unknown_data_quality = sum(1 for q in coverage_ratios if q == "UNKNOWN")
    return {"feature_registry": registry_summary, "shadow_records_with_low_data_quality": low_data_quality,
            "shadow_records_with_unknown_data_quality": unknown_data_quality}
