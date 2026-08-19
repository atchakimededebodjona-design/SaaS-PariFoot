"""
scripts/retrain_ml_models.py — Phase 9, Partie G : réentraînement continu
contrôlé pour XGBoost/LightGBM.
=============================================================================

Réutilise TEL QUEL app/ai/arena/retraining.py::run_retrain — aucune logique
de données/promotion dupliquée ici, ce script n'est qu'une CLI autour de ce
cœur commun (§26 du ticket Phase 9).

Comportement par MODE (jamais destructif par défaut, §26-27) :

  --dry-run  : vérifie la disponibilité des données (check_training_data)
               et affiche un APERÇU du split temporel + de la règle de
               promotion qui s'appliquerait — n'écrit RIEN en base, ne
               construit ni n'entraîne aucun modèle (rapide).
  (défaut)   : construit réellement les features (app.ai.engine.features.
               build_ml_features_from_db(), ~35 min sur la base de dev
               réelle), entraîne, crée une ModelVersion CANDIDATE
               (status="candidate", is_active=False) — NE LA PROMEUT JAMAIS.
  --force    : après création du candidat, évalue ET applique la promotion
               dans la foulée si evaluate_promotion() l'accepte — reste
               TOUJOURS gated par cette règle, jamais un raccourci qui la
               court-circuite (§23 : "ne pas utiliser une règle qui garantit
               artificiellement la promotion").

Codes de sortie (§26, même convention que fetch_daily_results.py) :
  0 = succès (dry-run informatif, candidat créé, ou promotion appliquée/
      rejetée — un rejet de promotion n'est PAS un échec du script)
  1 = données insuffisantes pour au moins un des model_type demandés
      (check_training_data a explicitement ABORT)
  2 = erreur d'entraînement (exception) pour au moins un des model_type

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/retrain_ml_models.py --model xgboost --dry-run
    DATABASE_URL="sqlite:///./api/app.db" python scripts/retrain_ml_models.py --model all
    DATABASE_URL="sqlite:///./api/app.db" python scripts/retrain_ml_models.py --model lightgbm --force
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from sqlmodel import Session  # noqa: E402
from app.core.database import engine, init_db  # noqa: E402
from app.ai.arena.retraining import SUPPORTED_MODEL_TYPES, run_retrain  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("retrain_ml_models")


def _models_to_run(arg: str) -> list[str]:
    if arg == "all":
        return list(SUPPORTED_MODEL_TYPES)
    if arg not in SUPPORTED_MODEL_TYPES:
        raise ValueError(f"--model doit être un de {SUPPORTED_MODEL_TYPES} ou 'all', reçu : {arg}")
    return [arg]


def main(model_arg: str, dry_run: bool, force: bool) -> int:
    init_db()
    models = _models_to_run(model_arg)

    worst_code = 0
    with Session(engine) as session:
        for model_type in models:
            logger.info("=" * 80)
            logger.info(f"model_type={model_type}  dry_run={dry_run}  force={force}")
            logger.info("=" * 80)

            result = run_retrain(session, model_type, dry_run=dry_run, force=force)

            logger.info(f"status={result.status}")
            logger.info(result.message)
            if result.candidate_version_id is not None:
                logger.info(f"candidate_version_id={result.candidate_version_id}")
            if result.decision is not None:
                logger.info(f"promote={result.decision.promote} reason={result.decision.reason}")

            if result.status == "data_not_ready":
                worst_code = max(worst_code, 1)
            elif result.status == "error":
                worst_code = max(worst_code, 2)

    return worst_code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", choices=[*SUPPORTED_MODEL_TYPES, "all"], required=True,
                         help="Modèle(s) à réentraîner.")
    parser.add_argument("--dry-run", action="store_true",
                         help="Aperçu sans écriture en base ni entraînement réel.")
    parser.add_argument("--force", action="store_true",
                         help="Évalue et applique la promotion après création du candidat, si la règle passe.")
    args = parser.parse_args()

    sys.exit(main(args.model, dry_run=args.dry_run, force=args.force))
