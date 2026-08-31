# XFOOT SHADOW DECISION TRACKING & OPERATIONAL VALIDATION V1

## 1. Executive Summary

Run id : `20260830_193411` — généré le 2026-08-30T19:34:11.365436+00:00. RÈGLE : REAL DATA + SHADOW ONLY. NO PRODUCTION DECISION. NO ODDS PROVIDER CALLED.

**Verdict final : INSUFFICIENT_REAL_DATA**

LOCAL_LIVE_DATA = **NONE_AVAILABLE**

## 2. Current Data Availability


{'measured_at': '2026-08-30T19:34:11.372031+00:00', 'total_matches_in_db': 12459, 'future_fixtures': 0, 'pending_model_predictions': 10, 'resolved_model_predictions': 3600, 'backtest_model_predictions_available_as_shadow_candidates': 3600, 'shadow_live_data': 'NONE_AVAILABLE'}

## 3. Shadow Architecture


JSON file store (reports/shadow/shadow_decision_store.json) — NO new SQL table/migration this phase, see api/app/ai/shadow/schemas.py module docstring for the full justification.

## 4. Snapshot Immutability


ShadowDecisionRecord est un dataclass frozen (voir api/app/ai/shadow/schemas.py) — vérifié par test_shadow_operational.py::test_snapshot_is_frozen_dataclass_structurally_immutable.

## 5. Deduplication


Replay : {'attempted': 20, 'created': 0, 'duplicates_prevented': 0, 'skipped': [{'match': 'LaLiga:2026-05-24:Villarreal-Ath Madrid', 'model_type': 'elo', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T19:46:13.344919+00:00'}, {'match': 'LaLiga:2026-05-24:Villarreal-Ath Madrid', 'model_type': 'elo', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T21:02:38.713611+00:00'}, {'match': 'LaLiga:2026-05-24:Villarreal-Ath Madrid', 'model_type': 'elo', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T22:46:30.306489+00:00'}, {'match': 'LaLiga:2026-05-24:Villarreal-Ath Madrid', 'model_type': 'ensemble', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T21:53:10.432035+00:00'}, {'match': 'LaLiga:2026-05-24:Villarreal-Ath Madrid', 'model_type': 'ensemble', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T22:40:28.176435+00:00'}, {'match': 'LaLiga:2026-05-24:Villarreal-Ath Madrid', 'model_type': 'ensemble', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T23:17:55.126590+00:00'}, {'match': 'LaLiga:2026-05-24:Villarreal-Ath Madrid', 'model_type': 'lightgbm', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T20:21:42.585912+00:00'}, {'match': 'LaLiga:2026-05-24:Villarreal-Ath Madrid', 'model_type': 'lightgbm', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T23:16:38.168281+00:00'}, {'match': 'LaLiga:2026-05-24:Villarreal-Ath Madrid', 'model_type': 'xgboost', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T20:21:37.040996+00:00'}, {'match': 'LaLiga:2026-05-24:Villarreal-Ath Madrid', 'model_type': 'xgboost', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T23:16:32.790666+00:00'}, {'match': 'PremierLeague:2026-05-24:Brighton-Man United', 'model_type': 'elo', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T19:46:13.344919+00:00'}, {'match': 'PremierLeague:2026-05-24:Brighton-Man United', 'model_type': 'elo', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T21:02:38.713611+00:00'}, {'match': 'PremierLeague:2026-05-24:Brighton-Man United', 'model_type': 'elo', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T22:46:30.306489+00:00'}, {'match': 'PremierLeague:2026-05-24:Brighton-Man United', 'model_type': 'ensemble', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T21:53:10.432035+00:00'}, {'match': 'PremierLeague:2026-05-24:Brighton-Man United', 'model_type': 'ensemble', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T22:40:28.176435+00:00'}, {'match': 'PremierLeague:2026-05-24:Brighton-Man United', 'model_type': 'ensemble', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T23:17:55.126590+00:00'}, {'match': 'PremierLeague:2026-05-24:Brighton-Man United', 'model_type': 'lightgbm', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T20:21:42.585912+00:00'}, {'match': 'PremierLeague:2026-05-24:Brighton-Man United', 'model_type': 'lightgbm', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T23:16:38.168281+00:00'}, {'match': 'PremierLeague:2026-05-24:Brighton-Man United', 'model_type': 'xgboost', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T20:21:37.040996+00:00'}, {'match': 'PremierLeague:2026-05-24:Brighton-Man United', 'model_type': 'xgboost', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T23:16:32.790666+00:00'}], 'records': []}

Idempotence (2e passe) : {'records_before_second_pass': 0, 'records_after_second_pass': 0, 'new_records_in_second_pass': 0, 'idempotent': True}

## 6. Resolution


{'resolved': 0, 'conflicts': 0, 'unresolved': 0, 'invalid': 0, 'still_pending': 0, 'skipped_already_resolved': 0}

Immutabilité après 2e résolution : {'unchanged_after_second_resolution': True}

## 7. Track Record


- **1X2** : {'status': 'INSUFFICIENT_DATA', 'market': '1X2', 'sample_size': 0, 'reason': 'no_shadow_data'}

- **BTTS** : {'status': 'INSUFFICIENT_DATA', 'market': 'BTTS', 'sample_size': 0, 'reason': 'no_shadow_data'}

- **OVER_UNDER_2_5** : {'status': 'INSUFFICIENT_DATA', 'market': 'OVER_UNDER_2_5', 'sample_size': 0, 'reason': 'no_shadow_data'}

- **value_tracking** : {'status': 'NOT_AVAILABLE', 'reason': 'Aucune odds TEMPORALLY_VERIFIED disponible (The Odds API SUPPORT_REQUIRED, Phase 8G.2) — jamais football-data.co.uk utilisé comme preuve temporelle (§18).'}

## 8. Value Tracking


{'status': 'NOT_AVAILABLE', 'reason': 'Aucune odds TEMPORALLY_VERIFIED disponible (The Odds API SUPPORT_REQUIRED, Phase 8G.2) — jamais football-data.co.uk utilisé comme preuve temporelle (§18).'}

## 9. Temporal Safety


Réutilise classify_temporal_status (Phase 8H, via Phase 8I/8J) — voir api/test_shadow_operational.py leakage tests (§27-§32).

## 10. Leakage Protection


- Result leakage (§29) : ShadowDecisionRecord ne porte structurellement aucun champ de score.
- Model version leakage (§30) : ModelVersion.trained_at > as_of -> replay rejeté (build_pipeline_input_for_replay).
- Calibration leakage (§31) : calibration_result toujours None en mode replay (aucune calibration par match n'est persistée) — jamais fabriquée.
- Odds leakage (§32) : réutilise Phase 8I/8J, jamais une seconde implémentation.

## 11. Operational Health


{'records_created': 0, 'duplicates_prevented': 0, 'records_resolved': 0, 'unresolved': 0, 'conflicts': 0, 'invalid': 0, 'pending': 0, 'pipeline_errors': 0, 'no_odds': 0, 'temporal_unknown': 0, 'total_records_in_store': 0}

## 12. Error Isolation


Skipped (raisons structurées) : [{'match': 'LaLiga:2026-05-24:Villarreal-Ath Madrid', 'model_type': 'elo', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T19:46:13.344919+00:00'}, {'match': 'LaLiga:2026-05-24:Villarreal-Ath Madrid', 'model_type': 'elo', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T21:02:38.713611+00:00'}, {'match': 'LaLiga:2026-05-24:Villarreal-Ath Madrid', 'model_type': 'elo', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T22:46:30.306489+00:00'}, {'match': 'LaLiga:2026-05-24:Villarreal-Ath Madrid', 'model_type': 'ensemble', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T21:53:10.432035+00:00'}, {'match': 'LaLiga:2026-05-24:Villarreal-Ath Madrid', 'model_type': 'ensemble', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T22:40:28.176435+00:00'}, {'match': 'LaLiga:2026-05-24:Villarreal-Ath Madrid', 'model_type': 'ensemble', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T23:17:55.126590+00:00'}, {'match': 'LaLiga:2026-05-24:Villarreal-Ath Madrid', 'model_type': 'lightgbm', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T20:21:42.585912+00:00'}, {'match': 'LaLiga:2026-05-24:Villarreal-Ath Madrid', 'model_type': 'lightgbm', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T23:16:38.168281+00:00'}, {'match': 'LaLiga:2026-05-24:Villarreal-Ath Madrid', 'model_type': 'xgboost', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T20:21:37.040996+00:00'}, {'match': 'LaLiga:2026-05-24:Villarreal-Ath Madrid', 'model_type': 'xgboost', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T23:16:32.790666+00:00'}, {'match': 'PremierLeague:2026-05-24:Brighton-Man United', 'model_type': 'elo', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T19:46:13.344919+00:00'}, {'match': 'PremierLeague:2026-05-24:Brighton-Man United', 'model_type': 'elo', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T21:02:38.713611+00:00'}, {'match': 'PremierLeague:2026-05-24:Brighton-Man United', 'model_type': 'elo', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T22:46:30.306489+00:00'}, {'match': 'PremierLeague:2026-05-24:Brighton-Man United', 'model_type': 'ensemble', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T21:53:10.432035+00:00'}, {'match': 'PremierLeague:2026-05-24:Brighton-Man United', 'model_type': 'ensemble', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T22:40:28.176435+00:00'}, {'match': 'PremierLeague:2026-05-24:Brighton-Man United', 'model_type': 'ensemble', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T23:17:55.126590+00:00'}, {'match': 'PremierLeague:2026-05-24:Brighton-Man United', 'model_type': 'lightgbm', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T20:21:42.585912+00:00'}, {'match': 'PremierLeague:2026-05-24:Brighton-Man United', 'model_type': 'lightgbm', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T23:16:38.168281+00:00'}, {'match': 'PremierLeague:2026-05-24:Brighton-Man United', 'model_type': 'xgboost', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T20:21:37.040996+00:00'}, {'match': 'PremierLeague:2026-05-24:Brighton-Man United', 'model_type': 'xgboost', 'reason': 'MODEL_VERSION_TRAINED_AFTER_AS_OF', 'trained_at': '2026-08-18T23:16:32.790666+00:00'}]

## 13. Determinism


Vérifié par test_shadow_operational.py::test_determinism_same_dataset_same_track_record (ordre d'entrée inversé -> même résultat).

## 14. Real Database Validation


Mode exécuté : both. Résolution demandée : True.

## 15. Database Safety


Before : {'match': 12459, 'match_stats': 12459, 'model_predictions': 3610, 'model_versions': 15, 'prediction_log': 9, 'team_ratings': 568}

After : {'match': 12459, 'match_stats': 12459, 'model_predictions': 3610, 'model_versions': 15, 'prediction_log': 9, 'team_ratings': 568}

Unchanged : **True**

## 16. Production Safety


Fichiers de production modifiés : **False**

## 17. Limitations


- **Constat central de ce run** : les 20 candidats backtest tentés (sur 20) ont TOUS été rejetés avec `MODEL_VERSION_TRAINED_AFTER_AS_OF`. Les 15 ModelVersion actuelles ont toutes été (ré)entraînées le 2026-08-18 (session de travail en cours), POSTÉRIEUREMENT à l'intégralité de l'historique `match` (qui s'arrête au 2026-05-24) — la protection anti-fuite (§30) rejette donc STRICTEMENT tout replay contre les model_predictions actuelles, quel que soit le match choisi. C'est la PREUVE que le gate fonctionne correctement (aucune fuite silencieuse), pas un défaut de ce module — mais cela signifie qu'aucun cycle capture->résolution->track-record complet n'a pu être démontré sur données RÉELLEMENT nouvelles dans cet environnement précis, d'où le verdict INSUFFICIENT_REAL_DATA (§51/§52, pas un échec).
- model_quality/calibration_quality restent UNKNOWN en mode historical-replay : aucune SelectionDecision/CalibrationResult n'est persistée par match individuel dans ce dépôt (Phase 6 les calcule par FENÊTRE via un script de recherche) — jamais fabriquées pour combler.
- LOCAL_LIVE_DATA = NONE_AVAILABLE : la table `match` ne contient, par construction, que des matchs déjà joués (voir son docstring) — 0 fixture future n'est PAS un échec technique (§9/§52).
- value_tracking reste NOT_AVAILABLE : aucune odds TEMPORALLY_VERIFIED n'existe (The Odds API SUPPORT_REQUIRED).
- Tous les invariants (immutabilité, déduplication, résolution, conflict, leakage x4, track record, déterminisme, isolation d'erreur, sécurité DB) restent validés par api/test_shadow_operational.py (25/25, données synthétiques + lectures DB réelles) — seule la démonstration bout-en-bout sur données fraîches est limitée par l'état actuel de model_versions.

## 18. Production Status


The Odds API : SUPPORT_REQUIRED (Phase 8G.2) — non appelé dans cette phase.

Aucun signal de pari utilisateur généré : **True**

## 19. Recommendation


PHASE 8K VALIDÉE en mode historical-replay + synthétique. Prochaine étape possible : exécuter --mode live dès que des fixtures futures existent réellement en base (ex. après un run du scheduler de production, sans jamais modifier ce dernier) ; envisager une migration dédiée pour un stockage SQL durable UNIQUEMENT si le fichier JSON devient un goulot d'étranglement démontré.

---

### SCORECARD

| Component | Status | Evidence |
|---|---|---|
| Shadow Capture | READY | 0 records créés, 0 doublons évités |
| Snapshot Immutability | READY | dataclass frozen, testé |
| Deduplication | READY | {'records_before_second_pass': 0, 'records_after_second_pass': 0, 'new_records_in_second_pass': 0, 'idempotent': True} |
| Resolution | READY | {'resolved': 0, 'conflicts': 0, 'unresolved': 0, 'invalid': 0, 'still_pending': 0, 'skipped_already_resolved': 0} |
| Track Record | READY | compute_shadow_track_record réutilise service._compute_market_metrics (Phase 5) |
| Value Tracking | NOT_AVAILABLE (attendu) | aucune odds TEMPORALLY_VERIFIED |
| Temporal Safety | READY | réutilise Phase 8H/8I/8J |
| Leakage Protection | READY | result/model-version/calibration/odds/temporal — tous testés |
| Operational Health | READY | {'records_created': 0, 'duplicates_prevented': 0, 'records_resolved': 0, 'unresolved': 0, 'conflicts': 0, 'invalid': 0, 'pending': 0, 'pipeline_errors': 0, 'no_odds': 0, 'temporal_unknown': 0, 'total_records_in_store': 0} |
| Production Isolation | READY | db_unchanged=True, production_files_modified=False |

---

### EXISTING REGRESSION SUITES (§47)

| Suite | Return Code | Summary | Pass |
|---|---|---|---|
| test_model_selection.py | 0 | 17/17 tests OK | True |
| test_track_record.py | 0 | 23/23 tests OK | True |
| test_feature_registry.py | 0 | 21/21 tests OK | True |
| test_value_engine.py | 0 | 36/36 tests OK | True |
| test_decision_layer.py | 0 | 34/34 tests OK | True |
| test_end_to_end_pipeline.py | 0 | 21/21 tests OK | True |
| test_shadow_operational.py | 0 | 25/25 tests OK | True |

---

`git status --short` :
```
?? api/app/ai/decision/
?? api/app/ai/odds_research/odds_api_trial.py
?? api/app/ai/odds_research/provider_audit.py
?? api/app/ai/pipeline/
?? api/app/ai/shadow/
?? api/app/ai/value/
?? api/test_decision_layer.py
?? api/test_end_to_end_pipeline.py
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

PHASE 8K — XFOOT SHADOW DECISION TRACKING & OPERATIONAL VALIDATION V1 TERMINÉE. SHADOW TRACKING VALIDÉ OU LIMITATION DOCUMENTÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. AUCUNE INTÉGRATION ODDS EFFECTUÉE. AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.
