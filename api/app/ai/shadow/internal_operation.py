"""
api/app/ai/shadow/internal_operation.py — Phase 11 : XFOOT CONTROLLED
INTERNAL OPERATION & REAL EVIDENCE ACCUMULATION V1.

RÉUTILISE TEL QUEL (jamais réimplémenté, §1 : "ne pas réimplémenter") :
  - app.ai.shadow.tracking.ShadowDecisionStore (Phase 8K).
  - app.ai.shadow.prospective.run_prospective_capture (Phase 9.2/8M).
  - app.ai.shadow.monitoring.compute_shadow_health (Phase 8N).
  - app.ai.shadow.evidence.build_activation_matrix (Phase 9.3).
  - app.ai.shadow.watch.EvidenceHistoryStore/compute_blocker_evolution (Phase 9.5).
  - app.ai.readiness.matrix.evaluate_production_readiness (Phase 9).
  - app.ai.readiness.human_review (Phase 10).
  - app.ai.safety.kill_switch/operations.run_preflight_safety (Phase 9.1/9.4).

Ce module n'ajoute QUE ce qui n'existe pas déjà :
  1. OPERATING_MODE / assert_mode_1_only() — §4 : refus EXPLICITE de tout
     mode != MODE_1_SHADOW_ONLY, en défense en profondeur — le runner
     (scripts/internal_shadow_operation.py) n'expose d'ailleurs JAMAIS de
     paramètre --mode (§4 : "imposé par configuration, aucune variable
     utilisateur ne doit pouvoir contourner cette restriction") ; cette
     fonction reste néanmoins directement testable contre une tentative de
     contournement (MODE_2/MODE_3/MODE_4).
  2. evaluate_mode2_conditions() — §19/§38 : évaluation DOCUMENTARY_ONLY des
     prérequis MODE_2 (réutilise build_activation_matrix, Phase 9.3, tel
     quel) — ne sélectionne, n'active, ni ne recommande jamais MODE_2.
  3. compare_to_phase10_baseline() — §25/§39 : IMPROVED/UNCHANGED/REGRESSED
     par dimension, comparé indépendamment (§39 : "sans compensation") —
     PUR, ne recalcule rien, prend les valeurs déjà produites par les deux
     runs (Phase 10 et ce run) en paramètres.

STRICTEMENT SHADOW ONLY : aucune fonction ici n'appelle un modèle, n'écrit
en DB de production, ni ne trigger/reset le Kill Switch/promeut un modèle.
"""

from __future__ import annotations

from app.ai.shadow.evidence import build_activation_matrix

# ---------------------------------------------------------------------------
# §4 : MODE ENFORCEMENT — refus explicite, jamais contournable.
# ---------------------------------------------------------------------------

OPERATING_MODE = "MODE_1_SHADOW_ONLY"


def assert_mode_1_only(mode: str) -> None:
    """§4 : rejette EXPLICITEMENT tout mode != MODE_1_SHADOW_ONLY. Le runner réel n'appelle cette fonction
    qu'avec la constante OPERATING_MODE ci-dessus (jamais une valeur dérivée d'un argument utilisateur, §4 :
    "aucune variable utilisateur ne doit pouvoir contourner cette restriction") — testée ici directement
    contre des tentatives de contournement pour prouver que le refus est réel, pas seulement documenté."""
    if mode != "MODE_1_SHADOW_ONLY":
        raise ValueError(f"REFUSED: seul MODE_1_SHADOW_ONLY est autorisé dans cette phase (§4) — mode demandé : '{mode}'.")


# ---------------------------------------------------------------------------
# §19/§38 : MODE_2 — ÉVALUATION UNIQUEMENT, DOCUMENTARY_ONLY, jamais activé.
# ---------------------------------------------------------------------------

def evaluate_mode2_conditions(assessment) -> dict:
    """§38 : "Cette phase peut UNIQUEMENT ÉVALUER si les conditions du MODE_2 semblent réunies. Elle ne doit
    jamais l'activer." Réutilise build_activation_matrix (Phase 9.3) tel quel pour les prérequis — jamais un
    nouveau seuil (§24 du prompt Phase 11 : "ne pas inventer de seuil")."""
    matrix = build_activation_matrix()
    mode2 = matrix["MODE_2_LIMITED_INTERNAL"]
    by_name = {g.name: g for g in assessment.gates}
    required = mode2["critical_gates_required"]
    unmet = [name for name in required if by_name.get(name) is None or by_name[name].status != "PASS"]
    return {
        "mode": "MODE_2_LIMITED_INTERNAL", "documentary_only": True, "activated": False,
        "conditions_met": not unmet, "required_gates": list(required), "unmet_gates": unmet,
        "description": mode2["description"], "minimum_evidence": mode2["minimum_evidence"],
        "note": "ÉVALUATION UNIQUEMENT (§19/§38) — jamais activé automatiquement par cette phase, quel que soit le résultat.",
    }


# ---------------------------------------------------------------------------
# §25/§39 : comparaison à la baseline Phase 10 — PUR, sans compensation.
# ---------------------------------------------------------------------------

_READINESS_VERDICT_RANK = {"BLOCKED": 0, "NO_GO": 1, "CONDITIONALLY_READY": 2, "PRODUCTION_READY": 3}


def _rank_delta(old, new) -> str:
    o, n = _READINESS_VERDICT_RANK.get(old), _READINESS_VERDICT_RANK.get(new)
    if o is None or n is None:
        return "UNCHANGED"
    if n > o:
        return "IMPROVED"
    if n < o:
        return "REGRESSED"
    return "UNCHANGED"


def _count_delta(old: int, new: int) -> str:
    if new > old:
        return "IMPROVED"
    if new < old:
        return "REGRESSED"
    return "UNCHANGED"


def _gate_status_delta(old_status, new_status) -> str:
    """§39 : sans échelle inventée entre deux statuts non-PASS (ex. FAIL -> NOT_AVAILABLE n'est ni un gain ni
    une perte prouvée) — seule une transition DEPUIS/VERS PASS est classée IMPROVED/REGRESSED."""
    if old_status == new_status:
        return "UNCHANGED"
    if new_status == "PASS":
        return "IMPROVED"
    if old_status == "PASS":
        return "REGRESSED"
    return "UNCHANGED"


def compare_to_phase10_baseline(*, current_readiness_verdict: str, baseline_readiness_verdict: str,
                                 current_real_prospective_count: int, baseline_real_prospective_count: int,
                                 current_track_record_sample_size: int, baseline_track_record_sample_size: int,
                                 current_provenance_complete: int, baseline_provenance_complete: int,
                                 current_gate_statuses: dict, baseline_gate_statuses: dict) -> dict:
    """§25/§39 : chaque dimension comparée INDÉPENDAMMENT — un gain sur l'une ne masque jamais une régression
    sur une autre (§39 : "sans compensation")."""
    gate_names = set(baseline_gate_statuses) | set(current_gate_statuses)
    gate_deltas = {name: _gate_status_delta(baseline_gate_statuses.get(name), current_gate_statuses.get(name)) for name in gate_names}
    return {
        "readiness_verdict": {"baseline": baseline_readiness_verdict, "current": current_readiness_verdict,
                               "delta": _rank_delta(baseline_readiness_verdict, current_readiness_verdict)},
        "real_prospective_evidence": {"baseline": baseline_real_prospective_count, "current": current_real_prospective_count,
                                       "delta": _count_delta(baseline_real_prospective_count, current_real_prospective_count)},
        "track_record_sample_size": {"baseline": baseline_track_record_sample_size, "current": current_track_record_sample_size,
                                      "delta": _count_delta(baseline_track_record_sample_size, current_track_record_sample_size)},
        "provenance_complete": {"baseline": baseline_provenance_complete, "current": current_provenance_complete,
                                 "delta": _count_delta(baseline_provenance_complete, current_provenance_complete)},
        "critical_gates": gate_deltas,
        "note": "§39 : chaque dimension comparée indépendamment, sans compensation.",
    }
