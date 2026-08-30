"""
feature_sets.py — Phase 8B : versions de feature sets RECHERCHE (§44 du prompt).

Centralise, en un seul endroit, la composition de chaque expérience de
l'EXPERIMENT MATRIX (§19). Aucune de ces versions n'est une version de
PRODUCTION (§18) : baseline_v1 REPRODUIT le feature set de production
(api/app/ai/engine/features.py::FEATURE_COLUMNS) pour comparaison, il ne le
remplace pas.

Note (§3, §19) : FORM (Group A) et GOALS (Group B) sont déjà, texto,
FEATURE_COLUMNS de baseline_v1 (home_form_points_avg, home_form_goals_scored_avg,
home_form_goals_conceded_avg, etc. — voir api/app/ai/engine/features.py). Les
lister à nouveau comme "Baseline + Form" ou "Baseline + Goals" produirait une
expérience strictement identique au baseline (mêmes colonnes), ce qui n'est
pas un test valide. EXPERIMENT_1_FORM et EXPERIMENT_2_GOALS sont donc marquées
ci-dessous DUPLICATE_OF_BASELINE plutôt que simulées comme si elles apportaient
une information nouvelle (§65 : ne pas fabriquer un résultat).
"""

from __future__ import annotations

from app.ai.engine.features import FEATURE_COLUMNS  # 25 colonnes de production, réutilisées telles quelles

DUPLICATE_OF_BASELINE = "DUPLICATE_OF_BASELINE"  # sentinel : groupe déjà entièrement contenu dans baseline_v1

FEATURE_GROUPS: dict[str, list[str] | str] = {
    "form": DUPLICATE_OF_BASELINE,   # home/away_form_points_avg, _goals_scored_avg, _goals_conceded_avg déjà en baseline
    "goals": DUPLICATE_OF_BASELINE,  # mêmes colonnes que "form" ci-dessus — aucune colonne "goals" distincte n'existe
    "homeaway": [
        "home_home_win_rate_last5", "home_home_goals_scored_avg_last5", "home_home_goals_conceded_avg_last5",
        "away_away_win_rate_last5", "away_away_goals_scored_avg_last5", "away_away_goals_conceded_avg_last5",
    ],
    "rest_density": [
        "home_matches_last_7_days", "away_matches_last_7_days",
        "home_matches_last_14_days", "away_matches_last_14_days",
    ],
    "strength": [
        "dc_attack_diff", "dc_defense_diff", "dc_net_diff", "elo_diff",
    ],
    "ranking": [
        "home_standing_position", "away_standing_position",
        "home_points_per_game_season", "away_points_per_game_season",
    ],
    "season": [
        "season_year", "season_progress_pct",
    ],
}

# EXPERIMENT MATRIX (§19) — nom d'expérience -> (feature_set_version, groupe(s) ajoutés au-delà de baseline_v1).
# EXPERIMENT_8_COMBINED n'est PAS pré-déclarée ici : sa composition dépend du
# verdict (BETTER/EQUIVALENT) des expériences 3-7, décidé PAR LE RAPPORT
# lui-même (scripts/feature_engineering_walkforward.py), jamais choisie à
# l'avance ni sur le test (§20, §54).
EXPERIMENTS: dict[str, dict] = {
    "EXPERIMENT_0_BASELINE": {"feature_set_version": "baseline_v1", "groups": []},
    "EXPERIMENT_1_FORM": {"feature_set_version": "form_v1", "groups": ["form"]},
    "EXPERIMENT_2_GOALS": {"feature_set_version": "goals_v1", "groups": ["goals"]},
    "EXPERIMENT_3_HOMEAWAY": {"feature_set_version": "homeaway_v1", "groups": ["homeaway"]},
    "EXPERIMENT_4_REST": {"feature_set_version": "rest_v1", "groups": ["rest_density"]},
    "EXPERIMENT_5_STRENGTH": {"feature_set_version": "strength_v1", "groups": ["strength"]},
    "EXPERIMENT_6_RANKING": {"feature_set_version": "ranking_v1", "groups": ["ranking"]},
    "EXPERIMENT_7_SEASON": {"feature_set_version": "season_v1", "groups": ["season"]},
}


def feature_columns_for(groups: list[str]) -> list[str]:
    """Colonnes de baseline_v1 + colonnes des groupes listés (jamais un groupe
    DUPLICATE_OF_BASELINE — appelant responsable de filtrer, voir orchestrateur)."""
    cols = list(FEATURE_COLUMNS)
    for g in groups:
        spec = FEATURE_GROUPS[g]
        if spec == DUPLICATE_OF_BASELINE:
            continue
        cols.extend(spec)
    return cols
