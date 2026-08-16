"""
test_ml_stacking.py — Non-régression Phase 4 (XGBoost/LightGBM, features
nouvelles depuis match/match_stats).

Copie api/app.db dans un fichier temporaire ISOLÉ (même précaution que
test_dixon_coles_db.py/test_elo_backtest.py) — jamais écrit dans le dev DB
réel.

CHOIX DÉLIBÉRÉ DE PÉRIMÈTRE : ce test n'appelle PAS build_ml_features_from_db()
(le pipeline complet, 5 ligues) ni scripts/train_ml_stacking_from_db.py en
entier. build_ml_features_from_db() réutilise build_dixon_coles_features de
build_features.py, qui refait un fit Dixon-Coles PAR DATE DE MATCH PAR LIGUE
(walk-forward) — mesuré à ~35 min sur les 12 459 matchs de la base de dev,
le même ordre de coût que build_features.py lui-même (jamais exécuté par un
test automatisé dans ce dépôt, seulement en script autonome). Imposer ce
coût à la suite de tests casserait la convention déjà en place (tous les
tests existants se terminent en quelques secondes).

Ce que ce test couvre à la place, avec une vraie protection de non-régression
sur tout ce qui EST nouveau dans ce ticket :
  1. build_shot_stats_features / build_streak_features (les fonctions
     réellement nouvelles) sur des DataFrames synthétiques — logique de
     fuite, gestion des NaN (stats manquantes), arithmétique des séries,
     comportement de la fenêtre glissante. Rapide (aucune base, aucun fit).
  2. load_matches_with_stats_from_db() — la jointure match/match_stats
     elle-même (une requête SQL simple, rapide), sur la base de dev copiée.
  3. persist_model_version() — le contrat de persistance exigé par le
     ticket (is_active=False INCONDITIONNELLEMENT, aucune TeamRating créée)
     avec des métriques fabriquées, sans entraînement réel.

Le pipeline complet (features + XGBoost + LightGBM + verdict) est validé en
exécutant scripts/train_ml_stacking_from_db.py directement (voir son
docstring "Usage") — même statut que train_dixon_coles_from_db.py/
backtest_elo.py, jamais rejoués automatiquement par un test.

Usage : python test_ml_stacking.py (depuis api/)
"""

import os
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

_SOURCE_DB = Path(__file__).resolve().parent / "app.db"
_TEST_DB = Path(__file__).resolve().parent / "test_ml_stacking.db"
if _TEST_DB.exists():
    _TEST_DB.unlink()
if not _SOURCE_DB.exists():
    raise SystemExit(f"{_SOURCE_DB} introuvable — lancer scripts/load_historical_matches.py d'abord.")
shutil.copy2(_SOURCE_DB, _TEST_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB}"

from sqlmodel import Session, select  # noqa: E402
from app.core.database import engine  # noqa: E402
from app.models.match import Match, MatchStats  # noqa: E402
from app.models.team_rating import ModelVersion, TeamRating  # noqa: E402
from app.ai.engine.features import (  # noqa: E402
    load_matches_with_stats_from_db, build_shot_stats_features, build_streak_features,
    SHOT_STATS_WINDOW,
)
from train_ml_stacking_from_db import persist_model_version  # noqa: E402


def _synthetic_matches(rows: list[dict]) -> pd.DataFrame:
    """Construit un DataFrame conforme au contrat d'entrée de
    build_shot_stats_features/build_streak_features (déjà trié par date,
    une seule ligue) à partir d'une liste de dicts partiels — remplit les
    colonnes de stats absentes à None (match sans stats)."""
    base = {"home_shots": None, "away_shots": None, "home_shots_target": None,
            "away_shots_target": None, "home_corners": None, "away_corners": None}
    full_rows = [{**base, **r} for r in rows]
    df = pd.DataFrame(full_rows)
    df["date"] = pd.to_datetime(df["date"])
    return df


def test_shot_stats_features_no_leakage_first_appearance_nan():
    df = _synthetic_matches([
        {"date": "2024-01-01", "home_team": "A", "away_team": "B", "home_goals": 1, "away_goals": 0,
         "home_shots": 10, "away_shots": 4, "home_shots_target": 6, "away_shots_target": 2,
         "home_corners": 5, "away_corners": 1},
    ])
    feats = build_shot_stats_features(df)
    assert feats["home_shots_diff_avg"].isna().all(), "1er match d'une équipe : doit être NaN (aucun historique)"
    assert feats["away_shots_diff_avg"].isna().all(), "idem pour l'équipe qui reçoit à l'extérieur"
    print("  [OK] Premier match d'une équipe -> NaN (aucune fuite de la valeur du match courant)")


def test_shot_stats_features_rolling_average_correct():
    df = _synthetic_matches([
        {"date": "2024-01-01", "home_team": "A", "away_team": "B", "home_goals": 2, "away_goals": 0,
         "home_shots": 10, "away_shots": 4, "home_shots_target": 6, "away_shots_target": 2,
         "home_corners": 5, "away_corners": 1},
        {"date": "2024-01-08", "home_team": "C", "away_team": "A", "home_goals": 0, "away_goals": 1,
         "home_shots": 3, "away_shots": 12, "home_shots_target": 1, "away_shots_target": 7,
         "home_corners": 2, "away_corners": 6},
    ])
    feats = build_shot_stats_features(df)
    # Ligne 1 : A joue à l'extérieur, son seul match passé est la ligne 0 (A à domicile : for=10, against=4)
    expected_away_diff = 10.0 - 4.0
    assert feats["away_shots_diff_avg"].iloc[1] == expected_away_diff, (
        f"attendu {expected_away_diff}, obtenu {feats['away_shots_diff_avg'].iloc[1]}"
    )
    expected_target_diff = 6.0 - 2.0
    assert feats["away_shots_target_diff_avg"].iloc[1] == expected_target_diff
    expected_corners_diff = 5.0 - 1.0
    assert feats["away_corners_diff_avg"].iloc[1] == expected_corners_diff
    print("  [OK] Moyenne glissante FOR-AGAINST correcte sur un historique d'un seul match passé")


def test_shot_stats_features_missing_stats_excluded_not_zero():
    df = _synthetic_matches([
        {"date": "2024-01-01", "home_team": "A", "away_team": "B", "home_goals": 2, "away_goals": 0,
         "home_shots": 10, "away_shots": 4, "home_shots_target": 6, "away_shots_target": 2,
         "home_corners": 5, "away_corners": 1},
        # match sans stats (nullable en base) -- ne doit ni compter comme 0 ni casser le calcul suivant
        {"date": "2024-01-08", "home_team": "A", "away_team": "C", "home_goals": 1, "away_goals": 1},
        {"date": "2024-01-15", "home_team": "D", "away_team": "A", "home_goals": 0, "away_goals": 3,
         "home_shots": 2, "away_shots": 9, "home_shots_target": 1, "away_shots_target": 5,
         "home_corners": 1, "away_corners": 4},
    ])
    feats = build_shot_stats_features(df)
    # Ligne 2 : l'historique "stats" de A ne doit contenir QUE la ligne 0 (la ligne 1 est sans stats,
    # donc absente de l'historique) -- pas de moyenne sur 2 matchs, pas de 0 fabriqué pour la ligne 1.
    expected = 10.0 - 4.0
    assert feats["away_shots_diff_avg"].iloc[2] == expected, (
        f"le match sans stats a dû être exclu de la moyenne glissante, attendu {expected}, "
        f"obtenu {feats['away_shots_diff_avg'].iloc[2]}"
    )
    print("  [OK] Match sans match_stats exclu de l'historique (ni compté, ni fabriqué à 0)")


def test_shot_stats_features_window_drops_oldest():
    rows = []
    for i in range(SHOT_STATS_WINDOW + 2):
        rows.append({
            "date": f"2024-01-{i + 1:02d}", "home_team": "A", "away_team": f"OPP{i}",
            "home_goals": 1, "away_goals": 0,
            "home_shots": 1, "away_shots": 0,  # for=1, against=0 sur tous les matchs SAUF le tout premier
            "home_shots_target": 1, "away_shots_target": 0,
            "home_corners": 1, "away_corners": 0,
        })
    # Le tout premier match de A a des stats très différentes -- doit sortir de la fenêtre une fois
    # que A a joué SHOT_STATS_WINDOW matchs plus récents.
    rows[0].update({"home_shots": 100, "away_shots": 0, "home_shots_target": 100,
                     "away_shots_target": 0, "home_corners": 100, "away_corners": 0})
    df = _synthetic_matches(rows)
    feats = build_shot_stats_features(df)
    # À la dernière ligne, A a déjà joué SHOT_STATS_WINDOW + 1 matchs PASSÉS -- le tout premier
    # (for=100) doit être sorti de la fenêtre de SHOT_STATS_WINDOW.
    last_diff = feats["home_shots_diff_avg"].iloc[-1]
    assert last_diff < 50, (
        f"le match le plus ancien (for=100) devrait être sorti de la fenêtre de {SHOT_STATS_WINDOW}, "
        f"obtenu diff={last_diff}"
    )
    assert last_diff == 1.0, f"attendu diff=1.0 (for=1, against=0 sur les {SHOT_STATS_WINDOW} matchs les plus récents), obtenu {last_diff}"
    print(f"  [OK] Fenêtre glissante ({SHOT_STATS_WINDOW} matchs) exclut bien les matchs plus anciens")


def test_streak_features_all_transitions():
    df = _synthetic_matches([
        {"date": "2024-01-01", "home_team": "A", "away_team": "X", "home_goals": 1, "away_goals": 0},  # A win
        {"date": "2024-01-08", "home_team": "A", "away_team": "Y", "home_goals": 2, "away_goals": 0},  # A win
        {"date": "2024-01-15", "home_team": "A", "away_team": "Z", "home_goals": 0, "away_goals": 1},  # A loss
        {"date": "2024-01-22", "home_team": "A", "away_team": "W", "home_goals": 1, "away_goals": 1},  # A draw
        {"date": "2024-01-29", "home_team": "A", "away_team": "V", "home_goals": 3, "away_goals": 0},  # A win
    ])
    feats = build_streak_features(df)
    streak = feats["home_current_streak"]
    assert np.isnan(streak.iloc[0]), "1er match de A : aucun historique -> NaN"
    assert streak.iloc[1] == 1, f"après 1 victoire, streak attendu=1, obtenu={streak.iloc[1]}"
    assert streak.iloc[2] == 2, f"après 2 victoires, streak attendu=2, obtenu={streak.iloc[2]}"
    assert streak.iloc[3] == -1, f"après 1 défaite (venant d'une série de victoires), streak attendu=-1, obtenu={streak.iloc[3]}"
    assert streak.iloc[4] == 0, f"après un nul, streak attendu=0, obtenu={streak.iloc[4]}"
    print("  [OK] Séries signées correctes (victoire/défaite/nul, y compris les changements de signe)")


def test_load_matches_with_stats_join_matches_row_count():
    with Session(engine) as session:
        n_match = len(session.exec(select(Match)).all())
        # home_shots est nullable (voir match.py : 1 ligne sur 12459 sans stats renseignées,
        # bien qu'une ligne match_stats existe pour CHAQUE match) -- c'est CE compte,
        # pas le nombre total de lignes match_stats, qui doit correspondre après jointure.
        n_with_shots = len(session.exec(select(MatchStats).where(MatchStats.home_shots.is_not(None))).all())
    assert n_match > 0, "table match vide -- lancer scripts/load_historical_matches.py d'abord"

    df = load_matches_with_stats_from_db()
    assert len(df) == n_match, f"la jointure ne doit perdre aucune ligne : match={n_match}, jointe={len(df)}"
    for col in ("home_shots", "away_shots", "home_shots_target", "away_shots_target", "home_corners", "away_corners"):
        assert col in df.columns, f"colonne {col} manquante après jointure"
    n_with_stats = df["home_shots"].notna().sum()
    assert n_with_stats == n_with_shots, (
        f"lignes avec home_shots renseigné après jointure ({n_with_stats}) != en base ({n_with_shots})"
    )
    print(f"  [OK] Jointure match/match_stats : {len(df)} lignes, {n_with_stats} avec stats renseignées")


def test_persist_model_version_never_activates_and_no_team_rating():
    fake_metrics = {
        "model_logloss": 1.05, "dc_logloss": 1.02, "model_acc": 0.5, "dc_acc": 0.52,
        "frac_better": 0.6, "delta": -0.03, "gain": False, "delta_ci95": (-0.06, 0.01),
    }
    version_id = persist_model_version("xgboost", fake_metrics, ["dc_home_win", "home_shots_diff_avg"])

    with Session(engine) as session:
        version = session.get(ModelVersion, version_id)
        assert version is not None
        assert version.model_type == "xgboost"
        assert version.is_active is False, "ne doit JAMAIS s'auto-activer dans ce ticket, quel que soit le verdict"
        assert "PAS DE GAIN" in version.notes

        ratings = session.exec(select(TeamRating).where(TeamRating.model_version_id == version_id)).all()
        assert ratings == [], "aucune TeamRating ne doit être créée pour un modèle à features (pas de rating par équipe)"

    # Verdict positif -- doit quand même rester is_active=False (contrainte explicite du ticket).
    fake_metrics_gain = {**fake_metrics, "model_logloss": 0.90, "delta": 0.12, "frac_better": 0.95, "gain": True}
    version_id_2 = persist_model_version("lightgbm", fake_metrics_gain, ["dc_home_win"])
    with Session(engine) as session:
        version2 = session.get(ModelVersion, version_id_2)
        assert version2.is_active is False, "même avec un gain crédible, is_active doit rester False dans ce ticket"

    print("  [OK] ModelVersion créée avec is_active=False dans les deux cas (gain ou non), aucune TeamRating")


if __name__ == "__main__":
    tests = [
        test_shot_stats_features_no_leakage_first_appearance_nan,
        test_shot_stats_features_rolling_average_correct,
        test_shot_stats_features_missing_stats_excluded_not_zero,
        test_shot_stats_features_window_drops_oldest,
        test_streak_features_all_transitions,
        test_load_matches_with_stats_join_matches_row_count,
        test_persist_model_version_never_activates_and_no_team_rating,
    ]
    passed = 0
    for test in tests:
        print(f"\n=== {test.__name__} ===")
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  [ÉCHEC] {e}")

    print("\n" + "=" * 60)
    print(f"{passed}/{len(tests)} tests reussis")
    print("=" * 60)
    try:
        _TEST_DB.unlink(missing_ok=True)
    except PermissionError:
        pass  # verrou Windows bref sur le fichier SQLite -- sans conséquence
    sys.exit(0 if passed == len(tests) else 1)
