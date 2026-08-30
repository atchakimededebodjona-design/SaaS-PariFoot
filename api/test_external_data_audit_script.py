"""
test_external_data_audit_script.py — Phase 8C : tests de
scripts/external_data_audit.py (assemblage des ProviderRecord + rapport).

Complète api/test_external_data_audit.py (tests unitaires de scorecard.py) —
ici on teste l'assemblage réel des ~23 fournisseurs de recherche : cohérence
interne, déterminisme du rapport, sécurité DB, absence d'appel réseau.

Usage : python api/test_external_data_audit_script.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_external_data_audit_script.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match, MatchStats
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion, TeamRating

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import external_data_audit as eda  # noqa: E402
from app.ai.external_data.scorecard import DOMAINS  # noqa: E402

init_db()


def test_all_providers_pass_internal_validation():
    problems = eda.validate_all()
    assert problems == [], f"Incohérences dans PROVIDERS : {problems}"
    print(f"  [OK] les {len(eda.PROVIDERS)} ProviderRecord passent validate_provider() sans incohérence")


def test_at_least_one_provider_per_domain():
    covered = set()
    for p in eda.PROVIDERS:
        covered.update(p.domains)
    missing = set(DOMAINS) - covered
    assert not missing, f"Domaines sans aucun fournisseur étudié : {missing}"
    print(f"  [OK] les 6 domaines (§2 du prompt) ont chacun au moins un fournisseur étudié : {sorted(covered)}")


def test_no_provider_recommended_for_mvp_without_full_history_and_low_leakage():
    for p in eda.PROVIDERS:
        if p.verdict == "RECOMMENDED_FOR_MVP":
            assert p.history == "FULL_HISTORY", f"{p.name}: RECOMMENDED_FOR_MVP sans FULL_HISTORY"
            assert p.leakage_risk in ("LOW", "MEDIUM"), f"{p.name}: RECOMMENDED_FOR_MVP avec leakage_risk={p.leakage_risk}"
            assert p.commercial_usage == "ALLOWED", f"{p.name}: RECOMMENDED_FOR_MVP sans commercial_usage=ALLOWED"
    print("  [OK] tout fournisseur RECOMMENDED_FOR_MVP a bien FULL_HISTORY + faible leakage + usage commercial autorisé (§60)")


def test_verdict_never_hand_assigned_bypassing_decide_verdict():
    for p in eda.PROVIDERS:
        recomputed = eda.decide_verdict(
            coverage_score=p.coverage_score, history=p.history, leakage_risk=p.leakage_risk,
            cost_category=p.cost_category, commercial_usage=p.commercial_usage,
            integration_complexity=p.integration_complexity,
            historical_reconstruction=p.historical_reconstruction,
            duplicate_of_existing=p.duplicate_of_existing,
        )
        assert recomputed == p.verdict, f"{p.name}: verdict stocké '{p.verdict}' != recalculé '{recomputed}' à partir des mêmes faits"
    print("  [OK] chaque verdict est reproductible depuis les faits déclarés (jamais assigné à la main de façon incohérente)")


def test_restricted_commercial_usage_always_do_not_use():
    for p in eda.PROVIDERS:
        if p.commercial_usage == "RESTRICTED":
            assert p.verdict == "DO_NOT_USE", f"{p.name}: commercial_usage=RESTRICTED mais verdict={p.verdict}"
    print("  [OK] tout fournisseur à usage commercial RESTRICTED est classé DO_NOT_USE (OddsPortal/FotMob/Transfermarkt)")


def test_duplicate_providers_never_recommended():
    for p in eda.PROVIDERS:
        if p.duplicate_of_existing:
            assert p.verdict in ("CONSIDER", "DO_NOT_USE"), f"{p.name}: duplicate_of_existing=True mais verdict={p.verdict}"
    print("  [OK] les fournisseurs standings dupliquant la reconstruction interne Xfoot ne dépassent jamais CONSIDER (§43)")


def test_domain_priority_covers_all_six_domains():
    domains_covered = {row["domain"] for row in eda.DOMAIN_PRIORITY}
    assert domains_covered == set(DOMAINS)
    print("  [OK] DOMAIN_PRIORITY couvre exactement les 6 domaines du prompt (§42)")


def test_scorecards_are_non_empty_for_each_market():
    scorecards = eda.build_scorecards([vars(p) for p in eda.PROVIDERS])
    assert len(scorecards["odds_scorecard"]) >= 1
    assert len(scorecards["injury_scorecard"]) >= 1
    assert len(scorecards["lineup_scorecard"]) >= 1
    print(f"  [OK] scorecards non vides : odds={len(scorecards['odds_scorecard'])}, injuries={len(scorecards['injury_scorecard'])}, lineups={len(scorecards['lineup_scorecard'])}")


def test_report_generation_is_deterministic():
    problems = eda.validate_all()
    assert problems == []
    providers_dicts = [vars(p) for p in eda.PROVIDERS]
    scorecards1 = eda.build_scorecards(providers_dicts)
    scorecards2 = eda.build_scorecards(providers_dicts)
    assert scorecards1 == scorecards2
    print("  [OK] build_scorecards() est déterministe (même entrée -> même sortie)")


def test_no_fabricated_price_all_unknown_are_marked():
    """§65 : ne jamais inventer un prix — toute cost_notes contenant une
    estimation non officielle doit le signaler explicitement."""
    markers = ("UNKNOWN", "AUCUN", "N/A", "NON CONFIRM", "NON PUBLI", "PAS DE TARIF", "AUCUNE TARIFICATION")
    for p in eda.PROVIDERS:
        if p.cost_category == "UNKNOWN":
            upper = p.cost_notes.upper()
            assert any(m in upper for m in markers), (
                f"{p.name}: cost_category=UNKNOWN mais cost_notes ne le documente pas explicitement : {p.cost_notes}"
            )
    print("  [OK] tout cost_category=UNKNOWN est documenté comme tel dans cost_notes (jamais un prix inventé)")


# ---------------------------------------------------------------------------
# Sécurité DB (§56) — le module d'assemblage ne doit RIEN écrire.
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

    eda.validate_all()
    eda.build_scorecards([vars(p) for p in eda.PROVIDERS])
    eda.render_markdown({
        "run_id": "test", "generated_at": "test", "providers": [vars(p) for p in eda.PROVIDERS],
        "verdict_counts": {}, "recommended_for_mvp_count": 0, "pricing_summary": "",
        "top_providers_text": "", "mvp_recommendation": "", "limitations": [],
        "recommendations_phase_8d": [], "db_counts_before": {}, "db_counts_after": {}, "db_unchanged": True,
        "odds_scorecard": [], "injury_scorecard": [], "lineup_scorecard": [],
    })

    with Session(engine) as session:
        after = _row_counts(session)

    assert before == after, f"Compteurs DB modifiés par un module qui ne devrait faire aucune I/O : {before} != {after}"
    print(f"  [OK] validate_all()/build_scorecards()/render_markdown() sont strictement read-only vis-à-vis de la DB : {before}")


def test_no_network_imports_in_audit_script():
    """§52 : aucun client API de production ne doit être créé dans cette phase.
    Vérifie qu'aucune bibliothèque HTTP (httpx/requests) n'est importée par le
    module d'assemblage — toute recherche réseau appartient aux agents de
    recherche, jamais au code livré."""
    source = Path(eda.__file__).read_text(encoding="utf-8")
    for forbidden in ("import httpx", "import requests", "API_FOOTBALL_KEY", "os.environ"):
        assert forbidden not in source, f"'{forbidden}' trouvé dans external_data_audit.py — ce module doit rester 100% hors-ligne (§52/§54)"
    print("  [OK] scripts/external_data_audit.py ne contient aucun import réseau ni référence à une clé API (§52)")


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
