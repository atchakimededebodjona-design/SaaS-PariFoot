"""
scripts/shadow_track_record.py — Phase 7 : XFOOT SHADOW EVALUATION & TRACK
RECORD V1, rapport.
=============================================================================

LECTURE SEULE — aucune écriture DB. Appelle exclusivement les services de
app/ai/arena/track_record.py (eux-mêmes lecture seule sur
model_selection_decisions/shadow_selection_predictions, jamais sur
model_predictions/model_versions/team_ratings/match en écriture) et écrit
UNIQUEMENT des fichiers sous reports/shadow/.

Si AUCUNE prédiction shadow résolue n'existe pour un marché (cas courant :
voir docstring de scripts/model_selection_shadow.py, le mode live nécessite
de vraies fixtures pending — la base locale n'en a aucune au moment de la
Phase 7) : le rapport le dit explicitement (§23 du prompt — "NO SHADOW
DATA"), ne fabrique JAMAIS de tableau ni de métrique.

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/shadow_track_record.py \
        [--markets 1X2 BTTS OVER_UNDER_2_5] [--league Ligue1] \
        [--since 2026-01-01] [--until 2026-12-31] [--outdir reports/shadow]
"""

import argparse
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_API_DIR = _SCRIPTS_DIR.parent / "api"
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_API_DIR))

from sqlmodel import Session, select  # noqa: E402
from app.core.database import engine, init_db  # noqa: E402
from app.models.shadow_selection_prediction import ShadowSelectionPrediction  # noqa: E402
from app.ai.arena.ensemble import MIN_BENCHMARK_SAMPLE_SIZE  # noqa: E402
from app.ai.arena.service import MARKETS  # noqa: E402
from app.ai.arena import track_record  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("shadow_track_record")

ROLLING_LAST_N = (100, 250, 500)
ROLLING_DAYS = (30, 90)


def _dataclass_to_dict(obj) -> dict:
    from dataclasses import asdict, is_dataclass
    return asdict(obj) if is_dataclass(obj) else obj


def _distinct_match_dates(session, market: str, league=None) -> list[date]:
    stmt = select(ShadowSelectionPrediction.match_date).where(
        ShadowSelectionPrediction.market == market, ShadowSelectionPrediction.status == "resolved",
    )
    if league is not None:
        stmt = stmt.where(ShadowSelectionPrediction.league == league)
    rows = session.exec(stmt.distinct()).all()
    return sorted(set(rows))


def main(markets=None, league=None, since=None, until=None, min_sample_size=MIN_BENCHMARK_SAMPLE_SIZE,
         outdir: str = "reports/shadow"):
    markets = markets or list(MARKETS)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    generated_at = datetime.now(timezone.utc).isoformat()

    init_db()
    any_shadow_data = False
    comparisons, cumulative, selection_distribution = {}, {}, {}
    stability_tracking, calibration_tracking, raw_vs_calibrated, rolling_windows = {}, {}, {}, {}

    with Session(engine) as session:
        for market in markets:
            comp = track_record.compare_production_vs_shadow(
                session, market, since=since, until=until, league=league, min_sample_size=min_sample_size,
            )
            comparisons[market] = _dataclass_to_dict(comp)
            if comp.status != "no_shadow_data":
                any_shadow_data = True

            checkpoints = _distinct_match_dates(session, market, league=league)
            if checkpoints:
                any_shadow_data = True
                cum = track_record.compute_cumulative_track_record(
                    session, market, checkpoints, since=since, league=league, min_sample_size=min_sample_size,
                )
                cumulative[market] = [_dataclass_to_dict(c) for c in cum]
            else:
                cumulative[market] = []

            rolling = {}
            for n in ROLLING_LAST_N:
                rolling[f"last_{n}"] = _dataclass_to_dict(
                    track_record.compute_track_record(session, market, last_n=n, league=league, min_sample_size=min_sample_size)
                )
            latest = checkpoints[-1] if checkpoints else None
            for d in ROLLING_DAYS:
                since_d = (latest - timedelta(days=d)) if latest else None
                rolling[f"last_{d}_days"] = _dataclass_to_dict(
                    track_record.compute_track_record(session, market, since=since_d, until=latest, league=league, min_sample_size=min_sample_size)
                )
            rolling_windows[market] = rolling

            selection_distribution[market] = track_record.compute_selection_distribution(session, since=since, until=until, market=market)
            stability_tracking[market] = track_record.compute_stability_tracking(session, since=since, until=until, market=market)
            calibration_tracking[market] = track_record.compute_calibration_tracking(session, since=since, until=until, market=market)
            raw_vs_calibrated[market] = track_record.compare_raw_vs_calibrated(
                session, market, since=since, until=until, league=league, min_sample_size=min_sample_size,
            )
            if selection_distribution[market]["status"] != "no_shadow_data":
                any_shadow_data = True

            logger.info(f"[{market}] comparaison={comp.status}/{comp.conclusion} "
                        f"(N={comp.sample_size}) — sélection={selection_distribution[market]['status']}")

    limitations = [
        "Le mode SHADOW LIVE (scripts/model_selection_shadow.py --mode live) ne produit des prédictions que pour "
        "des fixtures déjà 'pending' en production (model_predictions) — aucun nouvel appel réseau/fixture n'est "
        "jamais effectué par cette phase ; l'échantillon dépend donc entièrement de l'activité réelle de la "
        "production entre deux exécutions.",
        "Les fenêtres glissantes 'last_N' peuvent afficher un échantillon identique si moins de N prédictions "
        "résolues existent au total — toujours vérifier sample_size avant de comparer deux fenêtres entre elles.",
        f"Seuil de significativité pratique utilisé pour toutes les conclusions : "
        f"{track_record.CONCLUSION_RELATIVE_THRESHOLD:.0%} relatif sur log_loss (même seuil que la calibration, Phase 6).",
    ]

    conclusion_lines = []
    for market, comp in comparisons.items():
        conclusion_lines.append(f"{market} : {comp.get('conclusion', 'INSUFFICIENT_DATA')} (N={comp.get('sample_size', 0)})")
    conclusion = "; ".join(conclusion_lines) if conclusion_lines else "Aucune comparaison possible."

    result = {
        "status": "ok" if any_shadow_data else "no_shadow_data",
        "run_id": run_id, "generated_at": generated_at,
        "since": str(since) if since else None, "until": str(until) if until else None, "league": league,
        "markets": markets,
        "comparisons": comparisons,
        "cumulative": cumulative,
        "rolling_windows": rolling_windows,
        "selection_distribution": selection_distribution,
        "stability_tracking": stability_tracking,
        "calibration_tracking": calibration_tracking,
        "raw_vs_calibrated": raw_vs_calibrated,
        "limitations": limitations,
        "conclusion": conclusion if any_shadow_data else "NO SHADOW DATA — aucune prédiction shadow résolue n'existe encore.",
    }

    json_path, md_path = track_record.write_track_record_reports(result, Path(outdir), run_id)
    logger.info(f"Rapports écrits : {json_path} / {md_path}")
    if not any_shadow_data:
        logger.warning("NO SHADOW DATA — aucune donnée shadow résolue trouvée, rien n'a été fabriqué pour ce rapport.")

    print("\nPHASE 7 — XFOOT SHADOW EVALUATION & TRACK RECORD V1 TERMINÉE. "
          "AUCUNE PROMOTION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--markets", nargs="+", default=None, choices=list(MARKETS))
    parser.add_argument("--league", type=str, default=None)
    parser.add_argument("--since", type=str, default=None)
    parser.add_argument("--until", type=str, default=None)
    parser.add_argument("--min-sample-size", type=int, default=MIN_BENCHMARK_SAMPLE_SIZE)
    parser.add_argument("--outdir", type=str, default=str(_SCRIPTS_DIR.parent / "reports" / "shadow"))
    args = parser.parse_args()

    since = date.fromisoformat(args.since) if args.since else None
    until = date.fromisoformat(args.until) if args.until else None

    main(markets=args.markets, league=args.league, since=since, until=until,
         min_sample_size=args.min_sample_size, outdir=args.outdir)
    sys.exit(0)
