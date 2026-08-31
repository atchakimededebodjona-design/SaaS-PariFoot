"""
api/app/ai/shadow/watch.py — Phase 9.5 : XFOOT SHADOW EVIDENCE WATCH &
LONGITUDINAL TRACKING V1.

RÉUTILISE TEL QUEL (jamais réimplémenté, §1 : "ne pas dupliquer") :
  - app.ai.shadow.tracking.ShadowDecisionStore (Phase 8K/8M).
  - app.ai.shadow.monitoring.compute_shadow_health (Phase 8N).
  - app.ai.shadow.metrics.compute_shadow_track_record/classify_maturity (Phase 5/7/8K/8M).
  - app.ai.shadow.evidence.classify_data_marking/compute_full_evidence_ledger/
    identify_activation_blockers/compute_breakdown/compute_model_version_tracking (Phase 9.3).
  - app.ai.shadow.operations.run_preflight_safety (Phase 9.4 — préflight RÉUTILISÉ tel quel,
    jamais un second mécanisme : les contrôles §3 de cette phase sont IDENTIQUES à ceux
    de Phase 9.4, voir sa docstring).
  - app.ai.readiness.matrix.evaluate_production_readiness (Phase 9).
  - app.ai.safety.guards.can_activate_production / kill_switch.KillSwitchStore (Phase 9.1, LECTURE SEULE).

Ce module n'ajoute QUE ce qui n'existe pas déjà :
  1. EvidenceHistoryStore — §8 : historique longitudinal des snapshots, fichier
     JSON dédié (reports/shadow/watch/evidence_snapshots.json), append-only,
     écriture atomique (même idiome que ShadowDecisionStore.save()/
     KillSwitchStore._atomic_write_json — un idiome déjà répété 3x dans ce
     dépôt pour des fichiers d'état indépendants, jamais partagé entre eux
     par construction). AUCUNE table DB créée (§8).
  2. compute_evidence_snapshot() — §7 : reshape PUR des sorties déjà produites
     par compute_shadow_health/compute_full_evidence_ledger — n'invente aucune
     nouvelle mesure.
  3. filter_real_prospective_entries() — §10 : filtre les entrées du store à
     la classe REAL_PROSPECTIVE (via classify_data_marking, Phase 9.3, réutilisée
     telle quelle) AVANT de les passer à compute_shadow_track_record — nécessaire
     car Phase 9.3 exposait déjà le COMPTAGE par classe mais jamais un sous-
     ensemble filtré prêt à nourrir un track record (§10 : "ne jamais utiliser
     HISTORICAL/SYNTHETIC comme preuve prospective").
  4. compute_evidence_trend() — §9 : comparaison PURE entre snapshots déjà
     persistés — TREND=INSUFFICIENT_DATA tant que < 2 snapshots existent,
     jamais une tendance inventée sur un seul point.
  5. compute_blocker_evolution() — §18 : NEW/PERSISTING/CLEARED/REGRESSED,
     dérivés de la liste réelle de blockers de chaque snapshot passé
     (identify_activation_blockers, Phase 9.3, réutilisée telle quelle) —
     BASELINE_ONLY tant qu'aucun historique n'existe.
  6. derive_watch_verdict() — §40 : vocabulaire de verdict PROPRE à cette
     phase (8 valeurs, sans SHADOW_OPERATIONAL contrairement à Phase 9.4 —
     volontairement un vocabulaire différent, jamais une réutilisation
     partielle qui produirait une valeur hors de la liste autorisée §40).

STRICTEMENT OBSERVATION : aucune fonction ici n'appelle un modèle, n'écrit
dans une table de production, ni ne trigger/reset le Kill Switch. Seule
EvidenceHistoryStore écrit — et uniquement son propre fichier dédié, jamais
le Shadow Store ni une table SQL.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from app.ai.shadow.evidence import classify_data_marking, identify_activation_blockers

DEFAULT_HISTORY_PATH = Path(__file__).resolve().parents[4] / "reports" / "shadow" / "watch" / "evidence_snapshots.json"

SNAPSHOT_FIELDS = (
    "timestamp", "future_fixtures", "pending_predictions", "eligible_candidates", "captured", "resolved",
    "blocked", "rejected", "conflicts", "real_prospective", "temporal_unverified", "historical", "synthetic",
    "provenance_complete", "provenance_incomplete", "provenance_unknown", "maturity",
    "track_record_sample_size", "readiness_verdict",
)

WATCH_VERDICTS = (
    "NO_DATA", "INSUFFICIENT_REAL_DATA", "EARLY_EVIDENCE", "TRACKING",
    "STATISTICALLY_INFORMATIVE", "READY_FOR_HUMAN_REVIEW", "NEEDS_FIXES", "BLOCKED",
)


# ---------------------------------------------------------------------------
# §8 : historique longitudinal — append-only, jamais un snapshot supprimé/modifié.
# ---------------------------------------------------------------------------

class EvidenceHistoryStore:
    """§8 : un fichier JSON dédié, DISTINCT du Shadow Store (jamais les observations Shadow elles-mêmes) —
    liste de snapshots (§7), toujours réécrite en entier de façon atomique (même idiome que
    KillSwitchStore.append_audit, Phase 9.1)."""

    def __init__(self, path: Path = DEFAULT_HISTORY_PATH):
        self.path = Path(path)

    def read(self) -> list[dict]:
        """Fichier absent -> liste vide (état par défaut légitime, jamais une erreur). Fichier présent mais
        invalide -> ValueError explicite, JAMAIS un historique tronqué/écrasé silencieusement."""
        if not self.path.exists():
            return []
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as e:
            raise ValueError(f"Evidence history corrompu/illisible ({self.path}) : {e}. STOP — jamais écrasé silencieusement.") from e
        if not isinstance(data, list):
            raise ValueError(f"Evidence history invalide ({self.path}) : racine attendue = liste, obtenu {type(data).__name__}.")
        return data

    def append(self, snapshot: dict) -> list[dict]:
        """§8 : append-only — relit l'existant (corruption -> ValueError, jamais silencieusement tronqué),
        ajoute, réécrit atomiquement. Retourne l'historique complet après ajout."""
        existing = self.read()
        existing.append(snapshot)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(existing, indent=2, ensure_ascii=False, sort_keys=True, default=str)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=".evidence_history_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return existing


# ---------------------------------------------------------------------------
# §7 : snapshot — reshape PUR de sorties déjà calculées ailleurs.
# ---------------------------------------------------------------------------

def compute_evidence_snapshot(*, as_of: datetime, health: dict, full_ledger: dict, capture_outcome: dict,
                               track_record_sample_size: int, readiness_verdict: str) -> dict:
    marking = full_ledger["by_data_marking_class"]
    return {
        "timestamp": as_of.isoformat(),
        "future_fixtures": health["reality"]["future_fixtures"],
        "pending_predictions": health["reality"]["pending_model_predictions"],
        "eligible_candidates": health["capturable"],
        "captured": health["captured"],
        "resolved": health["resolved_shadow"],
        "blocked": 1 if capture_outcome.get("blocked") else 0,
        "rejected": len(capture_outcome.get("rejected", [])) + len(capture_outcome.get("mismatches", [])),
        "conflicts": health["conflicts"],
        "real_prospective": marking["REAL_PROSPECTIVE"],
        "temporal_unverified": marking["REAL_BUT_TEMPORAL_UNVERIFIED"],
        "historical": marking["HISTORICAL"],
        "synthetic": marking["SYNTHETIC"],
        "provenance_complete": full_ledger["provenance_complete"],
        "provenance_incomplete": full_ledger["provenance_incomplete"],
        "provenance_unknown": full_ledger["provenance_unknown"],
        "maturity": full_ledger["maturity_real_prospective_resolved"],
        "track_record_sample_size": track_record_sample_size,
        "readiness_verdict": readiness_verdict,
    }


# ---------------------------------------------------------------------------
# §10 : track record — REAL_PROSPECTIVE + RESOLVED uniquement, jamais HISTORICAL/SYNTHETIC.
# ---------------------------------------------------------------------------

def filter_real_prospective_entries(session, entries: list[tuple]) -> list[tuple]:
    """§10 : sous-ensemble des entrées classées REAL_PROSPECTIVE (classify_data_marking, Phase 9.3, réutilisée
    telle quelle) — la SEULE base admissible pour un track record prospectif dans cette phase. HISTORICAL et
    SYNTHETIC sont structurellement exclus, jamais comptés comme preuve (§10)."""
    out = []
    for record, resolution in entries:
        cls, _ = classify_data_marking(session, record)
        if cls == "REAL_PROSPECTIVE":
            out.append((record, resolution))
    return out


# ---------------------------------------------------------------------------
# §9 : tendance — PURE, entre snapshots déjà persistés, jamais inventée.
# ---------------------------------------------------------------------------

_NUMERIC_TREND_FIELDS = (
    "future_fixtures", "pending_predictions", "eligible_candidates", "captured", "resolved",
    "conflicts", "real_prospective", "track_record_sample_size",
)


def compute_evidence_trend(history: list[dict]) -> dict:
    """§9 : compare les DEUX derniers snapshots de `history` (le plus récent inclus) — jamais une tendance sur
    un seul point (§9 : "si seulement un snapshot -> TREND = INSUFFICIENT_DATA")."""
    if len(history) < 2:
        return {"status": "INSUFFICIENT_DATA", "snapshots_available": len(history),
                "reason": "Au moins 2 snapshots requis pour une tendance réelle (§9) — jamais inventée."}
    previous, current = history[-2], history[-1]
    deltas = {}
    for field in _NUMERIC_TREND_FIELDS:
        p, c = previous.get(field), current.get(field)
        deltas[field] = (c - p) if isinstance(p, (int, float)) and isinstance(c, (int, float)) else None
    return {
        "status": "ok",
        "compared": {"previous_timestamp": previous.get("timestamp"), "current_timestamp": current.get("timestamp")},
        "deltas": deltas,
        "maturity_previous": previous.get("maturity"), "maturity_current": current.get("maturity"),
        "maturity_changed": previous.get("maturity") != current.get("maturity"),
        "readiness_previous": previous.get("readiness_verdict"), "readiness_current": current.get("readiness_verdict"),
        "readiness_changed": previous.get("readiness_verdict") != current.get("readiness_verdict"),
    }


# ---------------------------------------------------------------------------
# §18 : évolution des blockers — NEW/PERSISTING/CLEARED/REGRESSED.
# ---------------------------------------------------------------------------

def readiness_blockers(readiness_assessment) -> list[str]:
    """Réutilise identify_activation_blockers (Phase 9.3, evidence.py) tel quel — n'extrait que les noms."""
    return sorted({b["blocker"] for b in identify_activation_blockers(readiness_assessment)})


def compute_blocker_evolution(history_blockers: list[list[str]], current_blockers: list[str]) -> dict:
    """
    §18 : `history_blockers` = liste ORDONNÉE des blockers de chaque snapshot PASSÉ (jamais le courant).
    Premier run (`history_blockers` vide) -> BASELINE_ONLY (§18). NEW = jamais vu dans AUCUN snapshot passé.
    REGRESSED = vu à un moment, absent du snapshot immédiatement précédent, de retour maintenant — distinct
    de NEW (qui n'a jamais eu d'historique) et de PERSISTING (déjà présent au snapshot précédent).
    """
    if not history_blockers:
        return {"status": "BASELINE_ONLY", "current_blockers": sorted(current_blockers)}
    previous = set(history_blockers[-1])
    ever_seen: set = set().union(*history_blockers)
    curr = set(current_blockers)
    new = curr - ever_seen
    persisting = curr & previous
    cleared = previous - curr
    regressed = (curr & ever_seen) - previous - new
    return {"status": "ok", "new": sorted(new), "persisting": sorted(persisting), "cleared": sorted(cleared), "regressed": sorted(regressed)}


# ---------------------------------------------------------------------------
# §40 : verdict — vocabulaire PROPRE à cette phase (8 valeurs, sans SHADOW_OPERATIONAL).
# ---------------------------------------------------------------------------

def derive_watch_verdict(*, preflight_status: str, tests_green: bool, future_fixtures: int,
                          real_prospective_resolved: int, maturity: str, blockers: list, readiness_verdict: str) -> str:
    """§40 : jamais PRODUCTION_READY (absent de WATCH_VERDICTS, donc structurellement inatteignable)."""
    if preflight_status == "FAIL":
        return "BLOCKED"
    if not tests_green:
        return "NEEDS_FIXES"
    if future_fixtures == 0 and real_prospective_resolved == 0:
        return "NO_DATA"
    if real_prospective_resolved == 0:
        return "INSUFFICIENT_REAL_DATA"
    if maturity == "STATISTICALLY_INFORMATIVE":
        if not blockers and readiness_verdict in ("CONDITIONALLY_READY", "PRODUCTION_READY"):
            return "READY_FOR_HUMAN_REVIEW"
        return "STATISTICALLY_INFORMATIVE"
    if maturity == "TRACKING":
        return "TRACKING"
    if maturity == "EARLY_DATA":
        return "EARLY_EVIDENCE"
    return "INSUFFICIENT_REAL_DATA"
