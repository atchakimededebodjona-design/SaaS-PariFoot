# XFOOT VALUE ENGINE FOUNDATION V1

## 1. Executive Summary

Run id : `20260830_113136` — généré le 2026-08-30T11:31:36.757923+00:00. RÈGLE : RESEARCH + SHADOW ONLY. NO PRODUCTION INTEGRATION. NO ODDS PROVIDER CALLED.

Tests verts : **True**. Décision finale : **FOUNDATION_READY**

## 2. Architecture


- Package : `api/app/ai/value/` — modules : ['__init__.py', 'schemas.py', 'quality.py', 'core.py', 'provider.py']
- Tests : `api/test_value_engine.py`
- Appelé par la production : **False**
- Aucun module de api/main.py, scheduler.py, orchestrator.py, service.py, ensemble.py, models_common.py ou promotion.py n'importe api/app/ai/value/.

## 3. Input Contract


['ModelProbability', 'MarketProbability', 'OddsSnapshot', 'TemporalMetadata', 'PredictionQuality', 'ValueSignal', 'ValueThresholds']

## 4. Implied Probability


p_raw = 1/odds — réutilise `app.ai.odds_research.core.implied_probability` (Phase 8D), jamais réimplémentée. Exemple 1X2 : {'raw': {'home_win': 0.5555555555555556, 'draw': 0.2777777777777778, 'away_win': 0.2222222222222222}, 'normalized': {'home_win': 0.5263157894736842, 'draw': 0.2631578947368421, 'away_win': 0.21052631578947367}, 'overround': 0.05555555555555558}

## 5. Market Normalization


normalized_i = raw_i / sum(raw) — raw et normalized gardées séparées dans MarketProbability, jamais mélangées.

## 6. Overround


Exemple exact du prompt (Home 0.50/Draw 0.30/Away 0.25) : {'raw': {'home': 0.5, 'draw': 0.3, 'away': 0.25}, 'normalized': {'home': 0.47619047619047616, 'draw': 0.2857142857142857, 'away': 0.23809523809523808}, 'overround': 0.050000000000000044}

## 7. Edge


edge = p_model - p_market. Positif = Xfoot voit une probabilité plus élevée que le marché.

## 8. EV


EV = p_model x odds - 1. Convention : retour NET attendu par unité misée — jamais une garantie.

## 9. Temporal Safety


Statuts : ['TEMPORALLY_VERIFIED', 'HISTORICAL_UNVERIFIED', 'FUTURE_INFORMATION', 'UNKNOWN']. FUTURE_INFORMATION -> REJECT. UNKNOWN -> jamais SAFE. HISTORICAL_UNVERIFIED -> recherche uniquement, jamais production (voir quality.is_production_eligible).

## 10. Quality Gates


ODDS_VALID, MODEL_PROBABILITY_VALID, TEMPORAL_STATUS_VALID, MARKET_VALID, SAMPLE_VALID — ordre de vérification fixe et déterministe (voir api/app/ai/value/quality.py::evaluate_quality_gates).

## 11. Value Signals


5 signaux générés (synthétiques) — répartition : {'POSITIVE_VALUE': 2, 'NEUTRAL': 0, 'NEGATIVE_VALUE': 0, 'INSUFFICIENT_DATA': 3, 'TEMPORALLY_UNSAFE': 0, 'INVALID_ODDS': 0}

Classement multi-critères (expected_value puis edge) :

- {'model_probability': 0.6, 'expected_value': 0.19999999999999996, 'edge': 0.0736842105263158}
- {'model_probability': 0.55, 'expected_value': 0.10000000000000009, 'edge': 0.023684210526315863}
- {'model_probability': 0.5, 'expected_value': 0.0, 'edge': -0.02631578947368418}
- {'model_probability': 0.45, 'expected_value': -0.09999999999999998, 'edge': -0.07631578947368417}
- {'model_probability': 0.4, 'expected_value': -0.19999999999999996, 'edge': -0.12631578947368416}

## 12. Market Consensus


Test adversarial consensus (§34) : inclus=['A', 'B'], exclus=['C'], PASS=True

## 13. Model vs Market


edge/EV exposent la comparaison modèle vs marché — jamais qualifiée automatiquement de "BET", uniquement VALUE_CANDIDATE (statuts POSITIVE_VALUE/NEUTRAL/NEGATIVE_VALUE). classify_market_dominance() : UNKNOWN sans score de qualité explicite, jamais déduit d'un simple edge (§26).

## 14. Threshold Framework


Paramètres : ['min_edge', 'min_ev', 'min_probability', 'min_confidence', 'max_odds_age_hours']

RESEARCH_DEFAULT : {'min_edge': 0.0, 'min_ev': 0.0, 'min_probability': 0.0, 'min_confidence': 0.0, 'max_odds_age_hours': inf}

RESEARCH_DEFAULT — jamais un seuil de production. Grille de recherche : EDGE_GRID=(0.01, 0.02, 0.03, 0.05, 0.07, 0.1), EV_GRID=(0.01, 0.02, 0.03, 0.05, 0.1)

## 15. Synthetic Tests

| Case | Input | Expected | Got | Pass |
|---|---|---|---|---|
| A | p_model=0.60, odds=2.00 | EV=+20% | POSITIVE_VALUE (0.19999999999999996) | True |
| B | p_model=0.45, odds=2.00 | EV=-10% | INSUFFICIENT_DATA (-0.09999999999999998) | True |
| C | future odds (after cutoff) | TEMPORALLY_UNSAFE | TEMPORALLY_UNSAFE (FUTURE_INFORMATION) | True |
| D | unknown timestamp | TEMPORAL_UNVERIFIED | TEMPORALLY_UNSAFE (TEMPORAL_UNVERIFIED) | True |
| E | invalid odds <= 1 | INVALID_ODDS | INVALID_ODDS (INVALID_ODDS) | True |

## 16. Leakage Tests


Test adversarial exclusion snapshot (§33) : {'14:00': {'got': 'TEMPORALLY_VERIFIED', 'expected': 'TEMPORALLY_VERIFIED', 'pass': True}, '18:30': {'got': 'FUTURE_INFORMATION', 'expected': 'FUTURE_INFORMATION', 'pass': True}, '19:45': {'got': 'FUTURE_INFORMATION', 'expected': 'FUTURE_INFORMATION', 'pass': True}, '20:10': {'got': 'FUTURE_INFORMATION', 'expected': 'FUTURE_INFORMATION', 'pass': True}}

Tous PASS : **True**

## 17. Database Safety


Before : {'match': 12459, 'match_stats': 12459, 'model_predictions': 3610, 'model_versions': 15, 'prediction_log': 9, 'team_ratings': 568}

After : {'match': 12459, 'match_stats': 12459, 'model_predictions': 3610, 'model_versions': 15, 'prediction_log': 9, 'team_ratings': 568}

Unchanged : **True**

## 18. Limitations


- Aucune donnée odds réellement temporellement vérifiée disponible (Phase 8G.2 : SUPPORT_REQUIRED, The Odds API non intégré).
- Le consensus/dispersion multi-bookmaker n'a été exercé que sur des snapshots synthétiques — jamais sur des cotes réelles.
- classify_market_dominance() ne reçoit aucun score de qualité réel dans cette V1 (aucun historique de calibration n'y est câblé).

## 19. Production Status


The Odds API : SUPPORT_REQUIRED (Phase 8G.2) — NON intégré, aucun appel effectué dans cette phase.

football-data.co.uk : HISTORICAL_BUT_UNTIMESTAMPED (Phase 8D/8E) — jamais utilisé comme source temporellement sûre dans ce module.

STATISTICAL_VALUE_VALIDATION = **NOT_AVAILABLE**

## 20. Next Step


Recommandation : PHASE 8I, uniquement après validation de cette fondation. La Phase 8I devra traiter la prochaine priorité réelle de Xfoot (ex. lever SUPPORT_REQUIRED sur The Odds API, ou une autre source de données) SANS supposer que The Odds API sera le fournisseur final.

---

PHASE 8H — XFOOT VALUE ENGINE & MARKET INTELLIGENCE FOUNDATION V1 TERMINÉE. AUCUNE INTÉGRATION ODDS EFFECTUÉE. AUCUN SIGNAL DE PARI PRODUCTION GÉNÉRÉ. AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.
