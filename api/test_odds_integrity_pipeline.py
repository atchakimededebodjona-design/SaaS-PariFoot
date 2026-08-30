"""
test_odds_integrity_pipeline.py — Phase 8E : tests d'intégration de
scripts/odds_integrity_audit.py (classification, tables, réévaluation Phase
8D, sécurité DB). Aucun téléchargement réseau (données synthétiques /
fixtures en mémoire), sauf le test explicitement marqué comme réutilisant le
cache Phase 8D déjà présent sur disque (lecture seule).

Usage : python api/test_odds_integrity_pipeline.py
"""

import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_odds_integrity_pipeline.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match, MatchStats
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion, TeamRating

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import odds_integrity_audit as oia  # noqa: E402

init_db()


def _clean_all():
    with Session(engine) as session:
        for row in session.exec(select(MatchStats)).all():
            session.delete(row)
        for row in session.exec(select(Match)).all():
            session.delete(row)
        session.commit()


def _obs(pre=True, close=True, consensus=True):
    return {
        "match_id": 1, "_key": "k", "league": "PremierLeague", "date": datetime(2023, 8, 11),
        "pre_1x2": {"norm_home": 0.5, "norm_draw": 0.3, "norm_away": 0.2, "overround": 0.05, "raw_home": 0, "raw_draw": 0, "raw_away": 0} if pre else None,
        "close_1x2": {"norm_home": 0.5, "norm_draw": 0.3, "norm_away": 0.2, "overround": 0.05, "raw_home": 0, "raw_draw": 0, "raw_away": 0} if close else None,
        "consensus_1x2": {"norm_home": 0.5, "norm_draw": 0.3, "norm_away": 0.2, "overround": 0.05, "raw_home": 0, "raw_draw": 0, "raw_away": 0} if consensus else None,
        "pre_ou25": None, "consensus_ou25": None,
        "pre_timestamp_quality": "CAUTION", "close_timestamp_quality": "CAUTION",
        "season": "2324", "div": "E0",
    }


# ---------------------------------------------------------------------------
# 1. classify_all_observations — §8/§35
# ---------------------------------------------------------------------------

def test_classify_all_observations_valid_odds_are_historical_untimestamped():
    obs = [_obs(pre=True, close=True, consensus=True)]
    result = oia.classify_all_observations(obs)
    for series in oia.SNAPSHOT_SERIES:
        assert result[series]["HISTORICAL_BUT_UNTIMESTAMPED"] == 1
        assert result[series]["TEMPORALLY_VERIFIED"] == 0
    print("  [OK] classify_all_observations classe toute observation valide en HISTORICAL_BUT_UNTIMESTAMPED (jamais TEMPORALLY_VERIFIED, football-data.co.uk)")


def test_classify_all_observations_invalid_odds_are_rejected():
    obs = [_obs(pre=False, close=True, consensus=True)]
    result = oia.classify_all_observations(obs)
    assert result["pre_1x2"]["REJECTED"] == 1
    assert result["close_1x2"]["HISTORICAL_BUT_UNTIMESTAMPED"] == 1
    print("  [OK] une série invalide (pre_1x2=None) est REJECTED, indépendamment des autres séries du même match")


def test_classify_all_observations_mixed_batch_counts_correctly():
    obs = [_obs(pre=True), _obs(pre=True), _obs(pre=False)]
    result = oia.classify_all_observations(obs)
    assert result["pre_1x2"]["HISTORICAL_BUT_UNTIMESTAMPED"] == 2
    assert result["pre_1x2"]["REJECTED"] == 1
    print("  [OK] les comptes sont corrects sur un lot mixte (2 valides + 1 invalide)")


# ---------------------------------------------------------------------------
# 2. temporal_coverage_table / historical_reconstruction_table — §31/§36
# ---------------------------------------------------------------------------

def test_temporal_coverage_table_always_zero_verified():
    obs = [_obs(pre=True), _obs(pre=False)]
    rows = oia.temporal_coverage_table(obs, total_xfoot_matches=10)
    assert len(rows) == len(oia.CUTOFFS_HOURS)
    for row in rows:
        assert row["verified"] == 0
        assert row["coverage_pct"] == 0.0
    print("  [OK] temporal_coverage_table donne verified=0 pour TOUS les cutoffs testés (§9 : jamais transformé en donnée sûre)")


def test_temporal_coverage_table_unverified_matches_valid_pre_1x2_count():
    obs = [_obs(pre=True), _obs(pre=True), _obs(pre=False)]
    rows = oia.temporal_coverage_table(obs, total_xfoot_matches=3)
    assert rows[0]["unverified"] == 2
    assert rows[0]["rejected"] == 1
    print("  [OK] unverified/rejected reflètent exactement le nombre d'observations valides/invalides")


def test_historical_reconstruction_all_cutoffs_return_no():
    rows = oia.historical_reconstruction_table()
    assert len(rows) == 5
    assert all(r["reconstruction"] == "NO" for r in rows)
    assert all(r["verdict"] == "HISTORICAL_BUT_UNTIMESTAMPED" for r in rows)
    print("  [OK] historical_reconstruction_table répond NO pour les 5 cutoffs (T-24h à T-1h), jamais un YES non prouvé")


# ---------------------------------------------------------------------------
# 3. bookmaker_quality_table / consensus_quality_table — §33/§34
# ---------------------------------------------------------------------------

def test_bookmaker_quality_table_never_claims_safe():
    obs = [_obs(pre=True, close=True)]
    rows = oia.bookmaker_quality_table(obs)
    for row in rows:
        assert row["safe"] == 0
        assert row["timestamped"] == 0
    print("  [OK] bookmaker_quality_table n'attribue jamais 'safe' ou 'timestamped' > 0 (aucun timestamp mesuré disponible)")


def test_consensus_quality_table_always_unsafe():
    obs = [_obs(consensus=True)]
    rows = oia.consensus_quality_table(obs)
    for row in rows:
        assert row["safe_consensus"] == 0
        assert "UNSAFE" in row["verdict"]
    print("  [OK] consensus_quality_table classe systématiquement le consensus football-data.co.uk UNSAFE (provenance temporelle opaque, §18)")


# ---------------------------------------------------------------------------
# 4. reassess_phase8d — §19/§37
# ---------------------------------------------------------------------------

def test_reassess_phase8d_reclassifies_better_verdicts(tmp_path=Path(__file__).parent / "_tmp_integrity_test"):
    tmp_path.mkdir(exist_ok=True)
    fake_report = tmp_path / "fake_phase8d.json"
    fake_report.write_text(json.dumps({
        "group_verdicts": {
            "EXPERIMENT_0_BASELINE": "BASELINE",
            "EXPERIMENT_1_ODDS_1X2": "BETTER",
            "EXPERIMENT_4_ODDS_MOVEMENT": "EQUIVALENT",
        }
    }), encoding="utf-8")

    result = oia.reassess_phase8d(fake_report)
    assert result["found"] is True
    assert result["temporally_verified_verdicts"]["EXPERIMENT_0_BASELINE"] == "BASELINE"
    assert result["temporally_verified_verdicts"]["EXPERIMENT_1_ODDS_1X2"] == "HISTORICAL_SIGNAL_BUT_TEMPORAL_VALIDATION_INSUFFICIENT"
    assert result["temporally_verified_verdicts"]["EXPERIMENT_4_ODDS_MOVEMENT"] == "EQUIVALENT"  # non-BETTER : jamais requalifié
    fake_report.unlink()
    tmp_path.rmdir()
    print("  [OK] reassess_phase8d requalifie chaque verdict BETTER en HISTORICAL_SIGNAL_BUT_TEMPORAL_VALIDATION_INSUFFICIENT, jamais supprimé (§37)")


def test_reassess_phase8d_missing_report_is_honest():
    result = oia.reassess_phase8d(Path("/nonexistent/path/report.json"))
    assert result["found"] is False
    assert "note" in result
    print("  [OK] reassess_phase8d signale honnêtement l'absence de rapport Phase 8D (jamais une réévaluation fabriquée)")


# ---------------------------------------------------------------------------
# 5. Reproductibilité (§30)
# ---------------------------------------------------------------------------

def test_classify_all_observations_reproducible():
    obs = [_obs(pre=True), _obs(pre=False)]
    r1 = oia.classify_all_observations(obs)
    r2 = oia.classify_all_observations(obs)
    assert r1 == r2
    print("  [OK] classify_all_observations est déterministe (même entrée -> même sortie)")


# ---------------------------------------------------------------------------
# 6. Sécurité DB (§43) — les fonctions d'audit ne touchent jamais la DB
#    sauf load_xfoot_matches (read-only, déjà validé Phase 8D) et
#    snapshot_db_counts (lecture de comptage uniquement).
# ---------------------------------------------------------------------------

def _row_counts(session):
    return {
        "match": len(session.exec(select(Match)).all()),
        "match_stats": len(session.exec(select(MatchStats)).all()),
        "model_predictions": len(session.exec(select(ModelPrediction)).all()),
        "model_versions": len(session.exec(select(ModelVersion)).all()),
        "team_ratings": len(session.exec(select(TeamRating)).all()),
    }


def test_pure_audit_functions_never_touch_db():
    _clean_all()
    with Session(engine) as session:
        session.add(Match(league="PremierLeague", date=datetime.combine(date(2023, 8, 11), datetime.min.time()),
                           home_team="Arsenal", away_team="Chelsea", home_goals=2, away_goals=1))
        session.commit()

    with Session(engine) as session:
        before = _row_counts(session)

    obs = [_obs(pre=True), _obs(pre=False)]
    oia.classify_all_observations(obs)
    oia.temporal_coverage_table(obs, total_xfoot_matches=2)
    oia.historical_reconstruction_table()
    oia.bookmaker_quality_table(obs)
    oia.consensus_quality_table(obs)

    with Session(engine) as session:
        after = _row_counts(session)

    assert before == after, f"Une fonction d'audit a modifié la DB : {before} != {after}"
    print(f"  [OK] les fonctions de classification/tables sont strictement read-only vis-à-vis de la DB : {before}")
    _clean_all()


def test_snapshot_db_counts_reused_via_fewf():
    """§43 : réutilise fewf.snapshot_db_counts (Phase 8B), jamais une
    deuxième implémentation."""
    with Session(engine) as session:
        counts = oia.fewf.snapshot_db_counts(session)
    assert set(counts.keys()) == {"match", "match_stats", "model_predictions", "model_versions", "prediction_log", "team_ratings"}
    print("  [OK] odds_integrity_audit réutilise fewf.snapshot_db_counts (aucune réimplémentation)")


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
    cleanup_db(DB_PATH)
    print(f"\n{len(tests) - failures}/{len(tests)} tests OK")
    sys.exit(1 if failures else 0)
