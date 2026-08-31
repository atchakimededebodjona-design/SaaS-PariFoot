"""
api/app/ai/safety/kill_switch.py — Phase 9.1 : persistence + mécanisme
central (§2/§6/§7/§9/§10 du prompt).

Persistence : fichier JSON local, écriture ATOMIQUE (tempfile même
répertoire + fsync + os.replace), détection de corruption qui lève
explicitement (jamais un écrasement silencieux) — RÉUTILISE TEL QUEL le
même schéma que api/app/ai/shadow/tracking.py::ShadowDecisionStore (Phase
8K/8M, jamais réinventé, §9 : "inspecter les mécanismes existants avant de
choisir le stockage"). Un fichier absent est un état par défaut légitime
(ENABLED, jamais déclenché) — PAS une erreur ; un fichier présent mais
illisible/invalide EST une erreur → BLOCK (§3), jamais une hypothèse.

AUCUNE table SQL créée (§9 : "NE PAS créer une table automatiquement").
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.ai.safety.schemas import (
    KillSwitchState, AuditEvent, ProductionAllowedResult, KILL_SWITCH_TRIGGERS, BLOCKABLE_SCOPES,
)

DEFAULT_STATE_PATH = Path(__file__).resolve().parents[4] / "reports" / "safety" / "kill_switch_state.json"
DEFAULT_AUDIT_PATH = Path(__file__).resolve().parents[4] / "reports" / "safety" / "kill_switch_audit_log.json"

UTC = timezone.utc


def _atomic_write_json(path: Path, payload) -> None:
    """§10 : écriture atomique — même discipline que ShadowDecisionStore.save() (Phase 8N, `default=str` inclus
    pour les datetime imbriqués)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True, default=str)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".kill_switch_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _dt(v: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(v) if v else None


class KillSwitchStore:
    """§9/§10 : un store par (state_path, audit_path) — permet des switches indépendants pour les tests
    (jamais partagés avec le fichier réel `reports/safety/kill_switch_state.json` pendant un test)."""

    def __init__(self, state_path: Path = DEFAULT_STATE_PATH, audit_path: Path = DEFAULT_AUDIT_PATH):
        self.state_path = Path(state_path)
        self.audit_path = Path(audit_path)

    def read(self) -> KillSwitchState:
        """§3/§4 : fichier absent -> ENABLED (état par défaut légitime, jamais une erreur). Fichier présent
        mais JSON invalide ou champ `state` hors vocabulaire -> ValueError explicite, JAMAIS écrasé/ignoré
        silencieusement — l'appelant (assert_production_allowed) DOIT traiter cette exception comme BLOCK,
        jamais comme ENABLED par défaut (§3 : "jamais continuer par défaut")."""
        if not self.state_path.exists():
            return KillSwitchState(state="ENABLED")
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as e:
            raise ValueError(f"Kill Switch state corrompu/illisible ({self.state_path}) : {e}. "
                              "STOP — jamais interprété comme ENABLED par défaut (§3 du prompt Phase 9.1).") from e
        if not isinstance(data, dict) or data.get("state") not in ("ENABLED", "TRIGGERED"):
            raise ValueError(f"Kill Switch state invalide ({self.state_path}) : champ 'state' absent ou hors vocabulaire ({data.get('state') if isinstance(data, dict) else type(data).__name__}).")
        return KillSwitchState(
            state=data["state"], triggered_at=_dt(data.get("triggered_at")), trigger_code=data.get("trigger_code"),
            trigger_reason=data.get("trigger_reason"), trigger_scope=data.get("trigger_scope"),
            trigger_actor=data.get("trigger_actor"), trigger_automatic=data.get("trigger_automatic"),
        )

    def _write(self, state: KillSwitchState) -> None:
        payload = {
            "state": state.state,
            "triggered_at": state.triggered_at.isoformat() if state.triggered_at else None,
            "trigger_code": state.trigger_code, "trigger_reason": state.trigger_reason,
            "trigger_scope": state.trigger_scope, "trigger_actor": state.trigger_actor,
            "trigger_automatic": state.trigger_automatic,
        }
        _atomic_write_json(self.state_path, payload)

    def append_audit(self, event: AuditEvent) -> None:
        """§27 : append-only — jamais une entrée existante modifiée/supprimée. Lit l'historique existant
        (corruption -> ValueError, jamais un log tronqué silencieusement), ajoute, réécrit atomiquement."""
        existing = self.read_audit_log()
        existing.append({
            "event_type": event.event_type, "scope": event.scope, "code": event.code, "reason": event.reason,
            "actor": event.actor, "timestamp": event.timestamp.isoformat(), "model_version": event.model_version,
        })
        _atomic_write_json(self.audit_path, existing)

    def read_audit_log(self) -> list[dict]:
        if not self.audit_path.exists():
            return []
        try:
            raw = self.audit_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as e:
            raise ValueError(f"Kill Switch audit log corrompu ({self.audit_path}) : {e}. STOP.") from e
        if not isinstance(data, list):
            raise ValueError(f"Kill Switch audit log invalide ({self.audit_path}) : racine attendue = liste.")
        return data


# ---------------------------------------------------------------------------
# §6/§14/§15/§16 : trigger — idempotent (§22-like : deux trigger successifs
# ne créent jamais d'état incohérent, chacun laisse une trace d'audit).
# ---------------------------------------------------------------------------

def trigger(store: KillSwitchStore, code: str, reason: str, *, scope: Optional[str] = None,
            actor: str = "system", automatic: bool = True, now: Optional[datetime] = None) -> KillSwitchState:
    if code not in KILL_SWITCH_TRIGGERS:
        raise ValueError(f"Trigger code inconnu : '{code}'. Attendu un de {KILL_SWITCH_TRIGGERS}.")
    ts = now or datetime.now(UTC)
    new_state = KillSwitchState(state="TRIGGERED", triggered_at=ts, trigger_code=code, trigger_reason=reason,
                                 trigger_scope=scope, trigger_actor=actor, trigger_automatic=automatic)
    store._write(new_state)
    store.append_audit(AuditEvent(event_type="TRIGGERED", scope=scope, code=code, reason=reason, actor=actor, timestamp=ts))
    return new_state


# ---------------------------------------------------------------------------
# §16/§17 : reset — EXPLICITE, JAMAIS automatique, refusé si un gate
# critique fourni par l'appelant n'est pas PASS (§17). `critical_gates`
# DOIT être fourni par l'appelant (jamais recalculé ici — ce module ne
# dépend jamais de la DB/readiness, garde la même séparation que le reste
# de api/app/ai/safety/, l'appelant, généralement scripts/safety_control.py,
# est responsable d'obtenir un verdict Phase 9 FRAIS avant d'appeler reset()).
# ---------------------------------------------------------------------------

def reset(store: KillSwitchStore, critical_gates: dict[str, str], *, actor: str, reason: str,
          now: Optional[datetime] = None) -> tuple[bool, str, KillSwitchState]:
    """Retourne (approved, message, état résultant). `critical_gates` :
    {gate_name: status} — SEULS les gates critiques de Phase 9
    (api/app/ai/readiness/schemas.py::CRITICAL_GATES). Refuse si l'un
    d'entre eux != "PASS", ou si `critical_gates` est vide (§3 : absence de
    preuve n'est jamais une preuve de sécurité — un dict vide est traité
    comme UNKNOWN, jamais comme "aucun gate à vérifier -> OK")."""
    ts = now or datetime.now(UTC)
    current = store.read()
    if current.state == "ENABLED":
        store.append_audit(AuditEvent(event_type="RESET_REQUESTED", scope=None, code=None, reason=f"{reason} (déjà ENABLED — no-op)", actor=actor, timestamp=ts))
        return True, "ALREADY_ENABLED_NOOP", current

    store.append_audit(AuditEvent(event_type="RESET_REQUESTED", scope=current.trigger_scope, code=current.trigger_code, reason=reason, actor=actor, timestamp=ts))

    if not critical_gates:
        msg = "Reset refusé : aucun gate critique fourni (absence de preuve != preuve de sécurité, §3)."
        store.append_audit(AuditEvent(event_type="RESET_DENIED", scope=current.trigger_scope, code=current.trigger_code, reason=msg, actor=actor, timestamp=ts))
        return False, msg, current

    failing = {name: status for name, status in critical_gates.items() if status != "PASS"}
    if failing:
        msg = f"Reset refusé : gate(s) critique(s) non PASS : {failing}."
        store.append_audit(AuditEvent(event_type="RESET_DENIED", scope=current.trigger_scope, code=current.trigger_code, reason=msg, actor=actor, timestamp=ts))
        return False, msg, current

    new_state = KillSwitchState(state="ENABLED")
    store._write(new_state)
    store.append_audit(AuditEvent(event_type="RESET_APPROVED", scope=current.trigger_scope, code=current.trigger_code, reason=reason, actor=actor, timestamp=ts))
    return True, "RESET_APPROVED", new_state


# ---------------------------------------------------------------------------
# §7 : fail-closed API — LA fonction que tout flux sensible doit appeler.
# ---------------------------------------------------------------------------

def assert_production_allowed(store: KillSwitchStore, scope: str, *, now: Optional[datetime] = None) -> ProductionAllowedResult:
    ts = now or datetime.now(UTC)
    if scope not in BLOCKABLE_SCOPES:
        raise ValueError(f"Scope inconnu : '{scope}'. Attendu un de {BLOCKABLE_SCOPES}.")
    try:
        state = store.read()
    except ValueError as e:
        # store.read() convertit déjà toute erreur OS/JSON en ValueError (§3) — un seul point de capture,
        # jamais deux chemins d'erreur qui pourraient diverger sur le code retourné.
        return ProductionAllowedResult(allowed=False, code="KILL_SWITCH_CORRUPTED", reason=str(e), scope=scope, timestamp=ts)
    if state.state == "TRIGGERED":
        return ProductionAllowedResult(allowed=False, code="KILL_SWITCH_ACTIVE",
                                        reason=f"Kill Switch déclenché ({state.trigger_code}: {state.trigger_reason}), reset requis avant toute activation.",
                                        scope=scope, timestamp=ts)
    return ProductionAllowedResult(allowed=True, scope=scope, timestamp=ts)
