# XFOOT SHADOW EVALUATION & TRACK RECORD V1

## 1. Résumé exécutif

Run id : `20260829_221408` — généré le 2026-08-29T22:14:08.992751+00:00.

**NO SHADOW DATA** — aucune prédiction shadow résolue n'existe encore. Ce rapport ne contient donc aucune mesure de performance : rien n'est fabriqué.

## 2. Période et marchés couverts

- Période : depuis le début → aujourd’hui
- Marchés : 1X2, BTTS, OVER_UNDER_2_5

## 3. Production vs Shadow — Log Loss

| Market | N | Production LogLoss | Shadow LogLoss | Delta | Conclusion |
|---|---|---|---|---|---|
| 1X2 | 0 | N/A | N/A | N/A | INSUFFICIENT_DATA |
| BTTS | 0 | N/A | N/A | N/A | INSUFFICIENT_DATA |
| OVER_UNDER_2_5 | 0 | N/A | N/A | N/A | INSUFFICIENT_DATA |

## 4. Production vs Shadow — Accuracy

| Market | N | Production | Shadow | Delta | Significance |
|---|---|---|---|---|---|
| 1X2 | 0 | N/A | N/A | N/A | significant=None |
| BTTS | 0 | N/A | N/A | N/A | significant=None |
| OVER_UNDER_2_5 | 0 | N/A | N/A | N/A | significant=None |

## 5. Production vs Shadow — Brier

| Market | N | Production | Shadow | Delta | Conclusion |
|---|---|---|---|---|---|
| 1X2 | 0 | N/A | N/A | N/A | INSUFFICIENT_DATA |
| BTTS | 0 | N/A | N/A | N/A | INSUFFICIENT_DATA |
| OVER_UNDER_2_5 | 0 | N/A | N/A | N/A | INSUFFICIENT_DATA |

## 6. Selection distribution

| Model | Selected | Share |
|---|---|---|

## 7. Stability tracking


- **1X2** : {} (non attribué : {'insufficient_data': 1, 'no_identifiable_candidate': 0})

- **BTTS** : {} (non attribué : {'insufficient_data': 1, 'no_identifiable_candidate': 0})

- **OVER_UNDER_2_5** : {} (non attribué : {'insufficient_data': 1, 'no_identifiable_candidate': 0})

## 8. Calibration

| Market | NONE | PLATT | ISOTONIC | Verdict (raw vs calibrated) |
|---|---|---|---|---|
| 1X2 | N/A | N/A | N/A | N/A |
| BTTS | N/A | N/A | N/A | N/A |
| OVER_UNDER_2_5 | N/A | N/A | N/A | N/A |

## 9. Track Record cumulatif


### 1X2

| Until | N | Accuracy | LogLoss | Brier |
|---|---|---|---|---|

### BTTS

| Until | N | Accuracy | LogLoss | Brier |
|---|---|---|---|---|

### OVER_UNDER_2_5

| Until | N | Accuracy | LogLoss | Brier |
|---|---|---|---|---|

## 10. Limitations


- Le mode SHADOW LIVE (scripts/model_selection_shadow.py --mode live) ne produit des prédictions que pour des fixtures déjà 'pending' en production (model_predictions) — aucun nouvel appel réseau/fixture n'est jamais effectué par cette phase ; l'échantillon dépend donc entièrement de l'activité réelle de la production entre deux exécutions.

- Les fenêtres glissantes 'last_N' peuvent afficher un échantillon identique si moins de N prédictions résolues existent au total — toujours vérifier sample_size avant de comparer deux fenêtres entre elles.

- Seuil de significativité pratique utilisé pour toutes les conclusions : 1% relatif sur log_loss (même seuil que la calibration, Phase 6).

## 11. Conclusion


NO SHADOW DATA — aucune prédiction shadow résolue n'existe encore.

---

PHASE 7 — XFOOT SHADOW EVALUATION & TRACK RECORD V1 TERMINÉE. AUCUNE PROMOTION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.
