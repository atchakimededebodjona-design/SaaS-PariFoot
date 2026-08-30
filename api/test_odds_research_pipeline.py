"""
test_odds_research_pipeline.py — Phase 8D : tests d'intégration du pipeline
odds research (scripts/odds_research_walkforward.py) — parsing CSV
synthétique, mapping, construction du dataset, sécurité DB, fuite
structurelle. Aucun téléchargement réseau (données synthétiques en mémoire) —
voir scripts/odds_research_walkforward.py pour le run réel sur
football-data.co.uk.

Usage : python api/test_odds_research_pipeline.py
"""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_odds_research_pipeline.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match, MatchStats
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion, TeamRating

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import odds_research_walkforward as orw  # noqa: E402

init_db()


def _clean_all():
    with Session(engine) as session:
        for row in session.exec(select(MatchStats)).all():
            session.delete(row)
        for row in session.exec(select(Match)).all():
            session.delete(row)
        session.commit()


def _seed_match(session, league, home, away, d, hg=1, ag=1):
    m = Match(league=league, date=datetime.combine(d, datetime.min.time()), home_team=home, away_team=away, home_goals=hg, away_goals=ag)
    session.add(m)
    session.commit()
    session.refresh(m)
    return m


# ---------------------------------------------------------------------------
# 1. parse_and_map — mapping déterministe (§26), qualité (§25), rejets
# ---------------------------------------------------------------------------

def _write_csv(path: Path, rows: list[dict], columns: list[str]):
    lines = [",".join(columns)]
    for r in rows:
        lines.append(",".join(str(r.get(c, "")) for c in columns))
    path.write_text("\n".join(lines), encoding="latin-1")


COLUMNS = ["Div", "Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "B365H", "B365D", "B365A",
           "B365CH", "B365CD", "B365CA", "AvgH", "AvgD", "AvgA", "B365>2.5", "B365<2.5", "Avg>2.5", "Avg<2.5"]


def test_parse_and_map_matches_known_fixture(tmp_path=Path(__file__).parent / "_tmp_odds_test"):
    tmp_path.mkdir(exist_ok=True)
    _clean_all()
    with Session(engine) as session:
        m = _seed_match(session, "PremierLeague", "Arsenal", "Chelsea", date(2023, 8, 11), 2, 1)
        xfoot_index = orw.load_xfoot_matches()

    csv_path = tmp_path / "E0_2324.csv"
    _write_csv(csv_path, [{
        "Div": "E0", "Date": "11/08/2023", "HomeTeam": "Arsenal", "AwayTeam": "Chelsea",
        "FTHG": 2, "FTAG": 1, "B365H": 2.1, "B365D": 3.4, "B365A": 3.6,
        "B365CH": 2.0, "B365CD": 3.5, "B365CA": 3.7,
        "AvgH": 2.15, "AvgD": 3.35, "AvgA": 3.55, "B365>2.5": 1.9, "B365<2.5": 1.95, "Avg>2.5": 1.92, "Avg<2.5": 1.93,
    }], COLUMNS)

    observations, quality = orw.parse_and_map({("E0", "2324"): csv_path}, xfoot_index)
    assert quality["mapped"] == 1
    assert quality["unmapped"] == 0
    assert observations[0]["match_id"] == m.id
    assert observations[0]["pre_1x2"] is not None
    print("  [OK] parse_and_map rapproche un match connu et calcule ses features odds")
    _clean_all()


def test_parse_and_map_rejects_unmapped_row(tmp_path=Path(__file__).parent / "_tmp_odds_test"):
    tmp_path.mkdir(exist_ok=True)
    _clean_all()
    with Session(engine) as session:
        _seed_match(session, "PremierLeague", "Arsenal", "Chelsea", date(2023, 8, 11))
        xfoot_index = orw.load_xfoot_matches()

    csv_path = tmp_path / "E0_2324b.csv"
    _write_csv(csv_path, [{
        "Div": "E0", "Date": "12/08/2023", "HomeTeam": "Everton", "AwayTeam": "Fulham",
        "FTHG": 0, "FTAG": 0, "B365H": 2.1, "B365D": 3.4, "B365A": 3.6,
    }], COLUMNS)

    observations, quality = orw.parse_and_map({("E0", "2324b"): csv_path}, xfoot_index)
    assert quality["unmapped"] == 1
    assert quality["mapped"] == 0
    assert observations == []
    print("  [OK] une ligne CSV sans correspondance dans `match` est REJECTED, jamais forcée (§26)")
    _clean_all()


def test_parse_and_map_rejects_invalid_odds_but_keeps_match():
    _clean_all()
    tmp_path = Path(__file__).parent / "_tmp_odds_test"
    tmp_path.mkdir(exist_ok=True)
    with Session(engine) as session:
        _seed_match(session, "PremierLeague", "Arsenal", "Chelsea", date(2023, 8, 11))
        xfoot_index = orw.load_xfoot_matches()

    csv_path = tmp_path / "E0_invalid.csv"
    _write_csv(csv_path, [{
        "Div": "E0", "Date": "11/08/2023", "HomeTeam": "Arsenal", "AwayTeam": "Chelsea",
        "FTHG": 2, "FTAG": 1, "B365H": 0.9, "B365D": 3.4, "B365A": 3.6,  # B365H invalide (<=1)
    }], COLUMNS)

    observations, quality = orw.parse_and_map({("E0", "inv"): csv_path}, xfoot_index)
    assert quality["mapped"] == 1  # le MATCH est rapproché...
    assert quality["invalid_1x2_odds"] == 1  # ...mais ses cotes 1X2 sont invalides
    assert observations[0]["pre_1x2"] is None  # jamais imputée
    print("  [OK] un match rapproché avec des cotes 1X2 invalides garde pre_1x2=None (jamais imputé, §24)")
    _clean_all()


def test_parse_and_map_rejects_future_date():
    _clean_all()
    tmp_path = Path(__file__).parent / "_tmp_odds_test"
    tmp_path.mkdir(exist_ok=True)
    xfoot_index = orw.load_xfoot_matches()

    future_date = (date.today() + timedelta(days=30)).strftime("%d/%m/%Y")
    csv_path = tmp_path / "E0_future.csv"
    _write_csv(csv_path, [{
        "Div": "E0", "Date": future_date, "HomeTeam": "Arsenal", "AwayTeam": "Chelsea",
        "FTHG": 2, "FTAG": 1, "B365H": 2.1, "B365D": 3.4, "B365A": 3.6,
    }], COLUMNS)

    observations, quality = orw.parse_and_map({("E0", "future"): csv_path}, xfoot_index)
    assert quality["future_date"] == 1
    assert observations == []
    print("  [OK] une ligne avec une date future est rejetée (contrôle de sanité, §67)")


def test_parse_and_map_deduplicates_same_match():
    _clean_all()
    tmp_path = Path(__file__).parent / "_tmp_odds_test"
    tmp_path.mkdir(exist_ok=True)
    with Session(engine) as session:
        _seed_match(session, "PremierLeague", "Arsenal", "Chelsea", date(2023, 8, 11))
        xfoot_index = orw.load_xfoot_matches()

    row = {"Div": "E0", "Date": "11/08/2023", "HomeTeam": "Arsenal", "AwayTeam": "Chelsea",
           "FTHG": 2, "FTAG": 1, "B365H": 2.1, "B365D": 3.4, "B365A": 3.6}
    csv_path_a = tmp_path / "dupA.csv"
    csv_path_b = tmp_path / "dupB.csv"
    _write_csv(csv_path_a, [row], COLUMNS)
    _write_csv(csv_path_b, [row], COLUMNS)

    observations, quality = orw.parse_and_map({("E0", "a"): csv_path_a, ("E0", "b"): csv_path_b}, xfoot_index)
    assert quality["mapped"] == 1
    assert quality["duplicate_mapped"] == 1
    assert len(observations) == 1
    print("  [OK] le même match rapproché deux fois (fichiers différents) n'est compté qu'une fois (§25 duplicate snapshot)")
    _clean_all()


# ---------------------------------------------------------------------------
# 2. Fuite structurelle (§67) : CLOSING jamais dans les features de prédiction
# ---------------------------------------------------------------------------

def test_closing_odds_never_in_prediction_feature_groups():
    """§8/§48 : CLOSING_ODDS_REFERENCE ne doit JAMAIS être mélangée aux
    features de prédiction (Experiments 1/2/3/5). Seul Experiment 4
    (movement) utilise une quantité DÉRIVÉE de la clôture, jamais la valeur
    de clôture elle-même comme feature de probabilité directe."""
    prediction_groups = ["odds_1x2", "odds_1x2_overround", "odds_consensus", "odds_full"]
    for g in prediction_groups:
        for col in orw.ODDS_FEATURE_GROUPS[g]:
            assert "close" not in col.lower() and "market_close" not in col.lower(), (
                f"Fuite structurelle détectée : {g} contient une colonne de clôture ({col})"
            )
    print("  [OK] aucune colonne de clôture (market_close_*) n'apparaît dans les groupes de features de prédiction (§8/§48)")


def test_movement_group_is_a_delta_not_raw_closing_value():
    cols = orw.ODDS_FEATURE_GROUPS["odds_movement"]
    assert all("movement" in c for c in cols)
    print("  [OK] le groupe odds_movement n'expose que des deltas (jamais la valeur brute de clôture)")


# ---------------------------------------------------------------------------
# 3. build_odds_dataframe — cohérence de sortie
# ---------------------------------------------------------------------------

def test_build_odds_dataframe_missing_odds_stays_nan_never_zero():
    observations = [{
        "match_id": 1, "_key": ("PremierLeague", date(2023, 8, 11), "Arsenal", "Chelsea"),
        "league": "PremierLeague", "date": datetime(2023, 8, 11),
        "pre_1x2": None, "close_1x2": None, "consensus_1x2": None, "pre_ou25": None, "consensus_ou25": None,
        "pre_timestamp_quality": "CAUTION", "close_timestamp_quality": "CAUTION", "season": "2324", "div": "E0",
    }]
    df = orw.build_odds_dataframe(observations)
    assert df["odds_norm_home"].isna().all(), "cote manquante doit rester NaN, jamais 0 (§24)"
    print("  [OK] build_odds_dataframe laisse NaN une cote manquante (jamais 0.0 fabriqué)")


def test_build_odds_dataframe_movement_computed_only_when_both_snapshots_present():
    obs_both = {
        "match_id": 1, "_key": "k1", "league": "L", "date": datetime(2023, 8, 11),
        "pre_1x2": {"norm_home": 0.5, "norm_draw": 0.3, "norm_away": 0.2, "overround": 0.05, "raw_home": 0, "raw_draw": 0, "raw_away": 0},
        "close_1x2": {"norm_home": 0.55, "norm_draw": 0.28, "norm_away": 0.17, "overround": 0.05, "raw_home": 0, "raw_draw": 0, "raw_away": 0},
        "consensus_1x2": None, "pre_ou25": None, "consensus_ou25": None,
        "pre_timestamp_quality": "CAUTION", "close_timestamp_quality": "CAUTION", "season": "x", "div": "E0",
    }
    obs_pre_only = {**obs_both, "_key": "k2", "close_1x2": None}
    df = orw.build_odds_dataframe([obs_both, obs_pre_only])
    assert abs(df.iloc[0]["odds_movement_home"] - 0.05) < 1e-9
    assert df.iloc[1]["odds_movement_home"] != df.iloc[1]["odds_movement_home"]  # NaN
    print("  [OK] odds_movement calculé seulement quand pré-clôture ET clôture sont valides, NaN sinon")


# ---------------------------------------------------------------------------
# 4. flatten_obs_with_league — alignement fold <-> ligue
# ---------------------------------------------------------------------------

def test_flatten_obs_with_league_alignment():
    import pandas as pd
    df = pd.DataFrame({"league": ["A", "A", "B", "B"]})
    folds = [(0, 2), (2, 4)]
    exp_result = {"obs_by_fold": [
        [{"correct": True}, {"correct": False}],
        [{"correct": True}, {"correct": True}],
    ]}
    flat = orw.flatten_obs_with_league(exp_result, folds, df)
    leagues = [lg for lg, _ in flat]
    assert leagues == ["A", "A", "B", "B"]
    print("  [OK] flatten_obs_with_league ré-associe chaque observation à sa ligue dans le bon ordre")


# ---------------------------------------------------------------------------
# 5. Reproductibilité (§32)
# ---------------------------------------------------------------------------

def test_parsing_reproducible_same_csv():
    _clean_all()
    tmp_path = Path(__file__).parent / "_tmp_odds_test"
    tmp_path.mkdir(exist_ok=True)
    with Session(engine) as session:
        _seed_match(session, "PremierLeague", "Arsenal", "Chelsea", date(2023, 8, 11), 2, 1)
        xfoot_index = orw.load_xfoot_matches()

    csv_path = tmp_path / "repro.csv"
    _write_csv(csv_path, [{
        "Div": "E0", "Date": "11/08/2023", "HomeTeam": "Arsenal", "AwayTeam": "Chelsea",
        "FTHG": 2, "FTAG": 1, "B365H": 2.1, "B365D": 3.4, "B365A": 3.6,
    }], COLUMNS)

    obs1, _ = orw.parse_and_map({("E0", "r"): csv_path}, xfoot_index)
    obs2, _ = orw.parse_and_map({("E0", "r"): csv_path}, xfoot_index)
    assert obs1[0]["pre_1x2"] == obs2[0]["pre_1x2"]
    print("  [OK] parse_and_map est déterministe (même CSV -> mêmes features)")
    _clean_all()


# ---------------------------------------------------------------------------
# 6. Sécurité DB (§61) — load_xfoot_matches est strictement read-only
# ---------------------------------------------------------------------------

def _row_counts(session):
    return {
        "match": len(session.exec(select(Match)).all()),
        "match_stats": len(session.exec(select(MatchStats)).all()),
        "model_predictions": len(session.exec(select(ModelPrediction)).all()),
        "model_versions": len(session.exec(select(ModelVersion)).all()),
        "team_ratings": len(session.exec(select(TeamRating)).all()),
    }


def test_load_xfoot_matches_is_read_only():
    _clean_all()
    with Session(engine) as session:
        _seed_match(session, "PremierLeague", "Arsenal", "Chelsea", date(2023, 8, 11))

    with Session(engine) as session:
        before = _row_counts(session)

    orw.load_xfoot_matches()

    with Session(engine) as session:
        after = _row_counts(session)

    assert before == after, f"load_xfoot_matches a modifié la DB : {before} != {after}"
    print(f"  [OK] load_xfoot_matches() est strictement read-only : {before}")
    _clean_all()


def test_snapshot_db_counts_reused_not_reimplemented():
    """§38 : snapshot_db_counts doit être la MÊME fonction que
    scripts/feature_engineering_walkforward.py, jamais une deuxième
    implémentation."""
    assert orw.fewf.snapshot_db_counts is not None
    with Session(engine) as session:
        counts = orw.fewf.snapshot_db_counts(session)
    assert set(counts.keys()) == {"match", "match_stats", "model_predictions", "model_versions", "prediction_log", "team_ratings"}
    print("  [OK] odds_research_walkforward réutilise fewf.snapshot_db_counts (aucune réimplémentation, §61)")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    failures = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            failures += 1
            print(f"  [FAIL] {t.__name__}: {e!r}")
    _clean_all()
    tmp_dir = Path(__file__).parent / "_tmp_odds_test"
    if tmp_dir.exists():
        for f in tmp_dir.iterdir():
            f.unlink()
        tmp_dir.rmdir()
    cleanup_db(DB_PATH)
    print(f"\n{len(tests) - failures}/{len(tests)} tests OK")
    sys.exit(1 if failures else 0)
