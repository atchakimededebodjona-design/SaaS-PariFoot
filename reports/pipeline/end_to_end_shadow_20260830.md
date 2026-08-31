# XFOOT END-TO-END SHADOW DECISION PIPELINE V1

## 1. Executive Summary

Run id : `20260830_191546` — généré le 2026-08-30T19:15:46.319644+00:00. RÈGLE : RESEARCH + SHADOW ONLY. NO PRODUCTION INTEGRATION. NO ODDS PROVIDER CALLED.

Tests verts : **True**. Verdict final : **END_TO_END_SHADOW_READY**

## 2. Architecture


- Package : `api/app/ai/pipeline/` — modules : ['__init__.py', 'schemas.py', 'orchestrator.py', 'shadow.py']
- Tests : `api/test_end_to_end_pipeline.py`
- Appelé par la production : **False**

Réutilisé, jamais réimplémenté :
- app.ai.decision.decision.assess_decision (Phase 8I) — Quality+Decision en un seul appel
- app.ai.value.core.build_value_signal (Phase 8H) — Value stage
- app.ai.arena.service._compute_market_metrics (Phase 5) — preuve de compatibilité Track Record

## 3. Pipeline Stages


Prediction (input) → Quality (Phase 8I, via assess_decision) → Decision (Phase 8I) → Value (Phase 8H, conditionnel) → Final Status (orchestrateur)

## 4. Quality Propagation


Un seul appel à `assess_decision()` (Phase 8I) produit à la fois `quality` (PredictionConfidence) et `decision` (DecisionAssessment) — aucune logique de qualité parallèle (§7 du prompt).

## 5. Decision Propagation


{'match_id': 1, 'market': '1X2', 'final_status': 'INELIGIBLE', 'decision_eligibility': 'INELIGIBLE', 'confidence_overall': 'INELIGIBLE', 'value_status': None, 'value_stage_status': 'SKIPPED_DECISION_INELIGIBLE', 'reasons': ['TEMPORAL_UNKNOWN'], 'error': None, 'pass': True}

## 6. Value Propagation


Statuts d'étape Value : ['EVALUATED', 'SKIPPED_NO_ODDS', 'SKIPPED_DECISION_INELIGIBLE', 'SKIPPED_DECISION_INSUFFICIENT_DATA', 'SKIPPED_DECISION_UNKNOWN', 'SKIPPED_NO_MODEL_PROBABILITY']

Batch synthétique : [{'match_id': 1, 'market': '1X2', 'final_status': 'VALUE_CANDIDATE', 'decision_eligibility': 'ELIGIBLE', 'confidence_overall': 'HIGH', 'value_status': 'POSITIVE_VALUE', 'value_stage_status': 'EVALUATED', 'reasons': [], 'error': None}, {'match_id': 2, 'market': 'BTTS', 'final_status': 'VALUE_CANDIDATE', 'decision_eligibility': 'ELIGIBLE', 'confidence_overall': 'HIGH', 'value_status': 'POSITIVE_VALUE', 'value_stage_status': 'EVALUATED', 'reasons': [], 'error': None}, {'match_id': 3, 'market': 'OVER_UNDER_2_5', 'final_status': 'VALUE_CANDIDATE', 'decision_eligibility': 'ELIGIBLE', 'confidence_overall': 'HIGH', 'value_status': 'POSITIVE_VALUE', 'value_stage_status': 'EVALUATED', 'reasons': [], 'error': None}]

## 7. Temporal Safety


- **future_information_favorable_ev** : {'match_id': 1, 'market': '1X2', 'final_status': 'INELIGIBLE', 'decision_eligibility': 'INELIGIBLE', 'confidence_overall': 'INELIGIBLE', 'value_status': None, 'value_stage_status': 'SKIPPED_DECISION_INELIGIBLE', 'reasons': ['FUTURE_INFORMATION'], 'error': None, 'pass': True}

- **unknown_timestamp** : {'match_id': 1, 'market': '1X2', 'final_status': 'INELIGIBLE', 'decision_eligibility': 'INELIGIBLE', 'confidence_overall': 'INELIGIBLE', 'value_status': None, 'value_stage_status': 'SKIPPED_DECISION_INELIGIBLE', 'reasons': ['TEMPORAL_UNKNOWN'], 'error': None, 'pass': True}

- **historical_unverified** : {'match_id': 1, 'market': '1X2', 'final_status': 'RESEARCH_ONLY', 'decision_eligibility': 'RESEARCH_ONLY', 'confidence_overall': 'MEDIUM', 'value_status': 'POSITIVE_VALUE', 'value_stage_status': 'EVALUATED', 'reasons': ['HISTORICAL_UNVERIFIED'], 'error': None, 'pass': True}

## 8. Probability Validation


{'match_id': 1, 'market': '1X2', 'final_status': 'INELIGIBLE', 'decision_eligibility': 'INELIGIBLE', 'confidence_overall': 'HIGH', 'value_status': None, 'value_stage_status': 'SKIPPED_DECISION_INELIGIBLE', 'reasons': ['INVALID_PROBABILITY'], 'error': None, 'pass': True}

## 9. Odds Validation


- **invalid_odds_1.0** : {'match_id': 1, 'market': '1X2', 'final_status': 'INSUFFICIENT_DATA', 'decision_eligibility': 'INSUFFICIENT_DATA', 'confidence_overall': 'HIGH', 'value_status': None, 'value_stage_status': 'SKIPPED_DECISION_INSUFFICIENT_DATA', 'reasons': ['MARKET_UNAVAILABLE'], 'error': None, 'pass': True}

- **invalid_odds_0.0** : {'match_id': 1, 'market': '1X2', 'final_status': 'INSUFFICIENT_DATA', 'decision_eligibility': 'INSUFFICIENT_DATA', 'confidence_overall': 'HIGH', 'value_status': None, 'value_stage_status': 'SKIPPED_DECISION_INSUFFICIENT_DATA', 'reasons': ['MARKET_UNAVAILABLE'], 'error': None, 'pass': True}

- **invalid_odds_-1.5** : {'match_id': 1, 'market': '1X2', 'final_status': 'INSUFFICIENT_DATA', 'decision_eligibility': 'INSUFFICIENT_DATA', 'confidence_overall': 'HIGH', 'value_status': None, 'value_stage_status': 'SKIPPED_DECISION_INSUFFICIENT_DATA', 'reasons': ['MARKET_UNAVAILABLE'], 'error': None, 'pass': True}

Cas sans odds : {'summary': {'match_id': 1, 'market': '1X2', 'final_status': 'INELIGIBLE', 'decision_eligibility': 'INELIGIBLE', 'confidence_overall': 'INELIGIBLE', 'value_status': None, 'value_stage_status': 'SKIPPED_NO_ODDS', 'reasons': ['TEMPORAL_UNKNOWN'], 'error': None}, 'value_is_none': True, 'never_value_candidate': True, 'no_exception_raised': True}

## 10. Provenance


{'expected': {'model_source': 'xgboost', 'model_version': 'xfoot-xgboost-v1', 'calibration_source': 'isotonic', 'feature_snapshot': 'snapshot-id', 'odds_source': 'SYNTHETIC', 'odds_timestamp': datetime.datetime(2026, 1, 1, 6, 0, tzinfo=datetime.timezone.utc), 'cutoff_timestamp': datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)}, 'got': {'model_source': 'xgboost', 'model_version': 'xfoot-xgboost-v1', 'calibration_source': 'isotonic', 'feature_snapshot': 'snapshot-id', 'odds_source': 'SYNTHETIC', 'odds_timestamp': datetime.datetime(2026, 1, 1, 6, 0, tzinfo=datetime.timezone.utc), 'cutoff_timestamp': datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)}, 'intact': True}

## 11. Error Isolation


{'results': [{'match_id': 10, 'market': '1X2', 'final_status': 'VALUE_CANDIDATE', 'decision_eligibility': 'ELIGIBLE', 'confidence_overall': 'HIGH', 'value_status': 'POSITIVE_VALUE', 'value_stage_status': 'EVALUATED', 'reasons': [], 'error': None}, {'match_id': 11, 'market': '1X2', 'final_status': 'REJECTED', 'decision_eligibility': None, 'confidence_overall': None, 'value_status': None, 'value_stage_status': 'SKIPPED_NO_ODDS', 'reasons': ['PIPELINE_ERROR: ValueError("PipelineInput.as_of (ou kickoff) doit être fourni explicitement — jamais déduit de l\'heure système (§17/§28/§44 du prompt Phase 8J).")'], 'error': 'ValueError("PipelineInput.as_of (ou kickoff) doit être fourni explicitement — jamais déduit de l\'heure système (§17/§28/§44 du prompt Phase 8J).")'}, {'match_id': 12, 'market': '1X2', 'final_status': 'VALUE_CANDIDATE', 'decision_eligibility': 'ELIGIBLE', 'confidence_overall': 'HIGH', 'value_status': 'POSITIVE_VALUE', 'value_stage_status': 'EVALUATED', 'reasons': [], 'error': None}], 'a_ok': True, 'b_isolated': True, 'c_ok': True}

## 12. Synthetic Tests

| Case | Market | Final Status | Value Status |
|---|---|---|---|
| match_id=1 | 1X2 | VALUE_CANDIDATE | POSITIVE_VALUE |
| match_id=2 | BTTS | VALUE_CANDIDATE | POSITIVE_VALUE |
| match_id=3 | OVER_UNDER_2_5 | VALUE_CANDIDATE | POSITIVE_VALUE |

## 13. Adversarial Tests

| Case | Final Status | Pass |
|---|---|---|
| invalid_odds_1.0 | INSUFFICIENT_DATA | True |
| invalid_odds_0.0 | INSUFFICIENT_DATA | True |
| invalid_odds_-1.5 | INSUFFICIENT_DATA | True |
| invalid_probability_sum_1_40 | INELIGIBLE | True |
| future_information_favorable_ev | INELIGIBLE | True |
| unknown_timestamp | INELIGIBLE | True |
| historical_unverified | RESEARCH_ONLY | True |

Tous PASS : **True**

## 14. Determinism


{'same_input_same_output': True}

## 15. Track Record Compatibility


{'observation_shape': ['p_true', 'probs', 'actual', 'correct'], 'compatible_with_service_compute_market_metrics': True, 'limitation': "compute_track_record/compute_cumulative_track_record/compute_selection_distribution/compute_stability_tracking/compute_calibration_tracking (Phase 7) interrogent TOUTES directement shadow_selection_predictions via une Session SQLModel — aucune n'accepte une liste d'observations externes. Une intégration complète nécessiterait d'écrire dans shadow_selection_predictions (mécanisme réel : scripts/model_selection_shadow.py), explicitement hors périmètre de cette phase (aucune écriture production). Compatibilité prouvée au niveau de la FORME uniquement."}

## 16. Database Safety


Before : {'match': 12459, 'match_stats': 12459, 'model_predictions': 3610, 'model_versions': 15, 'prediction_log': 9, 'team_ratings': 568}

After : {'match': 12459, 'match_stats': 12459, 'model_predictions': 3610, 'model_versions': 15, 'prediction_log': 9, 'team_ratings': 568}

Unchanged : **True**

## 17. Production Safety


Aucune écriture production. Aucun fichier de production modifié — voir §40 (git diff --stat).

## 18. Limitations


- compute_track_record/compute_cumulative_track_record/compute_selection_distribution/compute_stability_tracking/compute_calibration_tracking (Phase 7) interrogent TOUTES directement shadow_selection_predictions via une Session SQLModel — aucune n'accepte une liste d'observations externes. Une intégration complète nécessiterait d'écrire dans shadow_selection_predictions (mécanisme réel : scripts/model_selection_shadow.py), explicitement hors périmètre de cette phase (aucune écriture production). Compatibilité prouvée au niveau de la FORME uniquement.
- Sans odds, temporal_quality (Phase 8I, réutilisée telle quelle) retourne UNKNOWN faute de référence temporelle -> Decision devient INELIGIBLE même si le modèle/data/sample sont excellents — c'est la règle stricte de Phase 8I appliquée honnêtement, jamais contournée par ce pipeline (voir §10/§21 du rapport).
- Une cote invalide peut faire échouer GATE_MARKET (Phase 8I) avant même que build_value_signal (Phase 8H) ne soit atteint — les deux couches rejettent indépendamment, la Decision gagnant la course.

## 19. Production Status


The Odds API : SUPPORT_REQUIRED (Phase 8G.2) — non appelé dans cette phase.

Aucun signal de pari utilisateur généré : **True**

## 20. Recommendation


PHASE 8J VALIDÉE. Le pipeline Quality->Decision->Value est traçable, déterministe et sûr en RECHERCHE/SHADOW. Prochaine étape possible : persister des PipelineAssessment réels (matchs déjà résolus) dans shadow_selection_predictions via scripts/model_selection_shadow.py pour un vrai Track Record — hors périmètre de cette phase (aucune écriture production ici).

---

### EXISTING REGRESSION SUITES (§39)

| Suite | Return Code | Summary | Pass |
|---|---|---|---|
| test_model_selection.py | 0 | 17/17 tests OK | True |
| test_track_record.py | 0 | 23/23 tests OK | True |
| test_feature_registry.py | 0 | 21/21 tests OK | True |
| test_value_engine.py | 0 | 36/36 tests OK | True |
| test_decision_layer.py | 0 | 34/34 tests OK | True |
| test_end_to_end_pipeline.py | 0 | 21/21 tests OK | True |

---

### GIT (§40/§46)


`git status --short` :
```
?? api/app/ai/decision/
?? api/app/ai/odds_research/odds_api_trial.py
?? api/app/ai/odds_research/provider_audit.py
?? api/app/ai/pipeline/
?? api/app/ai/value/
?? api/test_decision_layer.py
?? api/test_end_to_end_pipeline.py
?? api/test_odds_api_trial.py
?? api/test_odds_provider_audit.py
?? api/test_timestamped_odds_provider_audit.py
?? api/test_value_engine.py
?? reports/decision/
?? reports/odds_providers/
?? reports/value_engine/
?? scripts/decision_layer_research.py
?? scripts/end_to_end_shadow_research.py
?? scripts/odds_api_access_confirmation.py
?? scripts/odds_api_cost_audit.py
?? scripts/odds_api_smoke_test.py
?? scripts/odds_api_trial.py
?? scripts/timestamped_odds_provider_audit.py
?? scripts/value_engine_research.py

```

`git diff --stat` :
```

```

Fichiers de production modifiés : **False**

---

PHASE 8J — XFOOT END-TO-END SHADOW DECISION PIPELINE V1 TERMINÉE. QUALITY → DECISION → VALUE → SHADOW VALIDÉS. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. AUCUNE INTÉGRATION ODDS EFFECTUÉE. AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.
