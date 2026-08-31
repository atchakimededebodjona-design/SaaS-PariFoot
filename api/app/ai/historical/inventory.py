"""
api/app/ai/historical/inventory.py — Phase 8L : inventaire READ-ONLY (§2/§19/
§20/§21 du prompt).

Lecture seule sur `model_versions`/`team_ratings` (DB) et sur
api/model_artifacts/*.json (filesystem) — AUCUNE écriture, AUCUNE
modification d'artefact. Le hachage SHA-256 sert uniquement à identifier/
comparer (§20), jamais à transformer un fichier.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlmodel import Session, func, select

from app.models.team_rating import ModelVersion, TeamRating

from app.ai.historical.schemas import ArtifactFileInventoryEntry, CalibrationInventoryEntry, ModelVersionInventoryEntry

MODEL_ARTIFACTS_DIR = Path(__file__).resolve().parents[3] / "model_artifacts"  # api/model_artifacts/


def build_model_version_inventory(session: Session) -> list[ModelVersionInventoryEntry]:
    """§2 : une entrée par ModelVersion RÉELLEMENT présente — jamais une version fabriquée."""
    rows = session.exec(select(ModelVersion).order_by(ModelVersion.id)).all()
    entries = []
    for r in rows:
        team_ratings_count = session.exec(
            select(func.count()).select_from(TeamRating).where(TeamRating.model_version_id == r.id)
        ).one()
        entries.append(ModelVersionInventoryEntry(
            model_version_id=r.id, model_type=r.model_type, status=r.status, is_active=r.is_active,
            trained_at=r.trained_at, artifact_present_in_db=bool(r.artifact), artifact_length=len(r.artifact) if r.artifact else 0,
            config_present=bool(r.config), feature_version=r.feature_version,
            training_period_start=str(r.training_period_start) if r.training_period_start else None,
            training_period_end=str(r.training_period_end) if r.training_period_end else None,
            team_ratings_count=team_ratings_count,
        ))
    return entries


def scan_filesystem_artifacts(artifacts_dir: Path = MODEL_ARTIFACTS_DIR) -> list[ArtifactFileInventoryEntry]:
    """§19/§20 : artefacts Dixon-Coles réellement présents sur disque
    (api/model_artifacts/*.json — export_model_artifacts.py::export_league).
    `filesystem_mtime` est INFORMATIF UNIQUEMENT (§19 : jamais une preuve
    d'entraînement) — `embedded_trained_at`/`embedded_data_up_to` (lus DANS
    le JSON lui-même) sont la seule preuve utilisée ailleurs pour l'éligibilité."""
    if not artifacts_dir.exists():
        return []
    entries = []
    for path in sorted(artifacts_dir.glob("*.json")):
        raw = path.read_bytes()
        sha256 = hashlib.sha256(raw).hexdigest()
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = {}
        entries.append(ArtifactFileInventoryEntry(
            path=str(path.relative_to(artifacts_dir.parents[1])), league=path.stem,
            size_bytes=len(raw), sha256=sha256, filesystem_mtime=mtime,
            embedded_trained_at=data.get("trained_at"), embedded_data_up_to=data.get("data_up_to"),
        ))
    return entries


def build_calibration_inventory(model_versions: list[ModelVersionInventoryEntry]) -> list[CalibrationInventoryEntry]:
    """
    §8/§21 : calibration réellement identifiable, PAR TYPE DE MODÈLE (constaté
    en inspectant le dépôt, jamais supposé) :
      - dixon_coles : la probabilité EST le modèle — aucune étape de
        calibration séparée n'existe dans ce dépôt (N/A, jamais MISSING —
        distinction délibérée : "n'existe pas conceptuellement" != "existe
        mais absent").
      - elo : config JSON {"c","scale"} par ligue (api/app/ai/engine/elo.py)
        — MÊME instant que `trained_at` (aucun timestamp de calibration
        distinct persisté nulle part) — AVAILABLE si config_present, sinon
        CALIBRATION_MISSING.
      - xgboost/lightgbm : Platt/Isotonic (Phase 6, calibration_engine.py)
        est RECHERCHE UNIQUEMENT — jamais persisté sur une ModelVersion —
        CALIBRATION_MISSING par construction (vérifié : aucune colonne
        dédiée, `config` ne contient que feature_columns/league_categories/
        class_order, voir train_ml_stacking_from_db.py).
      - ensemble : pas de calibration propre (combine des sorties déjà
        calibrées ou non selon la stratégie) — N/A.
    """
    entries = []
    for mv in model_versions:
        if mv.model_type == "dixon_coles":
            entries.append(CalibrationInventoryEntry(mv.model_version_id, mv.model_type, "n/a_probability_is_the_model", None, "NOT_AVAILABLE"))
        elif mv.model_type == "elo":
            if mv.config_present:
                entries.append(CalibrationInventoryEntry(mv.model_version_id, mv.model_type, "elo_ordered_logit", mv.trained_at, "AVAILABLE"))
            else:
                entries.append(CalibrationInventoryEntry(mv.model_version_id, mv.model_type, None, None, "CALIBRATION_MISSING"))
        elif mv.model_type in ("xgboost", "lightgbm"):
            entries.append(CalibrationInventoryEntry(mv.model_version_id, mv.model_type, "none_persisted", None, "CALIBRATION_MISSING"))
        else:  # ensemble ou type inconnu
            entries.append(CalibrationInventoryEntry(mv.model_version_id, mv.model_type, None, None, "NOT_AVAILABLE"))
    return entries
