"""
api/app/ai/readiness/matrix.py — Phase 9 : assemble tous les gates (§2/§4 du
prompt) en une seule ProductionReadinessAssessment. STRICTEMENT READ-ONLY —
n'appelle jamais store.save()/session.commit().

RÈGLE DE VERDICT (§4/§52, "no compensation") :
  - Un gate CRITIQUE (schemas.CRITICAL_GATES) dont le status != "PASS"
    empêche PRODUCTION_READY, quelle que soit la qualité des autres gates.
  - BLOCKED : l'évaluation elle-même n'a pas pu s'exécuter proprement
    (Shadow Store corrompu — SHADOW=FAIL) — distinct d'un NO_GO raisonné.
  - NO_GO : évaluation complète, mais au moins un gate critique bloquant.
  - CONDITIONALLY_READY : tous les gates critiques PASS, mais au moins un
    gate non-critique n'est pas PASS.
  - PRODUCTION_READY : TOUS les gates (critiques et non-critiques) PASS.
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Session

from app.ai.shadow.monitoring import compute_shadow_health
from app.ai.shadow.tracking import ShadowDecisionStore

from app.ai.readiness import gates as g
from app.ai.readiness.schemas import (
    ProductionGate, ProductionReadinessAssessment, ChecklistItem, ModelPromotionReadiness,
    CRITICAL_GATES, CHECKLIST_ITEMS,
)


def _checklist_from_gates(by_name: dict[str, ProductionGate]) -> list[ChecklistItem]:
    mapping = {
        "model_approved": "MODEL", "artifact_verified": "MODEL_VERSION", "feature_set_verified": "FEATURES",
        "temporal_integrity_verified": "TEMPORAL_INTEGRITY", "calibration_verified": "CALIBRATION",
        "statistical_evidence_sufficient": "STATISTICAL_EVIDENCE", "prospective_track_record_sufficient": "TRACK_RECORD",
        "odds_verified": "ODDS", "value_engine_validated": "VALUE", "decision_layer_validated": "DECISION",
        "shadow_healthy": "SHADOW", "monitoring_healthy": "MONITORING", "rollback_tested": "ROLLBACK",
        "database_isolation_verified": "DATABASE_SAFETY", "api_exposure_verified": "API_EXPOSURE",
        "frontend_exposure_verified": "FRONTEND_EXPOSURE", "security_verified": "SECURITY",
        "legal_provider_review_complete": "LEGAL_PROVIDER_STATUS", "kill_switch_tested": "KILL_SWITCH",
    }
    # kill_switch_tested/rollback_tested (Phase 9.1) : "tested" ne veut jamais dire seulement "actuellement
    # dans un état sain" (gate PASS) — cite explicitement la suite isolée qui a réellement exercé le
    # mécanisme (api/test_safety_controls.py), jamais une équivalence implicite gate==PASS -> testé.
    isolated_test_note = {
        "kill_switch_tested": " ; mécanisme exercé par api/test_safety_controls.py (trigger/reset/fail-closed/concurrency, base isolée).",
        "rollback_tested": " ; mécanisme exercé par api/test_safety_controls.py (rollback isolé, jamais api/app.db) — mais 0 rollback réel sur CE déploiement (voir gate ROLLBACK).",
    }
    items = []
    for key in CHECKLIST_ITEMS:
        gate_name = mapping[key]
        gate = by_name.get(gate_name)
        checked = gate is not None and gate.status == "PASS"
        evidence = f"gate {gate_name} status={gate.status if gate else 'MISSING'}" + (f" — {gate.blocking_reason}" if gate and gate.blocking_reason else "") + isolated_test_note.get(key, "")
        items.append(ChecklistItem(key=key, checked=checked, evidence=evidence))
    return items


def evaluate_production_readiness(session: Session, store: ShadowDecisionStore, as_of: datetime) -> ProductionReadinessAssessment:
    # §2 : évalue les 23 gates (20 minimum + API/FRONTEND exposure + KILL_SWITCH Phase 9.1).
    health = compute_shadow_health(session, store, as_of)
    entries = store.all()  # store.load() déjà appelé par compute_shadow_health() ci-dessus.

    odds_gate = g.gate_odds()
    gate_list: list[ProductionGate] = [
        g.gate_model(session, as_of),
        g.gate_model_version(session),
        g.gate_data(session),
        g.gate_features(),
        g.gate_temporal_integrity(session, as_of),
        g.gate_calibration(session),
        g.gate_sample_size(session),
        g.gate_statistical_evidence(session),
        odds_gate,
        g.gate_value(odds_gate, entries),
        g.gate_decision(entries),
        g.gate_shadow(store),
        g.gate_track_record(entries),
        g.gate_monitoring(health),
        g.gate_provenance(entries),
        g.gate_database_safety(session),
        g.gate_rollback(session),
        g.gate_observability(session),
        g.gate_security(),
        g.gate_legal_provider_status(),
        g.gate_api_exposure(),
        g.gate_frontend_exposure(),
        g.gate_kill_switch(),
    ]
    by_name = {gate.name: gate for gate in gate_list}

    # §4/§5/§52 : dérivation du verdict — aucune compensation.
    shadow_gate = by_name["SHADOW"]
    evaluation_blocked = shadow_gate.status == "FAIL"

    critical_blocking = [name for name in CRITICAL_GATES if by_name[name].status != "PASS"]
    non_critical_blocking = [gate.name for gate in gate_list if gate.name not in CRITICAL_GATES and gate.status != "PASS"]

    if evaluation_blocked:
        final_verdict = "BLOCKED"
        recommended_mode = "MODE_0_DISABLED"
    elif critical_blocking:
        final_verdict = "NO_GO"
        recommended_mode = "MODE_1_SHADOW_ONLY"
    elif non_critical_blocking:
        final_verdict = "CONDITIONALLY_READY"
        recommended_mode = "MODE_2_INTERNAL_RESEARCH"
    else:
        final_verdict = "PRODUCTION_READY"
        recommended_mode = "MODE_3_LIMITED_PRODUCTION"

    # §7 : promotion — jamais automatique, jamais PROMOTION_ELIGIBLE sans preuve statistique PROSPECTIVE suffisante (§21/§41).
    track_record_gate = by_name["TRACK_RECORD"]
    stat_gate = by_name["STATISTICAL_EVIDENCE"]
    promotion_readiness = []
    for mt in g.MODEL_TYPES:
        reasons = []
        if track_record_gate.status != "PASS":
            reasons.append(f"TRACK_RECORD={track_record_gate.status} ({track_record_gate.evidence.get('maturity')}) — REAL_PROSPECTIVE_TRACK_RECORD insuffisant.")
        if stat_gate.status != "PASS":
            reasons.append(f"STATISTICAL_EVIDENCE={stat_gate.status} — preuve issue de backtests/recherche, pas de suivi prospectif réel.")
        status = "PROMOTION_NOT_ELIGIBLE" if reasons else "PROMOTION_ELIGIBLE"
        promotion_readiness.append(ModelPromotionReadiness(model_type=mt, status=status, reasons=reasons,
                                                             evidence={"track_record_maturity": track_record_gate.evidence.get("maturity"), "statistical_evidence_status": stat_gate.status}))

    checklist = _checklist_from_gates(by_name)

    open_risks = [f"{gate.name}: {gate.blocking_reason}" for gate in gate_list if gate.blocking_reason]
    blocking_conditions = [f"{gate.name}: {gate.blocking_reason}" for gate in gate_list if gate.critical and gate.status != "PASS" and gate.blocking_reason]

    conditions_required_now = [
        "Aucune activation MODE_3/MODE_4 sans nouvelle exécution de cette évaluation (§37 dry-run, §50 déterminisme).",
    ]
    conditions_required_before_production = [gate.required_action for gate in gate_list if gate.critical and gate.required_action]
    conditions_optional_future = [gate.required_action for gate in gate_list if not gate.critical and gate.required_action]

    phase10_readiness = {
        "ready": [gate.name for gate in gate_list if gate.status == "PASS"],
        "blocked": [gate.name for gate in gate_list if gate.status in ("FAIL", "UNKNOWN")],
        "missing_real_data": [gate.name for gate in gate_list if gate.status == "NOT_AVAILABLE"],
        "open_risks": open_risks,
        "conditions_before_any_production_activation": conditions_required_before_production,
        "decisions_requiring_explicit_human_approval": [
            "Achat/upgrade The Odds API (§16 : jamais automatique).",
            "Toute intégration réelle d'un fournisseur d'odds (§16).",
            "Passage à MODE_3/MODE_4 (§22/§47 : jamais automatique).",
            "Tout reset() du Kill Switch réel (Phase 9.1 : mécanisme implémenté et testé, mais un reset reste refusé tant qu'un gate critique n'est pas PASS — jamais automatique).",
            "Premier rollback réel exercé contre api/app.db (le mécanisme, Phase 9.1, n'a été exercé qu'en base isolée à ce jour).",
        ],
    }

    db_safety = {
        "matrix": g.PRODUCTION_TABLES_MATRIX,
        "row_counts": by_name["DATABASE_SAFETY"].evidence.get("row_counts_at_evaluation_time"),
    }

    return ProductionReadinessAssessment(
        generated_at=datetime.now(as_of.tzinfo), as_of=as_of, gates=gate_list,
        critical_gate_failures=critical_blocking, final_verdict=final_verdict, recommended_mode=recommended_mode,
        promotion_readiness=promotion_readiness, checklist=checklist, open_risks=open_risks,
        blocking_conditions=blocking_conditions, conditions_required_now=conditions_required_now,
        conditions_required_before_production=conditions_required_before_production,
        conditions_optional_future=conditions_optional_future, phase10_readiness=phase10_readiness, db_safety=db_safety,
    )
