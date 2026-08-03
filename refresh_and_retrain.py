"""
refresh_and_retrain.py — Job unifié de rafraîchissement périodique des
modèles Dixon-Coles.
=============================================================================

Enchaîne, avec logs clairs à chaque étape et arrêt propre en cas d'échec :

  a. Mise à jour des données brutes (update_raw_data.py) — télécharge la
     saison en cours + précédente depuis le miroir GitHub
     datasets/football-datasets et fusionne avec dédoublonnage.
  b. Ré-entraînement Dixon-Coles par ligue, EN MÉMOIRE
     (export_model_artifacts.train_all_leagues) — rien n'est encore écrit
     sur disque à ce stade.
  c. Validation automatique de chaque nouvel artefact PAR LIGUE
     (validate_artifacts.validate_artifact) — nombre d'équipes, home_advantage,
     rho, cohérence avec l'artefact précédent.
  d. Écriture atomique : chaque ligue qui passe la validation est écrite
     dans model_artifacts/<league>.json.tmp puis renommée (os.replace,
     atomique y compris sous Windows) — jamais d'écrasement direct d'un
     artefact valide par un fichier possiblement incomplet. Une ligue qui
     ÉCHOUE la validation garde son ancien artefact tel quel.
  e. Log récapitulatif : ligues traitées/écrites/conservées, nombre de
     matchs par ligue, paramètres appris, date d'exécution.

Codes de sortie :
  0 = succès complet (toutes les ligues mises à jour)
  1 = échec total (étape a ou b en échec — AUCUN artefact touché, tous
      les anciens model_artifacts/*.json valides restent en place)
  2 = succès partiel (au moins une ligue a échoué la validation à l'étape
      c et a gardé son ancien artefact — les autres ont été mises à jour)

Usage :
    python refresh_and_retrain.py                  # exécution normale
    python refresh_and_retrain.py --skip-refresh    # saute l'étape a (re-entraîne
                                                     # sur les données locales
                                                     # déjà présentes — utile pour
                                                     # tester b/c/d sans réseau)
    python refresh_and_retrain.py --raw-file PATH --artifacts-dir DIR
                                                     # surcharge des chemins,
                                                     # utilisé par les tests
                                                     # (ne touche jamais aux
                                                     # vrais fichiers du projet)

Voir README_REFRESH_JOB.md pour la planification (cron / Planificateur de
tâches Windows) et la procédure de test manuel.
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import update_raw_data
import export_model_artifacts
from validate_artifacts import validate_artifact

logger = logging.getLogger("refresh_and_retrain")

DEFAULT_RAW_FILE = "data/all_leagues_raw_with_stats.csv"
DEFAULT_ARTIFACTS_DIR = Path("model_artifacts")
DEFAULT_LOG_DIR = Path("logs")


def _setup_logging(log_dir: Path) -> Path:
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / "refresh_and_retrain.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )
    return log_path


def _load_existing_artifact(artifacts_dir: Path, league: str) -> dict | None:
    path = artifacts_dir / f"{league}.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp_path.replace(path)  # os.replace — atomique, y compris sous Windows


def run(raw_file: str = DEFAULT_RAW_FILE,
        artifacts_dir: Path = DEFAULT_ARTIFACTS_DIR,
        skip_refresh: bool = False,
        reference_date: datetime | None = None) -> int:
    """Exécute le job complet. Retourne le code de sortie (voir docstring module)."""
    started_at = datetime.now(timezone.utc)
    logger.info("=" * 80)
    logger.info(f"DÉBUT DU JOB — {started_at.isoformat()}")
    logger.info(f"raw_file={raw_file}  artifacts_dir={artifacts_dir}  skip_refresh={skip_refresh}")
    logger.info("=" * 80)

    artifacts_dir = Path(artifacts_dir)

    # --- Étape a : mise à jour des données brutes ---
    if skip_refresh:
        logger.info("[a] Mise à jour des données — SAUTÉE (--skip-refresh)")
        refresh_summary = None
    else:
        logger.info("[a] Mise à jour des données brutes...")
        try:
            refresh_summary = update_raw_data.update_raw_data_file(raw_file=raw_file, reference_date=reference_date)
        except Exception as e:
            logger.error(f"[a] ÉCHEC de la mise à jour des données : {e}")
            logger.error("Job arrêté — aucun artefact touché, les modèles existants restent en service.")
            return 1
        logger.info(f"[a] OK — {refresh_summary['n_before_total']} -> {refresh_summary['n_after_total']} lignes "
                    f"({refresh_summary['n_added_total']:+d})")
        for league, s in refresh_summary["per_league"].items():
            logger.info(f"    {league:15s} {s['before']:5d} -> {s['after']:5d}  ({s['added']:+d})")

    # --- Étape b : ré-entraînement en mémoire ---
    logger.info("[b] Ré-entraînement Dixon-Coles par ligue (en mémoire)...")
    try:
        new_artifacts = export_model_artifacts.train_all_leagues(raw_file=raw_file)
    except Exception as e:
        logger.error(f"[b] ÉCHEC du ré-entraînement : {e}")
        logger.error("Job arrêté — aucun artefact touché, les modèles existants restent en service.")
        return 1
    logger.info(f"[b] OK — {len(new_artifacts)} ligues entraînées : {sorted(new_artifacts.keys())}")

    # --- Étape c : validation par ligue ---
    logger.info("[c] Validation automatique par ligue...")
    to_write = {}
    kept_old = []
    for league, artifact in new_artifacts.items():
        old_artifact = _load_existing_artifact(artifacts_dir, league)
        result = validate_artifact(artifact, old_artifact)

        for w in result.warnings:
            logger.warning(f"    [{league}] {w}")

        if result.ok:
            logger.info(f"    [{league}] OK — home_advantage={artifact['home_advantage']:+.4f} "
                        f"rho={artifact['rho']:+.4f} {len(artifact['teams'])} équipes")
            to_write[league] = artifact
        else:
            for reason in result.reasons:
                logger.error(f"    [{league}] VALIDATION ÉCHOUÉE : {reason}")
            if old_artifact is None:
                logger.error(f"    [{league}] Aucun artefact précédent à conserver — cette ligue restera absente.")
            else:
                logger.error(f"    [{league}] Ancien artefact CONSERVÉ tel quel (non écrasé).")
            kept_old.append(league)

    # --- Étape d : écriture atomique des ligues validées ---
    logger.info("[d] Écriture atomique des artefacts validés...")
    artifacts_dir.mkdir(exist_ok=True)
    for league, artifact in to_write.items():
        out_path = artifacts_dir / f"{league}.json"
        _atomic_write_json(out_path, artifact)
        logger.info(f"    [{league}] -> {out_path}")

    # --- Étape e : log récapitulatif ---
    finished_at = datetime.now(timezone.utc)
    logger.info("=" * 80)
    logger.info("RÉCAPITULATIF")
    logger.info(f"  Démarré  : {started_at.isoformat()}")
    logger.info(f"  Terminé  : {finished_at.isoformat()}")
    logger.info(f"  Durée    : {(finished_at - started_at).total_seconds():.1f}s")
    logger.info(f"  Ligues mises à jour ({len(to_write)}) : {sorted(to_write.keys())}")
    if kept_old:
        logger.info(f"  Ligues CONSERVÉES (validation échouée) ({len(kept_old)}) : {sorted(kept_old)}")
    for league, artifact in to_write.items():
        logger.info(f"    {league:15s} matchs={artifact['trained_on_matches']:5d}  "
                    f"home_advantage={artifact['home_advantage']:+.4f}  rho={artifact['rho']:+.4f}  "
                    f"équipes={len(artifact['teams'])}")
    logger.info("=" * 80)

    if kept_old:
        logger.warning(f"JOB TERMINÉ EN SUCCÈS PARTIEL — {len(kept_old)} ligue(s) conservée(s) sans mise à jour.")
        return 2
    logger.info("JOB TERMINÉ AVEC SUCCÈS COMPLET.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--raw-file", default=DEFAULT_RAW_FILE,
                         help="Chemin du CSV brut multi-ligues (défaut : data/all_leagues_raw_with_stats.csv)")
    parser.add_argument("--artifacts-dir", default=str(DEFAULT_ARTIFACTS_DIR),
                         help="Dossier des artefacts JSON (défaut : model_artifacts/)")
    parser.add_argument("--skip-refresh", action="store_true",
                         help="Saute le téléchargement/fusion des données (étape a) — "
                              "ré-entraîne directement sur le fichier raw-file existant")
    parser.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR),
                         help="Dossier des logs (défaut : logs/)")
    args = parser.parse_args()

    log_path = _setup_logging(Path(args.log_dir))
    logger.info(f"Log écrit dans : {log_path}")

    exit_code = run(
        raw_file=args.raw_file,
        artifacts_dir=Path(args.artifacts_dir),
        skip_refresh=args.skip_refresh,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
