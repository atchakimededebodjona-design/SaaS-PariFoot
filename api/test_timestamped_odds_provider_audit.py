"""
test_timestamped_odds_provider_audit.py — Phase 8F : tests d'intégration de
scripts/timestamped_odds_provider_audit.py (assemblage des
TimestampedOddsProvider, tables, sécurité DB, déterminisme du rapport).

Usage : python api/test_timestamped_odds_provider_audit.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_timestamped_odds_provider_audit.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match, MatchStats
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion, TeamRating

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import timestamped_odds_provider_audit as toda  # noqa: E402
from app.ai.odds_research.provider_audit import CUTOFF_HORIZONS, XFOOT_LEAGUES  # noqa: E402

init_db()


def test_all_providers_pass_internal_validation():
    problems = toda.validate_all()
    assert problems == [], f"Incohérences dans PROVIDERS : {problems}"
    print(f"  [OK] les {len(toda.PROVIDERS)} TimestampedOddsProvider passent validate_provider() sans incohérence")


def test_at_least_the_odds_api_and_betfair_are_present():
    names = {p.name for p in toda.PROVIDERS}
    assert any("Odds API" in n for n in names)
    assert any("Betfair" in n for n in names)
    print("  [OK] les deux candidats principaux (The Odds API, Betfair) sont bien présents dans PROVIDERS")


def test_no_provider_shortlisted_without_yes_reconstruction():
    for p in toda.PROVIDERS:
        if p.verdict in ("SHORTLIST", "RECOMMENDED_FOR_MVP"):
            assert p.can_reconstruct_snapshot == "YES", f"{p.name}: verdict={p.verdict} mais can_reconstruct_snapshot={p.can_reconstruct_snapshot}"
    print("  [OK] tout fournisseur SHORTLIST/RECOMMENDED_FOR_MVP a can_reconstruct_snapshot=YES (§69, critère n°1)")


def test_football_data_co_uk_is_never_promoted():
    """Cas de référence Phase 8D/8E — doit rester un verdict défavorable
    dans cette nouvelle grille, cohérent avec le résultat déjà établi."""
    fd = next(p for p in toda.PROVIDERS if "football-data.co.uk" in p.name)
    assert fd.can_reconstruct_snapshot == "NO"
    assert fd.verdict not in ("SHORTLIST", "RECOMMENDED_FOR_MVP")
    print(f"  [OK] football-data.co.uk reste non promu dans cette phase (verdict={fd.verdict}), cohérent avec Phase 8E")


def test_betfair_commercial_usage_is_legal_review_required_never_allowed():
    """§69/§45 : le risque légal France (non confirmé sur source primaire
    mais fortement corroboré) doit rester LEGAL_REVIEW_REQUIRED, jamais
    ALLOWED tant que non vérifié directement."""
    betfair = next(p for p in toda.PROVIDERS if "Betfair" in p.name)
    assert betfair.commercial_usage == "LEGAL_REVIEW_REQUIRED"
    print("  [OK] Betfair reste commercial_usage=LEGAL_REVIEW_REQUIRED (risque France non tranché sur source primaire)")


def test_sportmonks_reconfirms_short_retention_never_reconstructible():
    sportmonks = next(p for p in toda.PROVIDERS if "Sportmonks" in p.name)
    assert all(v == "NO" for v in sportmonks.cutoff_reconstruction.values())
    print("  [OK] Sportmonks Premium Odds Feed : NO sur tous les cutoffs (rétention 7 jours, confirmé insuffisant pour 2019-2026)")


# ---------------------------------------------------------------------------
# Tables (§44/§22/§67)
# ---------------------------------------------------------------------------

def test_temporal_scorecard_covers_all_providers_and_horizons():
    rows = toda.build_temporal_scorecard(toda.PROVIDERS)
    assert len(rows) == len(toda.PROVIDERS)
    for row in rows:
        for h in CUTOFF_HORIZONS:
            assert h in row
    print(f"  [OK] build_temporal_scorecard couvre les {len(rows)} fournisseurs x {len(CUTOFF_HORIZONS)} horizons")


def test_league_coverage_table_covers_all_eleven_leagues():
    rows = toda.build_league_coverage_table(toda.PROVIDERS)
    assert len(rows) == len(toda.PROVIDERS)
    for row in rows:
        for lg in XFOOT_LEAGUES:
            assert lg in row
    print("  [OK] build_league_coverage_table couvre les 11 ligues Xfoot pour chaque fournisseur")


def test_the_odds_api_covers_all_eleven_leagues_with_known_status():
    odds_api = next(p for p in toda.PROVIDERS if p.name.startswith("The Odds API"))
    assert all(v != "UNKNOWN" for v in odds_api.league_coverage.values()), "The Odds API a une table de couverture officielle par ligue — aucune ne devrait rester UNKNOWN"
    print("  [OK] The Odds API : couverture connue (FULL/PARTIAL) sur les 11 ligues (table officielle 'Earliest Historical Timestamps')")


# ---------------------------------------------------------------------------
# Déterminisme (§63 : deterministic scorecard / report generation)
# ---------------------------------------------------------------------------

def test_report_generation_is_deterministic():
    result_a = {
        "run_id": "test", "generated_at": "test", "providers": [vars(p) for p in toda.PROVIDERS],
        "verdict_counts": {}, "temporal_scorecard": toda.build_temporal_scorecard(toda.PROVIDERS),
        "league_coverage": toda.build_league_coverage_table(toda.PROVIDERS),
        "top3_text": "", "best_temporal": "", "best_coverage": "", "best_price_value": "", "best_overall": "",
        "recommendation": "PROCEED_TO_PROVIDER_TRIAL", "recommendation_notes": "", "trial_plan": [],
        "limitations": [], "recommendations_phase_8g": [], "timestamped_source_status": "🟡 PARTIAL",
        "top_candidate": "The Odds API", "trial_status": "PROPOSED",
    }
    md1 = toda.render_markdown(result_a)
    md2 = toda.render_markdown(result_a)
    assert md1 == md2
    print("  [OK] render_markdown est déterministe (même résultat -> même rapport à chaque appel)")


def test_decide_verdict_reproducible_across_all_providers():
    from app.ai.odds_research.provider_audit import decide_verdict
    for p in toda.PROVIDERS:
        recomputed = decide_verdict(
            can_reconstruct_snapshot=p.can_reconstruct_snapshot, snapshot_model=p.snapshot_model,
            coverage_score=p.coverage_score, temporal_score=p.temporal_score,
            leakage_risk=p.leakage_risk, commercial_usage=p.commercial_usage,
            duplicate_of_existing=p.duplicate_of_existing,
        )
        assert recomputed == p.verdict, f"{p.name}: verdict stocké '{p.verdict}' != recalculé '{recomputed}'"
    print("  [OK] chaque verdict de PROVIDERS est reproductible depuis les faits déclarés (jamais assigné à la main)")


# ---------------------------------------------------------------------------
# Sécurité DB (§64)
# ---------------------------------------------------------------------------

def _row_counts(session):
    return {
        "match": len(session.exec(select(Match)).all()),
        "match_stats": len(session.exec(select(MatchStats)).all()),
        "model_predictions": len(session.exec(select(ModelPrediction)).all()),
        "model_versions": len(session.exec(select(ModelVersion)).all()),
        "team_ratings": len(session.exec(select(TeamRating)).all()),
    }


def test_audit_assembly_never_touches_production_tables():
    with Session(engine) as session:
        before = _row_counts(session)

    toda.validate_all()
    toda.build_temporal_scorecard(toda.PROVIDERS)
    toda.build_league_coverage_table(toda.PROVIDERS)

    with Session(engine) as session:
        after = _row_counts(session)

    assert before == after, f"Compteurs DB modifiés par un module qui ne devrait faire aucune I/O : {before} != {after}"
    print(f"  [OK] validate_all()/build_temporal_scorecard()/build_league_coverage_table() sont strictement read-only : {before}")


def test_no_network_imports_in_audit_script():
    source = Path(toda.__file__).read_text(encoding="utf-8")
    for forbidden in ("import httpx", "import requests", "API_KEY", "os.environ"):
        assert forbidden not in source, f"'{forbidden}' trouvé dans timestamped_odds_provider_audit.py — ce module doit rester 100% hors-ligne (§58)"
    print("  [OK] scripts/timestamped_odds_provider_audit.py ne contient aucun import réseau ni référence à une clé API")


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
