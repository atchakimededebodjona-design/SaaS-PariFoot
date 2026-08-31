"""
api/app/ai/shadow/tracking.py — Phase 8K : capture (§1/§4/§6/§7) et stockage
(§3, voir schemas.py pour la justification "pas de migration") des Shadow
Decision Records.

RÉUTILISE TEL QUEL (jamais réimplémenté) :
  - api/app/ai/pipeline/orchestrator.py::run_pipeline (Phase 8J) — produit
    le PipelineAssessment capturé ici, jamais recalculé.
  - api/app/ai/pipeline/schemas.py::PipelineInput/PipelineAssessment
    (Phase 8J/8I/8H) — la SEULE source des champs quality/eligibility/
    value_status/provenance.

AUCUNE fonction ici n'écrit dans une table SQL — uniquement le fichier JSON
local (ShadowDecisionStore), jamais api/app.db.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.ai.pipeline.schemas import PipelineAssessment, PipelineInput

from app.ai.shadow.schemas import ShadowDecisionRecord, ShadowResolution, pending_resolution

DEFAULT_STORE_PATH = Path(__file__).resolve().parents[4] / "reports" / "shadow" / "shadow_decision_store.json"


def dedup_key(match_id, market: str, model_version, as_of: datetime) -> str:
    """§7 : clé logique (match_id, market, model_version, as_of) — INSPECTÉE
    contre Phase 6/7 avant adoption (voir schemas.py) : aucune structure
    existante n'imposait déjà une clé différente pour ce besoin précis."""
    as_of_str = as_of.isoformat() if isinstance(as_of, datetime) else str(as_of)
    return f"{match_id}|{market}|{model_version}|{as_of_str}"


# ---------------------------------------------------------------------------
# (Dé)sérialisation JSON — datetimes en ISO8601, jamais un format ambigu.
# ---------------------------------------------------------------------------

def _dt(v):
    return v.isoformat() if isinstance(v, datetime) else v


def _parse_dt(v: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(v) if v else None


def _record_to_dict(r: ShadowDecisionRecord) -> dict:
    d = asdict(r)
    d["kickoff"] = _dt(r.kickoff)
    d["as_of"] = _dt(r.as_of)
    d["odds_timestamp"] = _dt(r.odds_timestamp)
    d["created_at"] = _dt(r.created_at)
    return d


def _record_from_dict(d: dict) -> ShadowDecisionRecord:
    d = dict(d)
    d["kickoff"] = _parse_dt(d.get("kickoff"))
    d["as_of"] = _parse_dt(d["as_of"])
    d["odds_timestamp"] = _parse_dt(d.get("odds_timestamp"))
    d["created_at"] = _parse_dt(d["created_at"])
    return ShadowDecisionRecord(**d)


def _resolution_to_dict(r: ShadowResolution) -> dict:
    d = asdict(r)
    d["resolved_at"] = _dt(r.resolved_at)
    return d


def _resolution_from_dict(d: dict) -> ShadowResolution:
    d = dict(d)
    d["resolved_at"] = _parse_dt(d.get("resolved_at"))
    return ShadowResolution(**d)


# ---------------------------------------------------------------------------
# §3/§6/§35 : stockage fichier — idempotent, jamais une table SQL.
# ---------------------------------------------------------------------------

class ShadowDecisionStore:
    """§6/§7/§35 : ajout idempotent par `shadow_id`. Un deuxième `upsert_new`
    avec le même `shadow_id` retourne False et NE MODIFIE RIEN (§6 : "deuxième
    run -> 0 nouveau record")."""

    def __init__(self, path: Path = DEFAULT_STORE_PATH):
        self.path = Path(path)
        self._data: dict[str, dict] = {}
        self._loaded = False

    def load(self) -> None:
        """§40 (Phase 8M) : valide le JSON existant AVANT de l'accepter — un store corrompu lève
        explicitement (`ValueError`), JAMAIS écrasé silencieusement (§40 : "Si corruption : STOP")."""
        if self.path.exists():
            raw = self.path.read_text(encoding="utf-8")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"Shadow store corrompu ({self.path}) : JSON invalide ({e}). STOP — jamais écrasé "
                    "silencieusement (§40 du prompt Phase 8M). Restaurer une sauvegarde ou investiguer manuellement."
                ) from e
            if not isinstance(data, dict):
                raise ValueError(f"Shadow store corrompu ({self.path}) : racine JSON attendue = objet, obtenu {type(data).__name__}.")
            self._data = data
        else:
            self._data = {}
        self._loaded = True

    def save(self) -> None:
        """§40 (Phase 8M) : écriture ATOMIQUE — fichier temporaire dans le même répertoire (garantit que
        `os.replace` reste une opération atomique sur le même système de fichiers), puis renommage atomique.
        Un crash pendant l'écriture laisse l'ancien fichier intact, jamais un fichier à moitié écrit."""
        import os
        import tempfile

        self.path.parent.mkdir(parents=True, exist_ok=True)
        # default=str (Phase 8N) : `record.provenance` est un dict LIBRE (Provenance.__dict__, Phase 8J) et
        # peut contenir des datetime imbriqués (odds_timestamp/cutoff_timestamp) que _record_to_dict ne
        # convertit pas champ par champ (seuls les 4 champs top-level nommés le sont) — filet de sécurité
        # générique plutôt qu'un risque de champ futur non couvert ; ces valeurs restent purement
        # informatives dans `provenance` (jamais reparsées en datetime ailleurs).
        payload = json.dumps(self._data, indent=2, ensure_ascii=False, sort_keys=True, default=str)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=".shadow_store_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, self.path)  # atomique sur Windows/POSIX (même volume, garanti par mkstemp(dir=...))
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def contains(self, shadow_id: str) -> bool:
        self._ensure_loaded()
        return shadow_id in self._data

    def get(self, shadow_id: str) -> Optional[tuple[ShadowDecisionRecord, ShadowResolution]]:
        self._ensure_loaded()
        entry = self._data.get(shadow_id)
        if entry is None:
            return None
        return _record_from_dict(entry["record"]), _resolution_from_dict(entry["resolution"])

    def upsert_new(self, record: ShadowDecisionRecord, resolution: ShadowResolution) -> bool:
        """§6/§7 : True si créé, False si `shadow_id` existait déjà — le
        record/résolution EXISTANT n'est jamais réécrit (immutabilité, §5)."""
        self._ensure_loaded()
        if record.shadow_id in self._data:
            return False
        self._data[record.shadow_id] = {"record": _record_to_dict(record), "resolution": _resolution_to_dict(resolution)}
        return True

    def update_resolution(self, shadow_id: str, resolution: ShadowResolution) -> bool:
        """§12/§13 : remplace la résolution UNIQUEMENT si l'actuelle est
        PENDING — jamais un résultat déjà RESOLVED/CONFLICT/UNRESOLVED écrasé."""
        self._ensure_loaded()
        entry = self._data.get(shadow_id)
        if entry is None:
            return False
        current = _resolution_from_dict(entry["resolution"])
        if current.result_status != "PENDING":
            return False
        entry["resolution"] = _resolution_to_dict(resolution)
        return True

    def all(self) -> list[tuple[ShadowDecisionRecord, ShadowResolution]]:
        self._ensure_loaded()
        return [(_record_from_dict(v["record"]), _resolution_from_dict(v["resolution"])) for v in self._data.values()]

    def clear(self) -> None:
        """Réinitialise le store EN MÉMOIRE (jamais appelé automatiquement — utilisé par les tests pour isoler
        un store temporaire, jamais par le pipeline réel)."""
        self._data = {}
        self._loaded = True


# ---------------------------------------------------------------------------
# §1/§4 : capture — adapte un PipelineAssessment (Phase 8J) DÉJÀ CALCULÉ,
# jamais recalculé ici.
# ---------------------------------------------------------------------------

def capture_shadow_decision(
    store: ShadowDecisionStore, pi: PipelineInput, assessment: PipelineAssessment,
    *, home_team: str, away_team: str, data_marking: str = "REAL",
) -> tuple[ShadowDecisionRecord, bool]:
    """
    §1/§4 : construit un ShadowDecisionRecord à partir d'un PipelineInput/
    PipelineAssessment déjà produits par Phase 8J (run_pipeline) — AUCUNE
    logique de qualité/décision/valeur n'est recalculée ici. `home_team`/
    `away_team` : nécessaires à la RÉSOLUTION (§12 — model_predictions/
    prediction_log/match sont indexées par clé naturelle, pas par match_id,
    voir leurs docstrings) — absents de PipelineInput (Phase 8J, jamais
    modifié), donc fournis explicitement par l'appelant à la capture.

    Retourne (record, created) — `created=False` si un record avec le même
    `shadow_id` existait déjà (§6 : idempotence, 0 nouveau record).
    """
    quality_dims = dict(assessment.quality.quality_dimensions.__dict__) if assessment.quality else {}
    calibrated_probs = pi.calibration.probabilities or {}

    shadow_id = dedup_key(pi.match_id, pi.market, pi.model_version, assessment.evaluated_at)
    record = ShadowDecisionRecord(
        shadow_id=shadow_id, match_id=pi.match_id, league=pi.league,
        home_team=home_team, away_team=away_team, kickoff=pi.kickoff, as_of=assessment.evaluated_at,
        model_type=pi.model, model_version=pi.model_version,
        calibration_source=assessment.provenance.calibration_source,
        market=pi.market, selection=pi.selection,
        raw_probability=(pi.probabilities or {}).get(pi.selection),
        calibrated_probability=calibrated_probs.get(pi.selection) if calibrated_probs else None,
        market_probabilities_raw=dict(pi.probabilities) if pi.probabilities else None,
        market_probabilities_calibrated=dict(calibrated_probs) if calibrated_probs else None,
        probability_source=pi.calibration.source,
        quality=quality_dims,
        confidence=assessment.quality.overall_status if assessment.quality else "UNKNOWN",
        eligibility=assessment.decision.eligibility if assessment.decision else "UNKNOWN",
        value_status=assessment.value.status if assessment.value else None,
        odds_source=assessment.provenance.odds_source, odds_timestamp=assessment.provenance.odds_timestamp,
        temporal_status=quality_dims.get("temporal_quality", "UNKNOWN"),
        provenance=dict(assessment.provenance.__dict__),
        status=assessment.final_status, created_at=datetime.now(timezone.utc), data_marking=data_marking,
    )
    created = store.upsert_new(record, pending_resolution())
    return record, created
