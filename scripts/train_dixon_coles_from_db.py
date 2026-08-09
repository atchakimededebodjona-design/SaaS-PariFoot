"""
scripts/train_dixon_coles_from_db.py — Phase 3, tâches 1-2.
=============================================================================

Entraîne Dixon-Coles depuis les tables match/match_stats (plutôt que
data/all_leagues_raw_with_stats.csv), compare les paramètres appris à ceux
actuellement en production (api/model_artifacts/<league>.json), puis
persiste les ratings en base (team_ratings + model_versions) — mais
SEULEMENT si la comparaison ne révèle aucune divergence significative,
conformément à la contrainte centrale du ticket : ce changement
d'infrastructure ne doit produire AUCUNE différence de résultat.

N'ACTIVE ni ne remplace rien côté API : les endpoints /predictions/*
continuent de lire api/model_artifacts/*.json exactement comme avant.

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/train_dixon_coles_from_db.py
"""

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from sqlmodel import Session  # noqa: E402
from app.core.database import engine, init_db  # noqa: E402
from app.models.team_rating import ModelVersion, TeamRating  # noqa: E402
from app.ai.engine.dixon_coles import train_all_leagues_from_db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("train_dixon_coles_from_db")

PRODUCTION_ARTIFACTS_DIR = Path(__file__).resolve().parent.parent / "api" / "model_artifacts"

# Tolérance sous laquelle un écart est attribué au bruit numérique de
# l'optimiseur (SLSQP, méthode de quasi-Newton par différences finies) et
# non à une vraie divergence — le même ordre de grandeur que l'écart déjà
# documenté entre la classe de référence et sa version vectorisée
# (1e-5, voir dixon_coles_fast.py).
TOLERANCE = 1e-4


def _load_production_artifact(league: str) -> dict | None:
    path = PRODUCTION_ARTIFACTS_DIR / f"{league}.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def compare_to_production(db_artifacts: dict) -> dict:
    """
    Compare, pour chaque ligue, attack/defense (par équipe) et
    home_advantage/rho entre l'artefact entraîné depuis la base et
    l'artefact JSON actuellement en production. Retourne un résumé par
    ligue (écart max, sur quel paramètre) et un verdict global.
    """
    results = {}
    max_diff_overall = 0.0

    for league, db_artifact in db_artifacts.items():
        prod = _load_production_artifact(league)
        if prod is None:
            results[league] = {"ok": False, "reason": "aucun artefact de production trouvé"}
            continue

        diffs = {}
        diffs["home_advantage"] = abs(db_artifact["home_advantage"] - prod["home_advantage"])
        diffs["rho"] = abs(db_artifact["rho"] - prod["rho"])

        team_set_db = set(db_artifact["teams"])
        team_set_prod = set(prod["teams"])
        missing = team_set_prod - team_set_db
        extra = team_set_db - team_set_prod

        attack_diffs = {t: abs(db_artifact["attack"][t] - prod["attack"][t])
                         for t in team_set_db & team_set_prod}
        defense_diffs = {t: abs(db_artifact["defense"][t] - prod["defense"][t])
                          for t in team_set_db & team_set_prod}

        max_attack_diff = max(attack_diffs.values()) if attack_diffs else 0.0
        max_defense_diff = max(defense_diffs.values()) if defense_diffs else 0.0
        max_diff = max(diffs["home_advantage"], diffs["rho"], max_attack_diff, max_defense_diff)
        max_diff_overall = max(max_diff_overall, max_diff)

        ok = (not missing and not extra and max_diff <= TOLERANCE
              and db_artifact["trained_on_matches"] == prod["trained_on_matches"])

        results[league] = {
            "ok": ok,
            "trained_on_matches_db": db_artifact["trained_on_matches"],
            "trained_on_matches_prod": prod["trained_on_matches"],
            "missing_teams": sorted(missing),
            "extra_teams": sorted(extra),
            "home_advantage_diff": diffs["home_advantage"],
            "rho_diff": diffs["rho"],
            "max_attack_diff": max_attack_diff,
            "max_defense_diff": max_defense_diff,
            "max_diff": max_diff,
        }

    return {"per_league": results, "max_diff_overall": max_diff_overall,
            "all_ok": all(r.get("ok") for r in results.values())}


def persist_ratings(db_artifacts: dict, comparison_ok: bool) -> int:
    """
    Crée une nouvelle ModelVersion ("xfoot-dixon-coles-v1", dixon_coles) et
    ses TeamRating associées. is_active = True SEULEMENT si la comparison
    de non-régression est passée intégralement — sinon la version est
    persistée à titre d'audit (traçabilité) mais marquée inactive, pour ne
    jamais laisser une divergence non expliquée passer inaperçue.
    """
    from datetime import datetime, timezone

    init_db()
    with Session(engine) as session:
        version = ModelVersion(
            name="xfoot-dixon-coles-v1",
            model_type="dixon_coles",
            trained_at=datetime.now(timezone.utc),
            is_active=comparison_ok,
            notes=("Entraîné depuis match/match_stats (Phase 3) ; non-régression vs "
                   "api/model_artifacts/*.json " + ("validée." if comparison_ok else "ÉCHOUÉE — voir logs.")),
        )
        session.add(version)
        session.commit()
        session.refresh(version)
        version_id = version.id

        n_ratings = 0
        for league, artifact in db_artifacts.items():
            for team in artifact["teams"]:
                session.add(TeamRating(
                    team=team, league=league,
                    attack=artifact["attack"][team], defense=artifact["defense"][team],
                    model_version_id=version_id,
                ))
                n_ratings += 1
        session.commit()

    return version_id


def main():
    logger.info("Entraînement Dixon-Coles depuis match/match_stats...")
    db_artifacts = train_all_leagues_from_db()
    for league, a in db_artifacts.items():
        logger.info(f"  {league:15s} {a['trained_on_matches']:5d} matchs  "
                    f"home_advantage={a['home_advantage']:+.6f}  rho={a['rho']:+.6f}")

    logger.info("=" * 80)
    logger.info("COMPARAISON NON-RÉGRESSION — base vs production (api/model_artifacts/*.json)")
    comparison = compare_to_production(db_artifacts)
    for league, r in comparison["per_league"].items():
        if not r.get("ok", False) and "reason" in r:
            logger.error(f"  [{league}] {r['reason']}")
            continue
        status = "OK" if r["ok"] else "ÉCART SIGNIFICATIF"
        logger.info(f"  [{league}] {status} — matchs db={r['trained_on_matches_db']} "
                    f"prod={r['trained_on_matches_prod']} | "
                    f"écart max={r['max_diff']:.2e} (home_advantage={r['home_advantage_diff']:.2e}, "
                    f"rho={r['rho_diff']:.2e}, attack={r['max_attack_diff']:.2e}, defense={r['max_defense_diff']:.2e})")
        if r["missing_teams"] or r["extra_teams"]:
            logger.warning(f"    équipes manquantes={r['missing_teams']} en trop={r['extra_teams']}")
    logger.info(f"  Écart max toutes ligues confondues : {comparison['max_diff_overall']:.2e} "
                f"(tolérance : {TOLERANCE:.0e})")
    logger.info(f"VERDICT NON-RÉGRESSION : {'OK — aucune divergence significative' if comparison['all_ok'] else 'ÉCHEC — divergence à investiguer'}")
    logger.info("=" * 80)

    version_id = persist_ratings(db_artifacts, comparison["all_ok"])
    logger.info(f"ModelVersion #{version_id} persistée (is_active={comparison['all_ok']}), "
                f"{sum(len(a['teams']) for a in db_artifacts.values())} team_ratings écrites.")

    return comparison


if __name__ == "__main__":
    result = main()
    sys.exit(0 if result["all_ok"] else 1)
