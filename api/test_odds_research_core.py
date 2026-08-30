"""
test_odds_research_core.py — Phase 8D : tests de app/ai/odds_research/core.py
(fonctions pures — parsing, probabilité implicite, overround, retrait de
marge, mapping, qualité du timestamp). Aucun accès réseau/DB.

Usage : python api/test_odds_research_core.py
"""

import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.ai.odds_research.core import (
    is_valid_decimal_odds, implied_probability, overround, normalize_margin,
    compute_1x2_odds_features, compute_ou25_odds_features,
    DIV_TO_LEAGUE, LEAGUE_TO_DIV, normalize_team_name, map_league, match_key,
    classify_source_timestamp_quality, validate_explicit_timestamp,
    check_row_quality, PRE_CLOSING_SOURCE, CLOSING_SOURCE,
)


# ---------------------------------------------------------------------------
# §25 : validation de cote décimale
# ---------------------------------------------------------------------------

def test_valid_decimal_odds_accepts_normal_values():
    assert is_valid_decimal_odds(1.5)
    assert is_valid_decimal_odds("2.10")
    assert is_valid_decimal_odds(100.0)
    print("  [OK] cotes décimales normales acceptées (1.5, '2.10', 100.0)")


def test_invalid_decimal_odds_rejects_impossible_values():
    assert not is_valid_decimal_odds(1.0)     # 100% impossible pour un marché incertain
    assert not is_valid_decimal_odds(0.5)     # <= 1
    assert not is_valid_decimal_odds(-2.0)
    assert not is_valid_decimal_odds(None)
    assert not is_valid_decimal_odds("")
    assert not is_valid_decimal_odds("abc")
    assert not is_valid_decimal_odds(float("nan"))
    assert not is_valid_decimal_odds(float("inf"))
    print("  [OK] cotes impossibles rejetées (<=1, None, vide, NaN, infini) — §25")


# ---------------------------------------------------------------------------
# §11 : probabilité implicite
# ---------------------------------------------------------------------------

def test_implied_probability_is_inverse_of_odds():
    assert math.isclose(implied_probability(2.0), 0.5)
    assert math.isclose(implied_probability(4.0), 0.25)
    print("  [OK] implied_probability(odds) == 1/odds")


def test_implied_probability_raises_on_invalid_odds():
    try:
        implied_probability(0.5)
        assert False, "aurait dû lever ValueError"
    except ValueError:
        pass
    print("  [OK] implied_probability lève ValueError sur une cote invalide (jamais une valeur fabriquée)")


# ---------------------------------------------------------------------------
# §12/§13 : overround et retrait de marge
# ---------------------------------------------------------------------------

def test_overround_positive_for_realistic_bookmaker_odds():
    # cotes 1X2 réalistes, marge bookmaker positive typique (~5%)
    raw = [implied_probability(v) for v in (2.1, 3.4, 3.6)]
    ov = overround(raw)
    assert ov > 0, f"overround devrait être positif (marge bookmaker), obtenu {ov}"
    print(f"  [OK] overround > 0 pour des cotes réalistes (overround={ov:.4f})")


def test_normalize_margin_sums_to_one():
    raw = [implied_probability(v) for v in (2.1, 3.4, 3.6)]
    norm = normalize_margin(raw)
    assert math.isclose(sum(norm), 1.0, abs_tol=1e-9)
    print(f"  [OK] normalize_margin produit des probabilités sommant à 1 ({norm})")


def test_normalize_margin_preserves_relative_order():
    raw = [implied_probability(v) for v in (2.1, 3.4, 3.6)]
    norm = normalize_margin(raw)
    assert norm[0] > norm[1] > norm[2], "l'ordre relatif des probabilités doit être préservé par une normalisation proportionnelle"
    print("  [OK] normalize_margin préserve l'ordre relatif des probabilités (méthode proportionnelle, §13)")


def test_compute_1x2_odds_features_none_on_any_invalid_odds():
    assert compute_1x2_odds_features(2.1, 3.4, None) is None
    assert compute_1x2_odds_features(2.1, 3.4, 0.9) is None
    assert compute_1x2_odds_features(2.1, 3.4, 3.6) is not None
    print("  [OK] compute_1x2_odds_features retourne None si UNE SEULE cote sur 3 est invalide (§24 : jamais imputé)")


def test_compute_ou25_odds_features_basic():
    out = compute_ou25_odds_features(1.9, 1.95)
    assert out is not None
    assert math.isclose(out["norm_over"] + out["norm_under"], 1.0, abs_tol=1e-9)
    print("  [OK] compute_ou25_odds_features calcule des probabilités O/U 2.5 normalisées sommant à 1")


# ---------------------------------------------------------------------------
# §26-§28 : mapping ligue / équipe
# ---------------------------------------------------------------------------

def test_div_to_league_covers_five_xfoot_leagues():
    assert set(DIV_TO_LEAGUE.values()) == {"PremierLeague", "LaLiga", "Bundesliga", "SerieA", "Ligue1"}
    assert LEAGUE_TO_DIV == {v: k for k, v in DIV_TO_LEAGUE.items()}
    print("  [OK] DIV_TO_LEAGUE couvre exactement les 5 ligues Xfoot en base locale")


def test_map_league_unknown_div_returns_none():
    assert map_league("XX") is None
    assert map_league("E0") == "PremierLeague"
    print("  [OK] map_league retourne None pour un code division inconnu — jamais une supposition")


def test_normalize_team_name_trivial_only():
    assert normalize_team_name("  Man   City ") == "Man City"
    assert normalize_team_name("Nott'm Forest") == "Nott'm Forest"
    print("  [OK] normalize_team_name ne fait qu'une normalisation triviale espace (jamais de fuzzy)")


def test_match_key_deterministic_and_exact():
    k1 = match_key("PremierLeague", date(2023, 8, 11), "Burnley", "Man City")
    k2 = match_key("PremierLeague", date(2023, 8, 11), " Burnley ", "Man  City")
    assert k1 == k2, "la clé doit être identique après normalisation triviale"
    k3 = match_key("PremierLeague", date(2023, 8, 11), "Burnley FC", "Man City")
    assert k1 != k3, "un nom réellement différent (\"Burnley FC\" vs \"Burnley\") ne doit PAS être fusionné silencieusement (§26 : jamais de fuzzy)"
    print("  [OK] match_key est déterministe (espaces ignorés) mais jamais fuzzy (aucune tolérance orthographique)")


# ---------------------------------------------------------------------------
# §5/§6/§29 : qualité du timestamp
# ---------------------------------------------------------------------------

def test_classify_source_timestamp_quality_never_safe():
    assert classify_source_timestamp_quality(PRE_CLOSING_SOURCE) == "CAUTION"
    assert classify_source_timestamp_quality(CLOSING_SOURCE) == "CAUTION"
    assert classify_source_timestamp_quality("unknown_source") == "UNKNOWN"
    print("  [OK] pre_closing/closing sont classés CAUTION (jamais SAFE — football-data.co.uk n'a pas de timestamp mesuré par ligne)")


def test_validate_explicit_timestamp_safe_before_kickoff():
    kickoff = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    before = datetime(2023, 12, 30, 12, 0, tzinfo=timezone.utc)
    assert validate_explicit_timestamp(before, kickoff) == "SAFE"
    print("  [OK] un timestamp explicite strictement antérieur au coup d'envoi est SAFE")


def test_validate_explicit_timestamp_leakage_at_or_after_kickoff():
    kickoff = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    at_kickoff = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    after = datetime(2024, 1, 1, 16, 0, tzinfo=timezone.utc)
    assert validate_explicit_timestamp(at_kickoff, kickoff) == "LEAKAGE_RISK"
    assert validate_explicit_timestamp(after, kickoff) == "LEAKAGE_RISK"
    print("  [OK] un timestamp explicite au moment du coup d'envoi ou après est LEAKAGE_RISK (jamais SAFE — §67 test de fuite)")


def test_validate_explicit_timestamp_rejected_when_missing():
    kickoff = datetime(2024, 1, 1, 15, 0, tzinfo=timezone.utc)
    assert validate_explicit_timestamp(None, kickoff) == "REJECTED"
    print("  [OK] un timestamp manquant est REJECTED (jamais fabriqué)")


# ---------------------------------------------------------------------------
# §25 : contrôle qualité d'une ligne brute
# ---------------------------------------------------------------------------

def test_check_row_quality_missing_date():
    assert check_row_quality({"Date": "", "HomeTeam": "Arsenal", "AwayTeam": "Chelsea"}) == "missing_date"
    print("  [OK] une ligne sans date est rejetée (missing_date)")


def test_check_row_quality_missing_teams():
    assert check_row_quality({"Date": "11/08/2023", "HomeTeam": "", "AwayTeam": "Chelsea"}) == "missing_teams"
    assert check_row_quality({"Date": "11/08/2023", "HomeTeam": "Arsenal", "AwayTeam": None}) == "missing_teams"
    print("  [OK] une ligne sans équipe domicile/extérieur est rejetée (missing_teams)")


def test_check_row_quality_valid_row_passes():
    assert check_row_quality({"Date": "11/08/2023", "HomeTeam": "Arsenal", "AwayTeam": "Chelsea"}) is None
    print("  [OK] une ligne structurellement valide ne remonte aucune raison de rejet")


# ---------------------------------------------------------------------------
# Reproductibilité (§32)
# ---------------------------------------------------------------------------

def test_reproducibility_same_input_same_output():
    a = compute_1x2_odds_features(2.1, 3.4, 3.6)
    b = compute_1x2_odds_features(2.1, 3.4, 3.6)
    assert a == b
    print("  [OK] compute_1x2_odds_features est déterministe (même entrée -> même sortie)")


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
