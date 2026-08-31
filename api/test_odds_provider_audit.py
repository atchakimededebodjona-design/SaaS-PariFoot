"""
test_odds_provider_audit.py — Phase 8F : tests de
app/ai/odds_research/provider_audit.py (fonctions/structures pures — aucun
accès réseau/DB).

Usage : python api/test_odds_provider_audit.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.ai.odds_research.provider_audit import (
    TimestampedOddsProvider, validate_provider, decide_verdict,
    SNAPSHOT_MODELS, CUTOFF_HORIZONS, XFOOT_LEAGUES,
)


def _base_kwargs(**overrides) -> dict:
    kwargs = dict(
        name="TestProvider", can_reconstruct_snapshot="YES", snapshot_model="TRUE_SNAPSHOT_HISTORY",
        timestamp_granularity="MINUTE", timestamp_semantics_notes="timestamp de snapshot du fournisseur",
        timestamp_origin="PROVIDER_INGESTION_TIMESTAMP",
        cutoff_reconstruction={h: "YES" for h in CUTOFF_HORIZONS},
        movement_status="MOVEMENT_AVAILABLE", opening_definition="première observation persistée",
        closing_definition="dernière observation avant coup d'envoi",
        consensus_capability_notes="reconstruction possible à un cutoff donné à partir des snapshots individuels",
        historical_first_year=2020, historical_last_year=2026,
        historical_depth_notes="vérifié officiellement depuis juin 2020",
        match_level_query="YES", bookmaker_granularity_notes="par bookmaker",
        markets_notes="1X2, O/U 2.5 confirmés ; BTTS non confirmé",
        odds_format_notes="décimal natif",
        league_coverage={lg: "FULL" for lg in XFOOT_LEAGUES},
        id_stability_notes="IDs stables par ligue/équipe",
        access_model="API_QUERY", rate_limits_notes="documenté",
        cost_category="LOW", cost_notes="9$/mois (vérifié 2026-08-30)",
        commercial_usage="ALLOWED", storage_rights_notes="stockage autorisé",
        redistribution_notes="usage interne autorisé", retention_notes="illimitée après téléchargement",
        data_quality_notes="RAS", reliability_notes="documentation complète",
        latency_notes="near-real-time", api_maturity="HIGH", integration_complexity="LOW",
        lock_in_notes="dépendance unique documentée", trial_availability="offre gratuite disponible",
        coverage_score="EXCELLENT", temporal_score="EXCELLENT", leakage_risk="LOW",
        duplicate_of_existing=False, duplicate_notes="",
        sources=["https://example.com/docs"], verified_date="2026-08-30",
        verdict="RECOMMENDED_FOR_MVP",
    )
    kwargs.update(overrides)
    return kwargs


# ---------------------------------------------------------------------------
# validate_provider
# ---------------------------------------------------------------------------

def test_valid_record_has_no_problems():
    p = TimestampedOddsProvider(**_base_kwargs())
    assert validate_provider(p) == []
    print("  [OK] un TimestampedOddsProvider entièrement cohérent ne déclenche aucun problème")


def test_missing_cutoff_horizon_is_flagged():
    kwargs = _base_kwargs()
    kwargs["cutoff_reconstruction"] = {h: "YES" for h in CUTOFF_HORIZONS if h != "T-6h"}
    p = TimestampedOddsProvider(**kwargs)
    problems = validate_provider(p)
    assert any("T-6h" in msg for msg in problems)
    print("  [OK] un cutoff_reconstruction incomplet (T-6h manquant) est détecté")


def test_missing_league_coverage_is_flagged():
    kwargs = _base_kwargs()
    kwargs["league_coverage"] = {lg: "FULL" for lg in XFOOT_LEAGUES if lg != "SaudiProLeague"}
    p = TimestampedOddsProvider(**kwargs)
    problems = validate_provider(p)
    assert any("SaudiProLeague" in msg for msg in problems)
    print("  [OK] une ligue Xfoot manquante dans league_coverage est détectée (§21 : les 11 ligues)")


def test_shortlist_requires_can_reconstruct_yes():
    """§69 : le critère temporel prime toujours — un verdict SHORTLIST/
    RECOMMENDED_FOR_MVP avec can_reconstruct_snapshot != YES est incohérent."""
    p = TimestampedOddsProvider(**_base_kwargs(can_reconstruct_snapshot="PARTIAL", verdict="SHORTLIST"))
    problems = validate_provider(p)
    assert any("critère temporel prime" in msg for msg in problems)
    print("  [OK] SHORTLIST/RECOMMENDED_FOR_MVP avec can_reconstruct_snapshot != YES est rejeté (§69)")


def test_recommended_for_mvp_requires_true_or_timestamped_snapshot_model():
    p = TimestampedOddsProvider(**_base_kwargs(snapshot_model="OPEN_CLOSE_ONLY"))
    problems = validate_provider(p)
    assert any("snapshot_model" in msg for msg in problems)
    print("  [OK] RECOMMENDED_FOR_MVP + snapshot_model=OPEN_CLOSE_ONLY est rejeté")


def test_recommended_for_mvp_requires_commercial_usage_allowed():
    p = TimestampedOddsProvider(**_base_kwargs(commercial_usage="LEGAL_REVIEW_REQUIRED"))
    problems = validate_provider(p)
    assert any("commercial_usage" in msg for msg in problems)
    print("  [OK] RECOMMENDED_FOR_MVP + commercial_usage=LEGAL_REVIEW_REQUIRED est rejeté")


def test_missing_sources_is_flagged():
    p = TimestampedOddsProvider(**_base_kwargs(sources=[]))
    problems = validate_provider(p)
    assert any("aucune source" in msg for msg in problems)
    print("  [OK] un TimestampedOddsProvider sans source citée est signalé")


def test_invalid_enum_values_detected():
    p = TimestampedOddsProvider(**_base_kwargs(snapshot_model="MAYBE_TIMESTAMPED"))
    problems = validate_provider(p)
    assert any("snapshot_model invalide" in msg for msg in problems)
    print("  [OK] une valeur hors énumération (snapshot_model) est détectée")


def test_snapshot_models_match_prompt_vocabulary():
    assert set(SNAPSHOT_MODELS) == {
        "TRUE_SNAPSHOT_HISTORY", "TIMESTAMPED_HISTORICAL", "OPEN_CLOSE_ONLY",
        "HISTORICAL_UNTIMESTAMPED", "CURRENT_ONLY", "UNKNOWN",
    }
    print("  [OK] SNAPSHOT_MODELS reproduit exactement les 6 classes A-F du §6")


def test_cutoff_horizons_match_prompt():
    assert CUTOFF_HORIZONS == ("T-24h", "T-12h", "T-6h", "T-3h", "T-1h")
    print("  [OK] CUTOFF_HORIZONS reproduit exactement les 5 horizons du §15")


def test_xfoot_leagues_has_eleven_entries():
    assert len(XFOOT_LEAGUES) == 11
    assert set(XFOOT_LEAGUES) == {
        "Bundesliga", "Ligue1", "PremierLeague", "SerieA", "LaLiga",
        "ChampionsLeague", "ConferenceLeague", "EuropaLeague", "MLS", "PrimeiraLiga", "SaudiProLeague",
    }
    print("  [OK] XFOOT_LEAGUES couvre exactement les 11 ligues du §21")


# ---------------------------------------------------------------------------
# decide_verdict
# ---------------------------------------------------------------------------

def test_decide_verdict_never_recommended_without_yes_reconstruction():
    verdict = decide_verdict(
        can_reconstruct_snapshot="PARTIAL", snapshot_model="TRUE_SNAPSHOT_HISTORY",
        coverage_score="EXCELLENT", temporal_score="EXCELLENT", leakage_risk="LOW",
        commercial_usage="ALLOWED",
    )
    assert verdict not in ("SHORTLIST", "RECOMMENDED_FOR_MVP")
    print(f"  [OK] can_reconstruct_snapshot=PARTIAL -> verdict={verdict} (jamais SHORTLIST/RECOMMENDED_FOR_MVP, §69)")


def test_decide_verdict_restricted_commercial_is_do_not_use():
    verdict = decide_verdict(
        can_reconstruct_snapshot="YES", snapshot_model="TRUE_SNAPSHOT_HISTORY",
        coverage_score="EXCELLENT", temporal_score="EXCELLENT", leakage_risk="LOW",
        commercial_usage="RESTRICTED",
    )
    assert verdict == "DO_NOT_USE"
    print("  [OK] commercial_usage=RESTRICTED -> DO_NOT_USE, quelle que soit la qualité temporelle")


def test_decide_verdict_untimestamped_never_exceeds_consider():
    """Cas football-data.co.uk (Phase 8D/8E) : historique réel mais aucun
    timestamp -> jamais mieux que CONSIDER, même si le coût est nul."""
    verdict = decide_verdict(
        can_reconstruct_snapshot="NO", snapshot_model="HISTORICAL_UNTIMESTAMPED",
        coverage_score="EXCELLENT", temporal_score="POOR", leakage_risk="MEDIUM",
        commercial_usage="ALLOWED",
    )
    assert verdict == "CONSIDER"
    print(f"  [OK] cas football-data.co.uk (HISTORICAL_UNTIMESTAMPED) -> verdict={verdict}, jamais promu malgré un coût nul")


def test_decide_verdict_best_case_reaches_recommended_for_mvp():
    verdict = decide_verdict(
        can_reconstruct_snapshot="YES", snapshot_model="TRUE_SNAPSHOT_HISTORY",
        coverage_score="EXCELLENT", temporal_score="EXCELLENT", leakage_risk="LOW",
        commercial_usage="ALLOWED",
    )
    assert verdict == "RECOMMENDED_FOR_MVP"
    print("  [OK] le meilleur cas (YES + TRUE_SNAPSHOT_HISTORY + EXCELLENT partout + LOW leakage + ALLOWED) atteint RECOMMENDED_FOR_MVP")


def test_decide_verdict_legal_review_capped_at_shortlist():
    verdict = decide_verdict(
        can_reconstruct_snapshot="YES", snapshot_model="TRUE_SNAPSHOT_HISTORY",
        coverage_score="EXCELLENT", temporal_score="EXCELLENT", leakage_risk="LOW",
        commercial_usage="LEGAL_REVIEW_REQUIRED",
    )
    assert verdict == "SHORTLIST"
    print("  [OK] commercial_usage=LEGAL_REVIEW_REQUIRED plafonne à SHORTLIST, jamais RECOMMENDED_FOR_MVP")


def test_decide_verdict_duplicate_capped_at_consider():
    verdict = decide_verdict(
        can_reconstruct_snapshot="YES", snapshot_model="TRUE_SNAPSHOT_HISTORY",
        coverage_score="EXCELLENT", temporal_score="EXCELLENT", leakage_risk="LOW",
        commercial_usage="ALLOWED", duplicate_of_existing=True,
    )
    assert verdict == "CONSIDER"
    print("  [OK] duplicate_of_existing=True plafonne à CONSIDER")


def test_decide_verdict_unknown_temporal_score_never_confident():
    verdict = decide_verdict(
        can_reconstruct_snapshot="YES", snapshot_model="TRUE_SNAPSHOT_HISTORY",
        coverage_score="EXCELLENT", temporal_score="UNKNOWN", leakage_risk="LOW",
        commercial_usage="ALLOWED",
    )
    assert verdict == "CONSIDER"
    print("  [OK] temporal_score=UNKNOWN plafonne à CONSIDER (jamais une confiance non justifiée)")


def test_decide_verdict_is_deterministic():
    kwargs = dict(
        can_reconstruct_snapshot="YES", snapshot_model="TIMESTAMPED_HISTORICAL",
        coverage_score="GOOD", temporal_score="GOOD", leakage_risk="MEDIUM", commercial_usage="ALLOWED",
    )
    v1 = decide_verdict(**kwargs)
    v2 = decide_verdict(**kwargs)
    assert v1 == v2
    print(f"  [OK] decide_verdict est déterministe (même entrée -> {v1} à chaque appel)")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    failures = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            failures += 1
            print(f"  [FAIL] {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests OK")
    sys.exit(1 if failures else 0)
