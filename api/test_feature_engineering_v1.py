"""
test_feature_engineering_v1.py — Phase 8B : tests de
app/ai/features/research_features_v1.py, app/ai/features/feature_sets.py et
scripts/feature_engineering_walkforward.py.

Base isolée dédiée (jamais api/app.db) — même précaution que les suites de
tests précédentes (voir _test_support.py). Aucun test n'entraîne un modèle
sur un dataset volumineux (voir scripts/feature_engineering_walkforward.py
pour le run réel sur `match` complet) — uniquement des DataFrames synthétiques
de taille réduite, construits en base isolée.

Usage : python api/test_feature_engineering_v1.py
"""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_feature_engineering_v1.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match, MatchStats
from app.ai.features.registry import FEATURE_REGISTRY
from app.ai.features.research_features_v1 import (
    PHASE8B_FEATURE_REGISTRY, _season_of,
    build_homeaway_split_features, build_schedule_density_features,
    build_dixon_coles_strength_features, build_elo_strength_features,
    build_league_standing_features, build_season_features,
    build_all_research_groups, validate_season_rule,
)
from app.ai.features.feature_sets import FEATURE_GROUPS, EXPERIMENTS, DUPLICATE_OF_BASELINE, feature_columns_for
from app.ai.engine.elo import EloEngine

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import feature_engineering_walkforward as fewf  # noqa: E402

init_db()


def _clean_all():
    with Session(engine) as session:
        for row in session.exec(select(MatchStats)).all():
            session.delete(row)
        for row in session.exec(select(Match)).all():
            session.delete(row)
        session.commit()


def _make_df(rows: list[dict]) -> pd.DataFrame:
    """Construit un DataFrame trié par date, contrat attendu par toutes les
    fonctions de research_features_v1.py (une seule ligue, déjà triée)."""
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date", kind="stable").reset_index(drop=True)


def _round_robin_rows(league, teams, n_rounds, start_date, score_fn=None):
    # Scores tirés d'une loi de Poisson réaliste (seed fixe, reproductible) plutôt qu'un
    # motif déterministe (r+i)%3 : ce dernier produit des scores parfaitement corrélés à
    # l'ordre des équipes, ce qui rend le fit Dixon-Coles (fast_fit) dégénéré/non convergé
    # sur un historique synthétique de petite taille (peu de variance résiduelle).
    rng = np.random.default_rng(20260829)
    rows = []
    d = start_date
    for r in range(n_rounds):
        for i in range(len(teams)):
            home, away = teams[i], teams[(i + 1) % len(teams)]
            hg, ag = score_fn(r, i) if score_fn else (int(rng.poisson(1.4)), int(rng.poisson(1.1)))
            rows.append({"league": league, "date": d, "home_team": home, "away_team": away,
                         "home_goals": hg, "away_goals": ag})
            d += timedelta(days=1)
    return rows


# ---------------------------------------------------------------------------
# 1. Registre additif Phase 8B — jamais fusionné à la production (§4)
# ---------------------------------------------------------------------------

def test_phase8b_registry_no_collision_with_production_registry():
    collisions = set(PHASE8B_FEATURE_REGISTRY) & set(FEATURE_REGISTRY)
    assert not collisions, f"Collision de noms avec le registre de production : {collisions}"
    print(f"  [OK] {len(PHASE8B_FEATURE_REGISTRY)} features Phase 8B, aucune collision avec les {len(FEATURE_REGISTRY)} de production")


def test_phase8b_registry_required_fields_and_experimental_status():
    required = ["feature_name", "category", "description", "source", "data_type",
                "availability", "cutoff_rule", "leakage_risk", "missing_value_strategy", "status"]
    for name, fd in PHASE8B_FEATURE_REGISTRY.items():
        for field_name in required:
            assert getattr(fd, field_name), f"{name}.{field_name} vide"
        assert fd.feature_name == name
        assert fd.status == "EXPERIMENTAL", f"{name}: status={fd.status}, attendu EXPERIMENTAL (jamais PRODUCTION en Phase 8B)"
        assert fd.current_model_usage == [], f"{name}: current_model_usage non vide — jamais consommée par un modèle de production"
    print(f"  [OK] {len(PHASE8B_FEATURE_REGISTRY)} features Phase 8B : champs complets, toutes EXPERIMENTAL, current_model_usage=[]")


def test_feature_groups_reference_only_known_columns():
    all_registered = set(PHASE8B_FEATURE_REGISTRY)
    for group, cols in FEATURE_GROUPS.items():
        if cols == DUPLICATE_OF_BASELINE:
            continue
        for c in cols:
            assert c in all_registered, f"Groupe {group} référence '{c}', absent de PHASE8B_FEATURE_REGISTRY"
    print("  [OK] toutes les colonnes de FEATURE_GROUPS sont enregistrées dans PHASE8B_FEATURE_REGISTRY")


# ---------------------------------------------------------------------------
# 2. GROUP C — Home/Away split
# ---------------------------------------------------------------------------

def test_homeaway_split_uses_only_same_venue_history():
    # A joue 2 fois à domicile (victoires), puis 1 fois à l'extérieur (défaite) ; sa forme
    # domicile ne doit PAS être polluée par sa défaite à l'extérieur.
    rows = [
        {"league": "L", "date": date(2024, 1, 1), "home_team": "A", "away_team": "B", "home_goals": 3, "away_goals": 0},
        {"league": "L", "date": date(2024, 1, 8), "home_team": "A", "away_team": "C", "home_goals": 2, "away_goals": 0},
        {"league": "L", "date": date(2024, 1, 15), "home_team": "B", "away_team": "A", "home_goals": 3, "away_goals": 0},
        {"league": "L", "date": date(2024, 1, 22), "home_team": "A", "away_team": "D", "home_goals": 1, "away_goals": 1},
    ]
    df = _make_df(rows)
    out = build_homeaway_split_features(df)
    # match #4 (A domicile) : historique domicile de A = les 2 premiers matchs (2 victoires) uniquement.
    assert out.loc[3, "home_home_win_rate_last5"] == 1.0
    assert out.loc[3, "home_home_goals_scored_avg_last5"] == 2.5
    print("  [OK] home_home_win_rate_last5 ignore la défaite extérieure de A (isolation stricte par contexte)")


def test_homeaway_split_excludes_future_matches():
    rows = [
        {"league": "L", "date": date(2024, 1, 1), "home_team": "A", "away_team": "B", "home_goals": 1, "away_goals": 0},
        {"league": "L", "date": date(2024, 1, 8), "home_team": "A", "away_team": "C", "home_goals": 0, "away_goals": 0},
        # match FUTUR, score extrême reconnaissable — ne doit influencer aucune ligne antérieure.
        {"league": "L", "date": date(2024, 1, 15), "home_team": "A", "away_team": "D", "home_goals": 99, "away_goals": 0},
    ]
    df = _make_df(rows)
    out = build_homeaway_split_features(df)
    assert out.loc[1, "home_home_goals_scored_avg_last5"] == 1.0  # seul le match #1 (1 but) est antérieur
    assert 99 not in out["home_home_goals_scored_avg_last5"].values
    print("  [OK] aucune valeur ne reflète le score extrême du match futur (99-0)")


# ---------------------------------------------------------------------------
# 3. GROUP D — densité de calendrier
# ---------------------------------------------------------------------------

def test_schedule_density_counts_correctly():
    rows = [
        {"league": "L", "date": date(2024, 1, 1), "home_team": "A", "away_team": "B", "home_goals": 1, "away_goals": 0},
        {"league": "L", "date": date(2024, 1, 4), "home_team": "A", "away_team": "C", "home_goals": 1, "away_goals": 0},
        {"league": "L", "date": date(2024, 1, 6), "home_team": "A", "away_team": "D", "home_goals": 1, "away_goals": 0},
    ]
    df = _make_df(rows)
    out = build_schedule_density_features(df)
    # match #3 (6 jan) : matchs de A dans les 7 jours précédents = ceux du 1er et 4 janvier (2).
    assert out.loc[2, "home_matches_last_7_days"] == 2
    assert out.loc[2, "home_matches_last_14_days"] == 2
    # match #2 (4 jan) : seul le match du 1er janvier précède (1).
    assert out.loc[1, "home_matches_last_7_days"] == 1
    # match #1 : aucun match antérieur.
    assert out.loc[0, "home_matches_last_7_days"] == 0
    print("  [OK] matches_last_7/14_days compte correctement, bornes strictes respectées")


def test_schedule_density_excludes_current_and_future_matches():
    rows = [
        {"league": "L", "date": date(2024, 1, 1), "home_team": "A", "away_team": "B", "home_goals": 1, "away_goals": 0},
        {"league": "L", "date": date(2024, 1, 3), "home_team": "A", "away_team": "C", "home_goals": 1, "away_goals": 0},
        {"league": "L", "date": date(2024, 1, 5), "home_team": "A", "away_team": "D", "home_goals": 1, "away_goals": 0},
    ]
    df = _make_df(rows)
    out = build_schedule_density_features(df)
    # match #2 (3 jan) ne doit PAS compter le match #3 (5 jan, futur) dans sa fenêtre.
    assert out.loc[1, "home_matches_last_7_days"] == 1
    print("  [OK] les matchs futurs ne sont jamais comptés dans la densité de calendrier")


# ---------------------------------------------------------------------------
# 4. GROUP E — force Dixon-Coles brute + Elo
# ---------------------------------------------------------------------------

def test_dc_strength_missing_during_burn_in():
    rows = _round_robin_rows("L", ["A", "B", "C", "D"], n_rounds=5, start_date=date(2024, 1, 1))  # 20 matchs < 100
    df = _make_df(rows)
    out = build_dixon_coles_strength_features(df, min_train_matches=100)
    assert out["dc_attack_diff"].isna().all(), "Aucune valeur ne devrait être disponible avant 100 matchs d'historique"
    print("  [OK] dc_attack_diff reste NaN tant que l'historique de la ligue est < min_train_matches")


def test_dc_strength_features_no_leakage():
    teams = ["A", "B", "C", "D", "E", "F"]
    rows = _round_robin_rows("L", teams, n_rounds=40, start_date=date(2020, 1, 1))  # 240 matchs >= 100
    df = _make_df(rows)

    out_before = build_dixon_coles_strength_features(df, min_train_matches=100)
    cutoff_idx = 150  # une ligne largement après le rodage

    # Rejoue le calcul sur un DataFrame TRONQUÉ à cutoff_idx (jamais les lignes futures) :
    # la valeur produite pour la ligne cutoff_idx-1 doit être IDENTIQUE, qu'on lui donne
    # ou non le "futur" (lignes >= cutoff_idx) — preuve directe qu'aucune fuite n'a lieu.
    truncated = df.iloc[:cutoff_idx].reset_index(drop=True)
    out_truncated = build_dixon_coles_strength_features(truncated, min_train_matches=100)

    a = out_before.loc[cutoff_idx - 1, "dc_attack_diff"]
    b = out_truncated.loc[cutoff_idx - 1, "dc_attack_diff"]
    assert (pd.isna(a) and pd.isna(b)) or abs(a - b) < 1e-9, f"Fuite détectée : {a} != {b}"
    print(f"  [OK] dc_attack_diff identique avec/sans les lignes futures (a={a}, b={b}) — aucune fuite")


def test_elo_strength_diff_matches_engine_walk_forward():
    rows = _round_robin_rows("L", ["A", "B", "C"], n_rounds=10, start_date=date(2023, 1, 1))
    df = _make_df(rows)
    out = build_elo_strength_features(df)
    walk = EloEngine().walk_forward(df.reset_index(drop=True))
    assert np.allclose(out["elo_diff"].to_numpy(), walk["diff"].to_numpy())
    print("  [OK] build_elo_strength_features reproduit exactement EloEngine.walk_forward (aucune réimplémentation)")


# ---------------------------------------------------------------------------
# 5. GROUP F — classement reconstruit
# ---------------------------------------------------------------------------

def test_league_standing_reconstruction_correctness():
    rows = [
        {"league": "L", "date": date(2024, 8, 1), "home_team": "A", "away_team": "B", "home_goals": 2, "away_goals": 0},  # A: 3pts
        {"league": "L", "date": date(2024, 8, 2), "home_team": "C", "away_team": "D", "home_goals": 1, "away_goals": 1},  # C,D: 1pt
        {"league": "L", "date": date(2024, 8, 8), "home_team": "A", "away_team": "C", "home_goals": 0, "away_goals": 0},  # avant ce match : A 1er (3pts), C/D 2e ex-aequo (1pt)
    ]
    df = _make_df(rows)
    out = build_league_standing_features(df)
    assert out.loc[2, "home_standing_position"] == 1.0  # A est 1er avant son 2e match
    assert out.loc[2, "away_standing_position"] == 2.0  # C est 2e (ex-aequo D, tie-break stable)
    print("  [OK] classement reconstruit correctement (points, avant le match courant)")


def test_league_standing_excludes_future_matches():
    rows = [
        {"league": "L", "date": date(2024, 8, 1), "home_team": "A", "away_team": "B", "home_goals": 1, "away_goals": 0},
        {"league": "L", "date": date(2024, 8, 8), "home_team": "A", "away_team": "C", "home_goals": 1, "away_goals": 0},
        # match FUTUR à score extrême — ne doit pas influencer la position calculée pour match #2.
        {"league": "L", "date": date(2024, 8, 15), "home_team": "A", "away_team": "D", "home_goals": 50, "away_goals": 0},
    ]
    df = _make_df(rows)
    out = build_league_standing_features(df)
    # avant le match #2, A n'a que 3pts (1 match), jamais les 50 buts du match futur.
    assert out.loc[1, "home_points_per_game_season"] == 3.0
    print("  [OK] league_standing n'est jamais influencé par un match futur")


def test_league_standing_resets_each_season():
    rows = [
        {"league": "L", "date": date(2023, 8, 1), "home_team": "A", "away_team": "B", "home_goals": 3, "away_goals": 0},
        {"league": "L", "date": date(2023, 9, 1), "home_team": "A", "away_team": "C", "home_goals": 3, "away_goals": 0},
        # nouvelle saison (mois >= 7 -> saison 2024) : A ne doit PLUS avoir 6pts hérités.
        {"league": "L", "date": date(2024, 8, 1), "home_team": "A", "away_team": "D", "home_goals": 0, "away_goals": 0},
        {"league": "L", "date": date(2024, 8, 8), "home_team": "A", "away_team": "E", "home_goals": 0, "away_goals": 0},
    ]
    df = _make_df(rows)
    out = build_league_standing_features(df)
    assert out.loc[3, "home_points_per_game_season"] == 1.0  # 1 nul = 1 pt / 1 match cette saison, pas 7/2
    print("  [OK] le classement est remis à zéro à chaque nouvelle saison (mois >= 7)")


# ---------------------------------------------------------------------------
# 6. GROUP G — saison
# ---------------------------------------------------------------------------

def test_season_year_derivation_matches_established_rule():
    cases = [
        (date(2024, 7, 1), 2024), (date(2024, 6, 30), 2023),
        (date(2024, 1, 15), 2023), (date(2024, 12, 31), 2024),
    ]
    for d, expected in cases:
        assert _season_of(d) == expected, f"{d} -> {_season_of(d)}, attendu {expected}"
    print("  [OK] _season_of reproduit exactement la règle de build_features.py::check_team_league_collisions (mois >= 7)")


def test_season_progress_pct_bounds():
    rows = _round_robin_rows("L", ["A", "B"], n_rounds=3, start_date=date(2024, 8, 1))
    df = _make_df(rows)
    out = build_season_features(df)
    assert out["season_progress_pct"].between(0.0, 100.0).all()
    print("  [OK] season_progress_pct reste borné à [0, 100]")


def test_validate_season_rule_diagnostic_shape():
    rows = _round_robin_rows("Bundesliga", ["A", "B"], n_rounds=3, start_date=date(2024, 8, 1))
    rows += _round_robin_rows("Bundesliga", ["A", "B"], n_rounds=3, start_date=date(2024, 3, 1))
    df = _make_df(rows)
    diag = validate_season_rule(df)
    assert "Bundesliga" in diag
    assert set(diag["Bundesliga"]) == {"months_present", "summer_break_fraction", "reliable"}
    print("  [OK] validate_season_rule produit un diagnostic exploitable par ligue")


# ---------------------------------------------------------------------------
# 7. Même ensemble de matchs entre groupes (§22) + missing values (§23)
# ---------------------------------------------------------------------------

def test_all_research_groups_same_row_count_as_input():
    rows = _round_robin_rows("L", ["A", "B", "C", "D"], n_rounds=30, start_date=date(2023, 1, 1))
    df = _make_df(rows)
    groups = build_all_research_groups(df)
    for name, gdf in groups.items():
        assert len(gdf) == len(df), f"{name}: {len(gdf)} lignes != {len(df)} attendues"
        assert list(gdf.index) == list(df.index), f"{name}: index désaligné"
    print(f"  [OK] les {len(groups)} groupes produisent tous exactement {len(df)} lignes, index aligné sur l'entrée")


def test_schedule_density_zero_is_not_a_missing_value_convention():
    # 0 match dans la fenêtre est une vraie mesure (documentée dans PHASE8B_FEATURE_REGISTRY),
    # jamais un NaN maquillé en 0 — vérifie qu'aucun NaN n'apparaît jamais dans ces colonnes.
    rows = _round_robin_rows("L", ["A", "B", "C"], n_rounds=5, start_date=date(2024, 1, 1))
    df = _make_df(rows)
    out = build_schedule_density_features(df)
    assert not out.isna().any().any()
    print("  [OK] matches_last_7/14_days n'est jamais NaN (0 = vraie mesure, jamais une convention de valeur manquante)")


# ---------------------------------------------------------------------------
# 8. feature_sets.py (§44)
# ---------------------------------------------------------------------------

def test_experiment_1_and_2_marked_duplicate_of_baseline():
    assert FEATURE_GROUPS["form"] == DUPLICATE_OF_BASELINE
    assert FEATURE_GROUPS["goals"] == DUPLICATE_OF_BASELINE
    print("  [OK] form/goals marqués DUPLICATE_OF_BASELINE — jamais simulés comme un test valide (§65)")


def test_feature_columns_for_includes_baseline_plus_group():
    from app.ai.engine.features import FEATURE_COLUMNS
    cols = feature_columns_for(["homeaway"])
    assert set(FEATURE_COLUMNS).issubset(set(cols))
    assert set(FEATURE_GROUPS["homeaway"]).issubset(set(cols))
    print("  [OK] feature_columns_for(['homeaway']) = baseline_v1 + homeaway")


def test_experiment_matrix_has_minimum_required_experiments():
    required = {"EXPERIMENT_0_BASELINE", "EXPERIMENT_3_HOMEAWAY", "EXPERIMENT_4_REST",
                "EXPERIMENT_5_STRENGTH", "EXPERIMENT_6_RANKING", "EXPERIMENT_7_SEASON"}
    assert required.issubset(EXPERIMENTS.keys())
    print("  [OK] EXPERIMENTS couvre les groupes minimum requis par §19")


# ---------------------------------------------------------------------------
# 9. scripts/feature_engineering_walkforward.py — folds et sécurité DB (§21/§57)
# ---------------------------------------------------------------------------

def test_make_folds_minimum_three_and_covers_range():
    folds = fewf.make_folds(1000, n_folds=4, burn_in_fraction=0.2)
    assert len(folds) == 4
    assert folds[0][0] == 200  # burn-in respecté
    assert folds[-1][1] == 1000  # dernier fold va jusqu'à la fin
    for (s1, e1), (s2, e2) in zip(folds, folds[1:]):
        assert e1 == s2, "folds non contigus"
    print(f"  [OK] make_folds produit {len(folds)} folds contigus après le burn-in : {folds}")


def test_build_target_class_convention():
    df = pd.DataFrame({"home_goals": [2, 1, 0], "away_goals": [0, 1, 3]})
    y = fewf.build_target(df)
    assert list(y) == [1, 0, 2]  # domicile, nul, extérieur — même convention que train_ml_stacking_from_db.py
    print("  [OK] build_target respecte la convention 0=nul/1=domicile/2=extérieur")


def test_db_safety_read_only_during_feature_construction():
    _clean_all()
    with Session(engine) as session:
        for row in _round_robin_rows("L", ["A", "B", "C", "D"], n_rounds=30, start_date=date(2023, 1, 1)):
            row = {**row, "date": datetime.combine(row["date"], datetime.min.time())}
            session.add(Match(**row))
        session.commit()

    with Session(engine) as session:
        before = fewf.snapshot_db_counts(session)

    rows = _round_robin_rows("L", ["A", "B", "C", "D"], n_rounds=30, start_date=date(2023, 1, 1))
    df = _make_df(rows)
    build_all_research_groups(df)  # lecture/calcul en mémoire uniquement

    with Session(engine) as session:
        after = fewf.snapshot_db_counts(session)

    assert before == after, f"Compteurs DB modifiés : avant={before} après={after}"
    _clean_all()
    print(f"  [OK] build_all_research_groups() est strictement read-only vis-à-vis de la DB : {before}")


def test_reproducibility_dc_strength_deterministic():
    rows = _round_robin_rows("L", ["A", "B", "C", "D", "E"], n_rounds=25, start_date=date(2022, 1, 1))
    df = _make_df(rows)
    out1 = build_dixon_coles_strength_features(df, min_train_matches=100)
    out2 = build_dixon_coles_strength_features(df, min_train_matches=100)
    pd.testing.assert_frame_equal(out1, out2)
    print("  [OK] build_dixon_coles_strength_features est déterministe (même dataset -> même sortie)")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    failures = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            failures += 1
            print(f"  [FAIL] {t.__name__}: {e!r}")
    cleanup_db(DB_PATH)
    print(f"\n{len(tests) - failures}/{len(tests)} tests OK")
    sys.exit(1 if failures else 0)
