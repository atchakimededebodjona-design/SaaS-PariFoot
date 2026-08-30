"""
research_features_v1.py — Phase 8B : XFOOT FEATURE ENGINEERING V1 (RECHERCHE).

Nouvelles familles de features CANDIDATES (jamais servies en production, jamais
injectées dans XGBoost/LightGBM de production) : Home/Away split, densité de
calendrier, force Dixon-Coles (attack/defense bruts, pas les probabilités
dérivées déjà en baseline), classement reconstruit, saison. Chaque fonction
respecte le même contrat que build_form_and_h2h_features/build_dixon_coles_features
(build_features.py, racine) et build_shot_stats_features/build_streak_features
(api/app/ai/engine/features.py) : `df` déjà filtré sur UNE seule ligue, trié
par date, features calculées ligne par ligne en lisant l'historique AVANT de
le mettre à jour avec la ligne courante — aucune fuite possible par construction.

Ne réimplémente aucun moteur existant : Elo (EloEngine.walk_forward, déjà
leak-free par construction) et Dixon-Coles (dixon_coles_fast.fast_fit, le
moteur de production vectorisé) sont réutilisés tels quels.

Ces features ne sont PAS ajoutées à api/app/ai/features/registry.py (Phase 8A,
production) : PHASE8B_FEATURE_REGISTRY ci-dessous est un registre SÉPARÉ,
additif, jamais fusionné dans le registre de production (aucune mutation d'un
état global partagé par d'autres modules).
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dixon_coles_fast import fast_fit  # noqa: E402

from app.ai.engine.elo import EloEngine  # noqa: E402
from app.ai.features.registry import FeatureDefinition  # noqa: E402

HOMEAWAY_WINDOW = 5
DC_STRENGTH_MIN_TRAIN_MATCHES = 100  # même seuil de rodage que build_dixon_coles_features
DC_STRENGTH_XI = 0.0018
DC_STRENGTH_L2 = 0.05
SEASON_START_MONTH_DAY = (8, 1)  # 1er août — approximation du coup d'envoi de saison pour les 5 championnats couverts
DC_STRENGTH_REFIT_EVERY = 7  # voir build_all_research_groups


# ---------------------------------------------------------------------------
# GROUP C — HOME/AWAY SPLIT
# ---------------------------------------------------------------------------

def build_homeaway_split_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Forme domicile-seule de l'équipe recevante / forme extérieur-seule de
    l'équipe visiteuse, sur les HOMEAWAY_WINDOW derniers matchs DANS CE
    CONTEXTE (jamais mélangé avec l'autre contexte, contrairement à
    home_form_points_avg existant qui mélange domicile+extérieur).
    """
    home_hist = defaultdict(list)  # team -> [{"points","gf","ga"}, ...] uniquement ses matchs À DOMICILE
    away_hist = defaultdict(list)  # team -> [{"points","gf","ga"}, ...] uniquement ses matchs À L'EXTÉRIEUR
    rows = []

    for row in df.itertuples():
        home, away = row.home_team, row.away_team
        h_hist = home_hist[home][-HOMEAWAY_WINDOW:]
        a_hist = away_hist[away][-HOMEAWAY_WINDOW:]

        def rate(hist):
            if not hist:
                return np.nan, np.nan, np.nan
            win_rate = float(np.mean([1.0 if h["points"] == 3 else (0.5 if h["points"] == 1 else 0.0) for h in hist]))
            gf = float(np.mean([h["gf"] for h in hist]))
            ga = float(np.mean([h["ga"] for h in hist]))
            return win_rate, gf, ga

        h_win, h_gf, h_ga = rate(h_hist)
        a_win, a_gf, a_ga = rate(a_hist)

        rows.append({
            "home_home_win_rate_last5": h_win,
            "home_home_goals_scored_avg_last5": h_gf,
            "home_home_goals_conceded_avg_last5": h_ga,
            "away_away_win_rate_last5": a_win,
            "away_away_goals_scored_avg_last5": a_gf,
            "away_away_goals_conceded_avg_last5": a_ga,
        })

        # -- mise à jour APRÈS lecture, jamais avant --
        home_pts = 3 if row.home_goals > row.away_goals else (1 if row.home_goals == row.away_goals else 0)
        away_pts = 3 if row.away_goals > row.home_goals else (1 if row.away_goals == row.home_goals else 0)
        home_hist[home].append({"points": home_pts, "gf": row.home_goals, "ga": row.away_goals})
        away_hist[away].append({"points": away_pts, "gf": row.away_goals, "ga": row.home_goals})

    return pd.DataFrame(rows, index=df.index)


# ---------------------------------------------------------------------------
# GROUP D — SCHEDULE DENSITY (matches_last_7_days / matches_last_14_days)
# ---------------------------------------------------------------------------

def build_schedule_density_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Nombre de matchs joués par chaque équipe dans les 7/14 jours PRÉCÉDANT le
    match courant (bornes strictement antérieures à `date`, aucun match futur
    ni le match courant lui-même compté). Complète home/away_days_since_last_match
    (déjà en production) qui ne capture que l'écart au DERNIER match, pas la
    densité de calendrier sur une fenêtre — deux congestions différentes
    (ex. 2 matchs espacés de 4 jours chacun vs 1 seul match il y a 4 jours ont
    le même days_since_last_match mais une densité très différente).
    """
    dates_hist = defaultdict(list)  # team -> [date, ...] triées (append en ordre chronologique du df)
    rows = []

    for row in df.itertuples():
        home, away, d = row.home_team, row.away_team, row.date

        def count_within(hist, days):
            cutoff = d - timedelta(days=days)
            return sum(1 for h in hist if cutoff <= h < d)

        rows.append({
            "home_matches_last_7_days": count_within(dates_hist[home], 7),
            "away_matches_last_7_days": count_within(dates_hist[away], 7),
            "home_matches_last_14_days": count_within(dates_hist[home], 14),
            "away_matches_last_14_days": count_within(dates_hist[away], 14),
        })

        dates_hist[home].append(d)
        dates_hist[away].append(d)

    return pd.DataFrame(rows, index=df.index)


# ---------------------------------------------------------------------------
# GROUP E — TEAM STRENGTH (Dixon-Coles attack/defense bruts + Elo)
# ---------------------------------------------------------------------------

def build_dixon_coles_strength_features(
    df: pd.DataFrame, refit_every: int = 1, min_train_matches: int = DC_STRENGTH_MIN_TRAIN_MATCHES,
) -> pd.DataFrame:
    """
    Écarts de force Dixon-Coles bruts (attack/defense/net, PAS les
    probabilités 1X2/O-U dérivées — celles-ci sont déjà des features de
    production, voir dc_home_win etc.), en walk-forward — même boucle que
    build_dixon_coles_features (build_features.py, racine), un seul fit
    (fast_fit, moteur de production vectorisé) par date de match DISTINCTE de
    cette ligue, entraîné STRICTEMENT sur les matchs antérieurs. NaN pendant
    le rodage (< min_train_matches) ou pour une équipe jamais vue à
    l'entraînement (promotion en cours de fenêtre).

    CAUTION documentée (voir PHASE8B_FEATURE_REGISTRY ci-dessous) : comme
    dc_home_win/... en production, ce n'est PAS une fuite (le fit est
    toujours strictement antérieur à `date`), mais reste une approximation
    "un fit par date" — pas un rating recalculé à la seconde près.
    """
    out = pd.DataFrame(
        index=df.index, columns=["dc_attack_diff", "dc_defense_diff", "dc_net_diff"], dtype=float,
    )

    dates = sorted(df["date"].unique())
    fit = None
    trained_teams: set = set()

    for i, d in enumerate(dates):
        idx = df.index[df["date"] == d]

        if i % refit_every == 0 or fit is None:
            train = df[df["date"] < d]
            if len(train) >= min_train_matches:
                trained_teams = set(train["home_team"]) | set(train["away_team"])
                fit = fast_fit(train, xi=DC_STRENGTH_XI, l2=DC_STRENGTH_L2, reference_date=d)
            else:
                fit = None

        if fit is None:
            continue

        for j in idx:
            home, away = df.at[j, "home_team"], df.at[j, "away_team"]
            if home not in trained_teams or away not in trained_teams:
                continue
            out.at[j, "dc_attack_diff"] = fit["attack"][home] - fit["attack"][away]
            out.at[j, "dc_defense_diff"] = fit["defense"][home] - fit["defense"][away]
            out.at[j, "dc_net_diff"] = fit["net"][home] - fit["net"][away]

    return out


def build_elo_strength_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Écart de rating Elo PRÉ-match (home + home_advantage - away), obtenu via
    EloEngine.walk_forward — déjà garanti leak-free par construction (le
    rating utilisé pour un match est toujours celui d'AVANT ce match, voir
    docstring de EloEngine.walk_forward). Hyperparamètres par défaut
    (k=20, home_advantage=60) — PAS le grid search par ligue de
    scripts/backtest_elo.py (limitation documentée : un même réglage
    uniforme sur les 5 ligues, plus simple à reproduire pour cette recherche).
    """
    walk = EloEngine().walk_forward(df.reset_index(drop=True))
    return pd.DataFrame({"elo_diff": walk["diff"].to_numpy()}, index=df.index)


# ---------------------------------------------------------------------------
# GROUP F — RANKING (classement reconstruit, jamais un classement final de saison)
# ---------------------------------------------------------------------------

def _season_of(d) -> int:
    """Même règle EXACTE que build_features.py::check_team_league_collisions
    (mois >= 7 -> saison de cette année, sinon année précédente) — réutilisée,
    jamais réinventée."""
    return d.year if d.month >= 7 else d.year - 1


def build_league_standing_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Classement reconstruit AVANT chaque match (jamais le classement final de
    la saison) : points cumulés / différence de buts / matchs joués par
    équipe, à l'intérieur de la saison (mois >= 7 -> saison en cours, sinon
    précédente — voir _season_of), remis à zéro à chaque nouvelle saison
    (une équipe ne conserve pas ses points d'une saison sur l'autre).
    Position = tri (points desc, diff de buts desc, buts marqués desc) parmi
    les seules équipes ayant déjà joué au moins un match cette saison AVANT
    la date courante. NaN si l'équipe n'a encore joué aucun match cette
    saison (position non définie, jamais fabriquée à une valeur arbitraire).
    """
    # season -> team -> {"points","gf","ga","played"}
    tables: dict[int, dict[str, dict]] = defaultdict(lambda: defaultdict(lambda: {"points": 0, "gf": 0, "ga": 0, "played": 0}))
    rows = []

    for row in df.itertuples():
        season = _season_of(row.date)
        table = tables[season]

        def position_and_ppg(team):
            if table[team]["played"] == 0:
                return np.nan, np.nan
            ranked = sorted(
                (t for t, s in table.items() if s["played"] > 0),
                key=lambda t: (-table[t]["points"], -(table[t]["gf"] - table[t]["ga"]), -table[t]["gf"]),
            )
            pos = ranked.index(team) + 1
            ppg = table[team]["points"] / table[team]["played"]
            return float(pos), float(ppg)

        home_pos, home_ppg = position_and_ppg(row.home_team)
        away_pos, away_ppg = position_and_ppg(row.away_team)

        rows.append({
            "home_standing_position": home_pos,
            "away_standing_position": away_pos,
            "home_points_per_game_season": home_ppg,
            "away_points_per_game_season": away_ppg,
        })

        # -- mise à jour APRÈS lecture --
        home_pts = 3 if row.home_goals > row.away_goals else (1 if row.home_goals == row.away_goals else 0)
        away_pts = 3 if row.away_goals > row.home_goals else (1 if row.away_goals == row.home_goals else 0)
        table[row.home_team]["points"] += home_pts
        table[row.home_team]["gf"] += row.home_goals
        table[row.home_team]["ga"] += row.away_goals
        table[row.home_team]["played"] += 1
        table[row.away_team]["points"] += away_pts
        table[row.away_team]["gf"] += row.away_goals
        table[row.away_team]["ga"] += row.home_goals
        table[row.away_team]["played"] += 1

    return pd.DataFrame(rows, index=df.index)


# ---------------------------------------------------------------------------
# GROUP G — SEASON (EXPERIMENTAL en Phase 8A, testée ici)
# ---------------------------------------------------------------------------

def validate_season_rule(df_all_leagues: pd.DataFrame) -> dict:
    """
    Teste la règle mois >= 7 CONTRE les compétitions réellement présentes
    (§14 du prompt Phase 8B) : pour chaque ligue, la répartition des mois de
    match doit être cohérente avec un calendrier août -> mai (championnats
    domestiques européens). Retourne un diagnostic PAR LIGUE, jamais une
    simple affirmation non vérifiée.
    """
    diag = {}
    for league, sub in df_all_leagues.groupby("league"):
        months = sub["date"].dt.month.value_counts().sort_index()
        summer_months = months.reindex([6, 7], fill_value=0).sum()
        total = months.sum()
        summer_fraction = float(summer_months / total) if total else None
        diag[league] = {
            "months_present": sorted(months.index.tolist()),
            "summer_break_fraction": round(summer_fraction, 4) if summer_fraction is not None else None,
            "reliable": bool(summer_fraction is not None and summer_fraction < 0.05),
        }
    return diag


def build_season_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    season_year (dérivé, mois >= 7) + season_progress_pct (0-100, jours
    écoulés depuis le 1er août de `season_year` / 300 jours, borné) — capture
    la dynamique début/milieu/fin de saison, plus exploitable pour un modèle
    à arbres qu'une simple étiquette d'année. Dérivée UNIQUEMENT de
    Match.date, jamais du résultat du match — SAFE par construction (aucune
    fenêtre glissante, aucun historique nécessaire).
    """
    rows = []
    for row in df.itertuples():
        season = _season_of(row.date)
        season_start = date(season, *SEASON_START_MONTH_DAY)
        progress_days = (row.date.date() - season_start).days
        progress_pct = max(0.0, min(100.0, progress_days / 300.0 * 100.0))
        rows.append({"season_year": season, "season_progress_pct": progress_pct})
    return pd.DataFrame(rows, index=df.index)


# ---------------------------------------------------------------------------
# Registre additif Phase 8B — jamais fusionné dans registry.FEATURE_REGISTRY
# (production, Phase 8A) : dictionnaire séparé, exposé pour le rapport.
# ---------------------------------------------------------------------------

def _phase8b_registry() -> dict[str, FeatureDefinition]:
    ml_source = (
        "api/app/ai/features/research_features_v1.py (Phase 8B, RECHERCHE UNIQUEMENT — "
        "jamais consommée par un modèle de production)."
    )
    cutoff_rule = "Match.date < cutoff (borne stricte), même discipline que build_features.py/live_features.py."
    out: dict[str, FeatureDefinition] = {}

    for side in ("home", "away"):
        venue = "domicile" if side == "home" else "extérieur"
        for stat, label in (
            ("win_rate", f"Taux de victoire (nul=0.5) sur les {HOMEAWAY_WINDOW} derniers matchs À {venue.upper()} de l'équipe."),
            ("goals_scored_avg", f"Buts marqués moyens sur les {HOMEAWAY_WINDOW} derniers matchs à {venue}."),
            ("goals_conceded_avg", f"Buts encaissés moyens sur les {HOMEAWAY_WINDOW} derniers matchs à {venue}."),
        ):
            name = f"{side}_{side}_{stat}_last5"
            out[name] = FeatureDefinition(
                feature_name=name, category="home_away_split", description=label, source=ml_source,
                data_type="float", unit=None,
                availability=f"NaN tant que l'équipe n'a aucun match antérieur À {venue.upper()}.",
                timestamp_field="Match.date", cutoff_rule=cutoff_rule, leakage_risk="SAFE",
                missing_value_strategy="NaN si aucun match antérieur dans ce contexte (jamais imputé à 0).",
                current_model_usage=[], status="EXPERIMENTAL", priority="P1",
                notes="Candidate Phase 8B — voir build_homeaway_split_features.",
            )

    for side in ("home", "away"):
        for window in (7, 14):
            name = f"{side}_matches_last_{window}_days"
            out[name] = FeatureDefinition(
                feature_name=name, category="rest",
                description=f"Nombre de matchs joués par l'équipe dans les {window} jours précédant le match (borne stricte).",
                source=ml_source, data_type="int", unit=None,
                availability="Toujours calculable (0 si aucun match dans la fenêtre).",
                timestamp_field="Match.date", cutoff_rule=cutoff_rule, leakage_risk="SAFE",
                missing_value_strategy="0 si aucun match dans la fenêtre (convention documentée, pas un NaN).",
                current_model_usage=[], status="EXPERIMENTAL", priority="P2",
                notes="Confirme/remplace la piste MISSING documentée en Phase 8A (registry.py::_not_available_features).",
            )

    for name, desc in (
        ("dc_attack_diff", "Écart de force offensive Dixon-Coles brute (attack[home] - attack[away])."),
        ("dc_defense_diff", "Écart de force défensive Dixon-Coles brute (defense[home] - defense[away])."),
        ("dc_net_diff", "Écart de force nette Dixon-Coles (net[home] - net[away], net = attack - defense)."),
    ):
        out[name] = FeatureDefinition(
            feature_name=name, category="team_strength", description=desc, source=ml_source,
            data_type="float", unit=None,
            availability="NaN pendant le rodage (< 100 matchs d'historique dans la ligue) ou équipe jamais vue à l'entraînement.",
            timestamp_field="Match.date",
            cutoff_rule=cutoff_rule + f" Approximation documentée : un seul fit toutes les {DC_STRENGTH_REFIT_EVERY} dates de match distinctes (build_all_research_groups), pas un rating recalculé à chaque match individuellement — voir DC_STRENGTH_REFIT_EVERY.",
            leakage_risk="CAUTION",
            missing_value_strategy="NaN si équipe/période insuffisamment couverte — jamais fabriqué.",
            current_model_usage=[], status="EXPERIMENTAL", priority="P1",
            notes="Distinct des dc_home_win/... déjà en production : ici les paramètres BRUTS du modèle, pas les probabilités dérivées.",
        )

    out["elo_diff"] = FeatureDefinition(
        feature_name="elo_diff", category="team_strength",
        description="Écart de rating Elo pré-match (home + home_advantage - away), walk-forward.",
        source=ml_source, data_type="float", unit=None,
        availability="Toujours calculable (cold-start à 1500.0 par équipe, convention EloEngine).",
        timestamp_field="Match.date", cutoff_rule="Garanti leak-free par construction de EloEngine.walk_forward.",
        leakage_risk="SAFE",
        missing_value_strategy="N/A — jamais NaN (cold-start conventionnel).",
        current_model_usage=[], status="EXPERIMENTAL", priority="P1",
        notes="Hyperparamètres Elo par défaut (k=20, home_advantage=60), pas le grid search par ligue de scripts/backtest_elo.py — limitation documentée.",
    )

    for side in ("home", "away"):
        out[f"{side}_standing_position"] = FeatureDefinition(
            feature_name=f"{side}_standing_position", category="ranking",
            description="Position au classement reconstruit de la saison en cours, avant le match.",
            source=ml_source, data_type="float", unit="rank (1=premier)",
            availability="NaN si l'équipe n'a encore joué aucun match cette saison.",
            timestamp_field="Match.date", cutoff_rule=cutoff_rule + " Remis à zéro à chaque nouvelle saison (mois >= 7).",
            leakage_risk="SAFE",
            missing_value_strategy="NaN en tout début de saison — jamais fabriqué.",
            current_model_usage=[], status="EXPERIMENTAL", priority="P1",
            notes="Dépend de la règle de dérivation de saison (voir season_year) — CAUTION héritée si la saison s'avère peu fiable pour une ligue donnée (voir validate_season_rule).",
        )
        out[f"{side}_points_per_game_season"] = FeatureDefinition(
            feature_name=f"{side}_points_per_game_season", category="ranking",
            description="Points par match de la saison en cours, avant le match.",
            source=ml_source, data_type="float", unit=None,
            availability="NaN si l'équipe n'a encore joué aucun match cette saison.",
            timestamp_field="Match.date", cutoff_rule=cutoff_rule, leakage_risk="SAFE",
            missing_value_strategy="NaN en tout début de saison.",
            current_model_usage=[], status="EXPERIMENTAL", priority="P1",
        )

    out["season_year"] = FeatureDefinition(
        feature_name="season_year", category="match",
        description="Année de début de saison (mois >= 7 -> année en cours, sinon année précédente).",
        source=ml_source, data_type="int", unit=None, availability="Toujours calculable depuis Match.date.",
        timestamp_field="Match.date", cutoff_rule="Dérivée uniquement de Match.date, jamais du résultat.", leakage_risk="CAUTION",
        missing_value_strategy="N/A (jamais nulle).", current_model_usage=[], status="EXPERIMENTAL", priority="P1",
        notes="Reprend exactement la règle déjà validée dans build_features.py::check_team_league_collisions ; fiabilité testée par ligue (validate_season_rule), voir rapport §14.",
    )
    out["season_progress_pct"] = FeatureDefinition(
        feature_name="season_progress_pct", category="match",
        description="Avancement dans la saison (0-100%), depuis le 1er août approximatif jusqu'à +300 jours.",
        source=ml_source, data_type="float", unit="percent", availability="Toujours calculable depuis Match.date.",
        timestamp_field="Match.date", cutoff_rule="Dérivée uniquement de Match.date.", leakage_risk="CAUTION",
        missing_value_strategy="N/A (jamais nulle).", current_model_usage=[], status="EXPERIMENTAL", priority="P2",
        notes="Approximation du coup d'envoi de saison (1er août) — pas une date officielle par ligue/saison, non vérifiée compétition par compétition.",
    )

    return out


PHASE8B_FEATURE_REGISTRY: dict[str, FeatureDefinition] = _phase8b_registry()


def build_all_research_groups(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """
    Applique les 6 fonctions ci-dessus à UNE seule ligue déjà triée par date
    (même contrat que build_ml_features_from_db) et retourne un dict
    groupe -> DataFrame de colonnes, prêt à être concaténé par l'orchestrateur.

    build_dixon_coles_strength_features est appelée avec refit_every=7 (un
    fit toutes les 7 dates de match DISTINCTES, pas 1) : build_dixon_coles_features
    (baseline, déjà en production) fait ~3450 fits sur le dataset complet
    (5 ligues) rien que pour dc_home_win/... — dupliquer ce coût pour un
    SECOND jeu de features (attack/defense bruts, jamais utilisés en
    production) n'est pas justifiable en temps de calcul pour cette
    recherche. refit_every=7 reste STRICTEMENT anti-fuite (chaque fit est
    toujours entraîné sur des matchs antérieurs au fit précédent inclus),
    seulement une granularité de rafraîchissement plus grossière — limitation
    documentée dans PHASE8B_FEATURE_REGISTRY et le rapport (§22 Limitations).
    """
    return {
        "homeaway": build_homeaway_split_features(df),
        "rest_density": build_schedule_density_features(df),
        "dc_strength": build_dixon_coles_strength_features(df, refit_every=DC_STRENGTH_REFIT_EVERY),
        "elo_strength": build_elo_strength_features(df),
        "ranking": build_league_standing_features(df),
        "season": build_season_features(df),
    }
