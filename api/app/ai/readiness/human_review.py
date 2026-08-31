"""
api/app/ai/readiness/human_review.py — Phase 10 : XFOOT CONTROLLED PRODUCTION
READINESS & EMPIRICAL VALIDATION V1.

RÉUTILISE TEL QUEL (jamais réimplémenté, §1 : "REUSE > REIMPLEMENT") :
  - app.ai.readiness.matrix.evaluate_production_readiness (Phase 9) — LA source de vérité des gates.
  - app.ai.readiness.schemas.CRITICAL_GATES/GATE_DIMENSIONS (Phase 9).
  - app.ai.shadow.evidence.build_activation_matrix/identify_activation_blockers (Phase 9.3).
  - app.ai.shadow.metrics.MATURITY_THRESHOLDS (Phase 8M).

Ce module n'ajoute QUE la couche de décision humaine PROPRE à cette phase —
AUCUN nouveau calcul de gate, AUCUN nouveau seuil statistique (§21 : "ne pas
inventer de seuil arbitraire — réutiliser les critères déjà établis") :

  1. build_phase10_checklist() — §24 : reshape des 16 items en une simple
     sélection/relabellisation des ProductionGate déjà évalués (Phase 9) —
     aucune nouvelle évaluation.
  2. classify_evidence_status() — §40 : PROVEN/OBSERVED/UNKNOWN/BLOCKED/
     REQUIRED_NEXT, dérivés des mêmes ProductionGate.
  3. human_review_gate() — §23 : READY_FOR_HUMAN_REVIEW/NOT_READY_FOR_HUMAN_REVIEW.
  4. build_entry_criteria()/build_exit_criteria() — §21/§22 : wrap de
     ProductionGate/build_activation_matrix()/MATURITY_THRESHOLDS, jamais modifiés.
  5. derive_phase10_verdict() — §35/§36 : vocabulaire PROPRE à cette phase
     (10 valeurs). §36 "no compensation" : un NO_GO readiness causé par un
     gate critique indépendant du volume de preuve (ex. ROLLBACK/PROVENANCE)
     n'est JAMAIS masqué par une maturité d'évidence croissante — vérifié
     AVANT les branches de maturité positives.

STRICTEMENT LECTURE SEULE : aucune fonction ici n'écrit en DB, dans le
Shadow Store, ni ne trigger/reset le Kill Switch, ni ne promeut un modèle.
"""

from __future__ import annotations

from app.ai.shadow.evidence import build_activation_matrix
from app.ai.shadow.metrics import MATURITY_THRESHOLDS

# ---------------------------------------------------------------------------
# §35 : vocabulaire de verdict — PRODUCTION_READY structurellement absent
# (§39 : jamais une activation automatique, quel que soit le résultat).
# ---------------------------------------------------------------------------

PHASE10_VERDICTS = (
    "NO_DATA", "INSUFFICIENT_REAL_DATA", "EARLY_EVIDENCE", "TRACKING", "STATISTICALLY_INFORMATIVE",
    "READY_FOR_HUMAN_REVIEW", "NOT_READY_FOR_HUMAN_REVIEW", "NO_GO", "NEEDS_FIXES", "BLOCKED",
)

HUMAN_REVIEW_STATUSES = ("READY_FOR_HUMAN_REVIEW", "NOT_READY_FOR_HUMAN_REVIEW")

# ---------------------------------------------------------------------------
# §24 : checklist — 16 items, chacun relié à UNE gate déjà évaluée (Phase 9).
# Odds/Value : seuls items où NOT_AVAILABLE/NOT_APPLICABLE/CONDITIONAL compte
# comme "NA" (checked) plutôt que bloquant — cohérent avec CRITICAL_GATES qui
# ne les inclut déjà pas (readiness/schemas.py).
# ---------------------------------------------------------------------------

PHASE10_CHECKLIST_ITEMS = (
    ("Model", "MODEL", False),
    ("Model Version", "MODEL_VERSION", False),
    ("Features", "FEATURES", False),
    ("Data", "DATA", False),
    ("Temporal", "TEMPORAL_INTEGRITY", False),
    ("Track Record", "TRACK_RECORD", False),
    ("Provenance", "PROVENANCE", False),
    ("Monitoring", "MONITORING", False),
    ("Rollback", "ROLLBACK", False),
    ("Safety", "KILL_SWITCH", False),
    ("Database", "DATABASE_SAFETY", False),
    ("Security", "SECURITY", False),
    ("API", "API_EXPOSURE", False),
    ("Frontend", "FRONTEND_EXPOSURE", False),
    ("Odds", "ODDS", True),
    ("Value", "VALUE", True),
)


def build_phase10_checklist(assessment) -> list[dict]:
    """§24 : toute gate critique non-PASS -> NO_GO (déjà la règle de verdict de evaluate_production_readiness,
    Phase 9 — jamais une deuxième logique ici, seulement une présentation)."""
    by_name = {g.name: g for g in assessment.gates}
    items = []
    for label, gate_name, na_allowed in PHASE10_CHECKLIST_ITEMS:
        gate = by_name.get(gate_name)
        if gate is None:
            items.append({"item": label, "gate": gate_name, "status": "UNKNOWN", "checked": False, "critical": False,
                          "evidence": "Gate absente de l'évaluation Phase 9 — jamais supposée PASS."})
            continue
        is_na = na_allowed and gate.status in ("NOT_AVAILABLE", "NOT_APPLICABLE", "CONDITIONAL")
        checked = gate.status == "PASS" or is_na
        items.append({
            "item": label, "gate": gate_name, "status": ("NA" if is_na else gate.status),
            "checked": checked, "critical": gate.critical,
            "evidence": gate.blocking_reason or "PASS",
        })
    return items


# ---------------------------------------------------------------------------
# §40 : PROVEN / OBSERVED / UNKNOWN / BLOCKED / REQUIRED_NEXT — jamais une
# limitation transformée en succès.
# ---------------------------------------------------------------------------

def classify_evidence_status(assessment) -> dict:
    proven, observed, unknown, blocked, required_next = [], [], [], [], []
    for gate in assessment.gates:
        entry = {"gate": gate.name, "status": gate.status, "critical": gate.critical}
        if gate.status == "PASS":
            proven.append(entry)
        elif gate.status == "CONDITIONAL":
            observed.append(entry)  # évalué avec des données réelles, mais pas (encore) suffisant pour PASS
        elif gate.status in ("UNKNOWN", "NOT_AVAILABLE"):
            unknown.append(entry)  # §37 : jamais transformé en PASS par inférence
        elif gate.status == "FAIL":
            blocked.append(entry)
        # NOT_APPLICABLE : gate hors du périmètre actuel — ni proven ni blocked ni unknown, volontairement ignorée.
        if gate.required_action:
            required_next.append({"gate": gate.name, "required_action": gate.required_action})
    return {"proven": proven, "observed": observed, "unknown": unknown, "blocked": blocked, "required_next": required_next}


# ---------------------------------------------------------------------------
# §23 : human review gate.
# ---------------------------------------------------------------------------

def human_review_gate(*, maturity: str, blockers: list, readiness_verdict: str) -> str:
    if maturity == "STATISTICALLY_INFORMATIVE" and not blockers and readiness_verdict in ("CONDITIONALLY_READY", "PRODUCTION_READY"):
        return "READY_FOR_HUMAN_REVIEW"
    return "NOT_READY_FOR_HUMAN_REVIEW"


# ---------------------------------------------------------------------------
# §21/§22 : entry/exit criteria — wrap PUR de constructs déjà établis, jamais un nouveau seuil.
# ---------------------------------------------------------------------------

ENTRY_CRITERIA_CATEGORIES = (
    "MODEL", "MODEL_VERSION", "FEATURES", "DATA", "TEMPORAL_INTEGRITY", "TRACK_RECORD", "PROVENANCE",
    "MONITORING", "ROLLBACK", "SECURITY", "DATABASE_SAFETY", "API_EXPOSURE", "FRONTEND_EXPOSURE", "ODDS", "VALUE",
)


def build_entry_criteria(assessment) -> dict:
    """§21 : conditions objectives par catégorie, avant tout passage de mode — dérivées des gates DÉJÀ
    évaluées (Phase 9), jamais un seuil inventé pour cette phase."""
    by_name = {g.name: g for g in assessment.gates}
    out = {}
    for cat in ENTRY_CRITERIA_CATEGORIES:
        gate = by_name.get(cat)
        if gate is None:
            out[cat] = {"status": "UNKNOWN", "critical": False, "requirement": "Gate non évaluée."}
            continue
        out[cat] = {
            "status": gate.status, "critical": gate.critical,
            "requirement": "Déjà PASS." if gate.status == "PASS" else (gate.required_action or f"status={gate.status}, aucune action documentée."),
        }
    return out


def build_exit_criteria() -> dict:
    """§22 : échelle de maturité (Phase 8M, MATURITY_THRESHOLDS, inchangés) + échelle de mode d'activation
    (Phase 9.3, build_activation_matrix, inchangé) — aucun nouveau seuil (§21)."""
    return {
        "maturity_ladder": {
            "NO_DATA_to_EARLY_DATA": f">= {MATURITY_THRESHOLDS['EARLY_DATA']} observations REAL_PROSPECTIVE + RESOLVED",
            "EARLY_DATA_to_TRACKING": f">= {MATURITY_THRESHOLDS['TRACKING']} observations REAL_PROSPECTIVE + RESOLVED",
            "TRACKING_to_STATISTICALLY_INFORMATIVE": f">= {MATURITY_THRESHOLDS['STATISTICALLY_INFORMATIVE']} observations REAL_PROSPECTIVE + RESOLVED",
        },
        "maturity_ladder_source": "app.ai.shadow.metrics.MATURITY_THRESHOLDS (Phase 8M) — inchangé.",
        "activation_mode_ladder": build_activation_matrix(),
        "activation_mode_ladder_source": "app.ai.shadow.evidence.build_activation_matrix (Phase 9.3) — inchangé.",
    }


# ---------------------------------------------------------------------------
# §35/§36 : verdict — jamais PRODUCTION_READY, jamais de compensation.
# ---------------------------------------------------------------------------

def derive_phase10_verdict(*, preflight_status: str, tests_green: bool, readiness_verdict: str, future_fixtures: int,
                            real_prospective_resolved: int, maturity: str, blockers: list) -> str:
    """§36 : "no compensation" — un readiness NO_GO causé par un gate critique indépendant du volume de
    preuve (ex. ROLLBACK/PROVENANCE) domine TOUJOURS, vérifié avant toute branche de maturité positive."""
    if preflight_status == "FAIL":
        return "BLOCKED"
    if not tests_green:
        return "NEEDS_FIXES"
    if readiness_verdict == "BLOCKED":
        return "BLOCKED"
    if future_fixtures == 0 and real_prospective_resolved == 0:
        return "NO_DATA"
    if real_prospective_resolved == 0:
        return "INSUFFICIENT_REAL_DATA"
    if readiness_verdict == "NO_GO":
        return "NO_GO"
    if maturity == "STATISTICALLY_INFORMATIVE":
        if not blockers and readiness_verdict in ("CONDITIONALLY_READY", "PRODUCTION_READY"):
            return "READY_FOR_HUMAN_REVIEW"
        return "STATISTICALLY_INFORMATIVE"
    if maturity == "TRACKING":
        return "TRACKING"
    if maturity == "EARLY_DATA":
        return "EARLY_EVIDENCE"
    return "INSUFFICIENT_REAL_DATA"
