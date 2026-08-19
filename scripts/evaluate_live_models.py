"""
scripts/evaluate_live_models.py — Phase 10, Partie 12 : évaluation LIVE
périodique de toutes les versions candidates à la promotion (status "shadow"
ou "candidate"), avec historique tracé et promotion automatique CONTRÔLÉE.
=============================================================================

Réutilise TEL QUEL app/ai/arena/promotion.py::evaluate_live_promotion —
aucune logique de décision dupliquée ici, ce script n'est qu'une CLI/cron
autour de ce cœur commun (même principe que scripts/retrain_ml_models.py).

Mode par défaut : EVALUATE ONLY (§12 du ticket Phase 10 : "Le mode par
défaut doit être SAFE : evaluate only et non : promote automatically").
Une promotion n'est appliquée QUE si AUTO_PROMOTION_ENABLED=true (variable
d'environnement, voir promotion.py) ET que evaluate_live_promotion renvoie
"eligible" — jamais un raccourci qui court-circuite cette décision.

Chaque évaluation (promue ou non) écrit EXACTEMENT une ligne dans
model_promotion_events (`automatic=True`, `actor="system/cron"`) — rien de
silencieux, y compris un rejet (§9/règle 11 du ticket).

Marché évalué : "1X2" uniquement — seul marché réellement modélisé par TOUS
les moteurs ML actuels (Elo/XGBoost/LightGBM, voir docstring
app/models/model_prediction.py) ; Dixon-Coles n'a pas de version "candidate"
au sens de ce pipeline (production servie directement depuis un artefact
JSON par ligue, jamais via model_versions — voir app/ai/arena/service.py).

Codes de sortie (même convention que fetch_daily_results.py/retrain_ml_models.py) :
  0 = succès (y compris si aucune promotion n'a été jugée éligible)
  2 = erreur inattendue (exception) pendant l'évaluation d'au moins une version

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/evaluate_live_models.py
    AUTO_PROMOTION_ENABLED=true DATABASE_URL="sqlite:///./api/app.db" python scripts/evaluate_live_models.py
"""

import logging
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from sqlmodel import Session, select  # noqa: E402
from app.core.database import engine, init_db  # noqa: E402
from app.models.team_rating import ModelVersion  # noqa: E402
from app.models.model_promotion_event import ModelPromotionEvent  # noqa: E402
from app.ai.arena import promotion as arena_promotion  # noqa: E402
from app.ai.arena.ensemble import KNOWN_MODEL_TYPES  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("evaluate_live_models")

MARKET = "1X2"
_PROMOTABLE_STATUSES = ("shadow", "candidate")


def _log_structured(event: str, **fields) -> None:
    details = " ".join(f"{k}={v}" for k, v in fields.items())
    logger.info(f"[{event}] {details}")


def _record_event(session: Session, decision, *, previous_model_version_id: int | None) -> ModelPromotionEvent:
    import json

    def _metrics(m):
        if m is None:
            return None
        return {**{k: v for k, v in m.items() if k not in ("period_start", "period_end")},
                "period_start": m["period_start"].isoformat() if m.get("period_start") else None,
                "period_end": m["period_end"].isoformat() if m.get("period_end") else None}

    event = ModelPromotionEvent(
        model_version_id=decision.candidate_version_id,
        previous_model_version_id=previous_model_version_id,
        model_type=decision.model_type, market=decision.market,
        decision=decision.status, reason=decision.reason,
        metrics=json.dumps({"candidate": _metrics(decision.candidate_metrics), "baseline": _metrics(decision.baseline_metrics)}),
        sample_size=decision.candidate_metrics.get("sample_size") if decision.candidate_metrics else None,
        actor="system/cron", automatic=True,
    )
    session.add(event)
    session.commit()
    return event


def run() -> int:
    init_db()
    worst_code = 0
    promoted, rejected, insufficient = 0, 0, 0

    with Session(engine) as session:
        candidates = session.exec(
            select(ModelVersion).where(
                ModelVersion.model_type.in_(KNOWN_MODEL_TYPES), ModelVersion.status.in_(_PROMOTABLE_STATUSES),
            )
        ).all()

        if not candidates:
            logger.info("Aucune version candidate/shadow à évaluer — rien à faire.")
            return 0

        for version in candidates:
            try:
                decision = arena_promotion.evaluate_live_promotion(session, version.id, MARKET)
                baseline = arena_promotion.get_active_version(session, decision.model_type)
                _record_event(session, decision, previous_model_version_id=baseline.id if baseline else None)

                if decision.status == "eligible":
                    _log_structured("MODEL_PROMOTION_ELIGIBLE", model_type=decision.model_type,
                                     model_version_id=version.id, market=MARKET, reason=decision.reason)
                    if arena_promotion.AUTO_PROMOTION_ENABLED:
                        arena_promotion.apply_promotion(session, version)
                        _record_event(session, replace(decision, status="promoted"),
                                      previous_model_version_id=baseline.id if baseline else None)
                        promoted += 1
                        _log_structured("MODEL_PROMOTED", model_type=decision.model_type, model_version_id=version.id)
                    else:
                        _log_structured("MODEL_PROMOTION_AUTO_DISABLED", model_type=decision.model_type,
                                         model_version_id=version.id, reason="AUTO_PROMOTION_ENABLED=false — évaluation seule.")
                elif decision.status == "insufficient_data":
                    insufficient += 1
                    _log_structured("MODEL_PROMOTION_INSUFFICIENT_DATA", model_type=decision.model_type,
                                     model_version_id=version.id, reason=decision.reason)
                else:
                    rejected += 1
                    _log_structured("MODEL_PROMOTION_REJECTED", model_type=decision.model_type,
                                     model_version_id=version.id, status=decision.status, reason=decision.reason)

                _log_structured("MODEL_EVALUATION_COMPLETED", model_type=decision.model_type,
                                 model_version_id=version.id, decision=decision.status)
            except Exception as e:
                worst_code = 2
                logger.exception(f"evaluation_failed model_version_id={version.id} model_type={version.model_type} error={e!r}")

    logger.info(f"Résumé : {len(candidates)} version(s) évaluée(s), {promoted} promue(s), "
                f"{rejected} rejetée(s), {insufficient} insufficient_data.")
    return worst_code


if __name__ == "__main__":
    sys.exit(run())
