"""
test_value_engine.py — Phase 8H : tests de api/app/ai/value/{core,quality,schemas}.py.

Base isolée dédiée (jamais api/app.db) — même précaution que les suites de
tests précédentes (voir _test_support.py). Le Value Engine est purement
fonctionnel (aucun accès DB) ; ce fichier vérifie néanmoins explicitement
qu'aucune table n'est modifiée (§37 du prompt Phase 8H).

Usage : python api/test_value_engine.py
"""

import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_value_engine.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match, MatchStats
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion, TeamRating

from app.ai.value.schemas import (
    ModelProbability, OddsSnapshot, ValueThresholds, RESEARCH_DEFAULT_THRESHOLDS,
)
from app.ai.value.quality import (
    classify_temporal_status, compute_odds_age_hours, evaluate_quality_gates, is_production_eligible,
    TEMPORAL_STATUSES,
)
from app.ai.value.core import (
    compute_market_probabilities, edge, expected_value, classify_value_type,
    build_market_consensus, bookmaker_dispersion, build_value_signal,
    rank_value_signals, evaluate_threshold_grid, classify_market_dominance,
    VALUE_TYPES, REJECTION_REASONS, EDGE_GRID, EV_GRID,
)

init_db()
UTC = timezone.utc


def _row_counts(session):
    return {
        "match": len(session.exec(select(Match)).all()),
        "match_stats": len(session.exec(select(MatchStats)).all()),
        "model_predictions": len(session.exec(select(ModelPrediction)).all()),
        "model_versions": len(session.exec(select(ModelVersion)).all()),
        "team_ratings": len(session.exec(select(TeamRating)).all()),
    }


# ---------------------------------------------------------------------------
# 1. Probabilité implicite / normalisation / overround (§5/§6/§12/§21/§22/§23)
# ---------------------------------------------------------------------------

def test_implied_probability_reused_from_odds_research_core():
    result = compute_market_probabilities({"home_win": 2.0, "draw": 3.0, "away_win": 4.0})
    assert result is not None
    assert math.isclose(result["raw"]["home_win"], 0.5)
    assert math.isclose(result["raw"]["draw"], 1 / 3, rel_tol=1e-9)
    print("  [OK] compute_market_probabilities réutilise implied_probability (Phase 8D) sans réimplémentation")


def test_normalization_removes_overround_and_keeps_raw_separate():
    result = compute_market_probabilities({"home": 1.9, "away": 1.9})  # overround structurel
    assert result is not None
    assert result["overround"] > 0
    assert math.isclose(sum(result["normalized"].values()), 1.0, rel_tol=1e-9)
    assert not math.isclose(sum(result["raw"].values()), 1.0, rel_tol=1e-9)
    print(f"  [OK] normalized somme à 1.0 (overround={result['overround']:.4f}), raw reste distinct de normalized")


def test_overround_example_from_prompt():
    # §12 : Home 0.50, Draw 0.30, Away 0.25 -> overround = 0.05
    result = compute_market_probabilities({"home": 1 / 0.50, "draw": 1 / 0.30, "away": 1 / 0.25})
    assert result is not None
    assert math.isclose(result["overround"], 0.05, abs_tol=1e-9)
    print(f"  [OK] overround exemple prompt : {result['overround']:.4f} == 0.05")


def test_market_1x2_full_pipeline():
    result = compute_market_probabilities({"home_win": 1.8, "draw": 3.6, "away_win": 4.5})
    assert result is not None and set(result["raw"]) == {"home_win", "draw", "away_win"}
    print("  [OK] 1X2 : pipeline complet (raw/normalized/overround) fonctionne pour 3 issues")


def test_market_btts_available_only_if_both_odds_provided():
    assert compute_market_probabilities({"yes": 1.9, "no": 1.9}) is not None
    assert compute_market_probabilities({"yes": 1.9}) is None  # une seule issue -> NOT_AVAILABLE
    print("  [OK] BTTS : disponible avec 2 cotes, NOT_AVAILABLE (None) avec une seule")


def test_market_over_under_same_rule_as_btts():
    assert compute_market_probabilities({"over": 2.0, "under": 1.8}) is not None
    assert compute_market_probabilities({}) is None
    print("  [OK] O/U 2.5 : même règle de disponibilité que BTTS")


def test_invalid_odds_never_imputed():
    assert compute_market_probabilities({"home": 1.5, "draw": 0.9, "away": 2.0}) is None  # 0.9 <= 1 invalide
    print("  [OK] une seule cote invalide (<=1) -> marché entier None, jamais imputée")


# ---------------------------------------------------------------------------
# 2. Edge / EV (§7/§8, exemples exacts du prompt)
# ---------------------------------------------------------------------------

def test_edge_sign_convention():
    assert math.isclose(edge(0.60, 0.50), 0.10, abs_tol=1e-9)
    assert math.isclose(edge(0.40, 0.50), -0.10, abs_tol=1e-9)
    print("  [OK] edge = p_model - p_market, signe conforme à la convention documentée")


def test_ev_example_from_prompt():
    assert math.isclose(expected_value(0.60, 2.00), 0.20, abs_tol=1e-9)
    print("  [OK] EV(p=0.60, odds=2.00) == +0.20 (exemple exact du prompt)")


def test_classify_value_type_requires_concordant_signs():
    assert classify_value_type(0.10, 0.20) == "POSITIVE_VALUE"
    assert classify_value_type(-0.10, -0.05) == "NEGATIVE_VALUE"
    assert classify_value_type(0.10, -0.02) == "NEUTRAL"  # signes discordants -> jamais forcé
    print("  [OK] classify_value_type : POSITIVE/NEGATIVE seulement si edge et EV concordants, sinon NEUTRAL")


# ---------------------------------------------------------------------------
# 3. Timestamp / cutoff / exclusion du futur (§10/§11/§33 test adversarial)
# ---------------------------------------------------------------------------

def test_temporal_status_unknown_never_safe_without_timestamp():
    assert classify_temporal_status(None, datetime(2026, 1, 1, tzinfo=UTC)) == "UNKNOWN"
    assert classify_temporal_status(datetime(2026, 1, 1, tzinfo=UTC), None) == "UNKNOWN"
    print("  [OK] timestamp ou cutoff absent -> UNKNOWN, jamais SAFE")


def test_temporal_status_verified_requires_measured_timestamp():
    ts = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    cutoff = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert classify_temporal_status(ts, cutoff, has_measured_timestamp=True) == "TEMPORALLY_VERIFIED"
    assert classify_temporal_status(ts, cutoff, has_measured_timestamp=False) == "HISTORICAL_UNVERIFIED"
    print("  [OK] même arithmétique de date, statut diffère selon has_measured_timestamp (§42)")


def test_football_data_co_uk_style_never_temporally_verified():
    # §42 : une source sans mesure prouvée (ex. football-data.co.uk) reste HISTORICAL_UNVERIFIED, jamais SAFE/TEMPORALLY_VERIFIED.
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    cutoff = datetime(2026, 1, 2, tzinfo=UTC)
    status = classify_temporal_status(ts, cutoff, has_measured_timestamp=False)
    assert status == "HISTORICAL_UNVERIFIED"
    assert not is_production_eligible(status)
    print("  [OK] source non mesurée (§42) : HISTORICAL_UNVERIFIED, is_production_eligible=False")


def test_future_information_case_C_from_prompt():
    # Case C (§32) : future odds -> TEMPORALLY_UNSAFE au niveau ValueSignal.
    kickoff = datetime(2026, 3, 16, 20, 0, tzinfo=UTC)
    cutoff = kickoff - timedelta(hours=6)
    future_ts = kickoff - timedelta(hours=1)  # après le cutoff, avant kickoff -> FUTURE_INFORMATION
    status = classify_temporal_status(future_ts, cutoff, kickoff, has_measured_timestamp=True)
    assert status == "FUTURE_INFORMATION"
    print("  [OK] Case C (§32) : odds postérieures au cutoff -> FUTURE_INFORMATION")


def test_unknown_timestamp_case_D_from_prompt():
    # Case D (§32) : unknown timestamp -> TEMPORAL_UNVERIFIED (via build_value_signal, testé plus bas).
    assert classify_temporal_status(None, datetime(2026, 1, 1, tzinfo=UTC)) == "UNKNOWN"
    print("  [OK] Case D (§32) : timestamp inconnu -> UNKNOWN")


def test_adversarial_snapshot_exclusion_section_33():
    # §33 : kickoff=20:00, odds à 14:00/18:30/19:45/20:10, cutoff=14:00 -> seul 14:00 est SAFE/TEMPORALLY_VERIFIED.
    day = datetime(2026, 3, 16, tzinfo=UTC)
    kickoff = day.replace(hour=20)
    cutoff = day.replace(hour=14)
    candidates = {
        "14:00": day.replace(hour=14), "18:30": day.replace(hour=18, minute=30),
        "19:45": day.replace(hour=19, minute=45), "20:10": day.replace(hour=20, minute=10),
    }
    results = {label: classify_temporal_status(ts, cutoff, kickoff, has_measured_timestamp=True) for label, ts in candidates.items()}
    assert results["14:00"] == "TEMPORALLY_VERIFIED"
    assert results["18:30"] == "FUTURE_INFORMATION"
    assert results["19:45"] == "FUTURE_INFORMATION"
    assert results["20:10"] == "FUTURE_INFORMATION"  # postérieur au kickoff -> rejeté aussi
    print(f"  [OK] Test adversarial §33 : {results}")


def test_odds_age_never_fabricated():
    assert compute_odds_age_hours(None, datetime(2026, 1, 1, tzinfo=UTC)) is None
    assert compute_odds_age_hours(datetime(2026, 1, 1, tzinfo=UTC), None) is None
    cutoff = datetime(2026, 1, 2, 6, 0, tzinfo=UTC)
    ts = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    assert math.isclose(compute_odds_age_hours(cutoff, ts), 24.0, abs_tol=1e-9)
    print("  [OK] odds_age None si un timestamp manque, 24.0h sinon (unité documentée : heures)")


# ---------------------------------------------------------------------------
# 4. Consensus / dispersion (§14/§15/§34 test adversarial)
# ---------------------------------------------------------------------------

def test_consensus_adversarial_section_34():
    # §34 : A=10:00, B=12:00, C=18:00, cutoff=14:00 -> consensus inclut A+B, exclut C.
    cutoff = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    snapshots = [
        OddsSnapshot(market="1X2", selection="home_win", decimal_odds=2.0, bookmaker="A",
                     odds_timestamp=datetime(2026, 1, 1, 10, 0, tzinfo=UTC), has_measured_timestamp=True),
        OddsSnapshot(market="1X2", selection="home_win", decimal_odds=2.1, bookmaker="B",
                     odds_timestamp=datetime(2026, 1, 1, 12, 0, tzinfo=UTC), has_measured_timestamp=True),
        OddsSnapshot(market="1X2", selection="home_win", decimal_odds=2.5, bookmaker="C",
                     odds_timestamp=datetime(2026, 1, 1, 18, 0, tzinfo=UTC), has_measured_timestamp=True),
    ]
    consensus = build_market_consensus(snapshots, "home_win", cutoff, min_bookmakers=2)
    assert consensus is not None
    assert consensus["bookmakers_included"] == ["A", "B"]
    assert consensus["bookmakers_excluded"] == ["C"]
    print(f"  [OK] Test adversarial §34 : inclus={consensus['bookmakers_included']} exclus={consensus['bookmakers_excluded']}")


def test_consensus_insufficient_data_never_fabricated():
    cutoff = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    snapshots = [
        OddsSnapshot(market="1X2", selection="home_win", decimal_odds=2.0, bookmaker="A",
                     odds_timestamp=datetime(2026, 1, 1, 10, 0, tzinfo=UTC), has_measured_timestamp=True),
    ]
    assert build_market_consensus(snapshots, "home_win", cutoff, min_bookmakers=2) is None
    assert bookmaker_dispersion(snapshots, "home_win", min_bookmakers=2) == "INSUFFICIENT_DATA"
    print("  [OK] 1 seul bookmaker -> consensus=None, dispersion=INSUFFICIENT_DATA (jamais fabriqués)")


def test_dispersion_computes_expected_statistics():
    snapshots = [
        OddsSnapshot(market="1X2", selection="home_win", decimal_odds=v, bookmaker=b)
        for b, v in [("A", 2.0), ("B", 2.2), ("C", 1.9)]
    ]
    d = bookmaker_dispersion(snapshots, "home_win", min_bookmakers=2)
    assert d["n"] == 3 and d["min_odds"] == 1.9 and d["max_odds"] == 2.2
    print(f"  [OK] dispersion : {d}")


def test_market_dominance_never_fabricated_without_scores():
    assert classify_market_dominance(None, None) == "UNKNOWN"
    assert classify_market_dominance(0.9, 0.8) == "MODEL_COMPETITIVE"
    assert classify_market_dominance(0.8, 0.9) == "MARKET_DOMINANT"
    print("  [OK] classify_market_dominance : UNKNOWN sans scores explicites, jamais déduit d'un simple edge")


# ---------------------------------------------------------------------------
# 5. Quality gates / rejection reasons / ValueSignal complet (§18/§19/§20/§32)
# ---------------------------------------------------------------------------

def _snap(selection, odds, ts=None, measured=False):
    return OddsSnapshot(market="1X2", selection=selection, decimal_odds=odds, odds_timestamp=ts, has_measured_timestamp=measured)


def test_synthetic_case_A_positive_ev():
    cutoff = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    ts = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    signal = build_value_signal(
        match_id=1, market="1X2", selection="home_win",
        model_probability=ModelProbability(market="1X2", selection="home_win", raw_probability=0.60),
        market_odds={"home_win": _snap("home_win", 2.00, ts, True), "draw": _snap("draw", 4.0, ts, True), "away_win": _snap("away_win", 5.0, ts, True)},
        cutoff_timestamp=cutoff,
    )
    assert signal.status == "POSITIVE_VALUE" and math.isclose(signal.expected_value, 0.20, abs_tol=1e-9)
    print(f"  [OK] Case A (§32) : EV={signal.expected_value:+.2f} -> {signal.status}")


def test_synthetic_case_B_negative_ev():
    cutoff = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    ts = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    signal = build_value_signal(
        match_id=1, market="1X2", selection="home_win",
        model_probability=ModelProbability(market="1X2", selection="home_win", raw_probability=0.45),
        market_odds={"home_win": _snap("home_win", 2.00, ts, True), "draw": _snap("draw", 4.0, ts, True), "away_win": _snap("away_win", 5.0, ts, True)},
        cutoff_timestamp=cutoff,
    )
    assert math.isclose(signal.expected_value, -0.10, abs_tol=1e-9)
    print(f"  [OK] Case B (§32) : EV={signal.expected_value:+.2f}")


def test_synthetic_case_C_future_odds_temporally_unsafe():
    kickoff = datetime(2026, 1, 1, 20, 0, tzinfo=UTC)
    cutoff = kickoff - timedelta(hours=6)
    future_ts = kickoff - timedelta(hours=1)
    signal = build_value_signal(
        match_id=1, market="1X2", selection="home_win",
        model_probability=ModelProbability(market="1X2", selection="home_win", raw_probability=0.60),
        market_odds={"home_win": _snap("home_win", 2.0, future_ts, True), "draw": _snap("draw", 4.0, future_ts, True), "away_win": _snap("away_win", 5.0, future_ts, True)},
        cutoff_timestamp=cutoff, match_kickoff=kickoff,
    )
    assert signal.status == "TEMPORALLY_UNSAFE" and signal.reason == "FUTURE_INFORMATION"
    print("  [OK] Case C (§32) : odds futures -> TEMPORALLY_UNSAFE / FUTURE_INFORMATION")


def test_synthetic_case_D_unknown_timestamp():
    cutoff = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    signal = build_value_signal(
        match_id=1, market="1X2", selection="home_win",
        model_probability=ModelProbability(market="1X2", selection="home_win", raw_probability=0.60),
        market_odds={"home_win": _snap("home_win", 2.0, None, False), "draw": _snap("draw", 4.0, None, False), "away_win": _snap("away_win", 5.0, None, False)},
        cutoff_timestamp=cutoff,
    )
    assert signal.status == "TEMPORALLY_UNSAFE" and signal.reason == "TEMPORAL_UNVERIFIED"
    print("  [OK] Case D (§32) : timestamp inconnu -> TEMPORALLY_UNSAFE / TEMPORAL_UNVERIFIED")


def test_synthetic_case_E_invalid_odds():
    cutoff = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    ts = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    signal = build_value_signal(
        match_id=1, market="1X2", selection="home_win",
        model_probability=ModelProbability(market="1X2", selection="home_win", raw_probability=0.60),
        market_odds={"home_win": _snap("home_win", 0.8, ts, True), "draw": _snap("draw", 4.0, ts, True), "away_win": _snap("away_win", 5.0, ts, True)},
        cutoff_timestamp=cutoff,
    )
    assert signal.status == "INVALID_ODDS" and signal.reason == "INVALID_ODDS"
    print("  [OK] Case E (§32) : cote <= 1 -> INVALID_ODDS")


def test_no_odds_rejection_reason():
    cutoff = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    signal = build_value_signal(
        match_id=1, market="1X2", selection="home_win",
        model_probability=ModelProbability(market="1X2", selection="home_win", raw_probability=0.60),
        market_odds={}, cutoff_timestamp=cutoff,
    )
    assert signal.reason == "NO_ODDS"
    print("  [OK] aucune cote fournie -> reason=NO_ODDS, jamais 'no value' générique")


def test_no_model_probability_rejection_reason():
    cutoff = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    ts = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    signal = build_value_signal(
        match_id=1, market="1X2", selection="home_win", model_probability=None,
        market_odds={"home_win": _snap("home_win", 2.0, ts, True), "draw": _snap("draw", 4.0, ts, True), "away_win": _snap("away_win", 5.0, ts, True)},
        cutoff_timestamp=cutoff,
    )
    assert signal.reason == "NO_MODEL_PROBABILITY"
    print("  [OK] probabilité modèle absente -> reason=NO_MODEL_PROBABILITY")


def test_all_rejection_reasons_are_from_fixed_vocabulary():
    for r in ("NO_ODDS", "INVALID_ODDS", "NO_MODEL_PROBABILITY", "FUTURE_INFORMATION", "TEMPORAL_UNVERIFIED"):
        assert r in REJECTION_REASONS
    for v in ("POSITIVE_VALUE", "NEGATIVE_VALUE", "NEUTRAL", "TEMPORALLY_UNSAFE", "INVALID_ODDS", "INSUFFICIENT_DATA"):
        assert v in VALUE_TYPES
    print("  [OK] toutes les raisons/statuts observés appartiennent au vocabulaire fixe (§13/§19)")


def test_quality_gates_deterministic_order():
    gate = evaluate_quality_gates(odds_valid=False, model_probability_valid=False, temporal_status="UNKNOWN", market_valid=False, sample_valid=False)
    assert gate.passed is False and gate.failure_reason == "INVALID_ODDS"  # premier gate en échec, ordre fixe
    gate2 = evaluate_quality_gates(odds_valid=True, model_probability_valid=True, temporal_status="TEMPORALLY_VERIFIED", market_valid=True, sample_valid=True)
    assert gate2.passed is True and gate2.failure_reason is None
    print("  [OK] evaluate_quality_gates : ordre de vérification déterministe, gate=True seulement si tout passe")


def test_temporal_gate_accepts_historical_unverified_for_research():
    gate = evaluate_quality_gates(odds_valid=True, model_probability_valid=True, temporal_status="HISTORICAL_UNVERIFIED", market_valid=True, sample_valid=True)
    assert gate.temporal_status_valid is True and gate.passed is True
    print("  [OK] HISTORICAL_UNVERIFIED passe le gate (utilisable en recherche, §10) sans devenir TEMPORALLY_VERIFIED")


# ---------------------------------------------------------------------------
# 6. Reproductibilité (§35)
# ---------------------------------------------------------------------------

def test_deterministic_output_same_input_same_output():
    cutoff = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    ts = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)

    def build():
        return build_value_signal(
            match_id=1, market="1X2", selection="home_win",
            model_probability=ModelProbability(market="1X2", selection="home_win", raw_probability=0.60),
            market_odds={"home_win": _snap("home_win", 2.0, ts, True), "draw": _snap("draw", 4.0, ts, True), "away_win": _snap("away_win", 5.0, ts, True)},
            cutoff_timestamp=cutoff,
        )
    s1, s2 = build(), build()
    assert s1 == s2
    print("  [OK] même input -> même ValueSignal (dataclass égal), aucune dépendance non déterministe")


def test_ranking_is_stable_and_explicit():
    cutoff = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    ts = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    signals = []
    for p, label in [(0.60, "a"), (0.55, "b"), (0.50, "c")]:
        signals.append(build_value_signal(
            match_id=1, market="1X2", selection="home_win",
            model_probability=ModelProbability(market="1X2", selection="home_win", raw_probability=p),
            market_odds={"home_win": _snap("home_win", 2.0, ts, True), "draw": _snap("draw", 4.0, ts, True), "away_win": _snap("away_win", 5.0, ts, True)},
            cutoff_timestamp=cutoff,
        ))
    ranked = rank_value_signals(signals, ["expected_value"])
    evs = [s.expected_value for s in ranked]
    assert evs == sorted(evs, reverse=True)
    print(f"  [OK] rank_value_signals(['expected_value']) : ordre décroissant respecté ({evs})")


def test_ranking_rejects_unknown_criteria():
    try:
        rank_value_signals([], ["not_a_real_criterion"])
        raise AssertionError("devait lever ValueError")
    except ValueError:
        print("  [OK] rank_value_signals rejette un critère inconnu (jamais un tri silencieux sur un mauvais champ)")


# ---------------------------------------------------------------------------
# 7. Grille de seuils (§28)
# ---------------------------------------------------------------------------

def test_threshold_grid_covers_full_matrix_research_only():
    cutoff = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    ts = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    signal = build_value_signal(
        match_id=1, market="1X2", selection="home_win",
        model_probability=ModelProbability(market="1X2", selection="home_win", raw_probability=0.60),
        market_odds={"home_win": _snap("home_win", 2.0, ts, True), "draw": _snap("draw", 4.0, ts, True), "away_win": _snap("away_win", 5.0, ts, True)},
        cutoff_timestamp=cutoff,
    )
    grid = evaluate_threshold_grid([signal])
    assert len(grid) == len(EDGE_GRID) * len(EV_GRID)
    print(f"  [OK] evaluate_threshold_grid : {len(grid)} combinaisons ({len(EDGE_GRID)}x{len(EV_GRID)}), RESEARCH ONLY")


# ---------------------------------------------------------------------------
# 8. Sécurité DB (§37) — le Value Engine n'accède jamais à la DB.
# ---------------------------------------------------------------------------

def test_value_engine_never_touches_the_database():
    with Session(engine) as session:
        session.add(Match(league="Ligue1", date=datetime.combine(date(2026, 1, 1), datetime.min.time()),
                           home_team="A", away_team="B", home_goals=1, away_goals=0))
        session.commit()
        before = _row_counts(session)

        cutoff = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        ts = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
        build_value_signal(
            match_id=1, market="1X2", selection="home_win",
            model_probability=ModelProbability(market="1X2", selection="home_win", raw_probability=0.60),
            market_odds={"home_win": _snap("home_win", 2.0, ts, True), "draw": _snap("draw", 4.0, ts, True), "away_win": _snap("away_win", 5.0, ts, True)},
            cutoff_timestamp=cutoff,
        )
        evaluate_threshold_grid([])
        rank_value_signals([], ["edge"])

        after = _row_counts(session)
    assert before == after, f"le Value Engine a modifié une table : avant={before} après={after}"
    print("  [OK] Value Engine strictement pur : aucune table modifiée")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    failures = 0
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  [FAIL] {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests OK")
    cleanup_db(DB_PATH)
    sys.exit(1 if failures else 0)
