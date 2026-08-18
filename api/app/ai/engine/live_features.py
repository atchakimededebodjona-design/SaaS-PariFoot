"""
api/app/ai/engine/live_features.py — Phase 8, Partie A/B (§6-§7-§8 du ticket).

Construit, POUR UN SEUL MATCH pas encore joué (LIVE), exactement les mêmes
25 features que api/app/ai/engine/features.py::build_ml_features_from_db
(EXISTING_FEATURE_COLUMNS + NEW_FEATURE_COLUMNS) — mêmes définitions
(FORM_WINDOW=5, H2H_WINDOW=5, SHOT_STATS_WINDOW=10), mais calculées AS OF
une date donnée via des requêtes ciblées sur les deux équipes concernées,
plutôt qu'en rejouant tout l'historique de la ligue ligne par ligne
(build_ml_features_from_db prend ~35 min sur la base de dev — voir
test_ml_stacking.py — jamais rejouable à chaque requête HTTP).

=== Preuve d'équivalence, pas une simple affirmation ===

api/test_live_features.py rejoue ce builder "as of" la date d'un VRAI match
historique déjà résolu et compare, colonne par colonne, son résultat à celui
que produit RÉELLEMENT build_ml_features_from_db pour cette même ligne — la
même donnée, deux chemins de calcul, jamais une divergence tolérée.

=== Écart assumé et documenté n°1 : days_since_last_match n'est PAS plafonné ===

Le pipeline CSV historique (build_features.py::main()) applique
`.clip(upper=BREAK_THRESHOLD_DAYS)` à home/away_days_since_last_match APRÈS
le calcul du flag returning_from_break — mais api/app/ai/engine/features.py
::build_ml_features_from_db (le pipeline RÉELLEMENT utilisé par
scripts/train_ml_stacking_from_db.py, celui dont XGBoost/LightGBM
apprennent) N'APPELLE JAMAIS ce clip (vérifié en lisant le code, pas
supposé) : les valeurs brutes (non plafonnées) sont celles vues à
l'entraînement. Ce module reproduit donc le comportement RÉEL du pipeline
DB (non plafonné), pas celui du pipeline CSV — sinon les features LIVE
divergeraient silencieusement de ce que le modèle a appris.

=== Écart assumé et documenté n°2 : dc_* ne réentraîne PAS Dixon-Coles ===

build_dixon_coles_features (build_features.py) réentraîne Dixon-Coles à
CHAQUE date de match distincte de l'historique (~3450 fits sur 5 ligues) —
correct pour construire un jeu d'ENTRAÎNEMENT sans fuite, mais bien trop
coûteux pour une requête LIVE. Pour un match d'AUJOURD'HUI, on réutilise
l'artefact de PRODUCTION déjà chargé (LEAGUE_MODELS, voir main.py) : lui
aussi est entraîné exclusivement sur l'historique disponible AVANT sa propre
date d'entraînement, jamais sur le match à prédire — même garantie
anti-fuite que le fit walk-forward, à la fraîcheur du dernier
ré-entraînement près (voir refresh_and_retrain.py). Approximation assumée
et documentée dans le rapport Phase 8, jamais cachée.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional, Protocol

from sqlmodel import Session, select

from app.models.match import Match, MatchStats

FORM_WINDOW = 5
H2H_WINDOW = 5
SHOT_STATS_WINDOW = 10
# Largement suffisant pour tout streak réel en football (un streak de 30
# matchs consécutifs identiques serait un record mondial) — évite de
# récupérer tout l'historique d'une équipe juste pour un streak signé qui,
# par construction, ne dépend que du SUFFIXE le plus récent de résultats
# identiques (voir _current_streak).
STREAK_LOOKBACK = 30


class DixonColesLeagueModel(Protocol):
    """Contrat minimal attendu de l'artefact de production (LeagueModel dans
    main.py) — duck-typé, jamais un import direct de main.py (import
    circulaire, même raison que models_common.py)."""

    def predict_1x2(self, home_team: str, away_team: str) -> dict: ...
    def predict_over_under(self, home_team: str, away_team: str, line: float = 2.5) -> dict: ...


def _points(gf: int, ga: int) -> int:
    if gf > ga:
        return 3
    if gf == ga:
        return 1
    return 0


def _team_matches_before(session: Session, league: str, team: str, as_of: datetime, limit: Optional[int] = None) -> list[Match]:
    stmt = (
        select(Match)
        .where(Match.league == league, Match.date < as_of)
        .where((Match.home_team == team) | (Match.away_team == team))
        .order_by(Match.date.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return session.exec(stmt).all()


def _h2h_matches_before(session: Session, league: str, team_a: str, team_b: str, as_of: datetime, limit: int) -> list[Match]:
    stmt = (
        select(Match)
        .where(Match.league == league, Match.date < as_of)
        .where(
            ((Match.home_team == team_a) & (Match.away_team == team_b))
            | ((Match.home_team == team_b) & (Match.away_team == team_a))
        )
        .order_by(Match.date.desc())
        .limit(limit)
    )
    return session.exec(stmt).all()


def _form_features(matches_desc: list[Match], team: str, prefix: str) -> dict[str, float]:
    """`matches_desc` : les FORM_WINDOW matchs les plus récents de `team`
    (peu importe domicile/extérieur), déjà triés du plus récent au plus
    ancien — même fenêtre que history[team][-FORM_WINDOW:] dans
    build_form_and_h2h_features (l'ordre interne n'affecte pas une moyenne)."""
    if not matches_desc:
        return {
            f"{prefix}_form_points_avg": float("nan"),
            f"{prefix}_form_goals_scored_avg": float("nan"),
            f"{prefix}_form_goals_conceded_avg": float("nan"),
        }
    pts, gf_list, ga_list = [], [], []
    for m in matches_desc:
        gf, ga = (m.home_goals, m.away_goals) if m.home_team == team else (m.away_goals, m.home_goals)
        pts.append(_points(gf, ga))
        gf_list.append(gf)
        ga_list.append(ga)
    return {
        f"{prefix}_form_points_avg": sum(pts) / len(pts),
        f"{prefix}_form_goals_scored_avg": sum(gf_list) / len(gf_list),
        f"{prefix}_form_goals_conceded_avg": sum(ga_list) / len(ga_list),
    }


def _rest_features(last_match: list[Match], as_of_date: date, prefix: str) -> dict[str, float]:
    """`last_match` : le SEUL match le plus récent de l'équipe (liste de 0
    ou 1 élément) — days_since_last_match n'est JAMAIS plafonné ici, voir
    docstring module (écart assumé n°1)."""
    if not last_match:
        return {f"{prefix}_days_since_last_match": float("nan"), f"{prefix}_returning_from_break": 0}
    days_raw = (as_of_date - last_match[0].date.date()).days
    return {
        f"{prefix}_days_since_last_match": float(days_raw),
        f"{prefix}_returning_from_break": int(days_raw > 90),
    }


def _h2h_features(h2h_matches_desc: list[Match], today_home_team: str) -> dict[str, float]:
    if not h2h_matches_desc:
        return {"h2h_matches_found": 0, "h2h_home_win_rate": float("nan")}
    wins_for_today_home, draws = 0, 0
    for m in h2h_matches_desc:
        if m.home_goals == m.away_goals:
            draws += 1
        else:
            winner = m.home_team if m.home_goals > m.away_goals else m.away_team
            if winner == today_home_team:
                wins_for_today_home += 1
    n = len(h2h_matches_desc)
    return {
        "h2h_matches_found": n,
        "h2h_home_win_rate": (wins_for_today_home + 0.5 * draws) / n,
    }


def _current_streak(matches_desc: list[Match], team: str) -> float:
    """Reproduit exactement l'état incrémental de build_streak_features (le
    streak après le dernier match connu) sans rejouer tout l'historique :
    par construction, ce streak ne dépend que du plus long SUFFIXE de
    résultats identiques se terminant au match le plus récent — on compte
    donc simplement ce suffixe plutôt que de simuler match après match."""
    if not matches_desc:
        return float("nan")

    def outcome(m: Match) -> int:
        gf, ga = (m.home_goals, m.away_goals) if m.home_team == team else (m.away_goals, m.home_goals)
        return 1 if gf > ga else (-1 if gf < ga else 0)

    first = outcome(matches_desc[0])
    if first == 0:
        return 0.0
    count = 0
    for m in matches_desc:
        if outcome(m) == first:
            count += 1
        else:
            break
    return float(count if first > 0 else -count)


def _shot_stats_features(session: Session, league: str, team: str, as_of: datetime, prefix: str) -> dict[str, float]:
    """SHOT_STATS_WINDOW derniers matchs de `team` AVEC match_stats
    réellement renseignées (les deux camps) — un match sans stats est exclu
    de l'historique, jamais compté comme 0 (même règle que
    build_shot_stats_features)."""
    stmt = (
        select(Match, MatchStats)
        .join(MatchStats, MatchStats.match_id == Match.id)
        .where(Match.league == league, Match.date < as_of)
        .where((Match.home_team == team) | (Match.away_team == team))
        .where(MatchStats.home_shots.is_not(None), MatchStats.away_shots.is_not(None))
        .order_by(Match.date.desc())
        .limit(SHOT_STATS_WINDOW)
    )
    rows = session.exec(stmt).all()
    if not rows:
        return {
            f"{prefix}_shots_diff_avg": float("nan"),
            f"{prefix}_shots_target_diff_avg": float("nan"),
            f"{prefix}_corners_diff_avg": float("nan"),
        }

    shots_for, shots_against = [], []
    st_for, st_against = [], []
    c_for, c_against = [], []
    for m, s in rows:
        if m.home_team == team:
            shots_for.append(s.home_shots); shots_against.append(s.away_shots)
            st_for.append(s.home_shots_target); st_against.append(s.away_shots_target)
            c_for.append(s.home_corners); c_against.append(s.away_corners)
        else:
            shots_for.append(s.away_shots); shots_against.append(s.home_shots)
            st_for.append(s.away_shots_target); st_against.append(s.home_shots_target)
            c_for.append(s.away_corners); c_against.append(s.home_corners)

    return {
        f"{prefix}_shots_diff_avg": (sum(shots_for) / len(shots_for)) - (sum(shots_against) / len(shots_against)),
        f"{prefix}_shots_target_diff_avg": (sum(st_for) / len(st_for)) - (sum(st_against) / len(st_against)),
        f"{prefix}_corners_diff_avg": (sum(c_for) / len(c_for)) - (sum(c_against) / len(c_against)),
    }


def _dixon_coles_features(league_model: Optional[DixonColesLeagueModel], home_team: str, away_team: str) -> dict[str, float]:
    if league_model is None:
        return {k: float("nan") for k in ("dc_home_win", "dc_draw", "dc_away_win", "dc_over_2_5", "dc_under_2_5")}
    try:
        probs_1x2 = league_model.predict_1x2(home_team, away_team)
        probs_ou = league_model.predict_over_under(home_team, away_team, line=2.5)
    except KeyError:
        # Équipe jamais vue par l'artefact de production (ex. promue très
        # récemment) — NaN, même comportement que le rodage Dixon-Coles
        # walk-forward côté entraînement (jamais fabriqué).
        return {k: float("nan") for k in ("dc_home_win", "dc_draw", "dc_away_win", "dc_over_2_5", "dc_under_2_5")}
    return {
        "dc_home_win": probs_1x2["home_win"], "dc_draw": probs_1x2["draw"], "dc_away_win": probs_1x2["away_win"],
        "dc_over_2_5": probs_ou["over"], "dc_under_2_5": probs_ou["under"],
    }


def build_live_features(
    session: Session,
    league_model: Optional[DixonColesLeagueModel],
    league: str,
    home_team: str,
    away_team: str,
    as_of_date: date,
) -> dict[str, float]:
    """
    Point d'entrée unique : retourne un dict des 25 colonnes de
    FEATURE_COLUMNS (voir features.py), AS OF `as_of_date` — n'utilise QUE
    des matchs strictement antérieurs à cette date (§7 du ticket, testé
    explicitement dans test_live_features.py). Un `float("nan")` est un
    résultat honnête (période de rodage, équipe jamais vue) — jamais une
    valeur fabriquée pour combler.
    """
    as_of = datetime.combine(as_of_date, time.min)

    home_recent = _team_matches_before(session, league, home_team, as_of, limit=FORM_WINDOW)
    away_recent = _team_matches_before(session, league, away_team, as_of, limit=FORM_WINDOW)
    home_last = _team_matches_before(session, league, home_team, as_of, limit=1)
    away_last = _team_matches_before(session, league, away_team, as_of, limit=1)
    home_streak_history = _team_matches_before(session, league, home_team, as_of, limit=STREAK_LOOKBACK)
    away_streak_history = _team_matches_before(session, league, away_team, as_of, limit=STREAK_LOOKBACK)
    h2h = _h2h_matches_before(session, league, home_team, away_team, as_of, limit=H2H_WINDOW)

    features: dict[str, float] = {}
    features.update(_form_features(home_recent, home_team, "home"))
    features.update(_form_features(away_recent, away_team, "away"))
    features.update(_rest_features(home_last, as_of_date, "home"))
    features.update(_rest_features(away_last, as_of_date, "away"))
    features.update(_h2h_features(h2h, home_team))
    features.update(_dixon_coles_features(league_model, home_team, away_team))
    features.update(_shot_stats_features(session, league, home_team, as_of, "home"))
    features.update(_shot_stats_features(session, league, away_team, as_of, "away"))
    features["home_current_streak"] = _current_streak(home_streak_history, home_team)
    features["away_current_streak"] = _current_streak(away_streak_history, away_team)
    return features
