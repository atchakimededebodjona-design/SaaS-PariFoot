"""
Pipeline de features ML depuis la base — Phase 4 du plan Xfoot AI.

Source : tables match/match_stats (Phase 2), jamais data/all_leagues_raw_
with_stats.csv — contrairement à build_features.py qui reste le pipeline
CSV historique, inchangé.

Réutilise TELLES QUELLES (import racine du dépôt, même pattern sys.path que
app.ai.engine.dixon_coles) les fonctions déjà validées de build_features.py :
build_form_and_h2h_features (forme 5 matchs, repos, head-to-head) et
build_dixon_coles_features (probabilités Dixon-Coles en walk-forward) — ce
sont les 17 features déjà utilisées dans les deux tentatives XGBoost
précédentes (voir train_xgboost_stacking.py / train_xgboost_stacking_
multileague.py), aucune réimplémentation ici.

Ce module ajoute UNIQUEMENT les features qui n'ont jamais été exploitées
dans aucune tentative passée :

  - Moyennes glissantes de tirs/tirs cadrés/corners (FOR - AGAINST), sur une
    fenêtre PLUS LONGUE (SHOT_STATS_WINDOW=10) que celle utilisée pour la
    forme (FORM_WINDOW=5 dans build_features.py). Ces colonnes existent dans
    match_stats depuis la Phase 2 mais ont toujours été explicitement
    exclues de FEATURE_COLUMNS dans les tentatives précédentes — à raison,
    puisque la valeur BRUTE du match courant est connue seulement après le
    coup de sifflet (fuite). Une moyenne glissante sur les matchs PASSÉS
    d'une équipe n'a pas ce problème, exactement comme home_form_goals_
    scored_avg ne l'a pas pour les buts — mais cette transformation n'avait
    jamais été appliquée aux stats de tirs/corners.
  - Séries de résultats en cours (streak signé), qui capturent l'ORDRE des
    résultats récents plutôt que leur seule moyenne (home_form_points_avg
    existant écrase cette information : 3 victoires-2 défaites et
    2 défaites-3 victoires ont la même moyenne mais une dynamique opposée).

Fenêtre de 10 fixée une fois pour toutes, jamais re-choisie contre le test
set d'évaluation (scripts/train_ml_stacking_from_db.py) : la re-choisir en
la comparant au même test set de 300 matchs invaliderait le bootstrap
apparié (comparaisons multiples, sur-ajustement au bruit d'un petit test).
"""

import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
from sqlmodel import Session, select

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from build_features import build_form_and_h2h_features, build_dixon_coles_features  # noqa: E402

from app.core.database import engine  # noqa: E402
from app.models.match import Match, MatchStats  # noqa: E402

SHOT_STATS_WINDOW = 10  # délibérément > FORM_WINDOW (5, dans build_features.py)

# Les 17 features déjà utilisées dans les deux tentatives XGBoost passées
# (voir leur FEATURE_COLUMNS) — inchangées ici, pour repère.
EXISTING_FEATURE_COLUMNS = [
    "home_form_points_avg", "home_form_goals_scored_avg", "home_form_goals_conceded_avg",
    "away_form_points_avg", "away_form_goals_scored_avg", "away_form_goals_conceded_avg",
    "home_days_since_last_match", "away_days_since_last_match",
    "home_returning_from_break", "away_returning_from_break",
    "h2h_matches_found", "h2h_home_win_rate",
    "dc_home_win", "dc_draw", "dc_away_win", "dc_over_2_5", "dc_under_2_5",
]

# Les 8 features réellement nouvelles introduites par ce ticket.
NEW_FEATURE_COLUMNS = [
    "home_shots_diff_avg", "away_shots_diff_avg",
    "home_shots_target_diff_avg", "away_shots_target_diff_avg",
    "home_corners_diff_avg", "away_corners_diff_avg",
    "home_current_streak", "away_current_streak",
]

FEATURE_COLUMNS = EXISTING_FEATURE_COLUMNS + NEW_FEATURE_COLUMNS
CATEGORICAL_COLUMNS = ["league"]

# Colonnes de match_stats jamais utilisées comme feature d'entrée dans
# aucune tentative passée (train_xgboost_stacking.py /
# train_xgboost_stacking_multileague.py les listaient explicitement dans
# EXCLUDED_RESULT_COLUMNS, en valeur brute du match courant). Ce ticket les
# exploite pour la première fois, mais uniquement via des moyennes
# glissantes sur les matchs PASSÉS de chaque équipe (build_shot_stats_
# features ci-dessous) — jamais la valeur brute du match courant.
PREVIOUSLY_UNUSED_MATCH_STATS_COLUMNS = [
    "home_shots", "away_shots",
    "home_shots_target", "away_shots_target",
    "home_corners", "away_corners",
]


def load_matches_with_stats_from_db(league: str | None = None) -> pd.DataFrame:
    """
    Comme app.ai.engine.dixon_coles.load_matches_from_db, mais jointe à
    match_stats. Fonction séparée plutôt que modification de celle-ci : son
    docstring garantit explicitement à Dixon-Coles/Elo de ne jamais joindre
    match_stats, contrat gardé intact.
    """
    with Session(engine) as session:
        stmt = select(Match, MatchStats).join(MatchStats, MatchStats.match_id == Match.id, isouter=True)
        if league is not None:
            stmt = stmt.where(Match.league == league)
        rows = session.exec(stmt).all()

    return pd.DataFrame([{
        "date": m.date, "home_team": m.home_team, "away_team": m.away_team,
        "home_goals": m.home_goals, "away_goals": m.away_goals, "league": m.league,
        "home_shots": s.home_shots if s else None, "away_shots": s.away_shots if s else None,
        "home_shots_target": s.home_shots_target if s else None,
        "away_shots_target": s.away_shots_target if s else None,
        "home_corners": s.home_corners if s else None, "away_corners": s.away_corners if s else None,
    } for m, s in rows])


def build_shot_stats_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Moyennes glissantes FOR - AGAINST (tirs, tirs cadrés, corners) sur les
    SHOT_STATS_WINDOW derniers matchs d'une équipe DANS CETTE LIGUE (le `df`
    passé doit déjà être filtré sur une seule ligue et trié par date, même
    contrat que build_form_and_h2h_features).

    Historique séparé de celui de build_form_and_h2h_features (qui, lui,
    utilise home_goals/away_goals, jamais NULL) : un match sans match_stats
    (nullable en base, ~1/12459 lignes) est simplement absent de cet
    historique-ci plutôt que compté comme 0 — NaN structurel, pas une fuite.
    """
    history = defaultdict(list)  # team -> [{"shots_for", "shots_against", ...}, ...]
    rows = []

    for row in df.itertuples():
        home, away = row.home_team, row.away_team
        home_hist = history[home][-SHOT_STATS_WINDOW:]
        away_hist = history[away][-SHOT_STATS_WINDOW:]

        def diff_avg(hist, for_key, against_key):
            if not hist:
                return np.nan
            return float(np.mean([h[for_key] for h in hist])) - float(np.mean([h[against_key] for h in hist]))

        rows.append({
            "home_shots_diff_avg": diff_avg(home_hist, "shots_for", "shots_against"),
            "away_shots_diff_avg": diff_avg(away_hist, "shots_for", "shots_against"),
            "home_shots_target_diff_avg": diff_avg(home_hist, "shots_target_for", "shots_target_against"),
            "away_shots_target_diff_avg": diff_avg(away_hist, "shots_target_for", "shots_target_against"),
            "home_corners_diff_avg": diff_avg(home_hist, "corners_for", "corners_against"),
            "away_corners_diff_avg": diff_avg(away_hist, "corners_for", "corners_against"),
        })

        # -- mise à jour APRÈS lecture des features de cette ligne — jamais avant --
        if pd.notna(row.home_shots) and pd.notna(row.away_shots):
            history[home].append({
                "shots_for": row.home_shots, "shots_against": row.away_shots,
                "shots_target_for": row.home_shots_target, "shots_target_against": row.away_shots_target,
                "corners_for": row.home_corners, "corners_against": row.away_corners,
            })
            history[away].append({
                "shots_for": row.away_shots, "shots_against": row.home_shots,
                "shots_target_for": row.away_shots_target, "shots_target_against": row.home_shots_target,
                "corners_for": row.away_corners, "corners_against": row.home_corners,
            })
        # sinon : match sans stats, ignoré des deux historiques (ni compté ni fabriqué à 0)

    return pd.DataFrame(rows, index=df.index)


def build_streak_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Série de résultats en cours (streak signé) par équipe DANS CETTE LIGUE :
    +N = N victoires consécutives jusqu'au dernier match inclus, -N = N
    défaites consécutives, 0 juste après un nul. NaN si l'équipe n'a encore
    aucun match antérieur dans cette ligue.

    Mise à jour incrémentale O(1) par équipe (même style que last_match_date
    dans build_form_and_h2h_features) plutôt qu'un rebalayage de l'historique
    à chaque match — capture l'ORDRE des résultats, ce que home_form_points_
    avg (une moyenne) efface : 3V-2D et 2D-3V ont la même moyenne de points
    mais une dynamique de forme opposée.
    """
    streak = {}  # team -> int (état APRÈS le dernier match connu)
    rows = []

    for row in df.itertuples():
        home, away = row.home_team, row.away_team

        rows.append({
            "home_current_streak": streak.get(home, np.nan),
            "away_current_streak": streak.get(away, np.nan),
        })

        home_prev = streak.get(home, 0)
        away_prev = streak.get(away, 0)
        if row.home_goals > row.away_goals:
            streak[home] = home_prev + 1 if home_prev > 0 else 1
            streak[away] = away_prev - 1 if away_prev < 0 else -1
        elif row.home_goals < row.away_goals:
            streak[home] = home_prev - 1 if home_prev < 0 else -1
            streak[away] = away_prev + 1 if away_prev > 0 else 1
        else:
            streak[home] = 0
            streak[away] = 0

    return pd.DataFrame(rows, index=df.index)


def build_ml_features_from_db(leagues: list[str] | None = None) -> pd.DataFrame:
    """
    Orchestrateur multi-ligues : assemble les 17 features existantes
    (réutilisées de build_features.py) + les 8 nouvelles de ce module,
    isolation stricte par ligue (même principe que build_features.py — une
    équipe ne joue jamais dans deux championnats la même saison, mélanger
    ferait fuiter des statistiques d'un championnat vers un autre).

    Retourne un DataFrame trié par date globale, une ligne par match, avec
    toutes les colonnes brutes de match/match_stats + les 25 features
    pré-match (17 existantes + 8 nouvelles) + `league`.
    """
    df = load_matches_with_stats_from_db()
    if leagues is not None:
        df = df[df["league"].isin(leagues)]
    df = df.sort_values("date", kind="stable").reset_index(drop=True)

    existing_parts, shot_parts, streak_parts = [], [], []
    for _league, sub in df.groupby("league", sort=False):
        sub_sorted = sub.sort_values("date", kind="stable")
        existing_parts.append(build_form_and_h2h_features(sub_sorted))
        existing_parts.append(build_dixon_coles_features(sub_sorted))
        shot_parts.append(build_shot_stats_features(sub_sorted))
        streak_parts.append(build_streak_features(sub_sorted))

    # build_form_and_h2h_features et build_dixon_coles_features retournent
    # chacune un DataFrame séparé par ligue -> concat par paires, puis les
    # deux moitiés (form/h2h, dixon-coles) entre elles.
    form_h2h = pd.concat(existing_parts[0::2]).loc[df.index]
    dc = pd.concat(existing_parts[1::2]).loc[df.index]
    shots = pd.concat(shot_parts).loc[df.index]
    streaks = pd.concat(streak_parts).loc[df.index]

    return pd.concat([df, form_h2h, dc, shots, streaks], axis=1)
