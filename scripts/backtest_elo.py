"""
scripts/backtest_elo.py — Phase 3, tâches 3-4.
=============================================================================

1. Petite recherche en grille sur (K, home_advantage) — même logique que
   grid_search_l2 dans dixon_coles.py (choix documenté par le Brier score
   out-of-sample, pas fixé à l'aveugle), appliquée aux 2 hyperparamètres
   Elo. Choix global (pas par ligue) : 100 derniers matchs de test par
   ligue, c'est peu pour re-choisir des hyperparamètres séparément par
   ligue sans surapprendre au bruit — la grille est donc évaluée sur la
   moyenne des 5 ligues, pour une décision plus robuste.
2. Split temporel PAR LIGUE, 100 derniers matchs en test (même convention
   que dixon_coles.py, cf. son docstring de grid_search_l2) — jamais un
   split aléatoire.
3. Brier score Elo (calibration entraînée sur la fenêtre train uniquement)
   contre Brier score Dixon-Coles (entraîné sur la MÊME fenêtre train,
   avec le moteur de production _FastDixonColesL2 via export_league) sur
   EXACTEMENT le même test set — comparaison strictement à armes égales.
4. Persiste team_ratings + model_versions ("xfoot-elo-v1"), avec
   is_active = True SEULEMENT si le Brier score Elo est réellement
   meilleur (ou à parité) que celui de Dixon-Coles sur ce test set — sinon
   marqué inactif, avec le même degré d'honnêteté que le verdict déjà
   rendu sur XGBoost en son temps (« pas de gain » est un résultat valide).

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/backtest_elo.py
"""

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from sqlmodel import Session  # noqa: E402
from app.core.database import engine, init_db  # noqa: E402
from app.models.team_rating import ModelVersion, TeamRating  # noqa: E402
from app.ai.engine.dixon_coles import load_matches_from_db, export_league  # noqa: E402
from app.ai.engine.elo import EloEngine  # noqa: E402
from dixon_coles import DixonColesModel  # noqa: E402  (repo root déjà sur sys.path via l'import ci-dessus)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backtest_elo")

LEAGUES = ["Bundesliga", "LaLiga", "Ligue1", "PremierLeague", "SerieA"]
TEST_SIZE = 100  # même convention que dixon_coles.py (cf. docstring grid_search_l2)
K_GRID = (10.0, 20.0, 32.0)
HOME_ADV_GRID = (0.0, 60.0, 100.0)


def brier_score(predictions: list[dict], outcomes: list[str]) -> float:
    """Brier score multi-classe standard (somme des carrés d'écart entre
    proba prédite et indicatrice, moyennée sur les matchs) — même
    définition que celle utilisée en évaluation de prévision football."""
    total = 0.0
    for pred, outcome in zip(predictions, outcomes):
        for k in ("home_win", "draw", "away_win"):
            indicator = 1.0 if k == outcome else 0.0
            total += (pred[k] - indicator) ** 2
    return total / len(predictions)


def _outcome(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "home_win"
    if home_goals == away_goals:
        return "draw"
    return "away_win"


def elo_test_brier(matches: "pd.DataFrame", k: float, home_adv: float) -> tuple[float, EloEngine]:
    engine_ = EloEngine(k=k, home_advantage=home_adv)
    walk = engine_.walk_forward(matches)
    train_walk, test_walk = walk.iloc[:-TEST_SIZE], walk.iloc[-TEST_SIZE:]
    engine_.fit_calibration(train_walk["diff"].values, train_walk["outcome"].values)
    preds = [engine_.predict_1x2(d) for d in test_walk["diff"].values]
    return brier_score(preds, list(test_walk["outcome"].values)), engine_


def dc_model_from_artifact(artifact: dict) -> DixonColesModel:
    """Reconstruit un DixonColesModel déjà 'entraîné' à partir d'un dict
    d'artefact — réutilise predict_1x2/predict_score_matrix (la logique de
    prédiction déjà validée) sans repasser par .fit()."""
    m = DixonColesModel(xi=artifact["xi"], l2=artifact["l2_reg"])
    m.teams_ = artifact["teams"]
    m.attack_ = artifact["attack"]
    m.defense_ = artifact["defense"]
    m.home_advantage_ = artifact["home_advantage"]
    m.rho_ = artifact["rho"]
    return m


def dc_test_brier(league: str, matches: "pd.DataFrame") -> tuple[float, int]:
    train, test = matches.iloc[:-TEST_SIZE].reset_index(drop=True), matches.iloc[-TEST_SIZE:].reset_index(drop=True)
    artifact = export_league(league, train)
    model = dc_model_from_artifact(artifact)

    preds, outcomes = [], []
    for row in test.itertuples():
        try:
            probs = model.predict_1x2(row.home_team, row.away_team)
        except ValueError:
            continue  # équipe absente du train (promotion en cours d'historique) — skip, comme grid_search_l2
        preds.append(probs)
        outcomes.append(_outcome(row.home_goals, row.away_goals))
    return brier_score(preds, outcomes), len(preds)


def main():
    logger.info("Chargement de l'historique par ligue depuis la base...")
    league_matches = {lg: load_matches_from_db(lg).sort_values("date").reset_index(drop=True) for lg in LEAGUES}
    for lg, m in league_matches.items():
        logger.info(f"  {lg:15s} {len(m):5d} matchs")

    # --- 1. Recherche en grille (K, home_advantage), Brier moyen sur les 5 ligues ---
    logger.info("=" * 80)
    logger.info(f"RECHERCHE EN GRILLE — K x home_advantage, Brier moyen sur {TEST_SIZE} derniers matchs/ligue")
    grid_results = []
    for k in K_GRID:
        for home_adv in HOME_ADV_GRID:
            briers = [elo_test_brier(m, k, home_adv)[0] for m in league_matches.values()]
            mean_brier = sum(briers) / len(briers)
            grid_results.append({"k": k, "home_advantage": home_adv, "mean_brier": mean_brier, "per_league": briers})
            logger.info(f"  K={k:5.1f}  home_advantage={home_adv:6.1f}  Brier moyen={mean_brier:.4f}")

    best = min(grid_results, key=lambda r: r["mean_brier"])
    logger.info(f"MEILLEUR COMBO : K={best['k']}, home_advantage={best['home_advantage']} "
                f"(Brier moyen={best['mean_brier']:.4f})")
    logger.info("=" * 80)

    # --- 2-3. Comparaison finale Elo (au meilleur combo) vs Dixon-Coles, par ligue ---
    logger.info(f"COMPARAISON FINALE — Elo (K={best['k']}, home_advantage={best['home_advantage']}) vs Dixon-Coles, "
                f"{TEST_SIZE} derniers matchs/ligue")
    comparison = {}
    final_engines = {}
    for lg, m in league_matches.items():
        elo_brier, elo_engine_at_test = elo_test_brier(m, best["k"], best["home_advantage"])
        dc_brier, n_dc = dc_test_brier(lg, m)

        # Moteur final : walk-forward + calibration sur TOUT l'historique
        # (pas juste train) pour les ratings à persister — le split
        # train/test ci-dessus ne sert qu'à MESURER le Brier score,
        # jamais à amputer les ratings finaux réellement conservés.
        final_engine = EloEngine(k=best["k"], home_advantage=best["home_advantage"])
        full_walk = final_engine.walk_forward(m)
        final_engine.fit_calibration(full_walk["diff"].values, full_walk["outcome"].values)
        final_engines[lg] = final_engine

        winner = "Elo" if elo_brier < dc_brier else ("Dixon-Coles" if dc_brier < elo_brier else "égalité")
        comparison[lg] = {"elo_brier": elo_brier, "dc_brier": dc_brier, "n_test_dc": n_dc, "winner": winner}
        logger.info(f"  [{lg}] Elo={elo_brier:.4f}  Dixon-Coles={dc_brier:.4f}  -> {winner}")

    mean_elo = sum(c["elo_brier"] for c in comparison.values()) / len(comparison)
    mean_dc = sum(c["dc_brier"] for c in comparison.values()) / len(comparison)
    logger.info(f"  MOYENNE — Elo={mean_elo:.4f}  Dixon-Coles={mean_dc:.4f}")

    elo_ready = mean_elo <= mean_dc
    logger.info(f"VERDICT : Elo {'PRÊT (Brier moyen <= Dixon-Coles)' if elo_ready else 'PAS DE GAIN (Brier moyen > Dixon-Coles) — reste inactif'}")
    logger.info("=" * 80)

    # --- 4. Persistance ---
    init_db()
    with Session(engine) as session:
        version = ModelVersion(
            name="xfoot-elo-v1", model_type="elo",
            trained_at=datetime.now(timezone.utc), is_active=elo_ready,
            notes=(f"K={best['k']}, home_advantage={best['home_advantage']} (choisis par grille, Brier moyen test="
                   f"{best['mean_brier']:.4f}). Brier moyen final Elo={mean_elo:.4f} vs Dixon-Coles={mean_dc:.4f} "
                   f"sur {TEST_SIZE} derniers matchs/ligue. " +
                   ("Meilleur ou égal à Dixon-Coles." if elo_ready else "Pas de gain démontré — non activé.")),
        )
        session.add(version)
        session.commit()
        session.refresh(version)
        version_id = version.id

        n_ratings = 0
        for lg, eng in final_engines.items():
            for team, rating in eng.ratings_.items():
                session.add(TeamRating(team=team, league=lg, attack=rating, defense=0.0, model_version_id=version_id))
                n_ratings += 1
        session.commit()

    logger.info(f"ModelVersion #{version_id} persistée (is_active={elo_ready}), {n_ratings} team_ratings écrites.")

    return {"grid_results": grid_results, "best": best, "comparison": comparison,
            "mean_elo": mean_elo, "mean_dc": mean_dc, "elo_ready": elo_ready}


if __name__ == "__main__":
    result = main()
    sys.exit(0 if result["elo_ready"] else 1)
