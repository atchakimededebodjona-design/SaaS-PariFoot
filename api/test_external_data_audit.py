"""
test_external_data_audit.py — Phase 8C : tests de
app/ai/external_data/scorecard.py.

Aucune DB nécessaire (ce module ne lit/écrit jamais match/match_stats/
model_predictions/model_versions/team_ratings/prediction_log — voir §56 du
prompt) : néanmoins un test de sécurité DB est inclus par cohérence avec les
autres suites Phase 8, pour prouver formellement l'absence d'effet de bord.

Usage : python api/test_external_data_audit.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_external_data_audit.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match, MatchStats
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion, TeamRating
from app.ai.external_data.scorecard import (
    ProviderRecord, validate_provider, derive_leakage_risk, decide_verdict,
    DOMAINS, HISTORY_CLASSES, LEAKAGE_RISKS, COST_CATEGORIES, VERDICTS,
)

init_db()


def _base_kwargs(**overrides) -> dict:
    kwargs = dict(
        name="TestProvider", domains=["odds"], coverage_notes="5/5 ligues DB, 6/6 CSV",
        coverage_score="EXCELLENT", history="FULL_HISTORY",
        timestamp_quality="opening_timestamp et closing_timestamp exposés",
        leakage_risk="LOW", cost_category="LOW", cost_notes="9$/mois (vérifié 2026-08-30)",
        commercial_usage="ALLOWED", storage_rights_notes="raw+derived autorisés",
        redistribution_notes="affichage utilisateur autorisé",
        integration_complexity="LOW", historical_reconstruction="YES",
        duplicate_of_existing=False, duplicate_notes="Aucune duplication (Xfoot n'a aucune donnée de cote).",
        sources=["https://example.com/docs"], verified_date="2026-08-30",
        verdict="RECOMMENDED_FOR_MVP",
    )
    kwargs.update(overrides)
    return kwargs


def test_enums_match_prompt_vocabulary():
    assert set(DOMAINS) == {"odds", "injuries", "suspensions", "lineups", "standings", "weather"}
    assert set(HISTORY_CLASSES) == {"FULL_HISTORY", "PARTIAL_HISTORY", "CURRENT_ONLY", "UNKNOWN"}
    assert set(LEAKAGE_RISKS) == {"LOW", "MEDIUM", "HIGH", "UNKNOWN"}
    assert set(COST_CATEGORIES) == {"FREE", "LOW", "MEDIUM", "HIGH", "ENTERPRISE", "UNKNOWN"}
    assert set(VERDICTS) == {"DO_NOT_USE", "CONSIDER", "SHORTLIST", "RECOMMENDED_FOR_MVP"}
    print("  [OK] les énumérations reproduisent exactement le vocabulaire du prompt Phase 8C (§3/§8/§29/§36/§59)")


def test_valid_record_has_no_problems():
    p = ProviderRecord(**_base_kwargs())
    problems = validate_provider(p)
    assert problems == [], f"Enregistrement valide signalé comme incohérent : {problems}"
    print("  [OK] un ProviderRecord entièrement cohérent ne déclenche aucun problème")


def test_recommended_for_mvp_requires_full_history():
    p = ProviderRecord(**_base_kwargs(history="CURRENT_ONLY"))
    problems = validate_provider(p)
    assert any("CURRENT_ONLY" in msg for msg in problems)
    print("  [OK] RECOMMENDED_FOR_MVP + history=CURRENT_ONLY est rejeté (§64.3)")


def test_recommended_for_mvp_requires_low_leakage():
    p = ProviderRecord(**_base_kwargs(leakage_risk="HIGH"))
    problems = validate_provider(p)
    assert any("leakage_risk" in msg for msg in problems)
    print("  [OK] RECOMMENDED_FOR_MVP + leakage_risk=HIGH est rejeté (§64.5)")


def test_recommended_for_mvp_requires_commercial_usage_allowed():
    p = ProviderRecord(**_base_kwargs(commercial_usage="LEGAL_REVIEW_REQUIRED"))
    problems = validate_provider(p)
    assert any("commercial_usage" in msg for msg in problems)
    print("  [OK] RECOMMENDED_FOR_MVP + commercial_usage=LEGAL_REVIEW_REQUIRED est rejeté (§64.8/§63)")


def test_missing_sources_is_flagged():
    p = ProviderRecord(**_base_kwargs(sources=[]))
    problems = validate_provider(p)
    assert any("aucune source" in msg for msg in problems)
    print("  [OK] un ProviderRecord sans source citée est signalé (§5 : jamais une décision non sourcée)")


def test_invalid_enum_values_are_flagged():
    p = ProviderRecord(**_base_kwargs(history="MOSTLY_AVAILABLE"))
    problems = validate_provider(p)
    assert any("history invalide" in msg for msg in problems)
    print("  [OK] une valeur hors énumération (history) est détectée")


def test_unknown_timestamp_quality_cannot_be_low_leakage():
    p = ProviderRecord(**_base_kwargs(
        timestamp_quality="UNKNOWN / NEEDS CONFIRMATION", leakage_risk="LOW",
    ))
    problems = validate_provider(p)
    assert any("timestamp_quality signale une incertitude" in msg for msg in problems)
    print("  [OK] timestamp incertain + leakage_risk=LOW est incohérent, détecté (§65 : timestamp insuffisant -> LEAKAGE_RISK)")


# ---------------------------------------------------------------------------
# derive_leakage_risk (§65)
# ---------------------------------------------------------------------------

def test_derive_leakage_risk_current_only_is_high():
    assert derive_leakage_risk("CURRENT_ONLY", has_reported_at_timestamp=False) == "HIGH"
    print("  [OK] derive_leakage_risk(CURRENT_ONLY, sans timestamp) = HIGH")


def test_derive_leakage_risk_full_history_with_timestamp_is_low():
    assert derive_leakage_risk("FULL_HISTORY", has_reported_at_timestamp=True) == "LOW"
    print("  [OK] derive_leakage_risk(FULL_HISTORY, avec timestamp) = LOW")


def test_derive_leakage_risk_unknown_history_is_unknown():
    assert derive_leakage_risk("UNKNOWN", has_reported_at_timestamp=True) == "UNKNOWN"
    print("  [OK] derive_leakage_risk(UNKNOWN, ...) = UNKNOWN (jamais optimiste par défaut)")


# ---------------------------------------------------------------------------
# decide_verdict (§59/§60)
# ---------------------------------------------------------------------------

def test_decide_verdict_never_recommended_solely_for_low_cost():
    # Un fournisseur GRATUIT mais avec un historique CURRENT_ONLY ne doit
    # JAMAIS devenir RECOMMENDED_FOR_MVP uniquement parce qu'il est gratuit (§60).
    verdict = decide_verdict(
        coverage_score="EXCELLENT", history="CURRENT_ONLY", leakage_risk="HIGH",
        cost_category="FREE", commercial_usage="ALLOWED", integration_complexity="LOW",
        historical_reconstruction="NO", duplicate_of_existing=False,
    )
    assert verdict != "RECOMMENDED_FOR_MVP"
    print(f"  [OK] cost=FREE mais history=CURRENT_ONLY/leakage=HIGH -> verdict={verdict} (jamais RECOMMENDED_FOR_MVP)")


def test_decide_verdict_duplicate_capped_at_consider():
    verdict = decide_verdict(
        coverage_score="EXCELLENT", history="FULL_HISTORY", leakage_risk="LOW",
        cost_category="LOW", commercial_usage="ALLOWED", integration_complexity="LOW",
        historical_reconstruction="YES", duplicate_of_existing=True,
    )
    assert verdict == "CONSIDER"
    print("  [OK] une source par ailleurs excellente mais DUPLICATE_OF_EXISTING est plafonnée à CONSIDER (§43)")


def test_decide_verdict_restricted_commercial_usage_is_do_not_use():
    verdict = decide_verdict(
        coverage_score="EXCELLENT", history="FULL_HISTORY", leakage_risk="LOW",
        cost_category="FREE", commercial_usage="RESTRICTED", integration_complexity="LOW",
        historical_reconstruction="YES", duplicate_of_existing=False,
    )
    assert verdict == "DO_NOT_USE"
    print("  [OK] commercial_usage=RESTRICTED -> DO_NOT_USE, quelle que soit la qualité par ailleurs")


def test_decide_verdict_best_case_reaches_recommended_for_mvp():
    verdict = decide_verdict(
        coverage_score="EXCELLENT", history="FULL_HISTORY", leakage_risk="LOW",
        cost_category="LOW", commercial_usage="ALLOWED", integration_complexity="LOW",
        historical_reconstruction="YES", duplicate_of_existing=False,
    )
    assert verdict == "RECOMMENDED_FOR_MVP"
    print("  [OK] le meilleur cas (couverture excellente, historique complet, faible fuite, usage commercial autorisé) atteint RECOMMENDED_FOR_MVP")


def test_decide_verdict_uncertain_facts_never_produce_confident_verdict():
    for uncertain_field, kwargs in [
        ("history", dict(history="UNKNOWN")),
        ("leakage_risk", dict(leakage_risk="UNKNOWN")),
        ("commercial_usage", dict(commercial_usage="UNKNOWN")),
    ]:
        base = dict(
            coverage_score="EXCELLENT", history="FULL_HISTORY", leakage_risk="LOW",
            cost_category="LOW", commercial_usage="ALLOWED", integration_complexity="LOW",
            historical_reconstruction="YES", duplicate_of_existing=False,
        )
        base.update(kwargs)
        verdict = decide_verdict(**base)
        assert verdict == "CONSIDER", f"{uncertain_field}=UNKNOWN devrait forcer CONSIDER, obtenu {verdict}"
    print("  [OK] toute incertitude factuelle (UNKNOWN) plafonne le verdict à CONSIDER, jamais SHORTLIST/RECOMMENDED_FOR_MVP")


# ---------------------------------------------------------------------------
# Déterminisme (§55 : "deterministic report")
# ---------------------------------------------------------------------------

def test_decide_verdict_is_deterministic():
    kwargs = dict(
        coverage_score="GOOD", history="PARTIAL_HISTORY", leakage_risk="MEDIUM",
        cost_category="MEDIUM", commercial_usage="ALLOWED", integration_complexity="MEDIUM",
        historical_reconstruction="PARTIAL", duplicate_of_existing=False,
    )
    v1 = decide_verdict(**kwargs)
    v2 = decide_verdict(**kwargs)
    assert v1 == v2
    print(f"  [OK] decide_verdict est déterministe (même entrée -> {v1} à chaque appel)")


# ---------------------------------------------------------------------------
# Sécurité DB (§56) — ce module n'importe même pas les tables concernées,
# mais on le prouve formellement comme les autres suites Phase 8.
# ---------------------------------------------------------------------------

def _row_counts(session):
    return {
        "match": len(session.exec(select(Match)).all()),
        "match_stats": len(session.exec(select(MatchStats)).all()),
        "model_predictions": len(session.exec(select(ModelPrediction)).all()),
        "model_versions": len(session.exec(select(ModelVersion)).all()),
        "team_ratings": len(session.exec(select(TeamRating)).all()),
    }


def test_scorecard_module_never_touches_production_tables():
    with Session(engine) as session:
        before = _row_counts(session)

    records = [ProviderRecord(**_base_kwargs(name=f"Provider{i}")) for i in range(5)]
    for r in records:
        validate_provider(r)
    decide_verdict(
        coverage_score="GOOD", history="PARTIAL_HISTORY", leakage_risk="MEDIUM",
        cost_category="MEDIUM", commercial_usage="ALLOWED", integration_complexity="MEDIUM",
        historical_reconstruction="PARTIAL", duplicate_of_existing=False,
    )

    with Session(engine) as session:
        after = _row_counts(session)

    assert before == after, f"Compteurs DB modifiés par un module qui ne devrait faire aucune I/O : {before} != {after}"
    print(f"  [OK] app.ai.external_data.scorecard est strictement sans effet de bord DB : {before}")


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
