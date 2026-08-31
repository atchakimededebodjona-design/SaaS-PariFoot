"""
api/app/ai/shadow/prospective.py — Phase 9.2 : XFOOT PROSPECTIVE SHADOW
ACTIVATION & EVIDENCE ACCUMULATION V1.

RÉUTILISE TEL QUEL (jamais réimplémenté, §1 : "inspecter avant de créer") :
  - api/app/ai/shadow/live.py::discover_live_candidates/assess_capture_eligibility/
    build_pipeline_input_for_live/check_production_consistency (Phase 8M).
  - api/app/ai/pipeline/orchestrator.py::run_pipeline (Phase 8J).
  - api/app/ai/shadow/tracking.py::ShadowDecisionStore/capture_shadow_decision (Phase 8K/8M).
  - api/app/ai/shadow/resolution.py::resolve_record/find_candidate_results (Phase 8K).
  - api/app/ai/shadow/metrics.py::compute_shadow_track_record/classify_maturity/value_tracking_status (Phase 8K/8M/8N).

Ce module n'ajoute QUE ce qui n'existe pas déjà :
  1. is_real_prospective() — définition formelle §11, jamais fabriquée.
  2. compute_as_of_window_label() — étiquette de fenêtre, PUREMENT informative
     (§4) — le "kickoff" utilisé partout ailleurs dans ce dépôt (live.py,
     resolution.py) est TOUJOURS `datetime.combine(match_date, time.min)`,
     un PLACEHOLDER à minuit, jamais une heure de coup d'envoi réelle
     (confirmé : ModelPrediction.match_date est typé `date`, zéro heure
     possible — voir api/app/models/model_prediction.py). Cette fonction
     n'invente donc jamais un "T-Xh avant coup d'envoi" comme preuve —
     seulement un intervalle informatif entre `as_of` et ce placeholder,
     explicitement documenté comme tel partout où il est utilisé.
  3. run_prospective_capture() — le run réel, UN as_of par appel (§4 :
     "plusieurs snapshots" est une CAPACITÉ testée avec des as_of successifs
     explicites — jamais une génération artificielle de plusieurs
     horodatages "futurs" en un seul run réel, ce qui reviendrait à
     fabriquer une fenêtre autour d'un kickoff dont on ne connaît pas
     l'heure réelle). Protégé par un verrou fichier exclusif (§29).
  4. compute_evidence_ledger() — §18.
  5. classify_capture_quality() — §19.
  6. backup_store()/restore_and_validate() — §27.
  7. compute_readiness_impact() — §49 (pure diff, ne recalcule rien de Phase 9).

STRICTEMENT SHADOW ONLY : aucune fonction ici n'appelle un modèle, n'écrit
dans model_predictions/prediction_log/match/model_versions, ni n'importe
scheduler.py/main.py.
"""

from __future__ import annotations

import os
import shutil
from contextlib import contextmanager
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Optional

from sqlmodel import Session

from app.ai.pipeline.orchestrator import run_pipeline
from app.ai.shadow.live import discover_live_candidates, build_pipeline_input_for_live, check_production_consistency
from app.ai.shadow.tracking import ShadowDecisionStore, capture_shadow_decision
from app.ai.shadow.metrics import classify_maturity

UTC = timezone.utc

MARKET_DEFAULT_SELECTION = {"1X2": "home_win", "BTTS": "yes", "OVER_UNDER_2_5": "over"}

# ---------------------------------------------------------------------------
# §11 : REAL_PROSPECTIVE — définition formelle. Jamais une preuve inventée
# quand le kickoff exact est indisponible (ce qui est TOUJOURS le cas ici).
# ---------------------------------------------------------------------------

PROSPECTIVE_TIMING_STATUSES = ("CONSISTENT_WITH_PLACEHOLDER_KICKOFF", "TIMING_VIOLATION", "UNKNOWN")


def is_real_prospective(
    *, capture_timestamp: Optional[datetime], match_date: Optional[date],
    prediction_timestamp: Optional[datetime], as_of: Optional[datetime],
) -> tuple[str, str]:
    """
    §11 : (status, reason). Vérifie capture_timestamp < kickoff ET
    prediction_timestamp <= as_of < kickoff — MAIS le "kickoff" disponible
    dans ce dépôt est TOUJOURS un placeholder à minuit (match_date, jamais
    une heure réelle, voir docstring module) : le status positif retourné
    est donc explicitement `CONSISTENT_WITH_PLACEHOLDER_KICKOFF`, JAMAIS un
    `REAL_PROSPECTIVE` non qualifié qui prétendrait à une preuve fondée sur
    une heure de coup d'envoi vérifiée (§11 : "si kickoff exact indisponible
    -> ne pas prétendre à une preuve prospective").
    """
    if capture_timestamp is None or match_date is None or prediction_timestamp is None or as_of is None:
        return "UNKNOWN", "Un ou plusieurs timestamps requis sont absents — jamais supposés."

    kickoff_placeholder = datetime.combine(match_date, time.min, tzinfo=UTC)
    ct = capture_timestamp if capture_timestamp.tzinfo else capture_timestamp.replace(tzinfo=UTC)
    pt = prediction_timestamp if prediction_timestamp.tzinfo else prediction_timestamp.replace(tzinfo=UTC)
    ao = as_of if as_of.tzinfo else as_of.replace(tzinfo=UTC)

    violations = []
    if not (ct < kickoff_placeholder):
        violations.append("capture_timestamp >= kickoff_placeholder")
    if not (pt <= ao):
        violations.append("prediction_timestamp > as_of")
    if not (ao < kickoff_placeholder):
        violations.append("as_of >= kickoff_placeholder")

    if violations:
        return "TIMING_VIOLATION", f"Violations : {violations}."
    return "CONSISTENT_WITH_PLACEHOLDER_KICKOFF", (
        "Ordre des timestamps cohérent avec une capture prospective, mais le kickoff utilisé est un "
        "placeholder à minuit (match_date), jamais une heure de coup d'envoi réelle vérifiée — voir "
        "KICKOFF_TIME_UNKNOWN (Phase 8N). Jamais présenté comme une preuve prospective à heure exacte."
    )


# ---------------------------------------------------------------------------
# §4 : étiquette de fenêtre — PUREMENT informative, jamais une preuve.
# ---------------------------------------------------------------------------

WINDOW_LABELS = ("T_MINUS_24H_OR_MORE", "T_MINUS_12H_TO_24H", "T_MINUS_6H_TO_12H", "T_MINUS_1H_TO_6H", "UNDER_T_MINUS_1H", "AT_OR_AFTER_KICKOFF_PLACEHOLDER", "UNKNOWN")


def compute_as_of_window_label(as_of: Optional[datetime], match_date: Optional[date]) -> str:
    """§4/§20 : bucket informatif — calculé contre le MÊME placeholder de
    kickoff que le reste du dépôt (minuit de match_date), jamais une heure
    réelle. Ne sert JAMAIS à générer artificiellement plusieurs as_of dans
    un run réel — uniquement à ÉTIQUETER un as_of réellement fourni."""
    if as_of is None or match_date is None:
        return "UNKNOWN"
    kickoff_placeholder = datetime.combine(match_date, time.min, tzinfo=UTC)
    ao = as_of if as_of.tzinfo else as_of.replace(tzinfo=UTC)
    delta_hours = (kickoff_placeholder - ao).total_seconds() / 3600.0
    if delta_hours <= 0:
        return "AT_OR_AFTER_KICKOFF_PLACEHOLDER"
    if delta_hours >= 24:
        return "T_MINUS_24H_OR_MORE"
    if delta_hours >= 12:
        return "T_MINUS_12H_TO_24H"
    if delta_hours >= 6:
        return "T_MINUS_6H_TO_12H"
    if delta_hours >= 1:
        return "T_MINUS_1H_TO_6H"
    return "UNDER_T_MINUS_1H"


# ---------------------------------------------------------------------------
# §29 : verrou fichier exclusif — en cas d'ambiguïté (verrou déjà pris), BLOCK,
# jamais une écriture concurrente non protégée.
# ---------------------------------------------------------------------------

DEFAULT_LOCK_PATH = Path(__file__).resolve().parents[4] / "reports" / "shadow" / "prospective_capture.lock"


@contextmanager
def acquire_capture_lock(lock_path: Path = DEFAULT_LOCK_PATH):
    """§29/§30 : `os.O_CREAT | os.O_EXCL` — création atomique, échoue si le
    fichier existe déjà (portable Windows/POSIX). Yield `True` si acquis
    (le bloc `with` s'exécute), sinon `False` (l'appelant DOIT traiter
    comme BLOCK, §29 : "en cas d'ambiguïté -> BLOCK") — jamais de nettoyage
    automatique d'un verrou existant (§9/§27 : jamais réparer
    automatiquement, un verrou périmé se supprime manuellement)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(os.getpid()).encode("utf-8"))
        finally:
            os.close(fd)
        acquired = True
    except FileExistsError:
        acquired = False
    except BaseException:
        # Toute autre erreur pendant la création (ex. disque plein) : ne jamais laisser un verrou fantôme.
        lock_path.unlink(missing_ok=True)
        raise
    try:
        yield acquired
    finally:
        if acquired:
            lock_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# §2/§3/§5/§6/§7/§8 : run réel — UN as_of, jamais une prédiction recalculée.
# ---------------------------------------------------------------------------

def run_prospective_capture(
    session: Session, store: ShadowDecisionStore, as_of: datetime, *,
    dry_run: bool = False, market: str = "1X2", lock_path: Path = DEFAULT_LOCK_PATH,
) -> dict:
    """
    §2 : consomme UNIQUEMENT model_predictions déjà produites par la
    production (discover_live_candidates) — AUCUN appel modèle ici.
    §29 : protégé par un verrou exclusif — si déjà pris par un autre run,
    retourne immédiatement `{"blocked": True, "reason": "LOCK_HELD"}`,
    AUCUNE écriture, même en dry_run (§29 : ambiguïté -> BLOCK).
    """
    with acquire_capture_lock(lock_path) as acquired:
        if not acquired:
            return {"blocked": True, "reason": "LOCK_HELD", "as_of": as_of.isoformat(), "candidates": 0,
                     "captured": 0, "duplicates_prevented": 0, "rejected": [], "errors": [], "mismatches": [], "captured_records": []}

        selection = MARKET_DEFAULT_SELECTION.get(market)
        if selection is None:
            return {"blocked": True, "reason": f"UNKNOWN_MARKET:{market}", "as_of": as_of.isoformat(), "candidates": 0,
                     "captured": 0, "duplicates_prevented": 0, "rejected": [], "errors": [], "mismatches": [], "captured_records": []}

        candidates = discover_live_candidates(session, as_of)
        outcome = {
            "blocked": False, "as_of": as_of.isoformat(), "candidates": len(candidates), "captured": 0,
            "duplicates_prevented": 0, "rejected": [], "errors": [], "mismatches": [], "captured_records": [],
        }

        for mp in candidates:
            pi, diagnostics = build_pipeline_input_for_live(session, mp, market, selection, as_of)
            if pi is None:
                outcome["rejected"].append(diagnostics)
                continue
            try:
                assessment = run_pipeline(pi)
            except Exception as e:  # noqa: BLE001 — §31/§25 Phase 8M : isolation, jamais interrompre le run
                outcome["errors"].append({"match": diagnostics["match"], "error_category": "PIPELINE_CRITICAL_ERROR", "message": str(e)[:200]})
                continue

            mismatches = check_production_consistency(mp, pi, assessment)
            if mismatches:
                outcome["mismatches"].append({"match": diagnostics["match"], "mismatches": mismatches})
                continue  # §6/§37 Phase 8M : jamais corrigé silencieusement

            capture_ts = datetime.now(UTC)
            prospective_status, prospective_reason = is_real_prospective(
                capture_timestamp=capture_ts, match_date=mp.match_date, prediction_timestamp=mp.predicted_at, as_of=as_of,
            )
            window_label = compute_as_of_window_label(as_of, mp.match_date)

            if dry_run:
                outcome["captured"] += 1  # "aurait été capturé", AUCUNE écriture
                outcome["captured_records"].append({"match": diagnostics["match"], "dry_run": True,
                                                      "prospective_status": prospective_status, "window_label": window_label})
                continue

            record, created = capture_shadow_decision(store, pi, assessment, home_team=mp.home_team, away_team=mp.away_team, data_marking="REAL")
            if created:
                outcome["captured"] += 1
                outcome["captured_records"].append({
                    "shadow_id": record.shadow_id, "status": record.status, "eligibility": record.eligibility,
                    "prospective_status": prospective_status, "prospective_reason": prospective_reason, "window_label": window_label,
                })
            else:
                outcome["duplicates_prevented"] += 1

        if not dry_run:
            store.save()
        return outcome


# ---------------------------------------------------------------------------
# §18 : Evidence Ledger — dérivé du store, jamais une nouvelle table.
# ---------------------------------------------------------------------------

def compute_evidence_ledger(entries: list[tuple]) -> dict:
    real_entries = [(r, res) for r, res in entries if r.data_marking == "REAL"]
    by_status = {}
    for _, res in real_entries:
        by_status[res.result_status] = by_status.get(res.result_status, 0) + 1
    resolved_count = by_status.get("RESOLVED", 0)
    kickoffs = [r.kickoff for r, _ in real_entries if r.kickoff is not None]
    as_ofs = [r.as_of for r, _ in real_entries if r.as_of is not None]
    return {
        "total_real_observations": len(real_entries),
        "by_resolution_status": by_status,
        "conflicts": by_status.get("CONFLICT", 0),
        "distinct_models": sorted({r.model_type for r, _ in real_entries if r.model_type}),
        "distinct_leagues": sorted({r.league for r, _ in real_entries if r.league}),
        "distinct_markets": sorted({r.market for r, _ in real_entries if r.market}),
        "period_covered": {"earliest_kickoff": min(kickoffs).isoformat() if kickoffs else None, "latest_kickoff": max(kickoffs).isoformat() if kickoffs else None},
        "last_observation_captured_at": (max(r.created_at for r, _ in real_entries).isoformat() if real_entries else None),
        "maturity": classify_maturity(resolved_count),
        "as_of_span": {"earliest": min(as_ofs).isoformat() if as_ofs else None, "latest": max(as_ofs).isoformat() if as_ofs else None},
    }


# ---------------------------------------------------------------------------
# §19 : capture quality — catégories mutuellement exclusives.
# ---------------------------------------------------------------------------

CAPTURE_QUALITY_CATEGORIES = ("CAPTURED", "REJECTED", "BLOCKED", "CONFLICT", "RESOLVED", "PENDING")


def classify_capture_quality(capture_outcome: dict, entries: list[tuple]) -> dict:
    return {
        "CAPTURED": capture_outcome.get("captured", 0),
        "REJECTED": len(capture_outcome.get("rejected", [])) + len(capture_outcome.get("mismatches", [])),
        "BLOCKED": 1 if capture_outcome.get("blocked") else 0,
        "CONFLICT": sum(1 for _, res in entries if res.result_status == "CONFLICT"),
        "RESOLVED": sum(1 for _, res in entries if res.result_status == "RESOLVED"),
        "PENDING": sum(1 for _, res in entries if res.result_status == "PENDING"),
    }


# ---------------------------------------------------------------------------
# §27 : backup / recovery — jamais sur l'original, toujours une copie.
# ---------------------------------------------------------------------------

def backup_store(store: ShadowDecisionStore, dest_dir: Path) -> Path:
    """§27 : copie brute (jamais un ré-encodage) — préserve les octets exacts."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    dest = dest_dir / f"{store.path.stem}_backup_{ts}.json"
    if store.path.exists():
        shutil.copy2(store.path, dest)
    else:
        dest.write_text("{}", encoding="utf-8")
    return dest


def restore_and_validate(backup_path: Path, restore_to: Path) -> ShadowDecisionStore:
    """§27 : restaure vers un NOUVEAU chemin (jamais l'original directement) puis VALIDE via `.load()`
    (§9 Phase 8K/8M : la même détection de corruption, jamais une deuxième logique)."""
    shutil.copy2(backup_path, restore_to)
    restored = ShadowDecisionStore(path=restore_to)
    restored.load()  # lève ValueError si corrompu — jamais un succès silencieux sur une copie invalide.
    return restored


# ---------------------------------------------------------------------------
# §49 : impact sur la readiness Phase 9 — diff PUR, ne recalcule rien.
# ---------------------------------------------------------------------------

READINESS_IMPACT_GATES = ("TRACK_RECORD", "PROVENANCE", "MONITORING", "SHADOW", "TEMPORAL_INTEGRITY", "ODDS", "VALUE")


def compute_readiness_impact(gates_before: list, gates_after: list) -> dict:
    before_by_name = {g.name: g.status for g in gates_before if g.name in READINESS_IMPACT_GATES}
    after_by_name = {g.name: g.status for g in gates_after if g.name in READINESS_IMPACT_GATES}
    impact = {}
    for name in READINESS_IMPACT_GATES:
        b, a = before_by_name.get(name, "UNKNOWN"), after_by_name.get(name, "UNKNOWN")
        impact[name] = {"before": b, "after": a, "changed": b != a}
    return impact
