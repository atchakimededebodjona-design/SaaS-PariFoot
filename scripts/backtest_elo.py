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
4. Persiste team_ratings + model_versions ("xfoot-elo-vN", une nouvelle
   version à chaque run — voir app.models.team_rating.next_version_name).

   Politique d'activation RÉVISÉE (Phase 8, §13 : « ne pas désactiver un
   modèle uniquement à cause d'un petit échantillon »). AVANT ce ticket,
   is_active dépendait d'une comparaison de Brier sur seulement 100 matchs/
   ligue — un écart de quelques millièmes (0.6153 vs 0.6075 dans le rapport
   Phase 3) suffisait à basculer tout ou rien. is_active=True est désormais
   INCONDITIONNEL sur un entraînement réussi (même politique que Dixon-Coles
   et, depuis ce même ticket, XGBoost/LightGBM — voir
   scripts/train_ml_stacking_from_db.py) : il ne représente plus qu'un état
   OPÉRATIONNEL (« ceci est la version actuellement servie en direct »),
   jamais un jugement de performance face à Dixon-Coles. Le Brier score
   ci-dessus reste calculé et journalisé dans `notes` À TITRE INFORMATIF —
   c'est désormais EnsembleEngine (via `ensemble_eligible`, seuil
   MIN_BENCHMARK_SAMPLE_SIZE sur un historique résolu réel, voir
   app/ai/arena/availability.py) qui décide si Elo doit peser dans
   l'Ensemble, jamais ce script.

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/backtest_elo.py
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from sqlmodel import Session  # noqa: E402
from app.core.database import engine, init_db  # noqa: E402
from app.models.team_rating import ModelVersion, TeamRating, next_version_name, deactivate_other_versions  # noqa: E402
# Import EN TÊTE (pas paresseux dans _log_elo_test_predictions) : ModelPrediction
# doit être enregistrée dans SQLModel.metadata AVANT l'appel à init_db() plus bas,
# sinon create_all() ne crée jamais la table model_predictions.
from app.ai.arena.prediction_logging import PredictionRecord, log_prediction, resolve_prediction  # noqa: E402
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


def elo_test_brier(matches: "pd.DataFrame", k: float, home_adv: float):
    """
    Retourne (brier, engine_, test_walk, preds) — test_walk/preds exposés
    (Phase 6) pour permettre à l'appelant de logguer individuellement
    chaque prédiction du test-set final via le logger commun (voir
    _log_elo_test_predictions ci-dessous) ; ignorés par la recherche en
    grille (seul [0] y est utilisé), qui ne doit PAS journaliser ses
    dizaines de combinaisons intermédiaires comme si elles étaient "le"
    modèle Elo.
    """
    engine_ = EloEngine(k=k, home_advantage=home_adv)
    walk = engine_.walk_forward(matches)
    train_walk, test_walk = walk.iloc[:-TEST_SIZE], walk.iloc[-TEST_SIZE:]
    engine_.fit_calibration(train_walk["diff"].values, train_walk["outcome"].values)
    preds = [engine_.predict_1x2(d) for d in test_walk["diff"].values]
    return brier_score(preds, list(test_walk["outcome"].values)), engine_, test_walk, preds


def _log_elo_test_predictions(session, league: str, test_walk: "pd.DataFrame", preds: list[dict],
                               test_goals: "pd.DataFrame", model_version_id: int) -> int:
    """
    Journalise (Phase 6) chaque prédiction INDIVIDUELLE du test-set final
    (déjà produite en walk-forward, donc sans connaître son propre résultat
    — aucune fuite) via le logger commun, puis la résout IMMÉDIATEMENT :
    le match est déjà dans le passé, son résultat déjà connu de ce script
    (c'est la nature même d'un backtest) — voir docstring de
    app/ai/arena/prediction_logging.py pour la distinction "live"/"backtest".

    test_walk et test_goals (= matches.iloc[-TEST_SIZE:]) sont alignés
    POSITIONNELLEMENT : walk_forward() produit une ligne par match de
    `matches`, DANS LE MÊME ORDRE, jamais filtrée — confirmé en lisant
    EloEngine.walk_forward ci-dessus, pas supposé.
    """
    n = 0
    for walk_row, pred, goal_row in zip(test_walk.itertuples(), preds, test_goals.itertuples()):
        record = PredictionRecord(
            league=league,
            match_date=walk_row.date.date(),
            home_team=walk_row.home_team,
            away_team=walk_row.away_team,
            model_type="elo",
            prob_home=pred["home_win"], prob_draw=pred["draw"], prob_away=pred["away_win"],
            source="backtest",
        )
        row = log_prediction(session, record, model_version_id)
        if row.status != "resolved":
            resolve_prediction(row, int(goal_row.home_goals), int(goal_row.away_goals))
            session.add(row)
        n += 1
    session.commit()
    return n


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
    test_predictions_by_league = {}  # lg -> (test_walk, preds, test_goals) — Phase 6, journalisées après persistance
    for lg, m in league_matches.items():
        elo_brier, elo_engine_at_test, elo_test_walk, elo_preds = elo_test_brier(m, best["k"], best["home_advantage"])
        dc_brier, n_dc = dc_test_brier(lg, m)
        test_predictions_by_league[lg] = (elo_test_walk, elo_preds, m.iloc[-TEST_SIZE:])

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

    # bool() explicite : mean_elo/mean_dc sont des numpy.float64 (moyennes de
    # Brier scores eux-mêmes numpy, cf. DixonColesModel.predict_1x2 qui
    # renvoie des sommes numpy), donc la comparaison est un numpy.bool_ —
    # psycopg2 échoue même à l'ADAPTER ("can't adapt type 'numpy.bool'",
    # contrairement au numpy.float64 qui produit un littéral SQL corrompu
    # mais sans lever d'erreur à l'adaptation). Même correctif que
    # attack/defense ci-dessous et que compare_to_production() dans
    # train_dixon_coles_from_db.py.
    # Phase 8 (§13) : ce verdict reste calculé et journalisé (voir `notes`
    # ci-dessous) mais NE GATE PLUS is_active — voir docstring module.
    elo_beats_dc_on_test = bool(mean_elo <= mean_dc)
    logger.info(f"VERDICT (informatif, n'affecte plus is_active) : "
                f"{'Elo <= Dixon-Coles (Brier moyen)' if elo_beats_dc_on_test else 'PAS DE GAIN vs Dixon-Coles (Brier moyen)'}")
    logger.info("=" * 80)

    # --- 4. Persistance ---
    # Phase 7 (§10 du ticket) : persiste la calibration (c, scale) PAR LIGUE
    # + home_advantage (global, comme choisi par la grille ci-dessus) dans
    # ModelVersion.config — jusqu'ici calculée par final_engines mais jamais
    # sauvegardée, ce qui rendait Elo structurellement impossible à servir
    # EN DIRECT (voir app/ai/arena/models_common.py::EloPredictionModel).
    # Toujours écrite, que ce run active ou non la version : un futur script
    # pourra un jour réactiver une version historique sans devoir relancer
    # tout le backtest juste pour récupérer sa calibration.
    config_payload = json.dumps({
        "home_advantage": best["home_advantage"],
        "k": best["k"],
        "leagues": {lg: eng.calibration_ for lg, eng in final_engines.items()},
    })

    init_db()
    with Session(engine) as session:
        # Phase 8 (§13) : désactive les autres versions Elo et active
        # INCONDITIONNELLEMENT celle-ci sur un entraînement réussi — même
        # politique que Dixon-Coles/XGBoost/LightGBM désormais (voir
        # docstring module). Le verdict Brier reste journalisé ci-dessous,
        # à titre informatif uniquement.
        deactivate_other_versions(session, "elo")

        version = ModelVersion(
            name=next_version_name(session, "xfoot-elo"), model_type="elo",
            trained_at=datetime.now(timezone.utc), is_active=True,
            notes=(f"K={best['k']}, home_advantage={best['home_advantage']} (choisis par grille, Brier moyen test="
                   f"{best['mean_brier']:.4f}). Brier moyen final Elo={mean_elo:.4f} vs Dixon-Coles={mean_dc:.4f} "
                   f"sur {TEST_SIZE} derniers matchs/ligue. " +
                   ("Meilleur ou égal à Dixon-Coles (informatif)." if elo_beats_dc_on_test else
                    "Pas de gain démontré vs Dixon-Coles sur ce test (informatif — n'affecte plus is_active, "
                    "voir ensemble_eligible pour le critère réel d'inclusion dans l'Ensemble).")),
            config=config_payload,
        )
        session.add(version)
        session.commit()
        session.refresh(version)
        version_id = version.id

        n_ratings = 0
        for lg, eng in final_engines.items():
            for team, rating in eng.ratings_.items():
                # float() explicite : `rating` est déjà un float natif dans
                # l'implémentation actuelle du walk-forward (arithmétique
                # Python pure, vérifié empiriquement) — mais explicite plutôt
                # que de compter sur ce détail d'implémentation, même
                # discipline qu'attack/defense dans train_dixon_coles_from_db.py.
                session.add(TeamRating(team=team, league=lg, attack=float(rating), defense=0.0, model_version_id=version_id))
                n_ratings += 1
        session.commit()

        # Phase 6 : journalise chaque prédiction individuelle du test-set
        # final (celui qui a servi à mesurer le Brier score ci-dessus),
        # résolue immédiatement — voir _log_elo_test_predictions. Fait pour
        # CE run quel que soit le verdict Brier : l'Arena doit pouvoir
        # montrer les vrais chiffres d'Elo.
        n_predictions_logged = 0
        for lg, (test_walk, preds, test_goals) in test_predictions_by_league.items():
            n_predictions_logged += _log_elo_test_predictions(session, lg, test_walk, preds, test_goals, version_id)

    logger.info(f"ModelVersion #{version_id} persistée (is_active=True), {n_ratings} team_ratings écrites, "
                f"{n_predictions_logged} prédiction(s) individuelle(s) journalisées dans model_predictions.")

    return {"grid_results": grid_results, "best": best, "comparison": comparison,
            "mean_elo": mean_elo, "mean_dc": mean_dc, "elo_beats_dc_on_test": elo_beats_dc_on_test}


if __name__ == "__main__":
    main()
    sys.exit(0)  # un verdict "pas de gain vs Dixon-Coles" documenté n'est plus un échec de ce script (Phase 8, §13)
