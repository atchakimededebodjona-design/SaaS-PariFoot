# XFOOT SHADOW MONITORING & DATA QUALITY OPERATIONS V1

## 1. Executive Summary

Run id : `20260830_201511` — 2026-08-30T20:15:11.723122+00:00. as_of=2026-08-30T20:15:11.723127+00:00

**Verdict : INSUFFICIENT_REAL_DATA** — shadow health = **NO_DATA**

## 2. Current Data Availability

{'measured_at': '2026-08-30T20:15:11.729937+00:00', 'total_matches_in_db': 12459, 'future_fixtures': 0, 'pending_model_predictions': 10, 'resolved_model_predictions': 3600, 'backtest_model_predictions_available_as_shadow_candidates': 3600, 'shadow_live_data': 'NONE_AVAILABLE'}

## 3. Operational Health

status=NO_DATA, future_fixtures=0, pending_predictions=10, capturable=0, captured=0, errors=0

## 4. Fixture Coverage

{'future_fixtures': 0, 'pending_predictions': 10, 'coverage': None, 'status': 'NO_DATA'}

## 5. Prediction Coverage

{'future_fixtures': 0, 'pending_predictions': 10, 'coverage': None, 'status': 'NO_DATA'}

## 6. Shadow Capture

{'eligible': 0, 'captured': 0, 'coverage': None, 'status': 'INSUFFICIENT_DATA'} — missed: []

## 7. Temporal Integrity

{'temporal_safe': 0, 'temporal_unknown': 0, 'temporal_rejected': 0, 'late_capture': 0, 'future_information_attempts': 0, 'by_status': {'TEMPORALLY_VERIFIED': 0, 'HISTORICAL_UNVERIFIED': 0, 'FUTURE_INFORMATION': 0, 'UNKNOWN': 0}}

## 8. Provenance

{'complete': 0, 'partial': 0, 'missing': 0, 'total': 0}

## 9. Model Consistency

mismatches=0

## 10. Probability Consistency

mismatches=0

## 11. Decision Consistency

Identique à Model/Probability Consistency (Phase 8I déterministe sur les mêmes inputs, aucun mismatch de décision distinct possible sans l'un des deux ci-dessus).

## 12. Store Integrity

{'status': 'OK', 'valid_json': True, 'record_count': 0, 'duplicate_keys_found': 0, 'records_with_invalid_timestamps': 0}

## 13. Resolution Health

{'counts': {'PENDING': 0, 'RESOLVED': 0, 'CONFLICT': 0, 'UNRESOLVED': 0, 'INVALID': 0}, 'resolution_latency_hours': {'status': 'INSUFFICIENT_DATA'}}

Matchs joués toujours PENDING : []

## 14. Track Record Health

{'per_market': {'1X2': {'status': 'INSUFFICIENT_DATA', 'market': '1X2', 'sample_size': 0, 'reason': 'no_shadow_data'}, 'BTTS': {'status': 'INSUFFICIENT_DATA', 'market': 'BTTS', 'sample_size': 0, 'reason': 'no_shadow_data'}, 'OVER_UNDER_2_5': {'status': 'INSUFFICIENT_DATA', 'market': 'OVER_UNDER_2_5', 'sample_size': 0, 'reason': 'no_shadow_data'}}, 'maturity': 'NO_DATA', 'maturity_thresholds': {'EARLY_DATA': 10, 'TRACKING': 30, 'STATISTICALLY_INFORMATIVE': 100}}

## 15. Value Data Health

{'odds_available': False, 'no_odds_count': 0, 'odds_unverified_count': 0, 'odds_verified_count': 0, 'value_candidate_count': 0, 'value_tracking': {'status': 'NOT_AVAILABLE', 'reason': 'Aucune odds TEMPORALLY_VERIFIED disponible (The Odds API SUPPORT_REQUIRED, Phase 8G.2) — jamais football-data.co.uk utilisé comme preuve temporelle (§18).'}}

## 16. Feature Data Health

{'feature_registry': {'production': 34, 'experimental': 1, 'missing': 11}, 'shadow_records_with_low_data_quality': 0, 'shadow_records_with_unknown_data_quality': 0}

## 17. Calibration Health

Réutilise Phase 8L (aucune calibration persistée par ModelVersion pour xgboost/lightgbm — constaté, jamais recalibré ici).

## 18. Alerts

| Category | Severity | Message |
|---|---|---|
| NO_DATA | INFO | Aucune donnée prospective actuellement disponible pour évaluer le fonctionnement opérationnel du Shadow. |

## 19. Data Quality Scorecard

| Dimension | Status | Count | Evidence |
|---|---|---|---|
| Fixture Availability | NO_DATA | 0 | {'future_fixtures': 0, 'pending_predictions': 10, 'coverage': None, 'status': 'N |
| Prediction Coverage | NO_DATA | 10 | {'future_fixtures': 0, 'pending_predictions': 10, 'coverage': None, 'status': 'N |
| Shadow Capture | INSUFFICIENT_DATA | 0 | {'eligible': 0, 'captured': 0, 'coverage': None, 'status': 'INSUFFICIENT_DATA'} |
| Temporal Integrity | OK | 0 | {'temporal_safe': 0, 'temporal_unknown': 0, 'temporal_rejected': 0, 'late_captur |
| Provenance | OK | 0 | {'complete': 0, 'partial': 0, 'missing': 0, 'total': 0} |
| Model Consistency | OK | 0 | {'checked': 0, 'model_mismatches': 0, 'probability_mismatches': 0, 'details': [] |
| Probability Consistency | OK | 0 | {'checked': 0, 'model_mismatches': 0, 'probability_mismatches': 0, 'details': [] |
| Decision Consistency | OK (identique à Probability/Model Consistency — Phase 8I déterministe sur les mêmes inputs) | None | None |
| Store Integrity | OK | 0 | {'status': 'OK', 'valid_json': True, 'record_count': 0, 'duplicate_keys_found':  |
| Resolution | OK | {'PENDING': 0, 'RESOLVED': 0, 'CONFLICT': 0, 'UNRESOLVED': 0, 'INVALID': 0} | {'counts': {'PENDING': 0, 'RESOLVED': 0, 'CONFLICT': 0, 'UNRESOLVED': 0, 'INVALI |
| Track Record | NO_DATA | None | {'1X2': {'status': 'INSUFFICIENT_DATA', 'market': '1X2', 'sample_size': 0, 'reas |

## 20. Limitations

- LATE_PREDICTION reste KICKOFF_TIME_UNKNOWN pour toutes les prédictions réelles (Match/ModelPrediction ne portent qu'une date, jamais une heure de coup d'envoi réelle).
- 0 donnée shadow réelle accumulée à ce jour (voir §2) — la plupart des dimensions ci-dessus sont donc démontrées par les tests synthétiques (api/test_shadow_monitoring.py, 29/29) plutôt que par un volume réel.
- value_health reste NOT_AVAILABLE (The Odds API SUPPORT_REQUIRED).

## 21. Production Safety

DB unchanged: True. Production files modified: **False**.

## 22. Verdict

**INSUFFICIENT_REAL_DATA**

---

### READINESS FOR PHASE 9


**ready**
- Data Foundation (Phase 8A)
- Feature Registry (Phase 8A)
- Model Selection + Calibration (Phase 6, SHADOW)
- Track Record engine (Phase 7, réutilisé partout)
- Value Engine Foundation (Phase 8H)
- Decision Layer Foundation (Phase 8I)
- End-to-End Shadow Pipeline (Phase 8J)
- Shadow Decision Tracking + atomic/corruption-safe store (Phase 8K, durci Phase 8M)
- Live capture runner avec anti-fuite (Phase 8M)
- Shadow Monitoring & Data Quality (Phase 8N, cette phase)

**partial**
- Live Shadow Track Record — mécanisme validé, MAIS 0 donnée réelle accumulée à ce jour (voir §5/§33)
- Calibration Platt/Isotonic — recherche uniquement, jamais persistée par ModelVersion (constaté Phase 8L)

**blocked**
- Historical Replay — HISTORICAL_REPLAY_NOT_AVAILABLE (Phase 8L, preuve exhaustive : 186 885/186 885 paires rejetées)
- Value Tracking / ROI réel — NOT_AVAILABLE tant qu'aucune odds TEMPORALLY_VERIFIED n'existe
- The Odds API — SUPPORT_REQUIRED (Phase 8G.2), réponse support toujours en attente

**missing_real_data**
- Fixtures futures avec prédiction production pending (0 actuellement, voir shadow_readiness)
- Au moins 10-30 décisions shadow RESOLVED pour sortir de NO_DATA/EARLY_DATA (seuils Phase 8M, §23)
- Une source d'odds réellement temporellement vérifiée (aucune intégrée à ce jour)

**open_risks**
- Aucune ModelVersion actuelle n'a training_period_start/end rempli (Phase 8L) — un futur replay restera impossible pour les versions déjà existantes.
- Deux systèmes de normalisation d'équipe non synchronisés (team_name_matching.py vs main.py, constaté Phase 8A) — risque de faux MISSED_CAPTURE si les noms divergent.
- kickoff réel (heure) n'est jamais persisté (Match/ModelPrediction ne portent qu'une date) — LATE_PREDICTION reste structurellement non vérifiable tant que ce n'est pas corrigé en amont (hors périmètre Phase 8).

**conditions_before_any_production_activation**
- Accumuler un track record shadow RESOLVED réellement STATISTICALLY_INFORMATIVE (>= 100, seuil déjà documenté).
- Résoudre l'inconnue The Odds API (SUPPORT_REQUIRED) OU documenter une alternative de données odds temporellement vérifiées.
- Décision produit explicite sur le vocabulaire calibration RESEARCH_WITHOUT_CALIBRATION vs exigée pour un usage réel.
- Toute activation reste une décision Phase 9, jamais implicite dans les phases 8.

---

### EXISTING REGRESSION SUITES

| Suite | Pass |
|---|---|
| test_model_selection.py | True |
| test_track_record.py | True |
| test_feature_registry.py | True |
| test_value_engine.py | True |
| test_decision_layer.py | True |
| test_end_to_end_pipeline.py | True |
| test_shadow_operational.py | True |
| test_historical_replay.py | True |
| test_live_shadow_track.py | True |
| test_shadow_monitoring.py | True |

---

PHASE 8N — XFOOT SHADOW MONITORING & DATA QUALITY OPERATIONS V1 TERMINÉE. MONITORING SHADOW ET DATA QUALITY VALIDÉS OU LIMITATIONS DOCUMENTÉES. PHASE 8 TERMINÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. AUCUNE INTÉGRATION ODDS EFFECTUÉE. AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. PRÊT POUR ÉVALUATION DE LA PHASE 9.
