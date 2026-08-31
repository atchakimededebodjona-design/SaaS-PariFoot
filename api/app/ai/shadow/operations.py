"""
api/app/ai/shadow/operations.py — Phase 9.4 : XFOOT REAL PROSPECTIVE SHADOW
OPERATIONS & EVIDENCE COLLECTION V1.

RÉUTILISE TEL QUEL (jamais réimplémenté, §1 : "inspecter avant modification") :
  - app.ai.shadow.tracking.ShadowDecisionStore (Phase 8K/8M) — store integrity
    via .load() (déjà durci : ValueError explicite sur corruption).
  - app.ai.shadow.prospective.compute_as_of_window_label (Phase 9.2) —
    étiquetage de fenêtre, jamais recalculé différemment.
  - app.ai.readiness.matrix.evaluate_production_readiness (Phase 9).
  - app.ai.safety.kill_switch.KillSwitchStore (Phase 9.1 — LECTURE SEULE ici,
    jamais trigger()/reset()).

Ce module n'ajoute QUE ce qui n'existe pas déjà :

  1. run_preflight_safety() — §3/§40 : combine Kill Switch (lecture),
     Production Readiness (évaluée sans erreur), MODE_1_SHADOW_ONLY
     (assertion structurelle), Store integrity, DB accessibility en UNE
     SEULE décision GO/STOP. Chacun de ces contrôles existe déjà séparément
     ailleurs (safety/readiness/tracking) — aucun n'était assemblé en un
     pré-vol opérationnel unique avant cette phase.
  2. summarize_multi_as_of_runs() — §7 : regroupe/étiquette les observations
     RÉELLES déjà présentes dans le store, capturées à des `as_of`
     RÉELLEMENT distincts (fournis un par un par l'opérateur au fil
     d'exécutions séparées de scripts/shadow_operations.py — JAMAIS générés
     artificiellement dans un seul run, §7 : "ne pas créer artificiellement
     plusieurs snapshots"). Réutilise compute_as_of_window_label (Phase 9.2)
     tel quel — n'invente aucune nouvelle notion de fenêtre.

STRICTEMENT SHADOW ONLY : aucune fonction ici n'appelle un modèle, n'écrit
en base ni dans le Shadow Store, ni ne trigger/reset le Kill Switch.
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, select

from app.ai.shadow.tracking import ShadowDecisionStore
from app.ai.shadow.prospective import compute_as_of_window_label
from app.ai.readiness.matrix import evaluate_production_readiness
from app.ai.safety.kill_switch import KillSwitchStore

PREFLIGHT_STATUSES = ("PASS", "FAIL")
PREFLIGHT_BLOCKING_CODES = ("STORE_CORRUPTION", "DB_UNREACHABLE", "KILL_SWITCH_UNREADABLE", "READINESS_EVALUATION_ERROR")

# §48 : vocabulaire de verdict final — PRODUCTION_READY est STRUCTURELLEMENT ABSENT (§49 : jamais un verdict possible ici).
FINAL_VERDICTS = (
    "NO_DATA", "INSUFFICIENT_REAL_DATA", "SHADOW_OPERATIONAL", "EARLY_EVIDENCE", "TRACKING",
    "STATISTICALLY_INFORMATIVE", "READY_FOR_HUMAN_REVIEW", "NEEDS_FIXES", "BLOCKED",
)


# ---------------------------------------------------------------------------
# §3 : pré-vol — GO/STOP unique, jamais un contournement partiel.
# ---------------------------------------------------------------------------

def run_preflight_safety(session: Session, store: ShadowDecisionStore, kill_switch_store: KillSwitchStore, as_of: datetime) -> dict:
    """
    §3/§40 : vérifie Kill Switch / Production Readiness / MODE_1_SHADOW_ONLY /
    Store integrity / DB accessibility. `status="FAIL"` -> l'appelant DOIT
    interrompre toute opération (§3 : "aucune tentative de contournement").

    Nuances documentées (jamais un blocage aveugle) :
      - Un Kill Switch TRIGGERED n'est PAS bloquant ici : SHADOW_RESEARCH est
        dans NEVER_BLOCKED_SCOPES (Phase 9.1, safety/schemas.py) — seule une
        LECTURE qui échoue (fichier corrompu/illisible) est bloquante.
      - Le VERDICT de Production Readiness (souvent NO_GO, §33/§34) n'est
        JAMAIS bloquant : accumuler de la preuve Shadow alors que la
        readiness est NO_GO est précisément le but de cette phase. Seule une
        ÉVALUATION QUI LÈVE UNE EXCEPTION est bloquante.
    """
    checks: dict = {}
    blocking: list[str] = []

    try:
        store.load()
        checks["store_integrity"] = {"status": "PASS"}
    except ValueError as e:
        checks["store_integrity"] = {"status": "FAIL", "reason": str(e)}
        blocking.append("STORE_CORRUPTION")

    try:
        session.exec(select(1)).one()
        checks["db_accessibility"] = {"status": "PASS"}
    except Exception as e:  # noqa: BLE001 — §42 : erreur isolée et catégorisée, jamais une exception qui remonte non traitée
        checks["db_accessibility"] = {"status": "FAIL", "reason": str(e)[:200]}
        blocking.append("DB_UNREACHABLE")

    try:
        ks_state = kill_switch_store.read()
        checks["kill_switch"] = {
            "status": "PASS", "state": ks_state.state, "effective_status": ks_state.effective_status,
            "note": "SHADOW_RESEARCH est dans NEVER_BLOCKED_SCOPES (Phase 9.1, safety/schemas.py) — un état "
                    "TRIGGERED est rapporté ici, jamais bloquant pour des opérations Shadow.",
        }
    except ValueError as e:
        checks["kill_switch"] = {"status": "FAIL", "reason": str(e)}
        blocking.append("KILL_SWITCH_UNREADABLE")

    readiness_assessment = None
    if "STORE_CORRUPTION" not in blocking and "DB_UNREACHABLE" not in blocking:
        try:
            readiness_assessment = evaluate_production_readiness(session, store, as_of)
            checks["production_readiness"] = {
                "status": "PASS", "verdict": readiness_assessment.final_verdict,
                "note": "Évaluée sans erreur — le VERDICT (fréquemment NO_GO) n'est jamais bloquant ici : "
                        "accumuler de la preuve Shadow est précisément le but de cette phase.",
            }
        except Exception as e:  # noqa: BLE001
            checks["production_readiness"] = {"status": "FAIL", "reason": str(e)[:200]}
            blocking.append("READINESS_EVALUATION_ERROR")
    else:
        checks["production_readiness"] = {"status": "SKIPPED", "reason": "Store corrompu ou DB inaccessible — l'évaluation readiness en dépend."}

    checks["mode"] = {
        "status": "PASS", "value": "MODE_1_SHADOW_ONLY",
        "note": "Assertion structurelle — aucun chemin de code d'activation production n'existe dans ce dépôt (§49).",
    }

    status = "FAIL" if blocking else "PASS"
    return {"status": status, "blocking": blocking, "checks": checks, "readiness_assessment": readiness_assessment, "as_of": as_of.isoformat()}


# ---------------------------------------------------------------------------
# §7 : multi-as_of — regroupement PUREMENT informatif d'observations réelles.
# ---------------------------------------------------------------------------

def summarize_multi_as_of_runs(entries: list[tuple]) -> dict:
    """
    §7 : regroupe par (match_id, market, model_type) les `as_of` RÉELLEMENT
    distincts déjà présents dans le store — issus d'exécutions SÉPARÉES de
    l'opérateur, jamais générés dans ce run — et les étiquette via
    compute_as_of_window_label (Phase 9.2). PUREMENT informatif : le kickoff
    utilisé reste un placeholder (minuit de match_date), jamais une preuve de
    fenêtre exacte (voir prospective.py).
    """
    groups: dict[tuple, list[dict]] = {}
    for record, _ in entries:
        key = (record.match_id, record.market, record.model_type)
        match_date = record.kickoff.date() if record.kickoff else None
        label = compute_as_of_window_label(record.as_of, match_date)
        groups.setdefault(key, []).append({
            "shadow_id": record.shadow_id,
            "as_of": record.as_of.isoformat() if record.as_of else None,
            "window_label": label,
        })

    multi_as_of_matches = {
        f"{k[0]}|{k[1]}|{k[2]}": sorted(v, key=lambda x: x["as_of"] or "")
        for k, v in groups.items() if len({o["as_of"] for o in v}) > 1
    }
    return {
        "distinct_match_market_model_combinations": len(groups),
        "combinations_with_multiple_as_of": len(multi_as_of_matches),
        "detail": multi_as_of_matches,
        "note": "Chaque as_of est fourni explicitement par l'opérateur à une exécution séparée de "
                "scripts/shadow_operations.py (§7) — jamais généré artificiellement au sein d'un seul run.",
    }


# ---------------------------------------------------------------------------
# §48 : dérivation du verdict final — fonction PURE, testable isolément et
# réutilisée telle quelle par scripts/shadow_operations.py (jamais un
# if/elif dupliqué entre le module et le script).
# ---------------------------------------------------------------------------

def derive_final_verdict(
    *, preflight_status: str, tests_green: bool, capture_blocked: bool, candidates: int,
    total_real_observations: int, maturity: str, blockers_after: list, readiness_after_verdict: str,
) -> str:
    """§48 : jamais PRODUCTION_READY (§49) — absent de FINAL_VERDICTS, donc structurellement inatteignable."""
    if preflight_status == "FAIL":
        return "BLOCKED"
    if not tests_green:
        return "NEEDS_FIXES"
    if capture_blocked:
        return "BLOCKED"
    if candidates == 0 and total_real_observations == 0:
        return "NO_DATA"
    if total_real_observations == 0:
        return "INSUFFICIENT_REAL_DATA"
    if maturity == "STATISTICALLY_INFORMATIVE" and not blockers_after and readiness_after_verdict in ("CONDITIONALLY_READY", "PRODUCTION_READY"):
        return "READY_FOR_HUMAN_REVIEW"
    if maturity in ("TRACKING", "STATISTICALLY_INFORMATIVE"):
        return "SHADOW_OPERATIONAL"
    if maturity == "EARLY_DATA":
        return "EARLY_EVIDENCE"
    return "INSUFFICIENT_REAL_DATA"
