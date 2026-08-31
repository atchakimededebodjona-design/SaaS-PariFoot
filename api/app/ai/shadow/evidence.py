"""
api/app/ai/shadow/evidence.py — Phase 9.3 : XFOOT SHADOW EVIDENCE
ACCUMULATION & ACTIVATION GATE REASSESSMENT V1.

RÉUTILISE TEL QUEL (jamais réimplémenté, §1 : "ne pas dupliquer") :
  - api/app/ai/shadow/prospective.py::is_real_prospective/compute_evidence_ledger/
    classify_capture_quality (Phase 9.2).
  - api/app/ai/shadow/metrics.py::compute_shadow_track_record/classify_maturity/
    value_tracking_status (Phase 8K/8M/8N).
  - api/app/ai/shadow/monitoring.py::compute_provenance_health/compute_consistency_health/
    compute_shadow_health (Phase 8N).
  - api/app/ai/shadow/replay.py::measure_data_reality (Phase 8K).
  - api/app/ai/arena/track_record.py::compare_production_vs_shadow (Phase 7 —
    UN AUTRE mécanisme shadow, `shadow_selection_predictions`, distinct de
    ShadowDecisionStore — jamais confondu, voir §2 de la docstring ci-dessous).
  - api/app/ai/readiness/matrix.py::evaluate_production_readiness (Phase 9).
  - api/app/ai/safety/kill_switch.py::assert_production_allowed (Phase 9.1 —
    LECTURE SEULE ici, jamais trigger()/reset()).

=== §12 : DEUX mécanismes "Shadow" distincts, jamais fusionnés ===

1. `ShadowDecisionStore` (Phase 8K-9.2) : fichier JSON, pipeline complet
   (quality/eligibility/value/provenance) capturé sur une prédiction déjà
   produite par la production — c'est la source de TOUTES les fonctions de
   ce module sauf `compare_production_vs_shadow`.
2. `shadow_selection_predictions` (Phase 6/7, table SQL) : candidate vs
   active du Model Selection Engine — alimentée par
   scripts/model_selection_shadow.py, complètement indépendante du store
   JSON. `compare_production_vs_shadow` (Phase 7) lit CETTE table, jamais
   le store — reportée séparément (§14 du rapport Phase 9.3), jamais
   présentée comme une preuve du même mécanisme.

STRICTEMENT LECTURE SEULE : aucune fonction ici n'écrit en DB, dans le
Shadow Store, ni ne trigger/reset le Kill Switch.
"""

from __future__ import annotations

from datetime import timezone

from sqlmodel import Session

from app.models.model_prediction import ModelPrediction

from app.ai.shadow.prospective import is_real_prospective
from app.ai.shadow.metrics import compute_shadow_track_record, classify_maturity
from app.ai.shadow.monitoring import compute_provenance_health, compute_consistency_health
from app.ai.shadow.replay import measure_data_reality

UTC = timezone.utc

# ---------------------------------------------------------------------------
# §3 : classification de la donnée — jamais une donnée synthétique/historique
# présentée comme prospective.
# ---------------------------------------------------------------------------

DATA_MARKING_CLASSES = ("REAL_PROSPECTIVE", "REAL_BUT_TEMPORAL_UNVERIFIED", "HISTORICAL", "SYNTHETIC")

# §5 : réutilise la sortie de compute_provenance_health (Phase 8N) — mappage direct,
# jamais un deuxième calcul de complétude de provenance.
PROVENANCE_STATES = ("PROVENANCE_COMPLETE", "PROVENANCE_INCOMPLETE", "PROVENANCE_UNKNOWN")


def classify_data_marking(session: Session, record) -> tuple[str, str]:
    """
    §3/§4 : (classe, raison). Ré-interroge `model_predictions` via
    `record.match_id` (même pattern que compute_consistency_health, Phase
    8N) pour obtenir `predicted_at` — jamais persisté dans
    ShadowDecisionRecord lui-même (Phase 8K/8M, voir schemas.py). Si la
    ligne production a disparu (jamais supprimée en pratique, mais fonction
    défensive), retombe sur UNKNOWN via is_real_prospective, jamais un
    REAL_PROSPECTIVE supposé.
    """
    if record.data_marking == "SYNTHETIC":
        return "SYNTHETIC", "data_marking=SYNTHETIC (test/recherche) — jamais compté dans un track record réel."

    predicted_at = None
    if record.match_id is not None:
        mp = session.get(ModelPrediction, record.match_id)
        if mp is not None:
            predicted_at = mp.predicted_at

    match_date = record.kickoff.date() if record.kickoff else None
    status, reason = is_real_prospective(
        capture_timestamp=record.created_at, match_date=match_date, prediction_timestamp=predicted_at, as_of=record.as_of,
    )
    if status == "CONSISTENT_WITH_PLACEHOLDER_KICKOFF":
        return "REAL_PROSPECTIVE", reason
    if status == "TIMING_VIOLATION":
        return "HISTORICAL", f"Ordre des timestamps incohérent avec une capture prospective — {reason} Jamais présentée comme prospective (§4)."
    return "REAL_BUT_TEMPORAL_UNVERIFIED", f"Donnée réelle (data_marking=REAL) mais timing non vérifiable — {reason}"


# ---------------------------------------------------------------------------
# §2 : Evidence Ledger étendu — dérivé EXCLUSIVEMENT des observations réelles.
# ---------------------------------------------------------------------------

def compute_full_evidence_ledger(session: Session, entries: list[tuple]) -> dict:
    marking_counts = {c: 0 for c in DATA_MARKING_CLASSES}
    marking_detail = []
    marking_by_shadow_id: dict[str, str] = {}
    for record, _ in entries:
        cls, reason = classify_data_marking(session, record)
        marking_counts[cls] += 1
        marking_detail.append({"shadow_id": record.shadow_id, "class": cls, "reason": reason})
        marking_by_shadow_id[record.shadow_id] = cls

    by_resolution = {}
    for _, res in entries:
        by_resolution[res.result_status] = by_resolution.get(res.result_status, 0) + 1

    provenance_health = compute_provenance_health(entries)
    consistency_health = compute_consistency_health(session, entries)

    kickoffs = [r.kickoff for r, _ in entries if r.kickoff is not None]
    model_versions = sorted({r.model_version for r, _ in entries if r.model_version})

    real_prospective_resolved = sum(
        1 for r, res in entries if res.result_status == "RESOLVED" and marking_by_shadow_id.get(r.shadow_id) == "REAL_PROSPECTIVE"
    )

    return {
        "total_observations": len(entries),
        "by_data_marking_class": marking_counts,
        "data_marking_detail": marking_detail,
        "by_resolution_status": by_resolution,
        "pending": by_resolution.get("PENDING", 0), "resolved": by_resolution.get("RESOLVED", 0),
        "conflicts": by_resolution.get("CONFLICT", 0), "unresolved": by_resolution.get("UNRESOLVED", 0),
        "invalid": by_resolution.get("INVALID", 0),
        "provenance_complete": provenance_health["complete"], "provenance_incomplete": provenance_health["partial"],
        "provenance_unknown": provenance_health["missing"],
        "temporal_invalid": marking_counts["HISTORICAL"],
        "model_mismatch": consistency_health["model_mismatches"], "probability_mismatch": consistency_health["probability_mismatches"],
        "decision_mismatch": 0,  # structurellement 0 : check_production_consistency (Phase 8M) rejette AVANT capture — jamais persisté.
        "distinct_fixtures": len({(r.league, r.kickoff, r.home_team, r.away_team) for r, _ in entries if r.league}),
        "distinct_leagues": sorted({r.league for r, _ in entries if r.league}),
        "distinct_markets": sorted({r.market for r, _ in entries if r.market}),
        "distinct_models": sorted({r.model_type for r, _ in entries if r.model_type}),
        "distinct_model_versions": model_versions,
        "first_observation": min((r.created_at for r, _ in entries), default=None),
        "latest_observation": max((r.created_at for r, _ in entries), default=None),
        "period_covered": {"earliest_kickoff": min(kickoffs).isoformat() if kickoffs else None, "latest_kickoff": max(kickoffs).isoformat() if kickoffs else None},
        "maturity_real_prospective_resolved": classify_maturity(real_prospective_resolved),
        "real_prospective_resolved_count": real_prospective_resolved,
    }


# ---------------------------------------------------------------------------
# §9 : model / version tracking.
# ---------------------------------------------------------------------------

def compute_model_version_tracking(entries: list[tuple]) -> dict:
    by_model_version: dict[str, dict] = {}
    for r, res in entries:
        key = f"{r.model_type}:{r.model_version}"
        bucket = by_model_version.setdefault(key, {"model_type": r.model_type, "model_version": r.model_version, "total": 0, "resolved": 0, "leagues": set(), "markets": set()})
        bucket["total"] += 1
        if res.result_status == "RESOLVED":
            bucket["resolved"] += 1
        if r.league:
            bucket["leagues"].add(r.league)
        if r.market:
            bucket["markets"].add(r.market)
    for bucket in by_model_version.values():
        bucket["leagues"] = sorted(bucket["leagues"])
        bucket["markets"] = sorted(bucket["markets"])
    versions_per_model_type: dict[str, set] = {}
    for r, _ in entries:
        if r.model_type:
            versions_per_model_type.setdefault(r.model_type, set()).add(r.model_version)
    multi_version_models = {mt: sorted(v) for mt, v in versions_per_model_type.items() if len(v) > 1}
    return {
        "by_model_version": by_model_version,
        "multi_version_models_detected": multi_version_models,  # §9 : "détecter changement de version" — jamais mélangé silencieusement, juste signalé
    }


# ---------------------------------------------------------------------------
# §10 : temporal drift — early/middle/recent, jamais une tendance fabriquée.
# ---------------------------------------------------------------------------

def compute_temporal_drift(entries: list[tuple], market: str, *, min_total: int = 30) -> dict:
    resolved = sorted(
        [(r, res) for r, res in entries if r.market == market and res.result_status == "RESOLVED" and r.kickoff is not None],
        key=lambda pair: pair[0].kickoff,
    )
    n = len(resolved)
    if n < min_total:
        return {"status": "INSUFFICIENT_DATA", "market": market, "sample_size": n, "min_required": min_total}

    third = n // 3
    windows = {"early": resolved[:third], "middle": resolved[third:2 * third], "recent": resolved[2 * third:]}
    result = {"status": "ok", "market": market, "sample_size": n}
    for label, window_entries in windows.items():
        result[label] = compute_shadow_track_record(window_entries, market=market)
    return result


# ---------------------------------------------------------------------------
# §11 : breakdown par league/market/model — garanti par un seuil minimum.
# ---------------------------------------------------------------------------

def compute_breakdown(entries: list[tuple], market: str, *, min_sample_size: int = 10) -> dict:
    global_tr = compute_shadow_track_record(entries, market=market)
    by_league = {}
    for league in sorted({r.league for r, _ in entries if r.league and r.market == market}):
        tr = compute_shadow_track_record(entries, market=market, league=league)
        by_league[league] = tr if (tr.get("sample_size") or 0) >= min_sample_size else {"status": "INSUFFICIENT_DATA", "sample_size": tr.get("sample_size", 0)}
    by_model = {}
    for model_type in sorted({r.model_type for r, _ in entries if r.model_type and r.market == market}):
        tr = compute_shadow_track_record(entries, market=market, model_type=model_type)
        by_model[model_type] = tr if (tr.get("sample_size") or 0) >= min_sample_size else {"status": "INSUFFICIENT_DATA", "sample_size": tr.get("sample_size", 0)}
    return {"global": global_tr, "by_league": by_league, "by_model": by_model, "min_sample_size": min_sample_size}


# ---------------------------------------------------------------------------
# §16 : matrice d'activation — DOCUMENTAIRE UNIQUEMENT, jamais sélectionnée/activée ici.
# ---------------------------------------------------------------------------

def build_activation_matrix() -> dict:
    return {
        "MODE_1_SHADOW_ONLY": {
            "description": "État actuel — recherche/shadow uniquement, aucune exposition production.",
            "prerequisites": "Aucun — état par défaut.", "critical_gates_required": [],
            "minimum_evidence": "Aucune.", "monitoring": "Recommandé (Phase 8N).", "rollback": "Non applicable.",
            "kill_switch": "Non applicable (rien à bloquer).", "human_approval": "Non requise.",
        },
        "MODE_2_LIMITED_INTERNAL": {
            "description": "Recherche interne étendue — signaux visibles UNIQUEMENT par l'équipe technique, jamais un utilisateur final.",
            "prerequisites": "TRACK_RECORD >= EARLY_DATA, MONITORING opérationnel.",
            "critical_gates_required": ["MODEL", "MODEL_VERSION", "TEMPORAL_INTEGRITY", "DATABASE_SAFETY", "KILL_SWITCH"],
            "minimum_evidence": "Au moins quelques dizaines d'observations RÉELLES prospectives résolues.",
            "monitoring": "Obligatoire, health != NO_DATA.", "rollback": "Mécanisme testé en isolation (Phase 9.1) suffisant.",
            "kill_switch": "ENABLED, fail-closed vérifié.", "human_approval": "Décision produit explicite, hors du périmètre de cette phase.",
        },
        "MODE_3_LIMITED_PRODUCTION": {
            "description": "Exposition production limitée (1 marché, 1 ligue, volume research/observation).",
            "prerequisites": "TOUS les gates critiques Phase 9 = PASS, TRACK_RECORD >= STATISTICALLY_INFORMATIVE.",
            "critical_gates_required": ["MODEL", "MODEL_VERSION", "TEMPORAL_INTEGRITY", "PROVENANCE", "DATABASE_SAFETY", "ROLLBACK", "MONITORING", "TRACK_RECORD", "KILL_SWITCH"],
            "minimum_evidence": ">= 100 observations RÉELLES prospectives résolues (seuil STATISTICALLY_INFORMATIVE, déjà documenté).",
            "monitoring": "Obligatoire, health HEALTHY/DEGRADED (jamais CRITICAL/BLOCKED).",
            "rollback": "Empiriquement exercé sur ce déploiement (ModelPromotionEvent réel), pas seulement en isolation.",
            "kill_switch": "ENABLED, testé, triggers automatiques opérationnels.",
            "human_approval": "Décision explicite obligatoire — jamais automatique (§29/§47 Phase 9).",
        },
        "MODE_4_FULL_PRODUCTION": {
            "description": "Exposition production complète — hors du périmètre de toute phase actuelle.",
            "prerequisites": "MODE_3 validé sur une période prolongée + tous les critères MODE_3 + odds temporellement vérifiées si Value Engine activé.",
            "critical_gates_required": ["ALL"], "minimum_evidence": "Historique de production MODE_3 documenté et statistiquement stable.",
            "monitoring": "Obligatoire, alerting opérationnel.", "rollback": "Empiriquement exercé plusieurs fois.",
            "kill_switch": "ENABLED, testé, audité.", "human_approval": "Décision explicite obligatoire, jamais automatique.",
        },
    }


# ---------------------------------------------------------------------------
# §17 : blockers — dérivés du VRAI verdict Phase 9, jamais masqués.
# ---------------------------------------------------------------------------

def identify_activation_blockers(readiness_assessment) -> list[dict]:
    blockers = []
    for gate in readiness_assessment.gates:
        if gate.critical and gate.status != "PASS":
            blockers.append({
                "blocker": gate.name, "why": gate.status, "evidence": gate.blocking_reason or "Voir evidence dans le rapport Phase 9.",
                "required_to_clear": gate.required_action or "Aucune action documentée — ré-évaluer.",
            })
    return blockers


# ---------------------------------------------------------------------------
# §20 : data gaps — quantifiés, jamais approximés.
# ---------------------------------------------------------------------------

def compute_data_gaps(session: Session, entries: list[tuple]) -> dict:
    reality = measure_data_reality(session)
    return {
        "future_fixtures_in_db": reality["future_fixtures"],
        "pending_model_predictions": reality["pending_model_predictions"],
        "resolved_model_predictions": reality["resolved_model_predictions"],
        "kickoff_timestamps": "UNKNOWN pour 100% des lignes — ModelPrediction.match_date est typé `date`, aucune heure de coup d'envoi n'est structurellement disponible (confirmé, Phase 9.2).",
        "odds_timestamps": "Absents pour 100% des observations — The Odds API reste SUPPORT_REQUIRED (Phase 8G.2), aucune source odds temporellement vérifiée intégrée.",
        "distinct_markets_covered": sorted({r.market for r, _ in entries if r.market}),
        "distinct_leagues_covered": sorted({r.league for r, _ in entries if r.league}),
        "distinct_model_versions_covered": sorted({r.model_version for r, _ in entries if r.model_version}),
        "shadow_store_total_observations": len(entries),
    }
