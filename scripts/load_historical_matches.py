"""
scripts/load_historical_matches.py — Charge l'historique CSV multi-ligues
dans les tables `match`/`match_stats`.
=============================================================================

Étape 1 du plan Xfoot AI (voir audit Phase 1) : fondation de données pour
les phases futures (Radar, Track Record). PUREMENT ADDITIF — n'active
aucune fonctionnalité côté API, ne touche à rien d'autre :
build_features.py, export_model_artifacts.py et dixon_coles.py continuent
de lire data/all_leagues_raw_with_stats.csv exactement comme avant.

IDEMPOTENT : une ligne dont (league, date, home_team, away_team) existe
déjà en base est ignorée, jamais dupliquée — relancer ce script après
interruption ou par erreur ne change rien à l'état déjà chargé (même
contrat que update_raw_data.py::merge_and_dedup, et même garde-fou que la
contrainte unique posée en base — voir api/app/models/match.py — qui
refuserait de toute façon un doublon même si cette pré-vérification en
mémoire était contournée).

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/load_historical_matches.py

Sans DATABASE_URL, utilise le défaut de app/core/database.py
(sqlite:///./app.db, résolu depuis le RÉPERTOIRE COURANT — attention à ne
pas créer par erreur un fichier app.db à la racine du dépôt en lançant ce
script depuis un autre dossier que celui attendu ; DATABASE_URL explicite
recommandé).
"""

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from sqlmodel import Session, select, func  # noqa: E402
from app.core.database import engine, init_db  # noqa: E402
from app.models.match import Match, MatchStats  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("load_historical_matches")

DEFAULT_RAW_FILE = Path(__file__).resolve().parent.parent / "data" / "all_leagues_raw_with_stats.csv"

REQUIRED_COLUMNS = ["league", "date", "home_team", "away_team", "home_goals", "away_goals"]
STATS_COLUMNS = ["home_shots", "away_shots", "home_shots_target", "away_shots_target", "home_corners", "away_corners"]


def _opt_int(value) -> int | None:
    return None if pd.isna(value) else int(value)


def _existing_keys(session: Session) -> set[tuple[str, object, str, str]]:
    """Précharge toutes les clés (league, date, home_team, away_team) déjà en
    base en une seule requête — évite un SELECT par ligne sur >10 000 lignes."""
    rows = session.exec(select(Match.league, Match.date, Match.home_team, Match.away_team)).all()
    return set(rows)


def load(raw_file: Path = DEFAULT_RAW_FILE) -> dict:
    df = pd.read_csv(raw_file, parse_dates=["date"])
    logger.info(f"CSV source : {raw_file}")
    logger.info(f"  {len(df)} lignes, {df['date'].min().date()} -> {df['date'].max().date()}")
    logger.info(f"  par ligue : {df['league'].value_counts().to_dict()}")

    init_db()

    inserted = 0
    skipped_existing = 0
    skipped_invalid = 0
    per_league_inserted: dict[str, int] = {}
    per_league_skipped: dict[str, int] = {}

    with Session(engine) as session:
        existing = _existing_keys(session)
        logger.info(f"{len(existing)} match(s) déjà en base avant ce run.")

        for row in df.itertuples(index=False):
            row_d = row._asdict()
            if any(pd.isna(row_d[c]) for c in REQUIRED_COLUMNS):
                skipped_invalid += 1
                logger.warning(f"Ligne ignorée (champ obligatoire manquant) : {row_d}")
                continue

            date = row.date.to_pydatetime()
            key = (row.league, date, row.home_team, row.away_team)
            if key in existing:
                skipped_existing += 1
                per_league_skipped[row.league] = per_league_skipped.get(row.league, 0) + 1
                continue

            match = Match(
                league=row.league, date=date,
                home_team=row.home_team, away_team=row.away_team,
                home_goals=int(row.home_goals), away_goals=int(row.away_goals),
            )
            session.add(match)
            session.flush()  # pour obtenir match.id avant d'écrire la ligne match_stats liée

            session.add(MatchStats(
                match_id=match.id,
                home_shots=_opt_int(row.home_shots), away_shots=_opt_int(row.away_shots),
                home_shots_target=_opt_int(row.home_shots_target), away_shots_target=_opt_int(row.away_shots_target),
                home_corners=_opt_int(row.home_corners), away_corners=_opt_int(row.away_corners),
            ))

            existing.add(key)
            inserted += 1
            per_league_inserted[row.league] = per_league_inserted.get(row.league, 0) + 1

        session.commit()

    logger.info("=" * 80)
    logger.info("RÉSUMÉ DU CHARGEMENT")
    logger.info(f"  Lignes lues (CSV)        : {len(df)}")
    logger.info(f"  Insérées                 : {inserted}")
    logger.info(f"  Ignorées (déjà en base)  : {skipped_existing}")
    logger.info(f"  Ignorées (champ manquant): {skipped_invalid}")
    for league in sorted(set(per_league_inserted) | set(per_league_skipped)):
        logger.info(f"    {league:15s} insérées={per_league_inserted.get(league, 0):5d}  "
                    f"déjà en base={per_league_skipped.get(league, 0):5d}")
    logger.info("=" * 80)

    summary = {
        "csv_rows": len(df), "inserted": inserted,
        "skipped_existing": skipped_existing, "skipped_invalid": skipped_invalid,
        "per_league_inserted": per_league_inserted, "per_league_skipped": per_league_skipped,
    }

    validation = _validate(df)
    summary["validation"] = validation
    return summary


def _validate(df: pd.DataFrame) -> dict:
    """
    Vérifications après chargement :
      1. nombre total de lignes valides du CSV == nombre de matchs en base
      2. aucune match_stats orpheline (sans match correspondant)
      3. 5 matchs tirés au hasard : chaque champ (buts, tirs, corners)
         identique entre la ligne CSV d'origine et la ligne en base
    """
    result: dict = {"ok": True, "issues": []}

    valid_csv_rows = df.dropna(subset=REQUIRED_COLUMNS)

    with Session(engine) as session:
        db_match_count = session.exec(select(func.count()).select_from(Match)).one()
        if db_match_count != len(valid_csv_rows):
            result["ok"] = False
            result["issues"].append(
                f"total en base ({db_match_count}) != lignes CSV valides ({len(valid_csv_rows)})"
            )
        result["db_match_count"] = db_match_count
        result["csv_valid_rows"] = len(valid_csv_rows)

        orphan_ids = session.exec(
            select(MatchStats.id).where(MatchStats.match_id.not_in(select(Match.id)))
        ).all()
        result["orphan_match_stats"] = len(orphan_ids)
        if orphan_ids:
            result["ok"] = False
            result["issues"].append(f"{len(orphan_ids)} match_stats orpheline(s) : ids {list(orphan_ids)[:10]}")

        sample_size = min(5, len(valid_csv_rows))
        sample_rows = valid_csv_rows.sample(n=sample_size, random_state=None) if sample_size else valid_csv_rows
        mismatches = []
        for row in sample_rows.itertuples(index=False):
            date = row.date.to_pydatetime()
            db_match = session.exec(
                select(Match).where(
                    Match.league == row.league, Match.date == date,
                    Match.home_team == row.home_team, Match.away_team == row.away_team,
                )
            ).first()
            if db_match is None:
                mismatches.append(f"{row.league} {date.date()} {row.home_team}-{row.away_team} : introuvable en base")
                continue
            if db_match.home_goals != int(row.home_goals) or db_match.away_goals != int(row.away_goals):
                mismatches.append(
                    f"{row.league} {date.date()} {row.home_team}-{row.away_team} : "
                    f"buts CSV=({row.home_goals},{row.away_goals}) base=({db_match.home_goals},{db_match.away_goals})"
                )
                continue

            db_stats = session.exec(select(MatchStats).where(MatchStats.match_id == db_match.id)).first()
            for col in STATS_COLUMNS:
                csv_val = _opt_int(getattr(row, col))
                db_val = getattr(db_stats, col) if db_stats else None
                if csv_val != db_val:
                    mismatches.append(
                        f"{row.league} {date.date()} {row.home_team}-{row.away_team} : "
                        f"{col} CSV={csv_val} base={db_val}"
                    )

        result["sample_size"] = sample_size
        result["sample_mismatches"] = mismatches
        if mismatches:
            result["ok"] = False
            result["issues"].extend(mismatches)

    logger.info("VALIDATION")
    logger.info(f"  Total base ({result['db_match_count']}) vs CSV valide ({result['csv_valid_rows']}) : "
                f"{'OK' if result['db_match_count'] == result['csv_valid_rows'] else 'ÉCART'}")
    logger.info(f"  match_stats orphelines : {result['orphan_match_stats']} ({'OK' if result['orphan_match_stats'] == 0 else 'PROBLÈME'})")
    n_mismatches = len(result["sample_mismatches"])
    sample_verdict = "0 écart — OK" if n_mismatches == 0 else f"{n_mismatches} écart(s) — voir ci-dessous"
    logger.info(f"  Échantillon ({result['sample_size']} matchs) : {sample_verdict}")
    for m in result["sample_mismatches"]:
        logger.error(f"    {m}")
    logger.info(f"VALIDATION GLOBALE : {'OK' if result['ok'] else 'ÉCHEC'}")
    logger.info("=" * 80)

    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--raw-file", default=str(DEFAULT_RAW_FILE),
                         help="Chemin du CSV source (défaut : data/all_leagues_raw_with_stats.csv)")
    args = parser.parse_args()

    summary = load(Path(args.raw_file))
    sys.exit(0 if summary["validation"]["ok"] else 1)


if __name__ == "__main__":
    main()
