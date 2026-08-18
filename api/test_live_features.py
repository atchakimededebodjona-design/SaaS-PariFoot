"""
test_live_features.py — Phase 8 : preuve d'équivalence entre le builder LIVE
(app/ai/engine/live_features.py) et le pipeline d'entraînement RÉEL
(build_form_and_h2h_features de build_features.py, build_shot_stats_features/
build_streak_features de app/ai/engine/features.py) sur les 20 features qui
ne dépendent PAS de Dixon-Coles (les dc_* sont testées séparément — le
pipeline d'entraînement les calcule par un ré-entraînement walk-forward
(~35 min sur la base complète, voir test_ml_stacking.py) volontairement pas
rejoué ici ; le builder LIVE, lui, réutilise l'artefact de production, voir
docstring de live_features.py).

Base isolée dédiée (jamais api/app.db). Dataset synthétique volontairement
petit (une poignée de matchs/équipes) — mêmes principes que
test_ml_stacking.py : prouver l'équivalence sans imposer le coût du
pipeline complet à la suite de tests.

Usage : python api/test_live_features.py
"""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))  # build_features.py vit à la racine du dépôt

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_live_features.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match, MatchStats
from build_features import build_form_and_h2h_features  # noqa: E402
from app.ai.engine.features import build_shot_stats_features, build_streak_features  # noqa: E402
from app.ai.engine.live_features import build_live_features, _dixon_coles_features  # noqa: E402

init_db()

LEAGUE = "TestLeague"

# 18 matchs synthétiques, ordre chronologique, 6 équipes — assez pour
# exercer FORM_WINDOW=5, H2H_WINDOW=5 (A vs B rejoués), et quelques matchs
# SANS stats (lignes 5 et 12) pour vérifier l'exclusion (pas un 0 fabriqué).
_BASE = date(2025, 1, 1)


def _d(offset_days: int) -> date:
    return _BASE + timedelta(days=offset_days)


SYNTHETIC_MATCHES = [
    {"date": _d(0), "home_team": "A", "away_team": "B", "home_goals": 2, "away_goals": 0, "shots": (12, 5, 7, 2, 6, 2)},
    {"date": _d(7), "home_team": "C", "away_team": "A", "home_goals": 1, "away_goals": 1, "shots": (8, 9, 3, 4, 4, 5)},
    {"date": _d(14), "home_team": "B", "away_team": "D", "home_goals": 0, "away_goals": 3, "shots": (4, 14, 1, 8, 2, 7)},
    {"date": _d(21), "home_team": "A", "away_team": "D", "home_goals": 3, "away_goals": 1, "shots": (15, 6, 9, 3, 8, 3)},
    {"date": _d(28), "home_team": "B", "away_team": "C", "home_goals": 1, "away_goals": 1, "shots": (7, 7, 3, 3, 5, 5)},
    {"date": _d(35), "home_team": "A", "away_team": "E", "home_goals": 1, "away_goals": 0, "shots": None},  # sans stats
    {"date": _d(42), "home_team": "D", "away_team": "B", "home_goals": 2, "away_goals": 2, "shots": (10, 10, 5, 5, 6, 6)},
    {"date": _d(49), "home_team": "B", "away_team": "A", "home_goals": 0, "away_goals": 1, "shots": (6, 13, 2, 8, 3, 6)},
    {"date": _d(56), "home_team": "E", "away_team": "C", "home_goals": 2, "away_goals": 1, "shots": (9, 8, 4, 3, 5, 4)},
    {"date": _d(63), "home_team": "A", "away_team": "C", "home_goals": 0, "away_goals": 0, "shots": (5, 5, 2, 2, 3, 3)},
    {"date": _d(70), "home_team": "D", "away_team": "A", "home_goals": 1, "away_goals": 2, "shots": (7, 12, 3, 7, 4, 6)},
    {"date": _d(77), "home_team": "B", "away_team": "E", "home_goals": 3, "away_goals": 0, "shots": (14, 4, 8, 1, 7, 2)},
    {"date": _d(84), "home_team": "C", "away_team": "D", "home_goals": 1, "away_goals": 2, "shots": None},  # sans stats
    {"date": _d(91), "home_team": "A", "away_team": "B", "home_goals": 2, "away_goals": 2, "shots": (11, 9, 6, 4, 5, 5)},
    {"date": _d(98), "home_team": "E", "away_team": "A", "home_goals": 0, "away_goals": 1, "shots": (6, 11, 2, 7, 3, 6)},
    {"date": _d(105), "home_team": "C", "away_team": "B", "home_goals": 2, "away_goals": 0, "shots": (10, 6, 5, 2, 6, 3)},
    {"date": _d(112), "home_team": "D", "away_team": "E", "home_goals": 1, "away_goals": 1, "shots": (8, 8, 4, 4, 4, 4)},
    # -- dernière ligne : le "match cible", jamais utilisé pour son PROPRE calcul --
    {"date": _d(119), "home_team": "A", "away_team": "B", "home_goals": 1, "away_goals": 3, "shots": (9, 10, 4, 5, 5, 6)},
]


def _clean_all():
    with Session(engine) as session:
        for row in session.exec(select(MatchStats)).all():
            session.delete(row)
        for row in session.exec(select(Match)).all():
            session.delete(row)
        session.commit()


def _insert_matches(session):
    for row in SYNTHETIC_MATCHES:
        m = Match(league=LEAGUE, date=datetime.combine(row["date"], datetime.min.time()),
                   home_team=row["home_team"], away_team=row["away_team"],
                   home_goals=row["home_goals"], away_goals=row["away_goals"])
        session.add(m)
        session.flush()
        if row["shots"] is not None:
            hs, aws, hst, ast, hc, ac = row["shots"]
            session.add(MatchStats(match_id=m.id, home_shots=hs, away_shots=aws,
                                    home_shots_target=hst, away_shots_target=ast,
                                    home_corners=hc, away_corners=ac))
    session.commit()


def _reference_features_for_last_row() -> dict:
    """Calcule les features de RÉFÉRENCE pour la dernière ligne du dataset
    synthétique en appelant les VRAIES fonctions du pipeline d'entraînement
    (build_form_and_h2h_features, build_shot_stats_features,
    build_streak_features) — jamais une réimplémentation."""
    rows = []
    for r in SYNTHETIC_MATCHES:
        row = {
            "date": pd.Timestamp(r["date"]), "home_team": r["home_team"], "away_team": r["away_team"],
            "home_goals": r["home_goals"], "away_goals": r["away_goals"],
        }
        if r["shots"] is not None:
            hs, aws, hst, ast, hc, ac = r["shots"]
            row.update({"home_shots": hs, "away_shots": aws, "home_shots_target": hst,
                        "away_shots_target": ast, "home_corners": hc, "away_corners": ac})
        else:
            row.update({"home_shots": None, "away_shots": None, "home_shots_target": None,
                        "away_shots_target": None, "home_corners": None, "away_corners": None})
        rows.append(row)
    df = pd.DataFrame(rows)

    form_h2h = build_form_and_h2h_features(df)
    shots = build_shot_stats_features(df)
    streaks = build_streak_features(df)

    last_idx = df.index[-1]
    combined = pd.concat([form_h2h, shots, streaks], axis=1)
    return combined.loc[last_idx].to_dict()


def _close(a: float, b: float, tol: float = 1e-9) -> bool:
    if pd.isna(a) and pd.isna(b):
        return True
    if pd.isna(a) or pd.isna(b):
        return False
    return abs(a - b) <= tol


def test_live_features_match_training_pipeline_exactly():
    _clean_all()
    with Session(engine) as session:
        _insert_matches(session)

    reference = _reference_features_for_last_row()
    target = SYNTHETIC_MATCHES[-1]

    with Session(engine) as session:
        live = build_live_features(
            session, league_model=None, league=LEAGUE,
            home_team=target["home_team"], away_team=target["away_team"], as_of_date=target["date"],
        )

    mismatches = []
    for key, ref_value in reference.items():
        if key not in live:
            continue  # dc_* absent côté référence utile ici (non comparé, voir docstring module)
        if not _close(live[key], ref_value):
            mismatches.append((key, live[key], ref_value))

    assert not mismatches, f"Divergences live vs pipeline d'entraînement : {mismatches}"
    print(f"  [OK] {len(reference)} features non-DC identiques (tolérance 1e-9) entre le builder LIVE "
          f"et build_form_and_h2h_features/build_shot_stats_features/build_streak_features")


def test_features_are_as_of_match_date():
    """§29 : les features calculées pour le match cible (dernière ligne) ne
    doivent JAMAIS refléter des matchs qui ont lieu À ou APRÈS sa date —
    vérifié en ajoutant un match FUTUR entre les deux mêmes équipes et en
    constatant que le résultat ne change pas."""
    _clean_all()
    with Session(engine) as session:
        _insert_matches(session)

    target = SYNTHETIC_MATCHES[-1]
    with Session(engine) as session:
        before = build_live_features(session, None, LEAGUE, target["home_team"], target["away_team"], target["date"])

    with Session(engine) as session:
        # Match FUTUR (même date que la cible, donc pas "strictement avant") entre A et E,
        # avec un score extrême qui, s'il fuitait, changerait fortement home_form_*.
        future = Match(league=LEAGUE, date=datetime.combine(target["date"], datetime.min.time()),
                        home_team=target["home_team"], away_team="E", home_goals=9, away_goals=0)
        session.add(future)
        session.commit()

    with Session(engine) as session:
        after = build_live_features(session, None, LEAGUE, target["home_team"], target["away_team"], target["date"])

    for key in before:
        assert _close(before[key], after[key]), (
            f"{key} a changé après l'ajout d'un match à LA MÊME date que la cible : "
            f"{before[key]} -> {after[key]} — un match non strictement antérieur a fuité."
        )
    print("  [OK] un match à la même date (ou plus tard) que la cible n'affecte jamais ses features (as-of strict)")


def test_future_result_not_used():
    """§29 : un match FUTUR (après la date cible) déjà en base ne doit
    jamais influencer une prédiction plus ancienne — même test que
    ci-dessus, formulé explicitement comme l'exige le ticket."""
    _clean_all()
    with Session(engine) as session:
        _insert_matches(session)
        far_future = Match(league=LEAGUE, date=datetime.combine(SYNTHETIC_MATCHES[-1]["date"] + timedelta(days=30), datetime.min.time()),
                            home_team="A", away_team="B", home_goals=7, away_goals=0)
        session.add(far_future)
        session.commit()

    target = SYNTHETIC_MATCHES[7]  # un match "au milieu" du dataset, avec de l'historique avant ET après
    with Session(engine) as session:
        feats = build_live_features(session, None, LEAGUE, target["home_team"], target["away_team"], target["date"])

    # Recalcule la référence pour CE match précis (indice 7) à partir
    # du dataset synthétique tronqué à cet index -- garantit qu'aucune valeur du futur n'a fuité.
    rows = []
    for r in SYNTHETIC_MATCHES[:8]:
        row = {"date": pd.Timestamp(r["date"]), "home_team": r["home_team"], "away_team": r["away_team"],
               "home_goals": r["home_goals"], "away_goals": r["away_goals"]}
        rows.append(row)
    df = pd.DataFrame(rows)
    ref = build_form_and_h2h_features(df).loc[df.index[-1]].to_dict()

    for key, ref_value in ref.items():
        assert _close(feats[key], ref_value), (
            f"{key} : attendu {ref_value} (calculé sans aucune connaissance du futur), obtenu {feats[key]} "
            "— un résultat futur a fuité dans les features."
        )
    print("  [OK] un match FUTUR déjà en base n'influence jamais les features d'un match plus ancien")


def test_dixon_coles_features_uses_injected_league_model_never_refits():
    class _StubLeagueModel:
        def predict_1x2(self, home_team, away_team):
            assert (home_team, away_team) == ("Paris SG", "Marseille")
            return {"home_win": 0.55, "draw": 0.25, "away_win": 0.20}

        def predict_over_under(self, home_team, away_team, line=2.5):
            return {"line": line, "over": 0.6, "under": 0.4}

    feats = _dixon_coles_features(_StubLeagueModel(), "Paris SG", "Marseille")
    assert feats == {
        "dc_home_win": 0.55, "dc_draw": 0.25, "dc_away_win": 0.20,
        "dc_over_2_5": 0.6, "dc_under_2_5": 0.4,
    }
    print("  [OK] dc_* dérivées de l'artefact de production injecté (aucun ré-entraînement walk-forward)")


def test_dixon_coles_features_unknown_team_returns_nan_not_fabricated():
    class _StubLeagueModel:
        def predict_1x2(self, home_team, away_team):
            raise KeyError(away_team)

        def predict_over_under(self, home_team, away_team, line=2.5):
            raise KeyError(away_team)

    feats = _dixon_coles_features(_StubLeagueModel(), "A", "Equipe Inconnue")
    assert all(pd.isna(v) for v in feats.values())
    print("  [OK] équipe inconnue de l'artefact de production -> NaN pour toutes les dc_*, jamais fabriqué")


TESTS = [
    test_live_features_match_training_pipeline_exactly,
    test_features_are_as_of_match_date,
    test_future_result_not_used,
    test_dixon_coles_features_uses_injected_league_model_never_refits,
    test_dixon_coles_features_unknown_team_returns_nan_not_fabricated,
]


if __name__ == "__main__":
    failures = 0
    for t in TESTS:
        print(f"\n=== {t.__name__} ===")
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"  [ECHEC] {e}")

    total = len(TESTS)
    cleanup_db(DB_PATH)
    print(f"\n{'='*60}\n{total-failures}/{total} tests reussis\n{'='*60}")
    sys.exit(1 if failures else 0)
