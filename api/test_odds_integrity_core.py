"""
test_odds_integrity_core.py — Phase 8E : tests de
app/ai/odds_research/integrity.py (fonctions pures). Aucun accès réseau/DB.

Couvre §41/§42 (tests adversariaux de fuite) et §26 (timezone/DST).

Usage : python api/test_odds_integrity_core.py
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.ai.odds_research.integrity import (
    classify_explicit_timestamp, classify_observation, audit_source_label,
    safe_consensus, combine_date_time, TEMPORAL_CLASSES, OBSERVATION_CLASSES,
)

UTC = timezone.utc
KICKOFF = datetime(2024, 3, 16, 15, 0, tzinfo=UTC)  # samedi 15h00 UTC
CUTOFF_T6H = KICKOFF - timedelta(hours=6)  # T-6h


# ---------------------------------------------------------------------------
# §4 : cas limites de la hiérarchie temporelle
# ---------------------------------------------------------------------------

def test_odds_before_cutoff_is_safe():
    t_odds = CUTOFF_T6H - timedelta(hours=1)
    assert classify_explicit_timestamp(t_odds, CUTOFF_T6H, KICKOFF) == "SAFE"
    print("  [OK] T_ODDS < T_CUTOFF -> SAFE")


def test_odds_equal_cutoff_is_safe_by_convention():
    assert classify_explicit_timestamp(CUTOFF_T6H, CUTOFF_T6H, KICKOFF) == "SAFE"
    print("  [OK] T_ODDS == T_CUTOFF -> SAFE (convention documentée, §4)")


def test_odds_between_cutoff_and_kickoff_is_future_information():
    t_odds = CUTOFF_T6H + timedelta(hours=1)
    assert t_odds < KICKOFF
    assert classify_explicit_timestamp(t_odds, CUTOFF_T6H, KICKOFF) == "FUTURE_INFORMATION"
    print("  [OK] T_CUTOFF < T_ODDS < T_MATCH -> FUTURE_INFORMATION")


def test_odds_at_or_after_kickoff_is_rejected():
    assert classify_explicit_timestamp(KICKOFF, CUTOFF_T6H, KICKOFF) == "REJECTED"
    assert classify_explicit_timestamp(KICKOFF + timedelta(minutes=10), CUTOFF_T6H, KICKOFF) == "REJECTED"
    print("  [OK] T_ODDS >= T_MATCH -> REJECTED (y compris égalité)")


def test_odds_null_is_rejected():
    assert classify_explicit_timestamp(None, CUTOFF_T6H, KICKOFF) == "REJECTED"
    print("  [OK] T_ODDS = NULL -> REJECTED")


def test_unknown_kickoff_still_compares_to_cutoff():
    t_odds = CUTOFF_T6H - timedelta(hours=1)
    assert classify_explicit_timestamp(t_odds, CUTOFF_T6H, None) == "SAFE"
    print("  [OK] T_MATCH inconnu (None) n'empêche pas la comparaison à T_CUTOFF")


# ---------------------------------------------------------------------------
# §41 : tests adversariaux de fuite (fixtures EXACTES du prompt)
# ---------------------------------------------------------------------------

def test_leakage_adversarial_fixtures_at_cutoff_t6h():
    fixtures = {
        "T-10h": (KICKOFF - timedelta(hours=10), "SAFE"),
        "T-5h": (KICKOFF - timedelta(hours=5), "FUTURE_INFORMATION"),
        "T-1h": (KICKOFF - timedelta(hours=1), "FUTURE_INFORMATION"),
        "T+10min": (KICKOFF + timedelta(minutes=10), "REJECTED"),
        "null": (None, "REJECTED"),
    }
    for label, (t_odds, expected) in fixtures.items():
        got = classify_explicit_timestamp(t_odds, CUTOFF_T6H, KICKOFF)
        assert got == expected, f"{label}: attendu {expected}, obtenu {got}"
    print("  [OK] toutes les fixtures adversariales §41 (T-10h/T-5h/T-1h/T+10min/null) classées correctement pour un cutoff T-6h")


# ---------------------------------------------------------------------------
# §8 : classification à 5 voies
# ---------------------------------------------------------------------------

def test_classify_observation_rejected_invalid_odds():
    assert classify_observation(is_valid_odds=False, has_measured_timestamp=True, t_odds=CUTOFF_T6H, t_cutoff=CUTOFF_T6H, t_match=KICKOFF) == "REJECTED"
    print("  [OK] une cote invalide est REJECTED, quel que soit le timestamp")


def test_classify_observation_historical_untimestamped_is_the_football_data_co_uk_case():
    result = classify_observation(is_valid_odds=True, has_measured_timestamp=False)
    assert result == "HISTORICAL_BUT_UNTIMESTAMPED"
    print("  [OK] cote valide sans timestamp mesuré -> HISTORICAL_BUT_UNTIMESTAMPED (cas football-data.co.uk, §7/§51)")


def test_classify_observation_temporally_verified_requires_measured_timestamp_and_safe():
    t_odds = CUTOFF_T6H - timedelta(hours=1)
    result = classify_observation(is_valid_odds=True, has_measured_timestamp=True, t_odds=t_odds, t_cutoff=CUTOFF_T6H, t_match=KICKOFF)
    assert result == "TEMPORALLY_VERIFIED"
    print("  [OK] timestamp mesuré + antérieur au cutoff -> TEMPORALLY_VERIFIED")


def test_classify_observation_timestamped_but_after_cutoff():
    t_odds = CUTOFF_T6H + timedelta(hours=1)
    result = classify_observation(is_valid_odds=True, has_measured_timestamp=True, t_odds=t_odds, t_cutoff=CUTOFF_T6H, t_match=KICKOFF)
    assert result == "TIMESTAMPED_BUT_AFTER_CUTOFF"
    print("  [OK] timestamp mesuré mais postérieur au cutoff -> TIMESTAMPED_BUT_AFTER_CUTOFF")


def test_classify_observation_unknown_when_no_cutoff_provided():
    result = classify_observation(is_valid_odds=True, has_measured_timestamp=True, t_odds=CUTOFF_T6H, t_cutoff=None, t_match=KICKOFF)
    assert result == "UNKNOWN"
    print("  [OK] timestamp mesuré mais aucun cutoff fourni -> UNKNOWN (jamais une supposition)")


def test_all_five_classes_are_reachable_and_documented():
    assert set(OBSERVATION_CLASSES) == {
        "TEMPORALLY_VERIFIED", "HISTORICAL_BUT_UNTIMESTAMPED",
        "TIMESTAMPED_BUT_AFTER_CUTOFF", "UNKNOWN", "REJECTED",
    }
    print("  [OK] les 5 classes du §8 sont exactement celles définies dans OBSERVATION_CLASSES")


# ---------------------------------------------------------------------------
# §15/§16 : audit opening/closing
# ---------------------------------------------------------------------------

def test_closing_label_without_timestamp_stays_untimestamped():
    assert audit_source_label("closing", has_measured_timestamp=False) == "HISTORICAL_BUT_UNTIMESTAMPED"
    assert audit_source_label("opening", has_measured_timestamp=False) == "HISTORICAL_BUT_UNTIMESTAMPED"
    print("  [OK] une valeur étiquetée opening/closing SANS timestamp mesuré reste HISTORICAL_BUT_UNTIMESTAMPED (§16 : jamais promue sur la seule étiquette)")


# ---------------------------------------------------------------------------
# §17-§20, §42 : consensus SAFE — fixture EXACTE du prompt
# ---------------------------------------------------------------------------

def test_safe_consensus_adversarial_fixture_from_prompt():
    # A = T-10h, B = T-8h, C = T-2h ; cutoff = T-6h -> inclure A+B, exclure C.
    obs = [
        {"bookmaker": "A", "timestamp": KICKOFF - timedelta(hours=10), "implied_probs": {"home": 0.5, "draw": 0.3, "away": 0.2}},
        {"bookmaker": "B", "timestamp": KICKOFF - timedelta(hours=8), "implied_probs": {"home": 0.52, "draw": 0.28, "away": 0.2}},
        {"bookmaker": "C", "timestamp": KICKOFF - timedelta(hours=2), "implied_probs": {"home": 0.6, "draw": 0.25, "away": 0.15}},
    ]
    consensus = safe_consensus(obs, CUTOFF_T6H)
    assert consensus["bookmaker_count"] == 2
    assert consensus["bookmakers"] == ["A", "B"]
    assert consensus["excluded_bookmakers"] == ["C"]
    print(f"  [OK] safe_consensus(A=T-10h,B=T-8h,C=T-2h, cutoff=T-6h) inclut exactement A+B, exclut C : {consensus['bookmakers']}")


def test_safe_consensus_none_when_no_bookmaker_qualifies():
    obs = [{"bookmaker": "C", "timestamp": KICKOFF - timedelta(hours=2), "implied_probs": {"home": 0.5, "draw": 0.3, "away": 0.2}}]
    assert safe_consensus(obs, CUTOFF_T6H) is None
    print("  [OK] safe_consensus retourne None si aucun bookmaker ne qualifie (jamais un consensus fabriqué à partir de zéro observation)")


def test_safe_consensus_never_imputes_missing_bookmaker():
    obs = [
        {"bookmaker": "A", "timestamp": KICKOFF - timedelta(hours=10), "implied_probs": {"home": 0.5, "draw": 0.3, "away": 0.2}},
    ]
    consensus = safe_consensus(obs, CUTOFF_T6H)
    assert consensus["bookmaker_count"] == 1
    print("  [OK] safe_consensus documente bookmaker_count exact, jamais une imputation (§20)")


def test_safe_consensus_untimestamped_observation_never_included():
    obs = [
        {"bookmaker": "A", "timestamp": None, "implied_probs": {"home": 0.5, "draw": 0.3, "away": 0.2}},
        {"bookmaker": "B", "timestamp": KICKOFF - timedelta(hours=10), "implied_probs": {"home": 0.5, "draw": 0.3, "away": 0.2}},
    ]
    consensus = safe_consensus(obs, CUTOFF_T6H)
    assert consensus["bookmakers"] == ["B"]
    print("  [OK] une observation sans timestamp mesuré n'est jamais incluse dans le consensus SAFE, même si sa cote est valide")


# ---------------------------------------------------------------------------
# §25/§26 : timezone / DST
# ---------------------------------------------------------------------------

def test_naive_and_aware_datetime_comparison_fails_safely():
    """§25 : une comparaison temporelle ne doit JAMAIS mélanger naïf et aware
    silencieusement — Python lève TypeError nativement, ce comportement est
    volontairement laissé se propager (jamais capturé pour produire une
    fausse réponse)."""
    naive = datetime(2024, 3, 16, 9, 0)
    aware = KICKOFF
    try:
        classify_explicit_timestamp(naive, aware, aware)
        assert False, "aurait dû lever TypeError (comparaison naïf/aware)"
    except TypeError:
        pass
    print("  [OK] comparer un datetime naïf à un datetime aware lève TypeError (échec sûr, jamais une comparaison silencieuse erronée)")


def test_cet_cest_dst_transition_boundary():
    """Vérifie qu'un cutoff calculé across une transition d'heure d'été (CET
    +1 -> CEST +2, dernier dimanche de mars) reste cohérent quand les deux
    datetimes sont correctement construits en UTC (jamais un offset fixe
    supposé)."""
    from datetime import timezone as tz
    CET = tz(timedelta(hours=1))
    CEST = tz(timedelta(hours=2))
    # Coup d'envoi le 31 mars 2024 à 15:00 CEST (juste après le passage à l'heure d'été)
    kickoff_cest = datetime(2024, 3, 31, 15, 0, tzinfo=CEST)
    # Cotes observées le 29 mars 2024 à 10:00 CET (avant le changement d'heure)
    odds_cet = datetime(2024, 3, 29, 10, 0, tzinfo=CET)
    cutoff = kickoff_cest - timedelta(hours=24)
    result = classify_explicit_timestamp(odds_cet, cutoff, kickoff_cest)
    # En UTC : odds = 2024-03-29 09:00 UTC ; kickoff = 2024-03-31 13:00 UTC ; cutoff = 2024-03-30 13:00 UTC
    assert odds_cet.astimezone(tz.utc) < cutoff.astimezone(tz.utc)
    assert result == "SAFE"
    print("  [OK] la comparaison reste correcte à travers le changement d'heure CET->CEST (datetimes aware, jamais un offset fixe supposé)")


def test_midnight_crossover():
    kickoff = datetime(2024, 1, 2, 0, 30, tzinfo=UTC)  # coup d'envoi juste après minuit
    cutoff = kickoff - timedelta(hours=6)  # 2024-01-01 18:30 UTC
    odds_before_midnight = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    assert classify_explicit_timestamp(odds_before_midnight, cutoff, kickoff) == "SAFE"
    print("  [OK] un cutoff franchissant minuit (jour calendaire différent) est géré correctement")


# ---------------------------------------------------------------------------
# §24 : reconstruction Date+Time football-data.co.uk (kickoff, pas les odds)
# ---------------------------------------------------------------------------

def test_combine_date_time_basic():
    dt = combine_date_time("11/08/2023", "20:00")
    assert dt == datetime(2023, 8, 11, 20, 0)
    print("  [OK] combine_date_time reconstruit correctement Date+Time (11/08/2023, 20:00)")


def test_combine_date_time_short_year_format():
    dt = combine_date_time("11/08/23", "20:00")
    assert dt == datetime(2023, 8, 11, 20, 0)
    print("  [OK] combine_date_time gère le format d'année à 2 chiffres (anciennes saisons)")


def test_combine_date_time_missing_time_returns_date_only():
    dt = combine_date_time("11/08/2023", None)
    assert dt == datetime(2023, 8, 11, 0, 0)
    print("  [OK] Time absent -> combine_date_time retourne la date seule (minuit), jamais une heure inventée")


def test_combine_date_time_malformed_date_returns_none():
    assert combine_date_time("not-a-date", "20:00") is None
    print("  [OK] une date malformée retourne None (jamais une valeur fabriquée)")


# ---------------------------------------------------------------------------
# Reproductibilité (§30)
# ---------------------------------------------------------------------------

def test_reproducibility_same_input_same_classification():
    a = classify_observation(is_valid_odds=True, has_measured_timestamp=False)
    b = classify_observation(is_valid_odds=True, has_measured_timestamp=False)
    assert a == b
    assert set(TEMPORAL_CLASSES) == {"SAFE", "FUTURE_INFORMATION", "REJECTED", "UNKNOWN"}
    print("  [OK] classify_observation est déterministe (même entrée -> même sortie)")


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
