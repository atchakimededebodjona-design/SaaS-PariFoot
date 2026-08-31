# XFOOT HISTORICAL MODEL SNAPSHOT & REPLAY FOUNDATION V1

## 1. Executive Summary

Run id : `20260830_194650` — généré le 2026-08-30T19:46:50.787871+00:00. RÈGLE : RESEARCH ONLY. NO PRODUCTION CHANGE. NO MODEL CREATION. NO CALIBRATION TRAINING.

**Verdict final : HISTORICAL_REPLAY_NOT_AVAILABLE**

Preuve exhaustive (§25/§26) : 186885 paires (match, ModelVersion) évaluées sur l'intégralité du dataset local — {'REPLAYABLE': 0, 'NOT_REPLAYABLE': 186885, 'PARTIAL': 0, 'UNKNOWN': 0} — 0 REPLAYABLE. Toutes les ModelVersion actuelles ont été (ré)entraînées après le dernier match connu (voir §7/§14). Un HISTORICAL_REPLAY_NOT_AVAILABLE honnête est préféré à un faux backtest (§51).

## 2. Model Version Inventory

| ID | Type | Status | Active | trained_at | Artifact (DB) | Config | TeamRatings |
|---|---|---|---|---|---|---|---|
| 1 | xgboost | retired | False | 2026-08-16 22:21:41.386351 | 0B | False | 0 |
| 2 | lightgbm | retired | False | 2026-08-16 22:21:46.726761 | 0B | False | 0 |
| 3 | elo | retired | False | 2026-08-18 19:45:16.690669 | 0B | False | 142 |
| 4 | elo | retired | False | 2026-08-18 19:46:13.344919 | 0B | False | 142 |
| 5 | xgboost | retired | False | 2026-08-18 20:21:37.040996 | 0B | False | 0 |
| 6 | lightgbm | retired | False | 2026-08-18 20:21:42.585912 | 0B | False | 0 |
| 7 | elo | retired | False | 2026-08-18 21:02:38.713611 | 0B | True | 142 |
| 8 | ensemble | retired | False | 2026-08-18 21:53:10.432035 | 0B | False | 0 |
| 9 | dixon_coles | active | True | 2026-08-18 21:59:40.998657 | 0B | False | 0 |
| 10 | ensemble | retired | False | 2026-08-18 22:40:28.176435 | 0B | False | 0 |
| 11 | elo | active | True | 2026-08-18 22:46:30.306489 | 0B | True | 142 |
| 12 | xgboost | active | True | 2026-08-18 23:16:32.790666 | 406574B | True | 0 |
| 13 | lightgbm | active | True | 2026-08-18 23:16:38.168281 | 186648B | True | 0 |
| 14 | ensemble | active | True | 2026-08-18 23:17:38.220237 | 0B | False | 0 |
| 15 | ensemble | retired | False | 2026-08-18 23:17:55.126590 | 0B | False | 0 |

## 3. Artifact Inventory

| Path | League | Size | SHA-256 (12) | Filesystem mtime (info only) | Embedded trained_at | Embedded data_up_to |
|---|---|---|---|---|---|---|
| api\model_artifacts\Bundesliga.json | Bundesliga | 3723B | a12afebcc9c0 | 2026-08-28T21:48:36.222350+00:00 | 2026-08-03T19:13:03.321704+00:00 | 2026-05-16T00:00:00 |
| api\model_artifacts\ChampionsLeague.json | ChampionsLeague | 25176B | eb576e99acf9 | 2026-08-28T21:50:25.213683+00:00 | 2026-08-26T18:51:01.032482+00:00 | 2026-08-25T00:00:00 |
| api\model_artifacts\ConferenceLeague.json | ConferenceLeague | 40439B | c0c4da0484cf | 2026-08-26T18:51:45.930095+00:00 | 2026-08-26T18:51:45.927561+00:00 | 2026-08-20T00:00:00 |
| api\model_artifacts\EuropaLeague.json | EuropaLeague | 38476B | 7f804f3ecb54 | 2026-08-26T18:51:22.434806+00:00 | 2026-08-26T18:51:22.438738+00:00 | 2026-08-20T00:00:00 |
| api\model_artifacts\LaLiga.json | LaLiga | 5063B | d62789213fcb | 2026-08-28T21:48:35.378122+00:00 | 2026-08-03T19:13:04.190036+00:00 | 2026-05-24T00:00:00 |
| api\model_artifacts\Ligue1.json | Ligue1 | 3549B | a81bcca0506f | 2026-08-28T21:48:36.981810+00:00 | 2026-08-03T19:13:05.214247+00:00 | 2026-05-17T00:00:00 |
| api\model_artifacts\MLS.json | MLS | 3711B | 6f9bcd701e82 | 2026-08-26T18:50:48.013477+00:00 | 2026-08-26T18:50:48.027123+00:00 | 2026-08-23T00:00:00 |
| api\model_artifacts\PremierLeague.json | PremierLeague | 4400B | c033f81c4b83 | 2026-08-28T21:48:34.437846+00:00 | 2026-08-03T19:13:06.259457+00:00 | 2026-05-24T00:00:00 |
| api\model_artifacts\PrimeiraLiga.json | PrimeiraLiga | 2944B | 2960fa31eaf6 | 2026-08-26T17:16:54.314060+00:00 | 2026-08-26T17:16:54.328814+00:00 | 2026-05-16T00:00:00 |
| api\model_artifacts\SaudiProLeague.json | SaudiProLeague | 3169B | 92377c91447d | 2026-08-26T18:50:48.813728+00:00 | 2026-08-26T18:50:48.814733+00:00 | 2026-08-26T00:00:00 |
| api\model_artifacts\SerieA.json | SerieA | 3096B | df6b3c4e5000 | 2026-08-08T22:23:27.960873+00:00 | 2026-08-03T19:13:07.414380+00:00 | 2026-05-24T00:00:00 |

## 4. Feature Set Inventory


Réutilise Phase 8A (Feature Registry, jamais modifié) : {'production': 34, 'experimental': 1, 'missing': 11, 'total': 46}

## 5. Calibration Inventory

| Model Version | Type | Method | created_at | Availability |
|---|---|---|---|---|
| 1 | xgboost | none_persisted | None | CALIBRATION_MISSING |
| 2 | lightgbm | none_persisted | None | CALIBRATION_MISSING |
| 3 | elo | None | None | CALIBRATION_MISSING |
| 4 | elo | None | None | CALIBRATION_MISSING |
| 5 | xgboost | none_persisted | None | CALIBRATION_MISSING |
| 6 | lightgbm | none_persisted | None | CALIBRATION_MISSING |
| 7 | elo | elo_ordered_logit | 2026-08-18 21:02:38.713611 | AVAILABLE |
| 8 | ensemble | None | None | NOT_AVAILABLE |
| 9 | dixon_coles | n/a_probability_is_the_model | None | NOT_AVAILABLE |
| 10 | ensemble | None | None | NOT_AVAILABLE |
| 11 | elo | elo_ordered_logit | 2026-08-18 22:46:30.306489 | AVAILABLE |
| 12 | xgboost | none_persisted | None | CALIBRATION_MISSING |
| 13 | lightgbm | none_persisted | None | CALIBRATION_MISSING |
| 14 | ensemble | None | None | NOT_AVAILABLE |
| 15 | ensemble | None | None | NOT_AVAILABLE |

## 6. Point-in-Time Rules


trained_at <= as_of requis (§5) ; timestamp absent -> UNKNOWN, jamais SAFE (§10) ; voir api/app/ai/historical/eligibility.py.

## 7. Replay Eligibility


**Preuve exhaustive** (§25/§26) : {'proven': True, 'latest_match_date': '2026-05-24T00:00:00', 'earliest_model_trained_at': '2026-08-16T22:21:41.386351', 'conclusion': 'PROUVÉ : la ModelVersion la plus ANCIENNE (trained_at le plus bas) a été entraînée APRÈS le match le plus RÉCENT de toute la base — donc TOUTE paire (match, ModelVersion) de ce dépôt échoue le gate §5 (MODEL_TRAINED_AFTER_AS_OF), sans exception, sans avoir besoin de tester chaque paire individuellement.'}

## 8. 20-Match Sample

| Match | Model | model_trained_at | Feature Set | Calibration | Verdict | Reasons |
|---|---|---|---|---|---|---|
| LaLiga:2026-05-24:Villarreal-Ath Madrid | elo | 2026-08-18T19:46:13.344919 | PRODUCTION | RESEARCH_WITHOUT_CALIBRATION | NOT_REPLAYABLE | ['MODEL_TRAINED_AFTER_AS_OF'] |
| LaLiga:2026-05-24:Villarreal-Ath Madrid | elo | 2026-08-18T21:02:38.713611 | PRODUCTION | RESEARCH_WITHOUT_CALIBRATION | NOT_REPLAYABLE | ['MODEL_TRAINED_AFTER_AS_OF'] |
| LaLiga:2026-05-24:Villarreal-Ath Madrid | elo | 2026-08-18T22:46:30.306489 | PRODUCTION | RESEARCH_WITHOUT_CALIBRATION | NOT_REPLAYABLE | ['MODEL_TRAINED_AFTER_AS_OF'] |
| LaLiga:2026-05-24:Villarreal-Ath Madrid | ensemble | 2026-08-18T21:53:10.432035 | PRODUCTION | RESEARCH_WITHOUT_CALIBRATION | NOT_REPLAYABLE | ['MODEL_TRAINED_AFTER_AS_OF'] |
| LaLiga:2026-05-24:Villarreal-Ath Madrid | ensemble | 2026-08-18T22:40:28.176435 | PRODUCTION | RESEARCH_WITHOUT_CALIBRATION | NOT_REPLAYABLE | ['MODEL_TRAINED_AFTER_AS_OF'] |
| LaLiga:2026-05-24:Villarreal-Ath Madrid | ensemble | 2026-08-18T23:17:55.126590 | PRODUCTION | RESEARCH_WITHOUT_CALIBRATION | NOT_REPLAYABLE | ['MODEL_TRAINED_AFTER_AS_OF'] |
| LaLiga:2026-05-24:Villarreal-Ath Madrid | lightgbm | 2026-08-18T20:21:42.585912 | PRODUCTION | RESEARCH_WITHOUT_CALIBRATION | NOT_REPLAYABLE | ['MODEL_TRAINED_AFTER_AS_OF'] |
| LaLiga:2026-05-24:Villarreal-Ath Madrid | lightgbm | 2026-08-18T23:16:38.168281 | PRODUCTION | RESEARCH_WITHOUT_CALIBRATION | NOT_REPLAYABLE | ['MODEL_TRAINED_AFTER_AS_OF'] |
| LaLiga:2026-05-24:Villarreal-Ath Madrid | xgboost | 2026-08-18T20:21:37.040996 | PRODUCTION | RESEARCH_WITHOUT_CALIBRATION | NOT_REPLAYABLE | ['MODEL_TRAINED_AFTER_AS_OF'] |
| LaLiga:2026-05-24:Villarreal-Ath Madrid | xgboost | 2026-08-18T23:16:32.790666 | PRODUCTION | RESEARCH_WITHOUT_CALIBRATION | NOT_REPLAYABLE | ['MODEL_TRAINED_AFTER_AS_OF'] |
| PremierLeague:2026-05-24:Brighton-Man United | elo | 2026-08-18T19:46:13.344919 | PRODUCTION | RESEARCH_WITHOUT_CALIBRATION | NOT_REPLAYABLE | ['MODEL_TRAINED_AFTER_AS_OF'] |
| PremierLeague:2026-05-24:Brighton-Man United | elo | 2026-08-18T21:02:38.713611 | PRODUCTION | RESEARCH_WITHOUT_CALIBRATION | NOT_REPLAYABLE | ['MODEL_TRAINED_AFTER_AS_OF'] |
| PremierLeague:2026-05-24:Brighton-Man United | elo | 2026-08-18T22:46:30.306489 | PRODUCTION | RESEARCH_WITHOUT_CALIBRATION | NOT_REPLAYABLE | ['MODEL_TRAINED_AFTER_AS_OF'] |
| PremierLeague:2026-05-24:Brighton-Man United | ensemble | 2026-08-18T21:53:10.432035 | PRODUCTION | RESEARCH_WITHOUT_CALIBRATION | NOT_REPLAYABLE | ['MODEL_TRAINED_AFTER_AS_OF'] |
| PremierLeague:2026-05-24:Brighton-Man United | ensemble | 2026-08-18T22:40:28.176435 | PRODUCTION | RESEARCH_WITHOUT_CALIBRATION | NOT_REPLAYABLE | ['MODEL_TRAINED_AFTER_AS_OF'] |
| PremierLeague:2026-05-24:Brighton-Man United | ensemble | 2026-08-18T23:17:55.126590 | PRODUCTION | RESEARCH_WITHOUT_CALIBRATION | NOT_REPLAYABLE | ['MODEL_TRAINED_AFTER_AS_OF'] |
| PremierLeague:2026-05-24:Brighton-Man United | lightgbm | 2026-08-18T20:21:42.585912 | PRODUCTION | RESEARCH_WITHOUT_CALIBRATION | NOT_REPLAYABLE | ['MODEL_TRAINED_AFTER_AS_OF'] |
| PremierLeague:2026-05-24:Brighton-Man United | lightgbm | 2026-08-18T23:16:38.168281 | PRODUCTION | RESEARCH_WITHOUT_CALIBRATION | NOT_REPLAYABLE | ['MODEL_TRAINED_AFTER_AS_OF'] |
| PremierLeague:2026-05-24:Brighton-Man United | xgboost | 2026-08-18T20:21:37.040996 | PRODUCTION | RESEARCH_WITHOUT_CALIBRATION | NOT_REPLAYABLE | ['MODEL_TRAINED_AFTER_AS_OF'] |
| PremierLeague:2026-05-24:Brighton-Man United | xgboost | 2026-08-18T23:16:32.790666 | PRODUCTION | RESEARCH_WITHOUT_CALIBRATION | NOT_REPLAYABLE | ['MODEL_TRAINED_AFTER_AS_OF'] |

## 9. Full Dataset Coverage


- Total matchs : 12459 — Total ModelVersions : 15 — Paires évaluées : 186885
- Verdicts : {'REPLAYABLE': 0, 'NOT_REPLAYABLE': 186885, 'PARTIAL': 0, 'UNKNOWN': 0}
- Raisons de rejet : {'MODEL_TRAINED_AFTER_AS_OF': 186885}
- **replay_coverage = 0.0** (ok)

Par ligue : {'Bundesliga': {'REPLAYABLE': 0, 'NOT_REPLAYABLE': 32130, 'PARTIAL': 0, 'UNKNOWN': 0}, 'LaLiga': {'REPLAYABLE': 0, 'NOT_REPLAYABLE': 39900, 'PARTIAL': 0, 'UNKNOWN': 0}, 'Ligue1': {'REPLAYABLE': 0, 'NOT_REPLAYABLE': 35055, 'PARTIAL': 0, 'UNKNOWN': 0}, 'PremierLeague': {'REPLAYABLE': 0, 'NOT_REPLAYABLE': 39900, 'PARTIAL': 0, 'UNKNOWN': 0}, 'SerieA': {'REPLAYABLE': 0, 'NOT_REPLAYABLE': 39900, 'PARTIAL': 0, 'UNKNOWN': 0}}

Par modèle : {'xgboost': {'REPLAYABLE': 0, 'NOT_REPLAYABLE': 37377, 'PARTIAL': 0, 'UNKNOWN': 0}, 'lightgbm': {'REPLAYABLE': 0, 'NOT_REPLAYABLE': 37377, 'PARTIAL': 0, 'UNKNOWN': 0}, 'elo': {'REPLAYABLE': 0, 'NOT_REPLAYABLE': 49836, 'PARTIAL': 0, 'UNKNOWN': 0}, 'ensemble': {'REPLAYABLE': 0, 'NOT_REPLAYABLE': 49836, 'PARTIAL': 0, 'UNKNOWN': 0}, 'dixon_coles': {'REPLAYABLE': 0, 'NOT_REPLAYABLE': 12459, 'PARTIAL': 0, 'UNKNOWN': 0}}

## 10. Leakage Tests


22/22 tests api/test_historical_replay.py — cutoff (T-24h..T+1min), result/model/calibration/feature leakage, unknown timestamp, missing artifact : tous PASS.

## 11. Determinism


Vérifié (test_deterministic_snapshot_same_input_same_output) — même input -> même verdict/reasons.

## 12. Reproducibility


Vérifié (test_reproducibility_two_runs_identical) — 3 exécutions consécutives, résultats identiques.

## 13. Historical Replay Matrix


Résumé par modèle : {'xgboost': {'REPLAYABLE': 0, 'NOT_REPLAYABLE': 37377, 'PARTIAL': 0, 'UNKNOWN': 0}, 'lightgbm': {'REPLAYABLE': 0, 'NOT_REPLAYABLE': 37377, 'PARTIAL': 0, 'UNKNOWN': 0}, 'elo': {'REPLAYABLE': 0, 'NOT_REPLAYABLE': 49836, 'PARTIAL': 0, 'UNKNOWN': 0}, 'ensemble': {'REPLAYABLE': 0, 'NOT_REPLAYABLE': 49836, 'PARTIAL': 0, 'UNKNOWN': 0}, 'dixon_coles': {'REPLAYABLE': 0, 'NOT_REPLAYABLE': 12459, 'PARTIAL': 0, 'UNKNOWN': 0}}

## 14. Limitations


- Aucune ModelVersion actuelle n'est antérieure à l'historique `match` (voir §7 preuve exhaustive) — conséquence directe du fait que ce dépôt entier a été créé le 2026-08-03 (premier commit git), alors que l'historique `match` a été chargé en bloc depuis une source externe (dates jusqu'au 2026-05-24) — aucune trace git antérieure n'existe, aucun artefact antérieur n'a jamais existé dans ce dépôt.
- Aucune calibration Platt/Isotonic (Phase 6) n'est jamais persistée par ModelVersion — RESEARCH_WITHOUT_CALIBRATION est le seul contrat testable pour xgboost/lightgbm dans cette V1 (§36, documenté, jamais fabriqué).
- La 'calibration' Elo (config JSON c/scale) partage le même timestamp que trained_at — aucun instant de calibration distinct n'est jamais persisté séparément.

## 15. Database Safety


Before : {'match': 12459, 'match_stats': 12459, 'model_predictions': 3610, 'model_versions': 15, 'prediction_log': 9, 'team_ratings': 568}

After : {'match': 12459, 'match_stats': 12459, 'model_predictions': 3610, 'model_versions': 15, 'prediction_log': 9, 'team_ratings': 568}

Unchanged : **True**

## 16. Production Safety


Fichiers de production modifiés : **False**

`git diff --name-only` :
```

```

## 17. Verdict


**HISTORICAL_REPLAY_NOT_AVAILABLE**

Preuve exhaustive (§25/§26) : 186885 paires (match, ModelVersion) évaluées sur l'intégralité du dataset local — {'REPLAYABLE': 0, 'NOT_REPLAYABLE': 186885, 'PARTIAL': 0, 'UNKNOWN': 0} — 0 REPLAYABLE. Toutes les ModelVersion actuelles ont été (ré)entraînées après le dernier match connu (voir §7/§14). Un HISTORICAL_REPLAY_NOT_AVAILABLE honnête est préféré à un faux backtest (§51).

## 18. Recommendation


Ne pas tenter de contourner MODEL_TRAINED_AFTER_AS_OF. Si un replay historique point-in-time réel est nécessaire un jour, la seule voie honnête est de PERSISTER, dès maintenant, chaque nouvelle ModelVersion future avec `training_period_end` explicitement rempli (colonne déjà existante, jamais utilisée à ce jour — voir §14) — permettant, dans plusieurs mois, un premier replay réellement point-in-time sur les matchs postérieurs à CE run. Aucune reconstruction rétroactive des versions actuelles n'est possible ni souhaitable.

---

### EXISTING REGRESSION SUITES (§44)

| Suite | Return Code | Summary | Pass |
|---|---|---|---|
| test_model_selection.py | 0 | 17/17 tests OK | True |
| test_track_record.py | 0 | 23/23 tests OK | True |
| test_feature_registry.py | 0 | 21/21 tests OK | True |
| test_value_engine.py | 0 | 36/36 tests OK | True |
| test_decision_layer.py | 0 | 34/34 tests OK | True |
| test_end_to_end_pipeline.py | 0 | 21/21 tests OK | True |
| test_shadow_operational.py | 0 | 25/25 tests OK | True |
| test_historical_replay.py | 0 | 22/22 tests OK | True |

---

`git status --short` :
```
?? api/app/ai/decision/
?? api/app/ai/historical/
?? api/app/ai/odds_research/odds_api_trial.py
?? api/app/ai/odds_research/provider_audit.py
?? api/app/ai/pipeline/
?? api/app/ai/shadow/
?? api/app/ai/value/
?? api/test_decision_layer.py
?? api/test_end_to_end_pipeline.py
?? api/test_historical_replay.py
?? api/test_odds_api_trial.py
?? api/test_odds_provider_audit.py
?? api/test_shadow_operational.py
?? api/test_timestamped_odds_provider_audit.py
?? api/test_value_engine.py
?? reports/decision/
?? reports/odds_providers/
?? reports/pipeline/
?? reports/shadow/shadow_decision_store.json
?? reports/shadow/shadow_operational_validation_20260830.json
?? reports/shadow/shadow_operational_validation_20260830.md
?? reports/value_engine/
?? scripts/decision_layer_research.py
?? scripts/end_to_end_shadow_research.py
?? scripts/historical_replay_audit.py
?? scripts/odds_api_access_confirmation.py
?? scripts/odds_api_cost_audit.py
?? scripts/odds_api_smoke_test.py
?? scripts/odds_api_trial.py
?? scripts/shadow_operational_validation.py
?? scripts/timestamped_odds_provider_audit.py
?? scripts/value_engine_research.py

```

`git diff --stat` :
```

```

---

PHASE 8L — XFOOT HISTORICAL MODEL SNAPSHOT & REPLAY FOUNDATION V1 TERMINÉE. HISTORICAL REPLAY ÉVALUÉ AVEC INTÉGRITÉ POINT-IN-TIME. AUCUN MODÈLE HISTORIQUE FABRIQUÉ. AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. AUCUNE INTÉGRATION ODDS EFFECTUÉE. EN ATTENTE DE VALIDATION.
