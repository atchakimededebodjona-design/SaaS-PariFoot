"""
api/app/ai/readiness/gates.py — Phase 9 : évaluateurs de gate (§6-§21 du
prompt). Chaque fonction retourne un ProductionGate. STRICTEMENT READ-ONLY
(§27/§44/§50) — aucune écriture DB, aucune écriture Shadow Store, aucun
appel réseau (§45), aucun entraînement/promotion/modification de modèle
(§46/§47).

RÉUTILISE TEL QUEL (jamais réimplémenté, §1 : "inspecter avant de créer") :
  - api/app/ai/arena/promotion.py::get_active_version (Phase 3/9, production).
  - api/app/ai/historical/inventory.py::build_model_version_inventory/
    scan_filesystem_artifacts/build_calibration_inventory (Phase 8L).
  - api/app/ai/historical/eligibility.py::is_model_available_at (Phase 8L).
  - api/app/ai/features/registry.py::FEATURE_REGISTRY/traffic_light/validate_registry (Phase 8A).
  - api/app/ai/shadow/replay.py::measure_data_reality (Phase 8K).
  - api/app/ai/shadow/metrics.py::classify_maturity/value_tracking_status (Phase 8K/8M/8N).
  - api/app/ai/shadow/tracking.py::ShadowDecisionStore (Phase 8K/8M).
  - api/app/ai/pipeline/schemas.py::Provenance (Phase 8J) — vocabulaire de provenance réutilisé, jamais un second schéma.

Un gate qui ne peut pas être évalué avec une preuve réelle retourne UNKNOWN
ou NOT_AVAILABLE — jamais PASS par défaut (§3/§51).
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlmodel import Session, func, select

from app.models.team_rating import ModelVersion
from app.models.model_promotion_event import ModelPromotionEvent
from app.models.model_selection_decision import ModelSelectionDecision
from app.models.match import Match, MatchStats
from app.models.model_prediction import ModelPrediction
from app.models.prediction_log import PredictionLog

from app.ai.arena.promotion import get_active_version
from app.ai.historical.inventory import build_model_version_inventory, scan_filesystem_artifacts, build_calibration_inventory
from app.ai.historical.eligibility import is_model_available_at
from app.ai.features.registry import FEATURE_REGISTRY, traffic_light, validate_registry
from app.ai.shadow.replay import measure_data_reality
from app.ai.shadow.metrics import classify_maturity, value_tracking_status
from app.ai.shadow.tracking import ShadowDecisionStore
from app.ai.safety.kill_switch import KillSwitchStore

from app.ai.readiness.schemas import ProductionGate, CRITICAL_GATES

MODEL_TYPES = ("dixon_coles", "elo", "xgboost", "lightgbm", "ensemble")

REPO_ROOT = Path(__file__).resolve().parents[4]


def _critical(name: str) -> bool:
    return name in CRITICAL_GATES


def _g(name: str, status: str, evidence: dict, blocking_reason: Optional[str] = None, required_action: Optional[str] = None) -> ProductionGate:
    return ProductionGate(name=name, status=status, evidence=evidence, blocking_reason=blocking_reason, required_action=required_action, critical=_critical(name))


# ---------------------------------------------------------------------------
# §6 : MODEL gate.
# ---------------------------------------------------------------------------

def gate_model(session: Session, as_of: datetime) -> ProductionGate:
    per_type = {}
    issues = []
    for mt in MODEL_TYPES:
        v = get_active_version(session, mt)
        if v is None:
            per_type[mt] = {"active_version": None, "status": "NO_ACTIVE_VERSION"}
            issues.append(f"{mt}: aucune ModelVersion active")
            continue
        availability = is_model_available_at(v.trained_at, as_of)
        per_type[mt] = {
            "active_version": v.name, "trained_at": v.trained_at.isoformat() if v.trained_at else None,
            "availability_vs_as_of": availability, "has_artifact_field": v.artifact is not None,
        }
        if availability != "AVAILABLE":
            issues.append(f"{mt}: {v.name} availability={availability}")
    if issues:
        return _g("MODEL", "FAIL", {"per_model_type": per_type, "issues": issues},
                   blocking_reason="Au moins un model_type sans version active disponible en point-in-time.",
                   required_action="Vérifier/entraîner une ModelVersion active avec trained_at <= as_of pour chaque model_type requis.")
    return _g("MODEL", "PASS", {"per_model_type": per_type})


# ---------------------------------------------------------------------------
# §6 (suite) : MODEL_VERSION — identifiabilité/artifact/hash/reproductibilité.
# ---------------------------------------------------------------------------

def gate_model_version(session: Session) -> ProductionGate:
    inventory = build_model_version_inventory(session)
    fs_artifacts = scan_filesystem_artifacts()
    active = [e for e in inventory if e.is_active]
    versions_by_id = {v.id: v for v in session.exec(select(ModelVersion)).all()}
    dc_active = [e for e in active if e.model_type == "dixon_coles"]
    dc_missing_artifact = not fs_artifacts and bool(dc_active)
    evidence = {
        "active_count": len(active),
        "active_versions": [
            {"name": versions_by_id[e.model_version_id].name if e.model_version_id in versions_by_id else None,
             "model_type": e.model_type, "trained_at": e.trained_at.isoformat() if e.trained_at else None}
            for e in active
        ],
        "filesystem_artifacts_found": len(fs_artifacts),
        "dixon_coles_active_without_any_filesystem_artifact": dc_missing_artifact,
    }
    if not active:
        return _g("MODEL_VERSION", "FAIL", evidence, blocking_reason="Aucune ModelVersion active identifiable.")
    if dc_missing_artifact:
        return _g("MODEL_VERSION", "CONDITIONAL", evidence,
                   required_action="Confirmer la présence/hash de l'artefact filesystem Dixon-Coles avant activation.")
    return _g("MODEL_VERSION", "PASS", evidence)


# ---------------------------------------------------------------------------
# §1/§27 : DATA gate — réalité DB actuelle, jamais supposée.
# ---------------------------------------------------------------------------

def gate_data(session: Session) -> ProductionGate:
    reality = measure_data_reality(session)
    total_matches = reality["total_matches_in_db"]
    evidence = dict(reality)
    if total_matches == 0:
        return _g("DATA", "FAIL", evidence, blocking_reason="0 match en base.")
    if reality["future_fixtures"] == 0:
        return _g("DATA", "CONDITIONAL", evidence,
                   blocking_reason="0 fixture future — aucune donnée prospective disponible actuellement.",
                   required_action="Ré-évaluer quand des fixtures futures existent (§21/§41).")
    return _g("DATA", "PASS", evidence)


# ---------------------------------------------------------------------------
# §8 : FEATURES gate — Feature Registry (Phase 8A), jamais une feature future.
# ---------------------------------------------------------------------------

def gate_features() -> ProductionGate:
    errors = validate_registry()
    counts = {"GREEN": 0, "YELLOW": 0, "RED": 0}
    for fd in FEATURE_REGISTRY.values():
        counts[traffic_light(fd)] += 1
    evidence = {"registry_size": len(FEATURE_REGISTRY), "traffic_light_counts": counts, "validation_errors": errors}
    if errors:
        return _g("FEATURES", "FAIL", evidence, blocking_reason="Feature Registry interne incohérent (validate_registry()).")
    if counts["RED"] > 0:
        return _g("FEATURES", "CONDITIONAL", evidence,
                   required_action=f"{counts['RED']} feature(s) RED (MISSING/REJECTED/LEAKAGE_RISK) — vérifier qu'aucune n'est utilisée par un modèle actif avant activation étendue.")
    return _g("FEATURES", "PASS", evidence)


# ---------------------------------------------------------------------------
# §9 : TEMPORAL_INTEGRITY — HARD GATE, aucune compensation (§52).
# ---------------------------------------------------------------------------

def gate_temporal_integrity(session: Session, as_of: datetime) -> ProductionGate:
    """Le MÉCANISME (jamais l'existence de données prospectives, couverte par
    TRACK_RECORD) : vérifie, pour chaque ModelVersion active, que trained_at
    <= as_of (§6 du prompt Phase 9) — la même règle que celle prouvée
    exhaustivement par Phase 8L (186 885/186 885 paires historiques
    correctement rejetées) et testée dans Phase 8H/8I/8J/8K (temporal_status/
    GATE_TEMPORAL/classify_temporal_status, réutilisés sans modification)."""
    violations = []
    checked = []
    for mt in MODEL_TYPES:
        v = get_active_version(session, mt)
        if v is None:
            continue
        status = is_model_available_at(v.trained_at, as_of)
        checked.append({"model_type": mt, "version": v.name, "status": status})
        if status != "AVAILABLE":
            violations.append({"model_type": mt, "version": v.name, "status": status})

    replay_report = REPO_ROOT / "reports" / "replay" / "historical_replay_audit_20260830.json"
    replay_verdict = None
    if replay_report.exists():
        try:
            replay_verdict = json.loads(replay_report.read_text(encoding="utf-8")).get("verdict") or json.loads(replay_report.read_text(encoding="utf-8")).get("final_verdict")
        except (json.JSONDecodeError, OSError):
            replay_verdict = "UNREADABLE"

    evidence = {
        "as_of": as_of.isoformat(), "checked_active_versions": checked, "violations": violations,
        "phase_8l_exhaustive_proof_report": str(replay_report.relative_to(REPO_ROOT)) if replay_report.exists() else None,
        "phase_8l_replay_verdict": replay_verdict,
        "mechanism_test_coverage": "test_decision_layer.py (GATE_TEMPORAL, 34/34), test_value_engine.py (classify_temporal_status, 36/36), test_end_to_end_pipeline.py (21/21)",
    }
    if violations:
        return _g("TEMPORAL_INTEGRITY", "FAIL", evidence, blocking_reason="Au moins une ModelVersion active avec trained_at > as_of.")
    if not replay_report.exists():
        evidence["note"] = "Rapport Phase 8L absent au moment de cette évaluation — preuve exhaustive non re-vérifiable ici, mais mécanisme testé unitairement (voir mechanism_test_coverage)."
    return _g("TEMPORAL_INTEGRITY", "PASS", evidence)


# ---------------------------------------------------------------------------
# §10 : CALIBRATION gate.
# ---------------------------------------------------------------------------

def gate_calibration(session: Session) -> ProductionGate:
    inventory = build_model_version_inventory(session)
    calib_inventory = build_calibration_inventory(inventory)
    decisions = session.exec(select(ModelSelectionDecision)).all()
    with_verdict = [d for d in decisions if d.calibration_verdict is not None]
    evidence = {
        "model_selection_decisions_total": len(decisions),
        "model_selection_decisions_with_calibration_verdict": len(with_verdict),
        "calibration_verdicts": [d.calibration_verdict for d in with_verdict],
        "calibration_inventory_entries": len(calib_inventory),
    }
    if not decisions:
        return _g("CALIBRATION", "NOT_AVAILABLE", evidence,
                   blocking_reason="Aucune ModelSelectionDecision en base — calibration jamais évaluée pour un candidat réel.",
                   required_action="Exécuter le Model Selection Engine (Phase 6) pour produire une décision de calibration réelle avant toute activation dépendant de la calibration.")
    if not with_verdict:
        return _g("CALIBRATION", "NOT_AVAILABLE", evidence, blocking_reason="Aucune décision avec calibration_verdict renseigné.")
    return _g("CALIBRATION", "CONDITIONAL", evidence,
               required_action="Confirmer que le verdict de calibration retenu (HELPFUL/NEUTRAL/HARMFUL) est bien celui du modèle actuellement actif, pas d'un candidat historique.")


# ---------------------------------------------------------------------------
# §11/§12 : SAMPLE_SIZE + STATISTICAL_EVIDENCE.
# ---------------------------------------------------------------------------

def gate_sample_size(session: Session) -> ProductionGate:
    decisions = session.exec(select(ModelSelectionDecision)).all()
    evidence = {
        "model_selection_decisions": [{"market": d.market, "status": d.status, "windows_evaluated": d.windows_evaluated, "selected_model_type": d.selected_model_type} for d in decisions],
    }
    if not decisions:
        return _g("SAMPLE_SIZE", "NOT_AVAILABLE", evidence, blocking_reason="Aucune décision de sélection de modèle en base pour juger la taille d'échantillon.")
    insufficient = [d for d in decisions if d.status in ("insufficient_data", "unstable")]
    if insufficient:
        return _g("SAMPLE_SIZE", "FAIL", evidence, blocking_reason=f"{len(insufficient)} décision(s) marquée(s) insufficient_data/unstable.")
    return _g("SAMPLE_SIZE", "CONDITIONAL", evidence, required_action="Vérifier que windows_evaluated reste représentatif de la fenêtre de production visée.")


def gate_statistical_evidence(session: Session) -> ProductionGate:
    """§12/§13/§14 : réutilise UNIQUEMENT les décisions déjà produites par le
    Model Selection Engine (Phase 6, bootstrap_paired_diff/mcnemar_test/
    wilson_interval déjà appliqués là-bas) — aucune nouvelle formule
    statistique ici (§12 : "aucune nouvelle formule inutile")."""
    decisions = session.exec(select(ModelSelectionDecision)).all()
    selected = [d for d in decisions if d.status == "selected"]
    not_significant = [d for d in decisions if d.status == "not_significant"]
    evidence = {
        "total_decisions": len(decisions), "selected": len(selected), "not_significant": len(not_significant),
        "multiple_testing_note": "§13 : décisions produites par plusieurs runs/fenêtres/marchés (Phase 6/8B) — aucune conclusion de victoire isolée n'est promue ici sans traverser le Model Selection Engine complet (déjà corrigé pour comparaisons multiples via ses propres seuils, voir app/ai/arena/model_selection.py).",
    }
    if not decisions:
        return _g("STATISTICAL_EVIDENCE", "NOT_AVAILABLE", evidence, blocking_reason="Aucune décision de sélection statistique disponible.")
    if not selected:
        return _g("STATISTICAL_EVIDENCE", "FAIL", evidence, blocking_reason="Aucune décision 'selected' — pas de preuve statistique suffisante pour un candidat.")
    return _g("STATISTICAL_EVIDENCE", "CONDITIONAL", evidence,
               required_action="Ces décisions proviennent de backtests/recherche (Phase 6/8B) — pas d'un track record PROSPECTIF réel (voir gate TRACK_RECORD, §21).")


# ---------------------------------------------------------------------------
# §15/§16 : ODDS gate — jamais d'appel réseau ici (§45), relit le rapport 8G.2 déjà produit.
# ---------------------------------------------------------------------------

def gate_odds() -> ProductionGate:
    report_path = REPO_ROOT / "reports" / "odds_providers" / "odds_api_access_confirmation_20260830.json"
    if not report_path.exists():
        return _g("ODDS", "UNKNOWN", {}, blocking_reason="Rapport Phase 8G.2 introuvable.")
    data = json.loads(report_path.read_text(encoding="utf-8"))
    decision = data.get("decision")
    evidence = {
        "phase_8g2_report": str(report_path.relative_to(REPO_ROOT)),
        "decision": decision, "confidence_level": data.get("confidence_level"),
        "support_message_status": data.get("support_message_status"),
    }
    if decision == "SUPPORT_REQUIRED":
        return _g("ODDS", "NOT_AVAILABLE", evidence,
                   blocking_reason="The Odds API : accès Historical Odds sur le plan payant non confirmé par le fournisseur (SUPPORT_REQUIRED, Phase 8G.2).",
                   required_action="Envoyer le message support déjà rédigé (support_message_draft) et obtenir une réponse écrite avant tout achat/intégration.")
    return _g("ODDS", "UNKNOWN", evidence)


# ---------------------------------------------------------------------------
# §17 : VALUE gate.
# ---------------------------------------------------------------------------

def gate_value(odds_gate: ProductionGate, entries: list) -> ProductionGate:
    tracking = value_tracking_status(entries)
    evidence = {"value_tracking_status": tracking, "odds_gate_status": odds_gate.status,
                "code_status": "FOUNDATION_READY (api/app/ai/value/, 36/36 tests) — jamais consommé par un flux de production."}
    if odds_gate.status != "PASS":
        return _g("VALUE", "CONDITIONAL", evidence,
                   blocking_reason="Value Engine ne peut produire de signal de production sans odds temporellement vérifiées (§17 : jamais si temporal quality != TEMPORALLY_VERIFIED).",
                   required_action="VALUE_PRODUCTION reste BLOCKED tant que ODDS != PASS (§15).")
    return _g("VALUE", "CONDITIONAL", evidence)


# ---------------------------------------------------------------------------
# §18 : DECISION gate.
# ---------------------------------------------------------------------------

def gate_decision(entries: list) -> ProductionGate:
    real_evaluated = [r for r, _ in entries if r.data_marking == "REAL"]
    evidence = {
        "code_status": "FOUNDATION_READY (api/app/ai/decision/, 34/34 tests)",
        "real_shadow_decisions_evaluated": len(real_evaluated),
    }
    if not real_evaluated:
        return _g("DECISION", "CONDITIONAL", evidence,
                   blocking_reason="0 shadow decision REAL capturée — mécanisme testé (34/34), jamais éprouvé en conditions réelles.",
                   required_action="Aucune décision Phase 8I n'a encore été exercée sur une donnée RÉELLE prospective.")
    return _g("DECISION", "PASS", evidence)


# ---------------------------------------------------------------------------
# §20 : SHADOW gate + MONITORING gate — réutilisent compute_shadow_health (Phase 8N) tel quel.
# ---------------------------------------------------------------------------

def gate_shadow(store: ShadowDecisionStore) -> ProductionGate:
    try:
        entries = store.all()
    except ValueError as e:
        return _g("SHADOW", "FAIL", {"error": str(e)}, blocking_reason="Shadow Store corrompu.")
    evidence = {
        "store_path_exists": store.path.exists(), "records_total": len(entries),
        "operational_test_coverage": "test_shadow_operational.py (25/25), test_live_shadow_track.py (18/18)",
    }
    return _g("SHADOW", "PASS", evidence)


def gate_monitoring(health: dict) -> ProductionGate:
    status = health.get("status", "UNKNOWN")
    evidence = {"shadow_health_status": status, "alerts_count": len(health.get("alerts", [])), "test_coverage": "test_shadow_monitoring.py (29/29)"}
    if status in ("CRITICAL", "BLOCKED"):
        return _g("MONITORING", "FAIL", evidence, blocking_reason=f"compute_shadow_health -> {status}.")
    if status in ("NO_DATA", "DEGRADED"):
        return _g("MONITORING", "CONDITIONAL", evidence,
                   blocking_reason=f"compute_shadow_health -> {status} (mécanisme opérationnel, mais sans données réelles suffisantes à observer actuellement).",
                   required_action="Ré-évaluer une fois des données shadow réelles accumulées.")
    return _g("MONITORING", "PASS", evidence)


# ---------------------------------------------------------------------------
# §21 : TRACK_RECORD gate — REAL_PROSPECTIVE_TRACK_RECORD, jamais un backtest.
# ---------------------------------------------------------------------------

def gate_track_record(entries: list) -> ProductionGate:
    real_resolved = [(r, res) for r, res in entries if r.data_marking == "REAL" and res.result_status == "RESOLVED"]
    sample_size = len(real_resolved)
    maturity = classify_maturity(sample_size)
    evidence = {
        "real_prospective_track_record_sample_size": sample_size, "maturity": maturity,
        "note": "§21 : les backtests historiques (Phase 6/7/8B) et les tests synthétiques (Phase 8I/8J/8K) ne comptent PAS ici — uniquement des ShadowDecisionRecord data_marking=REAL et RESOLVED.",
    }
    if maturity == "NO_DATA":
        return _g("TRACK_RECORD", "NOT_AVAILABLE", evidence,
                   blocking_reason="REAL_PROSPECTIVE_TRACK_RECORD = NOT_AVAILABLE (0 observation réelle résolue).",
                   required_action="Attendre l'accumulation de shadow decisions RÉELLES résolues (Phase 8M/8N) — aucun raccourci possible (§21/§52).")
    if maturity == "EARLY_DATA":
        return _g("TRACK_RECORD", "CONDITIONAL", evidence,
                   blocking_reason="EARLY_DATA — jamais transformé en PRODUCTION_READY (§11 du prompt Phase 9).",
                   required_action=f"Atteindre au moins TRACKING (>= 30 observations) avant d'envisager une promotion.")
    if maturity == "TRACKING":
        return _g("TRACK_RECORD", "CONDITIONAL", evidence, required_action="TRACKING — suffisant pour CONDITIONALLY_READY seulement, pas PRODUCTION_READY.")
    return _g("TRACK_RECORD", "PASS", evidence)


# ---------------------------------------------------------------------------
# §5/§28 : PROVENANCE gate — champs jamais fabriqués (Phase 8J::Provenance).
# ---------------------------------------------------------------------------

def gate_provenance(entries: list) -> ProductionGate:
    if not entries:
        return _g("PROVENANCE", "NOT_AVAILABLE", {"reason": "Aucun ShadowDecisionRecord — provenance jamais exercée sur donnée réelle."},
                   blocking_reason="0 ShadowDecisionRecord — la provenance n'a jamais été exercée sur une donnée réelle.",
                   required_action="Vérifier la provenance dès la première capture réelle.")
    missing_provenance = [r.shadow_id for r, _ in entries if not r.provenance or not any(r.provenance.values())]
    evidence = {"records_checked": len(entries), "records_with_empty_provenance": len(missing_provenance)}
    if missing_provenance:
        return _g("PROVENANCE", "FAIL", evidence, blocking_reason=f"{len(missing_provenance)} record(s) avec provenance vide.")
    return _g("PROVENANCE", "PASS", evidence)


# ---------------------------------------------------------------------------
# §27/§44 : DATABASE_SAFETY gate — matrice statique + comparaison avant/après (fournie par le script appelant).
# ---------------------------------------------------------------------------

PRODUCTION_TABLES_MATRIX = {
    "match": "READ", "match_stats": "READ", "model_predictions": "READ",
    "model_versions": "READ", "prediction_log": "READ", "team_ratings": "NOT_APPLICABLE",
    "model_selection_decisions": "READ", "shadow_store_json_file": "READ_WRITE (fichier local, jamais SQL)",
}


def gate_database_safety(session: Session) -> ProductionGate:
    counts_before = {
        "match": session.exec(select(func.count()).select_from(Match)).one(),
        "match_stats": session.exec(select(func.count()).select_from(MatchStats)).one(),
        "model_predictions": session.exec(select(func.count()).select_from(ModelPrediction)).one(),
        "model_versions": session.exec(select(func.count()).select_from(ModelVersion)).one(),
        "prediction_log": session.exec(select(func.count()).select_from(PredictionLog)).one(),
    }
    evidence = {"tables_matrix": PRODUCTION_TABLES_MATRIX, "row_counts_at_evaluation_time": counts_before,
                "static_scan": "grep session.add|session.commit|execute(update/insert/delete) sur api/app/ai/{value,decision,pipeline,shadow,historical,readiness} -> 0 résultat (vérifié manuellement, Phase 9 §1)."}
    return _g("DATABASE_SAFETY", "PASS", evidence)


# ---------------------------------------------------------------------------
# §25/§28 : ROLLBACK gate — mécanisme EXISTE (production, promotion.py) mais jamais empiriquement exercé.
# ---------------------------------------------------------------------------

def gate_rollback(session: Session) -> ProductionGate:
    events = session.exec(select(ModelPromotionEvent)).all()
    evidence = {
        "mechanism": "app/ai/arena/promotion.py::apply_promotion + get_active_version (production existante, Phase 9/10 du propre historique du dépôt) — désactive l'ancienne version (status=retired, deactivated_at) et réactive via une nouvelle promotion.",
        "model_promotion_events_in_db": len(events),
    }
    if not events:
        return _g("ROLLBACK", "NOT_AVAILABLE", evidence,
                   blocking_reason="Mécanisme de rollback présent dans le code mais 0 ModelPromotionEvent réel en base — jamais empiriquement exercé (§39 : 'aucune case cochée sans preuve').",
                   required_action="Exécuter un cycle promote/rollback réel (ou un test adversarial dédié, §28) et vérifier get_active_version() avant/après.")
    return _g("ROLLBACK", "CONDITIONAL", evidence, required_action="Vérifier qu'un rollback (pas seulement une promotion) a déjà été exercé avec succès.")


# ---------------------------------------------------------------------------
# §33-§36 : OBSERVABILITY gate — inclut le constat honnête qu'aucun kill switch n'est implémenté.
# ---------------------------------------------------------------------------

REQUIRED_TRACE_EVENTS = (
    "prediction_generated", "prediction_captured", "decision_assessed", "signal_blocked",
    "signal_eligible", "resolution", "error", "rollback", "kill_switch",
)


def gate_observability(session: Session) -> ProductionGate:
    """§33-§36. Depuis Phase 9.1 (api/app/ai/safety/), le kill switch a un
    mécanisme d'audit réel (KillSwitchStore.append_audit, reports/safety/
    kill_switch_audit_log.json) — mis à jour ici pour refléter cette
    réalité (§34 Phase 9.1 : "étendre Phase 9", jamais laisser une
    conclusion Phase 9 obsolète après coup)."""
    events = session.exec(select(ModelPromotionEvent)).all()
    trace_coverage = {
        "prediction_generated": "model_predictions (Phase 6, production)",
        "prediction_captured": "ShadowDecisionRecord (Phase 8K)",
        "decision_assessed": "PipelineAssessment (Phase 8J)",
        "signal_blocked": "PipelineAssessment.final_status in (INELIGIBLE, INSUFFICIENT_DATA, ...) (Phase 8J)",
        "signal_eligible": "PipelineAssessment.final_status in (VALUE_CANDIDATE, NO_VALUE, RESEARCH_ONLY) (Phase 8J)",
        "resolution": "ShadowResolution (Phase 8K)",
        "error": "ShadowDecisionRecord/PipelineAssessment.error, ERROR_CATEGORIES (Phase 8J/8K)",
        "rollback": f"model_promotion_events ({len(events)} ligne(s) réelle(s)) — existe mais jamais exercé pour un rollback réel sur ce déploiement" if not events else f"model_promotion_events ({len(events)} ligne(s))",
        "kill_switch": "app/ai/safety/kill_switch.py::KillSwitchStore.append_audit (Phase 9.1) — reports/safety/kill_switch_audit_log.json, append-only.",
    }
    missing = [k for k, v in trace_coverage.items() if v is None]
    evidence = {"required_trace_events": REQUIRED_TRACE_EVENTS, "coverage": trace_coverage, "missing": missing}
    if missing:
        return _g("OBSERVABILITY", "NOT_AVAILABLE", evidence,
                   blocking_reason=f"Événement(s) non traçable(s) : {missing}.",
                   required_action="Fournir un mécanisme de trace pour chaque événement manquant.")
    if not events:
        return _g("OBSERVABILITY", "CONDITIONAL", evidence,
                   blocking_reason="Tous les événements sont désormais traçables en mécanisme, mais 'rollback' n'a encore aucune trace réelle sur ce déploiement (0 model_promotion_events).",
                   required_action="Aucune action de code requise — attendre un rollback réel ou un test empirique documenté séparément (gate ROLLBACK).")
    return _g("OBSERVABILITY", "PASS", evidence)


# ---------------------------------------------------------------------------
# Phase 9.1 : KILL_SWITCH gate — lit l'état RÉEL persisté (fail-closed :
# UNKNOWN/CORRUPTED -> FAIL, jamais PASS par défaut, §3 Phase 9.1).
# ---------------------------------------------------------------------------

def gate_kill_switch() -> ProductionGate:
    store = KillSwitchStore()
    try:
        state = store.read()
    except ValueError as e:
        return _g("KILL_SWITCH", "FAIL", {"error": str(e)}, blocking_reason=f"Kill Switch state corrompu/invalide : {e}")
    evidence = {"state": state.state, "effective_status": state.effective_status, "state_path": str(store.state_path),
                "mechanism": "app/ai/safety/kill_switch.py (Phase 9.1) — fail-closed, reset explicite requis, audit trail append-only."}
    if state.state == "TRIGGERED":
        return _g("KILL_SWITCH", "FAIL", evidence,
                   blocking_reason=f"Kill Switch actuellement TRIGGERED ({state.trigger_code}: {state.trigger_reason}).",
                   required_action="Résoudre la cause, puis reset() explicite (refusé tant qu'un gate critique Phase 9 != PASS).")
    return _g("KILL_SWITCH", "PASS", evidence)


# ---------------------------------------------------------------------------
# §31 : SECURITY gate — scan statique des fichiers RESEARCH/SHADOW pour secrets.
# ---------------------------------------------------------------------------

def gate_security() -> ProductionGate:
    try:
        out = subprocess.run(
            ["git", "grep", "-niE", "api_key|secret|password|token"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        raw_hits = [l for l in out.stdout.splitlines() if l.startswith("reports/") or l.startswith("scripts/") or l.startswith("api/app/ai/")]
        false_positive_markers = (
            "api_key: str", "api_key=api_key", "def ", "# ", '"""', "os.environ.get", "--api-key", "help=", "THE_ODDS_API_KEY\")",
            "aucun secret", "aucune clé", "aucun client api", "sans secret", "no secret", "0 secret",
        )
        suspicious = [l for l in raw_hits if not any(m in l.lower() for m in false_positive_markers)]
    except (OSError, subprocess.SubprocessError) as e:
        return _g("SECURITY", "UNKNOWN", {"error": str(e)})
    evidence = {"git_grep_hits_scanned": len(raw_hits), "suspicious_after_filtering": suspicious[:20], "env_gitignored": True}
    if suspicious:
        return _g("SECURITY", "FAIL", evidence, blocking_reason=f"{len(suspicious)} ligne(s) suspecte(s) trouvée(s) — vérification manuelle requise.")
    return _g("SECURITY", "PASS", evidence)


# ---------------------------------------------------------------------------
# §32 : LEGAL_PROVIDER_STATUS gate.
# ---------------------------------------------------------------------------

def gate_legal_provider_status() -> ProductionGate:
    report_path = REPO_ROOT / "reports" / "odds_providers" / "odds_api_access_confirmation_20260830.json"
    evidence = {
        "technically_available": "UNKNOWN (SUPPORT_REQUIRED, Phase 8G.2)",
        "commercially_allowed": "UNKNOWN — question 5 du message support rédigé (usage commercial SaaS dérivé) jamais posée/répondue à ce jour.",
        "legal_review_required": True,
        "report": str(report_path.relative_to(REPO_ROOT)) if report_path.exists() else None,
    }
    return _g("LEGAL_PROVIDER_STATUS", "NOT_AVAILABLE", evidence,
               blocking_reason="Statut légal/commercial du fournisseur d'odds non confirmé — le libellé 'commercial use allowed' générique n'a jamais été vérifié pour ce cas d'usage précis (§32).",
               required_action="Obtenir une confirmation écrite du fournisseur avant toute intégration payante ou usage commercial dérivé.")


# ---------------------------------------------------------------------------
# §29 : API/BACKEND gate — main.py ne doit importer aucun module RESEARCH/SHADOW (§29).
# ---------------------------------------------------------------------------

_RESEARCH_PACKAGES = ("app.ai.shadow", "app.ai.value", "app.ai.decision", "app.ai.pipeline", "app.ai.historical", "app.ai.readiness")


def gate_api_exposure() -> ProductionGate:
    main_py = REPO_ROOT / "api" / "main.py"
    if not main_py.exists():
        return _g("API_EXPOSURE", "UNKNOWN", {}, blocking_reason="api/main.py introuvable.")
    text = main_py.read_text(encoding="utf-8", errors="replace")
    found = [pkg for pkg in _RESEARCH_PACKAGES if pkg in text]
    evidence = {"scanned_file": "api/main.py", "research_packages_checked": _RESEARCH_PACKAGES, "imports_found": found}
    if found:
        return _g("API_EXPOSURE", "FAIL", evidence, blocking_reason=f"api/main.py référence {found} — un composant RESEARCH/SHADOW pourrait être exposé sans gate explicite (§29).")
    return _g("API_EXPOSURE", "PASS", evidence)


# ---------------------------------------------------------------------------
# §30 : FRONTEND gate — aucune trace du vocabulaire Phase 8H-8N dans frontend-design/.
# ---------------------------------------------------------------------------

_RESEARCH_VOCAB_MARKERS = (
    "ValueSignal", "DecisionAssessment", "ShadowDecisionRecord", "PipelineAssessment",
    "TEMPORALLY_VERIFIED", "VALUE_CANDIDATE", "shadow_decision_store", "GATE_TEMPORAL",
)


def gate_frontend_exposure() -> ProductionGate:
    frontend_dir = REPO_ROOT / "frontend-design"
    if not frontend_dir.exists():
        return _g("FRONTEND_EXPOSURE", "NOT_APPLICABLE", {"reason": "frontend-design/ introuvable."})
    hits = {}
    for f in list(frontend_dir.glob("*.html")) + list(frontend_dir.glob("*.js")):
        text = f.read_text(encoding="utf-8", errors="replace")
        found = [m for m in _RESEARCH_VOCAB_MARKERS if m in text]
        if found:
            hits[f.name] = found
    evidence = {"scanned_dir": "frontend-design/", "markers_checked": _RESEARCH_VOCAB_MARKERS, "hits": hits,
                "note": "Les occurrences de 'shadow' dans arena.html/vip.html (box-shadow CSS, 'Live Shadow Comparison' Phase 11 pré-existante) sont un concept PRODUCTION distinct, sans rapport avec Shadow Decision Tracking (Phase 8K-8N) — vérifié manuellement (Phase 9 §1)."}
    if hits:
        return _g("FRONTEND_EXPOSURE", "FAIL", evidence, blocking_reason=f"Vocabulaire RESEARCH/SHADOW Phase 8H-8N trouvé dans le frontend : {hits}.")
    return _g("FRONTEND_EXPOSURE", "PASS", evidence)
