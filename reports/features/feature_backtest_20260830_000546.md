# XFOOT FEATURE ENGINEERING & WALK-FORWARD EVALUATION V1

## 1. Executive Summary

Run id : `20260830_000546` — généré le 2026-08-30T00:05:46.491701+00:00.

RÈGLE ABSOLUE : AUCUNE MODIFICATION PRODUCTION. Résultats research-only.

- **EXPERIMENT_0_BASELINE** : BASELINE

- **EXPERIMENT_3_HOMEAWAY** : EQUIVALENT

- **EXPERIMENT_4_REST** : EQUIVALENT

- **EXPERIMENT_5_STRENGTH** : EQUIVALENT

- **EXPERIMENT_6_RANKING** : EQUIVALENT

- **EXPERIMENT_7_SEASON** : WORSE

- **EXPERIMENT_1_FORM** : DUPLICATE_OF_BASELINE

- **EXPERIMENT_2_GOALS** : DUPLICATE_OF_BASELINE

- **EXPERIMENT_8_COMBINED** : INSUFFICIENT_DATA

## 2. Dataset

- Ligues : Bundesliga, LaLiga, Ligue1, PremierLeague, SerieA
- Matchs (après filtrage dc_home_win non-NaN) : 11912
- Période : 2019-10-25 00:00:00 → 2026-05-24 00:00:00
- Répartition par ligue : {'PremierLeague': 2552, 'LaLiga': 2551, 'SerieA': 2547, 'Ligue1': 2229, 'Bundesliga': 2033}

## 3. Baseline

- feature_set_version : `baseline_v1`
- 25 colonnes (identiques à la production, voir api/app/ai/engine/features.py::FEATURE_COLUMNS) + `league`
- Modèle : XGBoost, hyperparamètres = {'max_depth': 4, 'learning_rate': 0.05, 'n_estimators': 300, 'objective': 'multi:softprob', 'num_class': 3, 'eval_metric': 'mlogloss', 'early_stopping_rounds': 20, 'enable_categorical': True, 'tree_method': 'hist'}
- seed : 42

## 4. Feature Registry (Phase 8B, additif — jamais fusionné à la production)

| Feature | Groupe | Status | Leakage Risk |
|---|---|---|---|
| home_home_win_rate_last5 | home_away_split | EXPERIMENTAL | SAFE |
| home_home_goals_scored_avg_last5 | home_away_split | EXPERIMENTAL | SAFE |
| home_home_goals_conceded_avg_last5 | home_away_split | EXPERIMENTAL | SAFE |
| away_away_win_rate_last5 | home_away_split | EXPERIMENTAL | SAFE |
| away_away_goals_scored_avg_last5 | home_away_split | EXPERIMENTAL | SAFE |
| away_away_goals_conceded_avg_last5 | home_away_split | EXPERIMENTAL | SAFE |
| home_matches_last_7_days | rest | EXPERIMENTAL | SAFE |
| home_matches_last_14_days | rest | EXPERIMENTAL | SAFE |
| away_matches_last_7_days | rest | EXPERIMENTAL | SAFE |
| away_matches_last_14_days | rest | EXPERIMENTAL | SAFE |
| dc_attack_diff | team_strength | EXPERIMENTAL | CAUTION |
| dc_defense_diff | team_strength | EXPERIMENTAL | CAUTION |
| dc_net_diff | team_strength | EXPERIMENTAL | CAUTION |
| elo_diff | team_strength | EXPERIMENTAL | SAFE |
| home_standing_position | ranking | EXPERIMENTAL | SAFE |
| home_points_per_game_season | ranking | EXPERIMENTAL | SAFE |
| away_standing_position | ranking | EXPERIMENTAL | SAFE |
| away_points_per_game_season | ranking | EXPERIMENTAL | SAFE |
| season_year | match | EXPERIMENTAL | CAUTION |
| season_progress_pct | match | EXPERIMENTAL | CAUTION |

## 5. Feature Groups

- **form** : DUPLICATE_OF_BASELINE
- **goals** : DUPLICATE_OF_BASELINE
- **homeaway** : home_home_win_rate_last5, home_home_goals_scored_avg_last5, home_home_goals_conceded_avg_last5, away_away_win_rate_last5, away_away_goals_scored_avg_last5, away_away_goals_conceded_avg_last5
- **rest_density** : home_matches_last_7_days, away_matches_last_7_days, home_matches_last_14_days, away_matches_last_14_days
- **strength** : dc_attack_diff, dc_defense_diff, dc_net_diff, elo_diff
- **ranking** : home_standing_position, away_standing_position, home_points_per_game_season, away_points_per_game_season
- **season** : season_year, season_progress_pct

## 6. Feature Definitions

Voir §4 (Feature Registry) — chaque feature y a name/definition/source/cutoff/availability/missing_strategy/leakage_status.

## 7. Temporal Cutoff

Toutes les features candidates respectent `Match.date < cutoff` (strict) — voir docstrings de api/app/ai/features/research_features_v1.py. dc_attack_diff/dc_defense_diff/dc_net_diff : approximation documentée (un fit par date de match distincte, jamais par seconde) — CAUTION, jamais LEAKAGE_RISK.

## 8. Leakage Audit


Toutes les fonctions de api/app/ai/features/research_features_v1.py suivent le même patron ligne-par-ligne que build_form_and_h2h_features (lecture de l'historique AVANT mise à jour) — aucune fonction ne lit home_goals/away_goals de la ligne courante avant d'avoir déjà produit ses features. Voir api/test_feature_engineering_v1.py pour les tests de fuite (match futur synthétique à score extrême, jamais visible avant sa propre date).

## 9. Feature Coverage

| Feature | Available | Total | Coverage | Leakage | Status |
|---|---|---|---|---|---|
| home_home_win_rate_last5 | 11770 | 11912 | 98.8% | SAFE | EXPERIMENTAL |
| home_home_goals_scored_avg_last5 | 11770 | 11912 | 98.8% | SAFE | EXPERIMENTAL |
| home_home_goals_conceded_avg_last5 | 11770 | 11912 | 98.8% | SAFE | EXPERIMENTAL |
| away_away_win_rate_last5 | 11770 | 11912 | 98.8% | SAFE | EXPERIMENTAL |
| away_away_goals_scored_avg_last5 | 11770 | 11912 | 98.8% | SAFE | EXPERIMENTAL |
| away_away_goals_conceded_avg_last5 | 11770 | 11912 | 98.8% | SAFE | EXPERIMENTAL |
| home_matches_last_7_days | 11912 | 11912 | 100.0% | SAFE | EXPERIMENTAL |
| home_matches_last_14_days | 11912 | 11912 | 100.0% | SAFE | EXPERIMENTAL |
| away_matches_last_7_days | 11912 | 11912 | 100.0% | SAFE | EXPERIMENTAL |
| away_matches_last_14_days | 11912 | 11912 | 100.0% | SAFE | EXPERIMENTAL |
| dc_attack_diff | 11325 | 11912 | 95.1% | CAUTION | EXPERIMENTAL |
| dc_defense_diff | 11325 | 11912 | 95.1% | CAUTION | EXPERIMENTAL |
| dc_net_diff | 11325 | 11912 | 95.1% | CAUTION | EXPERIMENTAL |
| elo_diff | 11912 | 11912 | 100.0% | SAFE | EXPERIMENTAL |
| home_standing_position | 11573 | 11912 | 97.2% | SAFE | EXPERIMENTAL |
| home_points_per_game_season | 11573 | 11912 | 97.2% | SAFE | EXPERIMENTAL |
| away_standing_position | 11562 | 11912 | 97.1% | SAFE | EXPERIMENTAL |
| away_points_per_game_season | 11562 | 11912 | 97.1% | SAFE | EXPERIMENTAL |
| season_year | 11912 | 11912 | 100.0% | CAUTION | EXPERIMENTAL |
| season_progress_pct | 11912 | 11912 | 100.0% | CAUTION | EXPERIMENTAL |

## 10. Feature Quality


Aucun NaN converti silencieusement en 0 sauf pour les compteurs de densité de calendrier (matches_last_7/14_days), où 0 est une vraie mesure (aucun match dans la fenêtre), jamais une convention de valeur manquante — documenté dans PHASE8B_FEATURE_REGISTRY.

## 11. Walk-Forward Methodology


Burn-in = 4 folds d'évaluation + 20% des lignes les plus anciennes jamais évaluées (historique insuffisant pour un walk-forward honnête). Pour chaque fold : train = tout ce qui précède strictement le fold, validation = les 100 dernières lignes du train (early stopping uniquement), test = le fold lui-même. Jamais optimisé sur le test.

## 12. Experiments

| Experiment | N | Log Loss | Brier | Accuracy | Delta LogLoss vs Baseline | Status |
|---|---|---|---|---|---|---|
| EXPERIMENT_0_BASELINE | 9530 | 0.9966 | 0.5937 | 0.5195 | +0.0000 | BASELINE |
| EXPERIMENT_3_HOMEAWAY | 9530 | 0.9957 | 0.5929 | 0.5202 | +0.0009 | EQUIVALENT |
| EXPERIMENT_4_REST | 9530 | 0.9966 | 0.5937 | 0.5210 | +0.0000 | EQUIVALENT |
| EXPERIMENT_5_STRENGTH | 9530 | 0.9955 | 0.5930 | 0.5192 | +0.0011 | EQUIVALENT |
| EXPERIMENT_6_RANKING | 9530 | 0.9955 | 0.5931 | 0.5238 | +0.0011 | EQUIVALENT |
| EXPERIMENT_7_SEASON | 9530 | 0.9984 | 0.5947 | 0.5177 | -0.0018 | WORSE |
| EXPERIMENT_1_FORM | — | — | — | — | DUPLICATE_OF_BASELINE — ces colonnes sont déjà dans baseline_v1 (home/away_form_points_avg, _goals_scored_avg, _goals_conceded_avg) ; aucun test valide possible, jamais simulé. |
| EXPERIMENT_2_GOALS | — | — | — | — | DUPLICATE_OF_BASELINE — ces colonnes sont déjà dans baseline_v1 (home/away_form_points_avg, _goals_scored_avg, _goals_conceded_avg) ; aucun test valide possible, jamais simulé. |

## 13. Fold Results

| Experiment | Fold | N | LogLoss | Brier | Accuracy |
|---|---|---|---|---|---|
| EXPERIMENT_0_BASELINE | 0 | 2382 | 1.0042 | 0.5979 | 0.5092 |
| EXPERIMENT_0_BASELINE | 1 | 2382 | 0.9959 | 0.5921 | 0.5277 |
| EXPERIMENT_0_BASELINE | 2 | 2382 | 0.9947 | 0.5942 | 0.5172 |
| EXPERIMENT_0_BASELINE | 3 | 2384 | 0.9917 | 0.5904 | 0.5239 |
| EXPERIMENT_3_HOMEAWAY | 0 | 2382 | 1.0028 | 0.5967 | 0.5151 |
| EXPERIMENT_3_HOMEAWAY | 1 | 2382 | 0.9991 | 0.5940 | 0.5218 |
| EXPERIMENT_3_HOMEAWAY | 2 | 2382 | 0.9898 | 0.5908 | 0.5164 |
| EXPERIMENT_3_HOMEAWAY | 3 | 2384 | 0.9910 | 0.5900 | 0.5277 |
| EXPERIMENT_4_REST | 0 | 2382 | 1.0038 | 0.5978 | 0.5126 |
| EXPERIMENT_4_REST | 1 | 2382 | 0.9959 | 0.5921 | 0.5269 |
| EXPERIMENT_4_REST | 2 | 2382 | 0.9943 | 0.5939 | 0.5189 |
| EXPERIMENT_4_REST | 3 | 2384 | 0.9924 | 0.5908 | 0.5256 |
| EXPERIMENT_5_STRENGTH | 0 | 2382 | 1.0071 | 0.5989 | 0.5021 |
| EXPERIMENT_5_STRENGTH | 1 | 2382 | 0.9945 | 0.5916 | 0.5306 |
| EXPERIMENT_5_STRENGTH | 2 | 2382 | 0.9924 | 0.5933 | 0.5139 |
| EXPERIMENT_5_STRENGTH | 3 | 2384 | 0.9879 | 0.5883 | 0.5302 |
| EXPERIMENT_6_RANKING | 0 | 2382 | 1.0049 | 0.5982 | 0.5151 |
| EXPERIMENT_6_RANKING | 1 | 2382 | 0.9922 | 0.5900 | 0.5332 |
| EXPERIMENT_6_RANKING | 2 | 2382 | 0.9929 | 0.5928 | 0.5227 |
| EXPERIMENT_6_RANKING | 3 | 2384 | 0.9921 | 0.5914 | 0.5243 |
| EXPERIMENT_7_SEASON | 0 | 2382 | 1.0102 | 0.6014 | 0.5080 |
| EXPERIMENT_7_SEASON | 1 | 2382 | 0.9974 | 0.5931 | 0.5248 |
| EXPERIMENT_7_SEASON | 2 | 2382 | 0.9942 | 0.5939 | 0.5155 |
| EXPERIMENT_7_SEASON | 3 | 2384 | 0.9919 | 0.5906 | 0.5226 |

## 14. Ablation Results

| Group | Coverage | Delta LogLoss (vs baseline) | Folds Improved/Worsened/Equal | Verdict |
|---|---|---|---|---|
| EXPERIMENT_3_HOMEAWAY | — | +0.00093 [-0.00058, +0.00244] | 3/1/0 | EQUIVALENT |
| EXPERIMENT_4_REST | — | +0.00003 [-0.00056, +0.00060] | 3/1/0 | EQUIVALENT |
| EXPERIMENT_5_STRENGTH | — | +0.00112 [-0.00104, +0.00316] | 3/1/0 | EQUIVALENT |
| EXPERIMENT_6_RANKING | — | +0.00108 [-0.00046, +0.00262] | 2/2/0 | EQUIVALENT |
| EXPERIMENT_7_SEASON | — | -0.00180 [-0.00299, -0.00067] | 1/3/0 | WORSE |

## 15. Feature Importance

Importance = gain XGBoost, agrégée sur tous les folds évaluables. IMPORTANCE ≠ CAUSALITÉ (§26/§51).


**EXPERIMENT_0_BASELINE** (top features) :

| Feature | Importance (gain %) |
|---|---|
| dc_away_win | 20.18% |
| dc_home_win | 17.94% |
| dc_draw | 4.48% |
| away_shots_diff_avg | 3.98% |
| home_shots_diff_avg | 3.34% |
| away_corners_diff_avg | 3.24% |
| away_shots_target_diff_avg | 3.13% |
| home_shots_target_diff_avg | 2.98% |
| home_corners_diff_avg | 2.96% |
| dc_under_2_5 | 2.92% |

**EXPERIMENT_3_HOMEAWAY** (top features) :

| Feature | Importance (gain %) |
|---|---|
| dc_home_win | 17.26% |
| dc_away_win | 16.74% |
| dc_draw | 3.97% |
| away_shots_diff_avg | 3.38% |
| home_shots_diff_avg | 2.82% |
| away_corners_diff_avg | 2.73% |
| home_shots_target_diff_avg | 2.60% |
| dc_under_2_5 | 2.58% |
| away_shots_target_diff_avg | 2.53% |
| home_corners_diff_avg | 2.52% |

**EXPERIMENT_4_REST** (top features) :

| Feature | Importance (gain %) |
|---|---|
| dc_away_win | 18.39% |
| dc_home_win | 16.37% |
| dc_draw | 3.97% |
| away_shots_diff_avg | 3.53% |
| home_shots_diff_avg | 3.00% |
| away_corners_diff_avg | 2.92% |
| away_shots_target_diff_avg | 2.85% |
| home_shots_target_diff_avg | 2.73% |
| home_corners_diff_avg | 2.71% |
| dc_under_2_5 | 2.66% |

**EXPERIMENT_5_STRENGTH** (top features) :

| Feature | Importance (gain %) |
|---|---|
| dc_away_win | 19.56% |
| dc_home_win | 17.71% |
| elo_diff | 4.19% |
| dc_draw | 3.89% |
| away_shots_diff_avg | 3.25% |
| dc_attack_diff | 2.81% |
| away_corners_diff_avg | 2.71% |
| home_shots_diff_avg | 2.64% |
| home_shots_target_diff_avg | 2.58% |
| dc_under_2_5 | 2.52% |

**EXPERIMENT_6_RANKING** (top features) :

| Feature | Importance (gain %) |
|---|---|
| dc_away_win | 18.51% |
| dc_home_win | 18.48% |
| dc_draw | 3.85% |
| away_shots_diff_avg | 3.30% |
| home_shots_diff_avg | 2.91% |
| away_corners_diff_avg | 2.83% |
| away_points_per_game_season | 2.64% |
| home_shots_target_diff_avg | 2.56% |
| away_shots_target_diff_avg | 2.55% |
| home_corners_diff_avg | 2.52% |

**EXPERIMENT_7_SEASON** (top features) :

| Feature | Importance (gain %) |
|---|---|
| dc_away_win | 19.23% |
| dc_home_win | 16.66% |
| dc_draw | 4.20% |
| away_shots_diff_avg | 3.73% |
| home_shots_diff_avg | 3.17% |
| away_corners_diff_avg | 3.14% |
| away_shots_target_diff_avg | 2.90% |
| home_shots_target_diff_avg | 2.87% |
| home_corners_diff_avg | 2.82% |
| dc_under_2_5 | 2.71% |

## 16. Statistical Tests

bootstrap_paired_diff (2000 tirages, seed fixe) + mcnemar_test — réutilisés de api/app/ai/arena/research.py, jamais réimplémentés. Voir §14.

## 17. Stability

Un groupe n'est retenu BETTER que si l'amélioration est significative (bootstrap, IC exclut 0) ET cohérente sur une MAJORITÉ des folds d'évaluation (jamais un seul fold isolé). Voir colonne « Folds Improved/Worsened/Equal » §14.

## 18. Reproducibility


seed=42, dataset=11912 matchs, feature_set_version par expérience (§4), même hyperparamètres XGBoost pour toutes les expériences — un run identique produit les mêmes features et les mêmes métriques (voir api/test_feature_engineering_v1.py::test_reproducibility_same_seed).

## 19. Database Safety


Compteurs AVANT : {'match': 12459, 'match_stats': 12459, 'model_predictions': 3610, 'model_versions': 15, 'prediction_log': 9, 'team_ratings': 568}

Compteurs APRÈS : {'match': 12459, 'match_stats': 12459, 'model_predictions': 3610, 'model_versions': 15, 'prediction_log': 9, 'team_ratings': 568}

Identiques : True

## 20. Production Isolation

Aucune écriture dans model_predictions / model_versions / prediction_log / team_ratings / active models. Tous les entraînements XGBoost sont EN MÉMOIRE, jamais persistés. Artefacts dans reports/features/ uniquement.

## 21. Results


Verdicts par groupe : {'EXPERIMENT_0_BASELINE': 'BASELINE', 'EXPERIMENT_3_HOMEAWAY': 'EQUIVALENT', 'EXPERIMENT_4_REST': 'EQUIVALENT', 'EXPERIMENT_5_STRENGTH': 'EQUIVALENT', 'EXPERIMENT_6_RANKING': 'EQUIVALENT', 'EXPERIMENT_7_SEASON': 'WORSE', 'EXPERIMENT_1_FORM': 'DUPLICATE_OF_BASELINE', 'EXPERIMENT_2_GOALS': 'DUPLICATE_OF_BASELINE', 'EXPERIMENT_8_COMBINED': 'INSUFFICIENT_DATA'}. Voir §14 (Ablation Results) pour les deltas de log-loss chiffrés et les intervalles de confiance bootstrap.

## 22. Limitations

- Base locale = 5 ligues sur les 11 du CSV source (Bundesliga, LaLiga, Ligue1, PremierLeague, SerieA) — aucune conclusion étendue aux 6 ligues non chargées (ChampionsLeague, ConferenceLeague, EuropaLeague, MLS, PrimeiraLiga, SaudiProLeague).
- elo_diff utilise les hyperparamètres Elo par défaut (k=20, home_advantage=60), pas le grid search par ligue de scripts/backtest_elo.py.
- dc_attack_diff/dc_defense_diff/dc_net_diff : un fit Dixon-Coles par DATE de match distincte (comme build_dixon_coles_features), pas un rating recalculé à la seconde près.
- season_progress_pct utilise un 1er août approximatif comme début de saison — non vérifié compétition par compétition.
- BTTS/Over-Under non évalués (§30) : XGBoost/LightGBM de production ne modélisent que 1X2 (confirmé dans train_ml_stacking_from_db.py) — INSUFFICIENT_DATA structurel, jamais simulé.
- Multiple testing (§29) : 5 groupes + 1 combinaison testés sur le même burn-in/folds — le seuil de significativité par comparaison (bootstrap IC95%) n'est pas corrigé pour comparaisons multiples ; un verdict BETTER isolé sur un seul groupe doit être lu avec cette réserve.

## 23. Recommended Features (RESEARCH_CANDIDATE)

- Aucune (aucun groupe n'a atteint le verdict BETTER de façon stable).

## 24. Rejected Features

- EXPERIMENT_3_HOMEAWAY (homeaway) — EQUIVALENT.
- EXPERIMENT_4_REST (rest_density) — EQUIVALENT.
- EXPERIMENT_5_STRENGTH (strength) — EQUIVALENT.
- EXPERIMENT_6_RANKING (ranking) — EQUIVALENT.
- EXPERIMENT_7_SEASON (season) — WORSE.

## 25. Recommendations Phase 8C

- Si un groupe est BETTER de façon stable : envisager une validation supplémentaire sur un dataset élargi (11 ligues) avant toute discussion de promotion — jamais dans cette phase.
- elo_diff avec hyperparamètres optimisés par ligue (réutiliser scripts/backtest_elo.py) pourrait changer le verdict du groupe strength — non testé ici.
- league_standing dépend de season_year (rule mois >= 7) — si season_year est EQUIVALENT/WORSE, revalider league_standing avec une règle de saison alternative avant d'abandonner le groupe ranking.

---

### BEST RESEARCH FEATURE SET


Aucun groupe candidat n'a atteint le verdict BETTER de façon stable — baseline_v1 reste la meilleure option testée.  — RESEARCH ONLY.

### PRODUCTION

DO NOT PROMOTE.

---

PHASE 8B — XFOOT FEATURE ENGINEERING & WALK-FORWARD EVALUATION V1 TERMINÉE. AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. AUCUN FEATURE SET PROMU. EN ATTENTE DE VALIDATION.
