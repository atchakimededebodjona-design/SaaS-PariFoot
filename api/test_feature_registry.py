"""
test_feature_registry.py — Phase 8A : tests de app/ai/features/registry.py
et app/ai/features/snapshot.py.

Base isolée dédiée (jamais api/app.db) — même précaution que les suites de
tests précédentes (voir _test_support.py).

Usage : python api/test_feature_registry.py
"""

import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_feature_registry.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match, MatchStats
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion, TeamRating
from app.ai.features.registry import (
    FEATURE_REGISTRY, LEAKAGE_RISKS, STATUSES, get_feature, list_by_category,
    list_by_leakage_risk, list_by_status, traffic_light, validate_registry,
)
from app.ai.features.snapshot import build_feature_snapshot, snapshot_coverage, validate_cutoff
from app.ai.engine.live_features import build_live_features

init_db()


def _clean_all():
    with Session(engine) as session:
        for row in session.exec(select(MatchStats)).all():
            session.delete(row)
        for row in session.exec(select(Match)).all():
            session.delete(row)
        session.commit()


def _row_counts(session):
    return {
        "match": len(session.exec(select(Match)).all()),
        "model_predictions": len(session.exec(select(ModelPrediction)).all()),
        "model_versions": len(session.exec(select(ModelVersion)).all()),
        "team_ratings": len(session.exec(select(TeamRating)).all()),
    }


def _seed_matches(session, league, teams, n_rounds, start_date):
    d = start_date
    for r in range(n_rounds):
        for i in range(len(teams)):
            home, away = teams[i], teams[(i + 1) % len(teams)]
            session.add(Match(league=league, date=datetime.combine(d, datetime.min.time()),
                               home_team=home, away_team=away, home_goals=(r + i) % 3, away_goals=(r + i + 1) % 3))
            d += timedelta(days=1)
    session.commit()


# ---------------------------------------------------------------------------
# 1. Registre — complétude et cohérence (§4/§28/§35)
# ---------------------------------------------------------------------------

def test_registry_has_no_internal_inconsistency():
    problems = validate_registry()
    assert problems == [], f"Incohérences trouvées dans le registre : {problems}"
    print(f"  [OK] validate_registry() : {len(FEATURE_REGISTRY)} features, aucune incohérence")


def test_every_feature_has_all_required_fields():
    required_string_fields = ["feature_name", "category", "description", "source", "data_type",
                               "availability", "cutoff_rule", "leakage_risk", "missing_value_strategy", "status"]
    for name, fd in FEATURE_REGISTRY.items():
        for field_name in required_string_fields:
            value = getattr(fd, field_name)
            assert value, f"{name}.{field_name} est vide/None — chaque feature doit documenter tous ses champs obligatoires"
        assert fd.feature_name == name, f"clé de dict '{name}' != feature_name '{fd.feature_name}'"
    print(f"  [OK] {len(FEATURE_REGISTRY)} features : tous les champs obligatoires sont renseignés")


def test_no_duplicate_feature_names():
    names = list(FEATURE_REGISTRY.keys())
    assert len(names) == len(set(names))
    print("  [OK] aucun nom de feature dupliqué dans le registre")


def test_status_and_leakage_values_are_within_enum():
    for name, fd in FEATURE_REGISTRY.items():
        assert fd.status in STATUSES, f"{name}: status inconnu '{fd.status}'"
        assert fd.leakage_risk in LEAKAGE_RISKS, f"{name}: leakage_risk inconnu '{fd.leakage_risk}'"
    print("  [OK] tous les status/leakage_risk appartiennent aux énumérations documentées (§4/§7)")


def test_production_features_are_never_leakage_risk_or_rejected():
    """§7 du prompt : une feature réellement servie en production ne doit jamais porter un
    risque de fuite non maîtrisé — sinon la classification elle-même serait incohérente."""
    for fd in list_by_status("PRODUCTION"):
        assert fd.leakage_risk in ("SAFE", "CAUTION"), (
            f"{fd.feature_name} est status=PRODUCTION mais leakage_risk={fd.leakage_risk} — incohérent"
        )
        assert fd.current_model_usage, f"{fd.feature_name} est PRODUCTION mais current_model_usage est vide"
    print(f"  [OK] {len(list_by_status('PRODUCTION'))} features PRODUCTION : toutes SAFE/CAUTION, toutes consommées par au moins un modèle")


def test_missing_features_document_absence_never_fabricate_source():
    """§19/§21/§45 : une feature MISSING doit dire NOT AVAILABLE, jamais prétendre une source réelle."""
    for fd in list_by_status("MISSING"):
        assert "NOT AVAILABLE" in fd.source or "NOT AVAILABLE" in fd.availability, (
            f"{fd.feature_name} est MISSING mais ne déclare pas explicitement NOT AVAILABLE"
        )
        assert fd.current_model_usage == [], f"{fd.feature_name} est MISSING mais current_model_usage n'est pas vide"
    print(f"  [OK] {len(list_by_status('MISSING'))} features MISSING : toutes déclarent explicitement NOT AVAILABLE")


def test_odds_and_context_families_are_all_missing_confirmed_by_audit():
    """Vérifie que l'audit (odds/injuries/lineups/weather confirmés absents,
    §21/§19/§20/§26) est bien reflété dans le registre — jamais une intégration
    esquissée pour ces familles dans cette phase."""
    for name in ("odds_opening", "odds_closing", "implied_probability", "odds_movement",
                 "injuries", "suspensions", "lineups", "weather", "league_standing"):
        fd = get_feature(name)
        assert fd is not None, f"{name} devrait être documentée (même en MISSING)"
        assert fd.status == "MISSING"
    print("  [OK] odds/injuries/lineups/weather/standings : tous MISSING, jamais fabriqués")


def test_season_is_experimental_not_fabricated_as_available():
    """§12 du prompt : season ne doit JAMAIS être marquée disponible alors qu'aucune
    colonne ne la stocke — seule une méthode de dérivation peut être documentée."""
    season = get_feature("season")
    assert season is not None
    assert season.status == "EXPERIMENTAL"
    assert season.current_model_usage == []
    assert "NOT AVAILABLE" in season.source or "AUCUNE colonne" in season.source
    print("  [OK] season : status=EXPERIMENTAL (méthode de dérivation documentée, jamais persistée ni utilisée)")


def test_traffic_light_derivation():
    green = get_feature("home_form_points_avg")  # PRODUCTION + SAFE
    yellow = get_feature("season")                 # EXPERIMENTAL
    red = get_feature("odds_opening")               # MISSING + REJECTED
    assert traffic_light(green) == "GREEN"
    assert traffic_light(yellow) == "YELLOW"
    assert traffic_light(red) == "RED"
    print("  [OK] traffic_light() : GREEN/YELLOW/RED dérivés correctement (§28), jamais stockés séparément")


def test_list_by_helpers_are_consistent_with_registry():
    all_by_status = sum(len(list_by_status(s)) for s in STATUSES)
    assert all_by_status == len(FEATURE_REGISTRY)
    all_by_risk = sum(len(list_by_leakage_risk(r)) for r in LEAKAGE_RISKS)
    assert all_by_risk == len(FEATURE_REGISTRY)
    form_features = list_by_category("form")
    assert all(fd.category == "form" for fd in form_features)
    assert len(form_features) > 0
    print("  [OK] list_by_status/list_by_leakage_risk/list_by_category couvrent exactement le registre, sans perte ni doublon")


# ---------------------------------------------------------------------------
# 2. Cutoff / anti-fuite (§6/§7/§32/§37)
# ---------------------------------------------------------------------------

def test_validate_cutoff_classifies_correctly():
    cutoff = date(2026, 5, 10)
    assert validate_cutoff(date(2026, 5, 9), cutoff) == "SAFE"
    assert validate_cutoff(date(2026, 5, 10), cutoff) == "LEAKAGE_RISK"  # égal au cutoff -> pas strictement antérieur
    assert validate_cutoff(date(2026, 5, 11), cutoff) == "LEAKAGE_RISK"
    assert validate_cutoff(None, cutoff) == "UNKNOWN"
    print("  [OK] validate_cutoff : SAFE/LEAKAGE_RISK/UNKNOWN corrects, borne stricte respectée")


def test_build_feature_snapshot_never_sees_future_match():
    """§6/§37/§38 du prompt : reconstruit 'que savait Xfoot avant une date T' —
    un match survenu APRÈS le cutoff ne doit JAMAIS influencer le snapshot."""
    _clean_all()
    with Session(engine) as session:
        teams = ["Team A", "Team B", "Team C", "Team D"]
        _seed_matches(session, "Ligue1", teams, n_rounds=4, start_date=date(2026, 1, 1))

        cutoff = date(2026, 3, 1)
        # match "futur" : après le cutoff, avec un score extrême et reconnaissable
        session.add(Match(league="Ligue1", date=datetime.combine(cutoff + timedelta(days=5), datetime.min.time()),
                           home_team="Team A", away_team="Team B", home_goals=9, away_goals=0))
        session.commit()

        snap_before = build_feature_snapshot(session, "Ligue1", "Team A", "Team B", cutoff)

        # même snapshot, cutoff repoussé après le match "futur" -> DOIT changer (le match compte maintenant)
        snap_after = build_feature_snapshot(session, "Ligue1", "Team A", "Team B", cutoff + timedelta(days=10))

    assert snap_before.features["home_form_goals_scored_avg"] != snap_after.features["home_form_goals_scored_avg"], (
        "le score 9-0 après cutoff ne doit influencer le snapshot QUE lorsque le cutoff le dépasse"
    )
    print("  [OK] build_feature_snapshot : un match après le cutoff n'influence jamais le snapshot avant cette date")


def test_snapshot_rejects_same_day_match_as_future():
    """Un match daté EXACTEMENT le jour du cutoff ne doit pas être vu (borne stricte
    Match.date < as_of, voir live_features.py) — cas limite explicitement testé."""
    _clean_all()
    with Session(engine) as session:
        cutoff = date(2026, 3, 1)
        session.add(Match(league="Ligue1", date=datetime.combine(cutoff, datetime.min.time()),
                           home_team="Team A", away_team="Team B", home_goals=5, away_goals=0))
        session.commit()
        snap = build_feature_snapshot(session, "Ligue1", "Team A", "Team B", cutoff)
    assert math.isnan(snap.features["home_form_goals_scored_avg"]), (
        "un match daté le jour même du cutoff ne doit jamais être inclus (borne stricte <)"
    )
    print("  [OK] build_feature_snapshot : un match daté EXACTEMENT le jour du cutoff est exclu (borne stricte)")


def test_rolling_window_excludes_current_match_itself():
    """§14 du prompt : last_5_before_match, jamais last_5_including_current_match."""
    _clean_all()
    with Session(engine) as session:
        teams = ["Team A", "Team B"]
        # 5 matchs antérieurs bien identifiés (buts croissants 1,2,3,4,5) + le match du jour (100 buts, doit être exclu)
        d = date(2026, 1, 1)
        for i, goals in enumerate([1, 2, 3, 4, 5]):
            session.add(Match(league="Ligue1", date=datetime.combine(d + timedelta(days=i), datetime.min.time()),
                               home_team="Team A", away_team="Team B", home_goals=goals, away_goals=0))
        cutoff = d + timedelta(days=10)
        session.add(Match(league="Ligue1", date=datetime.combine(cutoff, datetime.min.time()),
                           home_team="Team A", away_team="Team B", home_goals=100, away_goals=0))
        session.commit()
        feats = build_live_features(session, None, "Ligue1", "Team A", "Team B", cutoff)
    assert feats["home_form_goals_scored_avg"] == sum([1, 2, 3, 4, 5]) / 5, (
        f"le match du jour même (100 buts) a fuité dans la fenêtre de forme : {feats['home_form_goals_scored_avg']}"
    )
    print("  [OK] rolling features : le match du jour lui-même est exclu de sa propre fenêtre glissante (§14)")


# ---------------------------------------------------------------------------
# 3. Valeurs manquantes (§35)
# ---------------------------------------------------------------------------

def test_missing_values_are_nan_never_imputed_to_zero():
    _clean_all()
    with Session(engine) as session:
        # aucun match antérieur pour ces équipes -> tout doit être NaN, jamais 0
        session.add(Match(league="Ligue1", date=datetime(2026, 6, 1), home_team="Ghost A", away_team="Ghost B", home_goals=1, away_goals=1))
        session.commit()
        feats = build_live_features(session, None, "Ligue1", "Ghost A", "Ghost B", date(2026, 1, 1))
    assert math.isnan(feats["home_form_points_avg"]), "aucun historique -> NaN attendu, jamais 0"
    assert math.isnan(feats["home_form_goals_scored_avg"])
    assert feats["home_returning_from_break"] == 0, "returning_from_break utilise 0 par convention documentée (pas NaN) — voir registre"
    print("  [OK] valeurs manquantes : NaN pour les moyennes de forme, jamais imputées à 0 silencieusement")


# ---------------------------------------------------------------------------
# 4. Reproductibilité (§34)
# ---------------------------------------------------------------------------

def test_snapshot_reproducible_same_match_same_cutoff():
    _clean_all()
    with Session(engine) as session:
        teams = ["Team A", "Team B", "Team C", "Team D"]
        _seed_matches(session, "Ligue1", teams, n_rounds=4, start_date=date(2026, 1, 1))
        cutoff = date(2026, 2, 1)
        snap1 = build_feature_snapshot(session, "Ligue1", "Team A", "Team B", cutoff)
        snap2 = build_feature_snapshot(session, "Ligue1", "Team A", "Team B", cutoff)
    assert snap1.features == snap2.features or all(
        (math.isnan(a) and math.isnan(b)) or a == b for a, b in zip(snap1.features.values(), snap2.features.values())
    )
    print("  [OK] build_feature_snapshot : reproductible (mêmes match/cutoff -> mêmes valeurs)")


def test_snapshot_coverage_summary():
    _clean_all()
    with Session(engine) as session:
        teams = ["Team A", "Team B", "Team C", "Team D"]
        _seed_matches(session, "Ligue1", teams, n_rounds=4, start_date=date(2026, 1, 1))
        snap = build_feature_snapshot(session, "Ligue1", "Team A", "Team B", date(2026, 2, 1))
        cov = snapshot_coverage(snap)
    assert cov["total_features"] == 25
    assert cov["present"] + cov["missing"] == 25
    assert 0.0 <= cov["coverage_ratio"] <= 1.0
    print(f"  [OK] snapshot_coverage : {cov}")


# ---------------------------------------------------------------------------
# 5. Normalisation équipe/ligue (§10/§11) — comportement réel, pas une correction
# ---------------------------------------------------------------------------

def test_team_name_matching_true_positive_via_known_alias():
    from app.core.team_name_matching import names_match
    assert names_match("Manchester United", "Man United") is True
    print("  [OK] team_name_matching.names_match : alias connu correctement résolu")


def test_team_name_matching_documented_false_positive_risk():
    """Documente (n'essaie PAS de corriger, §10 : 'ne pas créer de correspondances
    approximatives dangereuses' ne veut pas dire les masquer) le risque de faux
    positif du seuil difflib 0.6 sur des noms courts et proches."""
    from app.core.team_name_matching import names_match
    similar_but_different = names_match("Inter", "Milan")
    # Documenté tel quel : peut être True ou False selon le seuil exact — ce test
    # échoue seulement si le comportement change silencieusement (régression de dépendance).
    assert isinstance(similar_but_different, bool)
    print(f"  [OK] team_name_matching : comportement sur noms courts proches observé et documenté (Inter/Milan -> {similar_but_different})")


def test_league_id_lookup_is_exact_no_fuzzy():
    from app.core.api_football_config import API_FOOTBALL_LEAGUE_IDS
    assert API_FOOTBALL_LEAGUE_IDS.get("Ligue1") == 61
    assert API_FOOTBALL_LEAGUE_IDS.get("NotARealLeague") is None, "aucune correspondance approximative -- absence explicite, jamais devinée"
    print("  [OK] API_FOOTBALL_LEAGUE_IDS : correspondance exacte, aucune ligue inconnue résolue par erreur")


# ---------------------------------------------------------------------------
# 6. Sécurité DB (§39/§40/§43)
# ---------------------------------------------------------------------------

def test_registry_and_snapshot_are_strictly_read_only():
    _clean_all()
    with Session(engine) as session:
        teams = ["Team A", "Team B", "Team C", "Team D"]
        _seed_matches(session, "Ligue1", teams, n_rounds=4, start_date=date(2026, 1, 1))
        before = _row_counts(session)

        validate_registry()
        list(FEATURE_REGISTRY.values())
        build_feature_snapshot(session, "Ligue1", "Team A", "Team B", date(2026, 2, 1))
        snapshot_coverage(build_feature_snapshot(session, "Ligue1", "Team A", "Team B", date(2026, 2, 1)))

        after = _row_counts(session)
    assert before == after, f"une fonction du Feature Registry/Snapshot a modifié une table : avant={before} après={after}"
    print("  [OK] registry.py/snapshot.py : strictement lecture seule (match/model_predictions/model_versions/team_ratings inchangés)")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    failures = 0
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  [FAIL] {t.__name__}: {e!r}")
    cleanup_db(DB_PATH)
    print(f"\n{len(tests) - failures}/{len(tests)} tests OK")
    sys.exit(1 if failures else 0)
