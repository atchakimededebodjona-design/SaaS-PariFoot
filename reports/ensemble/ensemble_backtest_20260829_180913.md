# XFOOT ENSEMBLE RESEARCH & BACKTEST V2

## 1. Résumé exécutif

Run id : `20260829_180913` — généré le 2026-08-29T18:09:13.932737+00:00.

- **1X2** : EQUIVALENT

- **BTTS** : INSUFFICIENT_DATA

- **O/U** : INSUFFICIENT_DATA

- **CALIBRATION** : NEUTRAL

- **PRODUCTION** : DO NOT PROMOTE

## 2. Dataset

- Ligues : Bundesliga, LaLiga, Ligue1, PremierLeague, SerieA
- Période : 2026-04-12 → 2026-05-24
- Matchs communs (elo/xgboost/lightgbm) : 300
- Échantillons par modèle (avant intersection) : {'elo': 500, 'xgboost': 300, 'lightgbm': 300}

## 3. Méthodologie

Walk-forward chronologique : fold 0..k = burn-in (historique encore insuffisant) ; premier fold exploitable = VALIDATION (sélection stratégie + température Softmax, jamais réutilisé pour la mesure finale) ; folds suivants = TEST (mesure finale, jamais utilisés pour choisir quoi que ce soit). Dixon-Coles réentraîné en mémoire par fold/ligue sur l'historique match.date <= until (jamais persisté).

## 4. Walk-forward

3 fold(s) construits sur 300 matchs communs ; burn-in=[], validation=0, test=[1, 2].

## 5. Folds

- Fold 0 (validation) : 100 matchs, 2026-04-12 → 2026-04-26, until=2026-04-11

- Fold 1 (test) : 100 matchs, 2026-04-26 → 2026-05-10, until=2026-04-25

- Fold 2 (test) : 100 matchs, 2026-05-10 → 2026-05-24, until=2026-05-09

## 6. Baselines (1X2, folds de test uniquement)

| Modèle/Stratégie | Accuracy | Log Loss | Brier | N | CI Accuracy (95%) |
|---|---|---|---|---|---|
| dixon_coles | 0.4700 | 1.0547 | 0.6354 | 200 | [0.4020, 0.5391] |
| elo | 0.4650 | 1.0712 | 0.6429 | 200 | [0.3972, 0.5341] |
| xgboost | 0.4650 | 1.0504 | 0.6320 | 200 | [0.3972, 0.5341] |
| lightgbm | 0.4600 | 1.0484 | 0.6310 | 200 | [0.3923, 0.5292] |

## 7. Simple Average

| Modèle/Stratégie | Accuracy | Log Loss | Brier | N | CI Accuracy (95%) |
|---|---|---|---|---|---|
| simple_average | 0.4700 | 1.0534 | 0.6347 | 200 | [0.4020, 0.5391] |

## 8. Inverse Log Loss

| Modèle/Stratégie | Accuracy | Log Loss | Brier | N | CI Accuracy (95%) |
|---|---|---|---|---|---|
| inverse_log_loss | 0.4700 | 1.0534 | 0.6347 | 200 | [0.4020, 0.5391] |

Diagnostic des poids : {'total_weight_computations': 9, 'zero_model_fold_count': 2, 'single_model_fold_count': 5, 'single_model_fraction': 0.5556, 'multi_model_fold_count': 2, 'multi_model_fraction': 0.2222, 'per_model': {'elo': {'mean': 0.5826, 'median': 0.4964, 'min': 0.2513, 'max': 1.0, 'count': 3, 'frequency_weight_over_90pct': 0.3333}, 'dixon_coles': {'mean': 0.7922, 'median': 1.0, 'min': 0.2495, 'max': 1.0, 'count': 6, 'frequency_weight_over_90pct': 0.6667}, 'xgboost': {'mean': 0.2495, 'median': 0.2495, 'min': 0.2495, 'max': 0.2495, 'count': 1, 'frequency_weight_over_90pct': 0.0}, 'lightgbm': {'mean': 0.2497, 'median': 0.2497, 'min': 0.2497, 'max': 0.2497, 'count': 1, 'frequency_weight_over_90pct': 0.0}}}

## 9. Softmax


Température sélectionnée (validation) : 1.0

| Modèle/Stratégie | Accuracy | Log Loss | Brier | N | CI Accuracy (95%) |
|---|---|---|---|---|---|
| softmax_log_loss | 0.4700 | 1.0534 | 0.6347 | 200 | [0.4020, 0.5391] |

## 10. Brier

| Modèle/Stratégie | Accuracy | Log Loss | Brier | N | CI Accuracy (95%) |
|---|---|---|---|---|---|
| brier | 0.4700 | 1.0534 | 0.6347 | 200 | [0.4020, 0.5391] |

## 11. Hybrid


alpha=0.5, fixe (non optimisé — même politique que la production).

| Modèle/Stratégie | Accuracy | Log Loss | Brier | N | CI Accuracy (95%) |
|---|---|---|---|---|---|
| hybrid | 0.4700 | 1.0534 | 0.6347 | 200 | [0.4020, 0.5391] |

## 12. Dixon-Coles (walk-forward, en mémoire)


Couverture par fold/ligue :

fold0/LaLiga: train=2585, évaluable=22, skip=0; fold0/Ligue1: train=2286, évaluable=17, skip=0; fold0/PremierLeague: train=2594, évaluable=24, skip=0; fold0/SerieA: train=2595, évaluable=19, skip=0; fold0/Bundesliga: train=2094, évaluable=18, skip=0; fold1/Ligue1: train=2303, évaluable=23, skip=0; fold1/SerieA: train=2614, évaluable=20, skip=0; fold1/LaLiga: train=2605, évaluable=20, skip=0; fold1/PremierLeague: train=2618, évaluable=19, skip=0; fold1/Bundesliga: train=2113, évaluable=18, skip=0; fold2/PremierLeague: train=2634, évaluable=23, skip=0; fold2/SerieA: train=2634, évaluable=26, skip=0; fold2/LaLiga: train=2625, évaluable=31, skip=0; fold2/Ligue1: train=2318, évaluable=11, skip=0; fold2/Bundesliga: train=2130, évaluable=9, skip=0

## 13. BTTS


Verdict couverture : single_model

| Modèle/Stratégie | Accuracy | Log Loss | Brier | N | CI Accuracy (95%) |
|---|---|---|---|---|---|
| simple_average | 0.5500 | 0.6762 | 0.4835 | 200 | [0.4808, 0.6174] |
| inverse_log_loss | 0.5500 | 0.6762 | 0.4835 | 200 | [0.4808, 0.6174] |
| softmax_log_loss | 0.5500 | 0.6762 | 0.4835 | 200 | [0.4808, 0.6174] |
| brier | 0.5500 | 0.6762 | 0.4835 | 200 | [0.4808, 0.6174] |
| hybrid | 0.5500 | 0.6762 | 0.4835 | 200 | [0.4808, 0.6174] |
| baseline:dixon_coles | 0.5500 | 0.6762 | 0.4835 | 200 | [0.4808, 0.6174] |
| baseline:elo | N/A | N/A | N/A | 0 | N/A |
| baseline:xgboost | N/A | N/A | N/A | 0 | N/A |
| baseline:lightgbm | N/A | N/A | N/A | 0 | N/A |

## 14. Over/Under 2.5


Verdict couverture : single_model

| Modèle/Stratégie | Accuracy | Log Loss | Brier | N | CI Accuracy (95%) |
|---|---|---|---|---|---|
| simple_average | 0.5500 | 0.6921 | 0.4986 | 200 | [0.4808, 0.6174] |
| inverse_log_loss | 0.5500 | 0.6921 | 0.4986 | 200 | [0.4808, 0.6174] |
| softmax_log_loss | 0.5500 | 0.6921 | 0.4986 | 200 | [0.4808, 0.6174] |
| brier | 0.5500 | 0.6921 | 0.4986 | 200 | [0.4808, 0.6174] |
| hybrid | 0.5500 | 0.6921 | 0.4986 | 200 | [0.4808, 0.6174] |
| baseline:dixon_coles | 0.5500 | 0.6921 | 0.4986 | 200 | [0.4808, 0.6174] |
| baseline:elo | N/A | N/A | N/A | 0 | N/A |
| baseline:xgboost | N/A | N/A | N/A | 0 | N/A |
| baseline:lightgbm | N/A | N/A | N/A | 0 | N/A |

## 15. Calibration

| Modèle/Stratégie | Accuracy | Log Loss | Brier | N | CI Accuracy (95%) |
|---|---|---|---|---|---|
| strategy:simple_average RAW | 0.4700 | 1.0534 | 0.6347 | 200 | [0.4020, 0.5391] |
| strategy:simple_average PLATT | 0.4700 | 1.0504 | 0.6334 | 200 | [0.4020, 0.5391] |
| strategy:simple_average ISOTONIC | 0.4700 | 1.6892 | 0.6447 | 200 | [0.4020, 0.5391] |
| baseline:dixon_coles RAW | 0.4700 | 1.0547 | 0.6354 | 200 | [0.4020, 0.5391] |
| baseline:dixon_coles PLATT | 0.4700 | 1.0548 | 0.6367 | 200 | [0.4020, 0.5391] |
| baseline:dixon_coles ISOTONIC | 0.4700 | 2.0566 | 0.6708 | 200 | [0.4020, 0.5391] |
| baseline:elo RAW | 0.4650 | 1.0712 | 0.6429 | 200 | [0.3972, 0.5341] |
| baseline:elo PLATT | 0.4650 | 1.0617 | 0.6392 | 200 | [0.3972, 0.5341] |
| baseline:elo ISOTONIC | 0.4650 | 2.1803 | 0.6564 | 200 | [0.3972, 0.5341] |
| baseline:xgboost RAW | 0.4650 | 1.0504 | 0.6320 | 200 | [0.3972, 0.5341] |
| baseline:xgboost PLATT | 0.4650 | 1.0662 | 0.6441 | 200 | [0.3972, 0.5341] |
| baseline:xgboost ISOTONIC | 0.4650 | 1.4066 | 0.6576 | 200 | [0.3972, 0.5341] |
| baseline:lightgbm RAW | 0.4600 | 1.0484 | 0.6310 | 200 | [0.3923, 0.5292] |
| baseline:lightgbm PLATT | 0.4600 | 1.0629 | 0.6424 | 200 | [0.3923, 0.5292] |
| baseline:lightgbm ISOTONIC | 0.4600 | 1.7099 | 0.6493 | 200 | [0.3923, 0.5292] |

## 16. Analyse des poids


```
{'simple_average': {'total_weight_computations': 9, 'zero_model_fold_count': 2, 'single_model_fold_count': 5, 'single_model_fraction': 0.5556, 'multi_model_fold_count': 2, 'multi_model_fraction': 0.2222, 'per_model': {'elo': {'mean': 0.5833, 'median': 0.5, 'min': 0.25, 'max': 1.0, 'count': 3, 'frequency_weight_over_90pct': 0.3333}, 'dixon_coles': {'mean': 0.7917, 'median': 1.0, 'min': 0.25, 'max': 1.0, 'count': 6, 'frequency_weight_over_90pct': 0.6667}, 'xgboost': {'mean': 0.25, 'median': 0.25, 'min': 0.25, 'max': 0.25, 'count': 1, 'frequency_weight_over_90pct': 0.0}, 'lightgbm': {'mean': 0.25, 'median': 0.25, 'min': 0.25, 'max': 0.25, 'count': 1, 'frequency_weight_over_90pct': 0.0}}}, 'inverse_log_loss': {'total_weight_computations': 9, 'zero_model_fold_count': 2, 'single_model_fold_count': 5, 'single_model_fraction': 0.5556, 'multi_model_fold_count': 2, 'multi_model_fraction': 0.2222, 'per_model': {'elo': {'mean': 0.5826, 'median': 0.4964, 'min': 0.2513, 'max': 1.0, 'count': 3, 'frequency_weight_over_90pct': 0.3333}, 'dixon_coles': {'mean': 0.7922, 'median': 1.0, 'min': 0.2495, 'max': 1.0, 'count': 6, 'frequency_weight_over_90pct': 0.6667}, 'xgboost': {'mean': 0.2495, 'median': 0.2495, 'min': 0.2495, 'max': 0.2495, 'count': 1, 'frequency_weight_over_90pct': 0.0}, 'lightgbm': {'mean': 0.2497, 'median': 0.2497, 'min': 0.2497, 'max': 0.2497, 'count': 1, 'frequency_weight_over_90pct': 0.0}}}, 'softmax_log_loss': {'total_weight_computations': 9, 'zero_model_fold_count': 2, 'single_model_fold_count': 5, 'single_model_fraction': 0.5556, 'multi_model_fold_count': 2, 'multi_model_fraction': 0.2222, 'per_model': {'elo': {'mean': 0.5826, 'median': 0.4965, 'min': 0.2513, 'max': 1.0, 'count': 3, 'frequency_weight_over_90pct': 0.3333}, 'dixon_coles': {'mean': 0.7922, 'median': 1.0, 'min': 0.2495, 'max': 1.0, 'count': 6, 'frequency_weight_over_90pct': 0.6667}, 'xgboost': {'mean': 0.2495, 'median': 0.2495, 'min': 0.2495, 'max': 0.2495, 'count': 1, 'frequency_weight_over_90pct': 0.0}, 'lightgbm': {'mean': 0.2497, 'median': 0.2497, 'min': 0.2497, 'max': 0.2497, 'count': 1, 'frequency_weight_over_90pct': 0.0}}}, 'brier': {'total_weight_computations': 9, 'zero_model_fold_count': 2, 'single_model_fold_count': 5, 'single_model_fraction': 0.5556, 'multi_model_fold_count': 2, 'multi_model_fraction': 0.2222, 'per_model': {'elo': {'mean': 0.5819, 'median': 0.4949, 'min': 0.2508, 'max': 1.0, 'count': 3, 'frequency_weight_over_90pct': 0.3333}, 'dixon_coles': {'mean': 0.7924, 'median': 1.0, 'min': 0.2492, 'max': 1.0, 'count': 6, 'frequency_weight_over_90pct': 0.6667}, 'xgboost': {'mean': 0.2499, 'median': 0.2499, 'min': 0.2499, 'max': 0.2499, 'count': 1, 'frequency_weight_over_90pct': 0.0}, 'lightgbm': {'mean': 0.2501, 'median': 0.2501, 'min': 0.2501, 'max': 0.2501, 'count': 1, 'frequency_weight_over_90pct': 0.0}}}, 'hybrid': {'total_weight_computations': 9, 'zero_model_fold_count': 2, 'single_model_fold_count': 5, 'single_model_fraction': 0.5556, 'multi_model_fold_count': 2, 'multi_model_fraction': 0.2222, 'per_model': {'elo': {'mean': 0.5822, 'median': 0.4957, 'min': 0.251, 'max': 1.0, 'count': 3, 'frequency_weight_over_90pct': 0.3333}, 'dixon_coles': {'mean': 0.7923, 'median': 1.0, 'min': 0.2493, 'max': 1.0, 'count': 6, 'frequency_weight_over_90pct': 0.6667}, 'xgboost': {'mean': 0.2497, 'median': 0.2497, 'min': 0.2497, 'max': 0.2497, 'count': 1, 'frequency_weight_over_90pct': 0.0}, 'lightgbm': {'mean': 0.2499, 'median': 0.2499, 'min': 0.2499, 'max': 0.2499, 'count': 1, 'frequency_weight_over_90pct': 0.0}}}}
```

## 17. Leakage audit


- Dixon-Coles walk-forward : **SAFE** — Entraîné (export_league) sur match.date <= until (fold_start - 1 jour) uniquement, jamais après ; équipe/ligue sans historique suffisant marquée indisponible plutôt que simulée.

- Sélection température Softmax : **SAFE** — Grid search (1.0, 2.0, 5.0, 10.0, 20.0, 50.0) évalué UNIQUEMENT sur le fold de validation (fold 0) — jamais sur un fold de test.

- Sélection de stratégie : **SAFE** — Choisie par log_loss minimal sur le fold de validation (fold 0) uniquement — TOUTES les stratégies restent néanmoins évaluées et rapportées sur test, pour transparence complète (§29), sans que cela ne change quelle stratégie est déclarée 'sélectionnée'.

- Calibration Platt/Isotonic : **SAFE** — Ajustée sur le fold de validation (fold 0) uniquement, évaluée sur les folds de test uniquement.

## 18. Résultats par fold


### Fold 1

| Modèle/Stratégie | Accuracy | Log Loss | Brier | N | CI Accuracy (95%) |
|---|---|---|---|---|---|
| simple_average [1X2] | 0.4800 | 1.0458 | 0.6312 | 100 | [0.3846, 0.5768] |
| simple_average [BTTS] | 0.5300 | 0.6814 | 0.4887 | 100 | [0.4329, 0.6249] |
| simple_average [OVER_UNDER_2_5] | 0.5600 | 0.6912 | 0.4972 | 100 | [0.4623, 0.6533] |
| inverse_log_loss [1X2] | 0.4800 | 1.0458 | 0.6312 | 100 | [0.3846, 0.5768] |
| inverse_log_loss [BTTS] | 0.5300 | 0.6814 | 0.4887 | 100 | [0.4329, 0.6249] |
| inverse_log_loss [OVER_UNDER_2_5] | 0.5600 | 0.6912 | 0.4972 | 100 | [0.4623, 0.6533] |
| softmax_log_loss [1X2] | 0.4800 | 1.0458 | 0.6312 | 100 | [0.3846, 0.5768] |
| softmax_log_loss [BTTS] | 0.5300 | 0.6814 | 0.4887 | 100 | [0.4329, 0.6249] |
| softmax_log_loss [OVER_UNDER_2_5] | 0.5600 | 0.6912 | 0.4972 | 100 | [0.4623, 0.6533] |
| brier [1X2] | 0.4800 | 1.0458 | 0.6312 | 100 | [0.3846, 0.5768] |
| brier [BTTS] | 0.5300 | 0.6814 | 0.4887 | 100 | [0.4329, 0.6249] |
| brier [OVER_UNDER_2_5] | 0.5600 | 0.6912 | 0.4972 | 100 | [0.4623, 0.6533] |
| hybrid [1X2] | 0.4800 | 1.0458 | 0.6312 | 100 | [0.3846, 0.5768] |
| hybrid [BTTS] | 0.5300 | 0.6814 | 0.4887 | 100 | [0.4329, 0.6249] |
| hybrid [OVER_UNDER_2_5] | 0.5600 | 0.6912 | 0.4972 | 100 | [0.4623, 0.6533] |
| baseline:dixon_coles [1X2] | 0.4900 | 1.0440 | 0.6295 | 100 | [0.3942, 0.5865] |
| baseline:dixon_coles [BTTS] | 0.5300 | 0.6814 | 0.4887 | 100 | [0.4329, 0.6249] |
| baseline:dixon_coles [OVER_UNDER_2_5] | 0.5600 | 0.6912 | 0.4972 | 100 | [0.4623, 0.6533] |
| baseline:elo [1X2] | 0.4700 | 1.0530 | 0.6361 | 100 | [0.3751, 0.5671] |
| baseline:elo [BTTS] | N/A | N/A | N/A | 0 | N/A |
| baseline:elo [OVER_UNDER_2_5] | N/A | N/A | N/A | 0 | N/A |
| baseline:xgboost [1X2] | 0.4800 | 1.0447 | 0.6280 | 100 | [0.3846, 0.5768] |
| baseline:xgboost [BTTS] | N/A | N/A | N/A | 0 | N/A |
| baseline:xgboost [OVER_UNDER_2_5] | N/A | N/A | N/A | 0 | N/A |
| baseline:lightgbm [1X2] | 0.4700 | 1.0404 | 0.6257 | 100 | [0.3751, 0.5671] |
| baseline:lightgbm [BTTS] | N/A | N/A | N/A | 0 | N/A |
| baseline:lightgbm [OVER_UNDER_2_5] | N/A | N/A | N/A | 0 | N/A |

### Fold 2

| Modèle/Stratégie | Accuracy | Log Loss | Brier | N | CI Accuracy (95%) |
|---|---|---|---|---|---|
| simple_average [1X2] | 0.4600 | 1.0610 | 0.6381 | 100 | [0.3656, 0.5574] |
| simple_average [BTTS] | 0.5700 | 0.6710 | 0.4783 | 100 | [0.4722, 0.6627] |
| simple_average [OVER_UNDER_2_5] | 0.5400 | 0.6931 | 0.5000 | 100 | [0.4426, 0.6344] |
| inverse_log_loss [1X2] | 0.4600 | 1.0611 | 0.6382 | 100 | [0.3656, 0.5574] |
| inverse_log_loss [BTTS] | 0.5700 | 0.6710 | 0.4783 | 100 | [0.4722, 0.6627] |
| inverse_log_loss [OVER_UNDER_2_5] | 0.5400 | 0.6931 | 0.5000 | 100 | [0.4426, 0.6344] |
| softmax_log_loss [1X2] | 0.4600 | 1.0611 | 0.6382 | 100 | [0.3656, 0.5574] |
| softmax_log_loss [BTTS] | 0.5700 | 0.6710 | 0.4783 | 100 | [0.4722, 0.6627] |
| softmax_log_loss [OVER_UNDER_2_5] | 0.5400 | 0.6931 | 0.5000 | 100 | [0.4426, 0.6344] |
| brier [1X2] | 0.4600 | 1.0610 | 0.6382 | 100 | [0.3656, 0.5574] |
| brier [BTTS] | 0.5700 | 0.6710 | 0.4783 | 100 | [0.4722, 0.6627] |
| brier [OVER_UNDER_2_5] | 0.5400 | 0.6931 | 0.5000 | 100 | [0.4426, 0.6344] |
| hybrid [1X2] | 0.4600 | 1.0611 | 0.6382 | 100 | [0.3656, 0.5574] |
| hybrid [BTTS] | 0.5700 | 0.6710 | 0.4783 | 100 | [0.4722, 0.6627] |
| hybrid [OVER_UNDER_2_5] | 0.5400 | 0.6931 | 0.5000 | 100 | [0.4426, 0.6344] |
| baseline:dixon_coles [1X2] | 0.4500 | 1.0654 | 0.6413 | 100 | [0.3561, 0.5476] |
| baseline:dixon_coles [BTTS] | 0.5700 | 0.6710 | 0.4783 | 100 | [0.4722, 0.6627] |
| baseline:dixon_coles [OVER_UNDER_2_5] | 0.5400 | 0.6931 | 0.5000 | 100 | [0.4426, 0.6344] |
| baseline:elo [1X2] | 0.4600 | 1.0893 | 0.6498 | 100 | [0.3656, 0.5574] |
| baseline:elo [BTTS] | N/A | N/A | N/A | 0 | N/A |
| baseline:elo [OVER_UNDER_2_5] | N/A | N/A | N/A | 0 | N/A |
| baseline:xgboost [1X2] | 0.4500 | 1.0561 | 0.6359 | 100 | [0.3561, 0.5476] |
| baseline:xgboost [BTTS] | N/A | N/A | N/A | 0 | N/A |
| baseline:xgboost [OVER_UNDER_2_5] | N/A | N/A | N/A | 0 | N/A |
| baseline:lightgbm [1X2] | 0.4500 | 1.0565 | 0.6363 | 100 | [0.3561, 0.5476] |
| baseline:lightgbm [BTTS] | N/A | N/A | N/A | 0 | N/A |
| baseline:lightgbm [OVER_UNDER_2_5] | N/A | N/A | N/A | 0 | N/A |

## 19. Résultats agrégés

| Modèle/Stratégie | Accuracy | Log Loss | Brier | N | CI Accuracy (95%) |
|---|---|---|---|---|---|
| strategy:simple_average | 0.4700 | 1.0534 | 0.6347 | 200 | [0.4020, 0.5391] |
| strategy:inverse_log_loss | 0.4700 | 1.0534 | 0.6347 | 200 | [0.4020, 0.5391] |
| strategy:softmax_log_loss | 0.4700 | 1.0534 | 0.6347 | 200 | [0.4020, 0.5391] |
| strategy:brier | 0.4700 | 1.0534 | 0.6347 | 200 | [0.4020, 0.5391] |
| strategy:hybrid | 0.4700 | 1.0534 | 0.6347 | 200 | [0.4020, 0.5391] |
| dixon_coles | 0.4700 | 1.0547 | 0.6354 | 200 | [0.4020, 0.5391] |
| elo | 0.4650 | 1.0712 | 0.6429 | 200 | [0.3972, 0.5341] |
| xgboost | 0.4650 | 1.0504 | 0.6320 | 200 | [0.3972, 0.5341] |
| lightgbm | 0.4600 | 1.0484 | 0.6310 | 200 | [0.3923, 0.5292] |

## 20. Comparaison aux modèles individuels


Meilleur modèle individuel (1X2, test, log_loss) : lightgbm. Stratégie sélectionnée (simple_average) : sample=200, log_loss=1.0534. Delta vs meilleur individuel : -0.004999999999999893.

## 21. Significativité / incertitude


```
{'compared_strategy': 'simple_average', 'compared_baseline': 'lightgbm', 'common_sample_size': 200, 'bootstrap_log_loss_diff': {'sample_size': 200, 'mean_diff': 0.005, 'ci_low': -0.01144, 'ci_high': 0.02128, 'significant': False}, 'bootstrap_brier_diff': {'sample_size': 200, 'mean_diff': 0.00366, 'ci_low': -0.00662, 'ci_high': 0.01386, 'significant': False}, 'mcnemar_accuracy': {'b': 4, 'c': 2, 'statistic': 0.1667, 'p_value': 0.6831, 'significant': False}}
```

## 22. Reproductibilité


- git_commit : 2cd0d83069e408713366261742ecf2d40ac9860d

- seed : 20260828

- library_versions : {'numpy': '2.5.1', 'scipy': '1.18.0', 'scikit-learn': '1.9.0', 'pandas': '3.0.3'}

- model_version_ids : {'elo': 11, 'xgboost': 12, 'lightgbm': 13}

- min_sample_size : 100

- n_folds_requested : 3

## 23. Limites


- Échantillons bruts différents par modèle avant intersection ({'elo': 500, 'xgboost': 300, 'lightgbm': 300}) — toutes les comparaisons ci-dessous portent sur les 300 matchs COMMUNS uniquement, jamais sur ces totaux bruts.

- La calibration Platt/Isotonic ici recalibre uniquement la confiance du pick (probabilité maximale), redistribuée proportionnellement sur les autres issues — pas un Platt/Isotonic multi-classe indépendant complet.

- La stratégie/température 'sélectionnée' l'est sur un seul fold de validation ; avec seulement 3 fold(s) non burn-in disponibles, cette sélection reste peu robuste — voir sample_size par fold.

- BTTS/OVER_UNDER_2_5 : couverture multi-modèle = {'BTTS': 'single_model', 'OVER_UNDER_2_5': 'single_model'} — voir §14/§15, Elo/XGBoost/LightGBM ne modélisent aujourd'hui que 1X2 (confirmé empiriquement, jamais supposé).

## 24. Conclusion


Sur 200 matchs de test (folds [1, 2]), la stratégie 'simple_average' (sélectionnée sur validation) obtient log_loss=1.0534 contre 1.0484 pour le meilleur modèle individuel (lightgbm) — verdict 1X2 : EQUIVALENT. BTTS/O-U : couverture multi-modèle insuffisante ({'BTTS': 'single_model', 'OVER_UNDER_2_5': 'single_model'}) -> INSUFFICIENT_DATA/INSUFFICIENT_DATA. Calibration : NEUTRAL. Décision production : DO NOT PROMOTE — la promotion effective reste une décision humaine séparée, jamais automatique.

---

PHASE 5.7 — XFOOT ENSEMBLE RESEARCH & BACKTEST V2 TERMINÉE. AUCUNE PROMOTION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.
