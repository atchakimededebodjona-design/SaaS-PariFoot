# XFOOT DATA INTELLIGENCE & FEATURE REGISTRY V1

## 1. Executive Summary

Run id : `20260829_223617` — généré le 2026-08-29T22:36:17.652135+00:00.

46 features documentées (34 en production, 11 absentes documentées). Fondation de données : READY. Registre de features : READY. Aucune modification de production.

## 2. Current Data Architecture

- `match` : 12459 lignes, 5 ligues, 2019-08-09 00:00:00 → 2026-05-24 00:00:00
- `match_stats` : 12458/12459 matchs avec statistiques complètes (0.9999)
- `model_predictions` par modèle : {'dixon_coles': {'total': 2, 'resolved': 0}, 'elo': {'total': 1502, 'resolved': 1500}, 'ensemble': {'total': 902, 'resolved': 900}, 'lightgbm': {'total': 602, 'resolved': 600}, 'xgboost': {'total': 602, 'resolved': 600}}
- `model_versions` : 15 ; `team_ratings` : 568

## 3. Data Sources

| Source | Exists | Historical | Timestamped | Coverage | Status |
|---|---|---|---|---|---|
| match (DB, ex-CSV all_leagues_raw_with_stats.csv) | True | True | Date seule, sans heure (naïve) — aucune heure de coup d'envoi persistée | Bonne (NOT NULL sur les champs clés) | True |
| match_stats (tirs/corners) | True | True | Hérité de match.date | Partielle — voir couverture réelle calculée ci-dessous | True |
| team_ratings (Elo, Dixon-Coles) | True | Snapshot le plus récent uniquement, pas de série temporelle | computed_at | Bonne pour l'usage servi, pas pour une reconstruction historique arbitraire | True |
| api/model_artifacts/*.json (Dixon-Coles production) | True | Snapshot le plus récent uniquement | data_up_to | Bonne | True |
| model_predictions / prediction_log | True | True | predicted_at / created_at | Bonne | True |
| API-Football (fixtures, scores, logos) | True | False | fixture.date (UTC, avec heure) | Externe, dépend de la disponibilité réseau/quota | Live uniquement, jamais pour l'historique persisté |
| Bookmaker odds | False | N/A | N/A | N/A | False |
| Injuries / suspensions | False | N/A | N/A | N/A | False |
| Lineups | False | N/A | N/A | N/A | False |
| Weather | False | N/A | N/A | N/A | False |
| Standings / league table | False | N/A (calculable par agrégation, non implémenté) | N/A | N/A | False |
| season / season_id | False | N/A (dérivable, jamais persisté) | N/A | N/A | False |

## 4. Availability Matrix

| Feature Family | Source | Historical | Timestamped | Coverage | Leakage Risk |
|---|---|---|---|---|---|
| Team Strength | export_model_artifacts.py | True | True | Une valeur par équipe/ligue, snapshot LE PLUS RÉCENT uniquem | CAUTION |
| Form | api/app/ai/engine/live_features.py::build_live_features | True | True | match (2019-08→2026-05, 11 ligues) — disponible dès qu'un ma | SAFE |
| Goals | api/app/ai/engine/live_features.py::build_live_features | True | True | match (2019-08→2026-05, 11 ligues) — disponible dès qu'un ma | SAFE |
| Head-to-Head | api/app/ai/engine/live_features.py::build_live_features | True | True | 0 à 5 selon l'historique réel des confrontations. | SAFE |
| Ranking | NOT AVAILABLE — aucune table de classement/standings dans le | False | True | Calculable par agrégation de `match` (points cumulés par équ | CAUTION |
| Schedule/Rest | api/app/ai/engine/live_features.py::build_live_features | True | True | Disponible dès qu'un match antérieur existe. | SAFE |
| Injuries | NOT AVAILABLE — confirmé absent | False | True | NOT AVAILABLE. | REJECTED |
| Suspensions | NOT AVAILABLE — confirmé absent | False | True | NOT AVAILABLE. | REJECTED |
| Lineups | NOT AVAILABLE — confirmé absent, aucun endpoint lineups jama | False | True | NOT AVAILABLE. | REJECTED |
| Odds | NOT AVAILABLE — aucune donnée de cote nulle part dans le dép | False | True | NOT AVAILABLE. | REJECTED |
| Odds Movement | NOT AVAILABLE — nécessite plusieurs snapshots de cotes dans  | False | True | NOT AVAILABLE. | REJECTED |
| Market Probability | NOT AVAILABLE — dépend d'odds_opening/closing, elles-mêmes a | False | True | NOT AVAILABLE. | REJECTED |
| Weather | NOT AVAILABLE — confirmé absent, aucune source météo dans le | False | True | NOT AVAILABLE. | REJECTED |

## 5. Feature Registry


Total : 46 features. Par statut : {'AVAILABLE': 0, 'PARTIAL': 0, 'MISSING': 11, 'EXPERIMENTAL': 1, 'REJECTED': 0, 'PRODUCTION': 34}. Par risque de fuite : {'SAFE': 29, 'CAUTION': 9, 'LEAKAGE_RISK': 0, 'REJECTED': 8}. Par feu tricolore (§28) : {'GREEN': 29, 'YELLOW': 6, 'RED': 11}.

| Feature | Source | Type | Cutoff | Leakage | Status |
|---|---|---|---|---|---|
| injuries | NOT AVAILABLE — confirmé absent | N/A | Règle FUTURE si intégré : le rapport de blessure doit être p | REJECTED | MISSING |
| league_standing | NOT AVAILABLE — aucune table de classement/standin | derived (int) | Serait SAFE si calculé strictement sur les matchs antérieurs | CAUTION | MISSING |
| lineups | NOT AVAILABLE — confirmé absent, aucun endpoint li | N/A | §20 du prompt : une composition publiée ~60 min avant le mat | REJECTED | MISSING |
| suspensions | NOT AVAILABLE — confirmé absent | N/A | N/A | REJECTED | MISSING |
| weather | NOT AVAILABLE — confirmé absent, aucune source mét | N/A | §26 du prompt : une PRÉVISION disponible avant kickoff serai | REJECTED | MISSING |
| away_current_streak | api/app/ai/engine/live_features.py::build_live_fea | float | Match.date < as_of (borne stricte) — appliqué dans chaque re | SAFE | PRODUCTION |
| away_form_goals_conceded_avg | api/app/ai/engine/live_features.py::build_live_fea | float | Match.date < as_of (borne stricte) — appliqué dans chaque re | SAFE | PRODUCTION |
| away_form_goals_scored_avg | api/app/ai/engine/live_features.py::build_live_fea | float | Match.date < as_of (borne stricte) — appliqué dans chaque re | SAFE | PRODUCTION |
| away_form_points_avg | api/app/ai/engine/live_features.py::build_live_fea | float | Match.date < as_of (borne stricte) — appliqué dans chaque re | SAFE | PRODUCTION |
| home_current_streak | api/app/ai/engine/live_features.py::build_live_fea | float | Match.date < as_of (borne stricte) — appliqué dans chaque re | SAFE | PRODUCTION |
| home_form_goals_conceded_avg | api/app/ai/engine/live_features.py::build_live_fea | float | Match.date < as_of (borne stricte) — appliqué dans chaque re | SAFE | PRODUCTION |
| home_form_goals_scored_avg | api/app/ai/engine/live_features.py::build_live_fea | float | Match.date < as_of (borne stricte) — appliqué dans chaque re | SAFE | PRODUCTION |
| home_form_points_avg | api/app/ai/engine/live_features.py::build_live_fea | float | Match.date < as_of (borne stricte) — appliqué dans chaque re | SAFE | PRODUCTION |
| h2h_home_win_rate | api/app/ai/engine/live_features.py::build_live_fea | float | Match.date < as_of (borne stricte) — appliqué dans chaque re | SAFE | PRODUCTION |
| h2h_matches_found | api/app/ai/engine/live_features.py::build_live_fea | int | Match.date < as_of (borne stricte) — appliqué dans chaque re | SAFE | PRODUCTION |
| implied_probability | NOT AVAILABLE — dépend d'odds_opening/closing, ell | float | Hériterait de la règle de la cote source (odds_opening SAFE  | REJECTED | MISSING |
| odds_closing | NOT AVAILABLE — voir odds_opening. | float | REJECTED par construction pour toute prédiction faite avant  | REJECTED | MISSING |
| odds_movement | NOT AVAILABLE — nécessite plusieurs snapshots de c | float | Chaque snapshot utilisé devrait individuellement être antéri | REJECTED | MISSING |
| odds_opening | NOT AVAILABLE — aucune donnée de cote nulle part d | float | Règle FUTURE si intégré un jour : odds_timestamp doit être < | REJECTED | MISSING |
| away_team | api/app/models/match.py::Match.away_team. | categorical (str) | N/A | SAFE | PRODUCTION |
| home_team | api/app/models/match.py::Match.home_team. | categorical (str) | N/A | SAFE | PRODUCTION |
| league | api/app/models/match.py::Match.league | categorical (str) | N/A | SAFE | PRODUCTION |
| match_date | api/app/models/match.py::Match.date | date | N/A — c'est la référence temporelle elle-même. | SAFE | PRODUCTION |
| season | AUCUNE colonne season/season_id nulle part dans le | derived (int, année de début) | Dérivable de Match.date, jamais du futur — SAFE si calculée  | CAUTION | EXPERIMENTAL |
| away_corners_diff_avg | api/app/ai/engine/live_features.py::build_live_fea | float | Match.date < as_of (borne stricte) — appliqué dans chaque re | SAFE | PRODUCTION |
| away_shots_diff_avg | api/app/ai/engine/live_features.py::build_live_fea | float | Match.date < as_of (borne stricte) — appliqué dans chaque re | SAFE | PRODUCTION |
| away_shots_target_diff_avg | api/app/ai/engine/live_features.py::build_live_fea | float | Match.date < as_of (borne stricte) — appliqué dans chaque re | SAFE | PRODUCTION |
| home_corners_diff_avg | api/app/ai/engine/live_features.py::build_live_fea | float | Match.date < as_of (borne stricte) — appliqué dans chaque re | SAFE | PRODUCTION |
| home_shots_diff_avg | api/app/ai/engine/live_features.py::build_live_fea | float | Match.date < as_of (borne stricte) — appliqué dans chaque re | SAFE | PRODUCTION |
| home_shots_target_diff_avg | api/app/ai/engine/live_features.py::build_live_fea | float | Match.date < as_of (borne stricte) — appliqué dans chaque re | SAFE | PRODUCTION |
| away_days_since_last_match | api/app/ai/engine/live_features.py::build_live_fea | float | Match.date < as_of (borne stricte) — appliqué dans chaque re | SAFE | PRODUCTION |
| away_returning_from_break | api/app/ai/engine/live_features.py::build_live_fea | int (0/1) | Match.date < as_of (borne stricte) — appliqué dans chaque re | SAFE | PRODUCTION |
| home_days_since_last_match | api/app/ai/engine/live_features.py::build_live_fea | float | Match.date < as_of (borne stricte) — appliqué dans chaque re | SAFE | PRODUCTION |
| home_returning_from_break | api/app/ai/engine/live_features.py::build_live_fea | int (0/1) | Match.date < as_of (borne stricte) — appliqué dans chaque re | SAFE | PRODUCTION |
| matches_last_14_days | NOT AVAILABLE en tant que feature — même remarque  | derived (int) | Serait SAFE (même principe). | CAUTION | MISSING |
| matches_last_7_days | NOT AVAILABLE en tant que feature — dérivable de M | derived (int) | Serait SAFE (même requête que _team_matches_before, bornée à | CAUTION | MISSING |
| dc_away_win | api/app/ai/engine/live_features.py::build_live_fea | float | Approximation DOCUMENTÉE (pas un fit tronqué à as_of) : réut | SAFE | PRODUCTION |
| dc_draw | api/app/ai/engine/live_features.py::build_live_fea | float | Approximation DOCUMENTÉE (pas un fit tronqué à as_of) : réut | SAFE | PRODUCTION |
| dc_home_win | api/app/ai/engine/live_features.py::build_live_fea | float | Approximation DOCUMENTÉE (pas un fit tronqué à as_of) : réut | SAFE | PRODUCTION |
| dc_over_2_5 | api/app/ai/engine/live_features.py::build_live_fea | float | Approximation DOCUMENTÉE (pas un fit tronqué à as_of) : réut | SAFE | PRODUCTION |
| dc_under_2_5 | api/app/ai/engine/live_features.py::build_live_fea | float | Approximation DOCUMENTÉE (pas un fit tronqué à as_of) : réut | SAFE | PRODUCTION |
| dixon_coles_attack | export_model_artifacts.py | float | SAFE pour servir une prédiction (l'artefact n'a appris que d | CAUTION | PRODUCTION |
| dixon_coles_defense | export_model_artifacts.py | float | Identique à dixon_coles_attack. | CAUTION | PRODUCTION |
| dixon_coles_home_advantage | export_model_artifacts.py | float | Identique à dixon_coles_attack. | CAUTION | PRODUCTION |
| dixon_coles_rho | export_model_artifacts.py | float | Identique à dixon_coles_attack. | CAUTION | PRODUCTION |
| elo_rating | api/app/ai/engine/elo.py::EloEngine | float | SAFE pour servir (rating pré-match par construction dans wal | CAUTION | PRODUCTION |

## 6. Feature Definitions


Voir §5 ci-dessus (tableau complet) et `api/app/ai/features/registry.py` pour la définition intégrale de chaque feature (description, unité, missing_value_strategy, current_model_usage).

## 7. Timestamp Analysis


`match.date` ne porte JAMAIS d'heure de coup d'envoi dans l'historique persisté (100% à minuit) — seules les fixtures LIVE (API-Football) portent une heure réelle (UTC), perdue dès l'écriture en base (`.date()` uniquement). Toute règle de cutoff sur l'historique est donc au JOUR près, jamais à l'heure près.

## 8. Temporal Cutoff


Règle uniforme pour les 25 features ML de production : `Match.date < as_of` (strict), appliquée directement dans chaque requête de `live_features.py` — jamais un filtre après coup. Voir `api/app/ai/features/snapshot.py::validate_cutoff` pour la primitive de vérification générique.

## 9. Leakage Audit


Répartition : {'SAFE': 29, 'CAUTION': 9, 'LEAKAGE_RISK': 0, 'REJECTED': 8}. Toute feature `PRODUCTION` a `leakage_risk` SAFE ou CAUTION documenté (jamais LEAKAGE_RISK/REJECTED) — vérifié par `validate_registry()` : aucun problème.

## 10. Team Normalization


Aucun identifiant canonique d'équipe (`team_id`) n'existe dans le dépôt — chaque référence à une équipe est une
chaîne de caractères exacte. DEUX mécanismes de normalisation fuzzy INDÉPENDANTS coexistent :

1. `api/app/core/team_name_matching.py` — 18 alias codés en dur, normalisation NFKD/minuscule, repli
   `difflib.SequenceMatcher.ratio() >= 0.6`. Résout SILENCIEUSEMENT sur un match fuzzy (booléen simple,
   `names_match()`), utilisé pour rapprocher les noms d'équipes API-Football lors de la résolution de résultats.
2. `api/main.py` (~300 lignes, `TEAM_ALIASES` + `resolve_team_name()`) — table d'alias totalement séparée, même
   seuil fuzzy 0.6, mais NE résout JAMAIS silencieusement en fuzzy : retourne toujours des suggestions à l'appelant.

Ces deux tables ne sont pas synchronisées entre elles — une correction apportée à l'une ne se propage pas à
l'autre. Risque de faux positif documenté ici (seuil 0.6 sur des noms de clubs courts et proches) mais NON corrigé
dans cette phase (audit uniquement, aucune modification du code de normalisation).

La normalisation de LIGUE, à l'inverse, est un dictionnaire EXACT {nom: id API-Football} (11 ligues) — aucun
risque de faux positif, mais toute compétition hors de cette liste est silencieusement ignorée partout où les
fixtures sont ingérées.


## 11. League Normalization

Dictionnaire exact {nom: id API-Football}, 11 ligues — voir §10 ci-dessus pour le détail complet (section combinée équipe+ligue).

## 12. Season Availability

Statut : **EXPERIMENTAL**. NOT AVAILABLE en tant que champ stocké. Une règle de dérivation FIABLE existe et est déjà utilisée ailleurs dans le code (jamais pour cette feature) : mois >= 7 -> saison = année en cours, sinon année précédente — voir build_features.py:113 et update_raw_data.py:132-147.

## 13. Form Features

Voir catégorie 'form' dans le registre (§5) — home/away_form_points_avg, goals_scored/conceded_avg, current_streak — toutes PRODUCTION, SAFE, fenêtre de 5 derniers matchs strictement antérieurs.

## 14. Team Strength

Voir catégorie 'team_strength' dans le registre (§5) — dixon_coles_attack/defense/home_advantage/rho, elo_rating — tous PRODUCTION, leakage_risk=CAUTION (snapshot le plus récent, pas une série temporelle persistée ; seul le walk-forward Phase 5.7 reconstruit un état historique fidèle).

## 15. Schedule / Rest

days_since_last_match/returning_from_break : PRODUCTION, SAFE. matches_last_7/14_days : MISSING, dérivable, non implémenté (P2).

## 16. Injuries

Statut : **MISSING**. NOT AVAILABLE — confirmé absent (grep exhaustif injur|suspension|lineup|weather|fatigue sur tout le dépôt : 2 résultats, tous deux sans rapport, une note sur l'interruption COVID-19 de la saison 2020). api_football_client.py n'appelle JAMAIS d'endpoint blessures.

## 17. Lineups

Statut : **MISSING**. NOT AVAILABLE — confirmé absent, aucun endpoint lineups jamais appelé.

## 18. Odds

Statut : **MISSING**. NOT AVAILABLE — aucune donnée de cote nulle part dans le dépôt (grep exhaustif odds|bookmaker|implied_prob|bet365|market_prob|betting sur tout le dépôt : zéro résultat réel, tous les hits sont le vocabulaire interne Xfoot pour 1X2/BTTS/O-U, jamais des prix de marché réels).

## 19. Odds Movement

Même statut MISSING que les cotes elles-mêmes (odds_movement dépend d'au moins deux snapshots de cotes, tous deux absents).

## 20. Weather

Statut : **MISSING**. NOT AVAILABLE — confirmé absent, aucune source météo dans le dépôt.

## 21. Data Quality

Couverture match_stats : 0.9999. Aucune valeur manquante n'est imputée à 0 par défaut dans les features de production (voir missing_value_strategy de chaque feature, §5).

## 22. Missing Data

11 features MISSING documentées explicitement (jamais simulées) — voir §5 et la matrice de priorité (§24).

## 23. Feature Lineage

Exemple (§8 du prompt) : `match` (résultats bruts) → fenêtre 5 derniers matchs → `home_form_points_avg`/`home_current_streak` → XGBoost/LightGBM. `match` → entraînement Dixon-Coles (export_model_artifacts.py) → `dc_home_win`/... → XGBoost/LightGBM (en tant que feature) ET Dixon-Coles lui-même (en tant que prédiction directe).

## 24. Feature Priority

| Feature | Quality | Coverage | Leakage Risk | Priority |
|---|---|---|---|---|
| season | NOT AVAILABLE en tant que champ stocké. Une règle  | dérivable | CAUTION | P1 |
| league_standing | Calculable par agrégation de `match` (points cumul | N/A | CAUTION | P1 |
| matches_last_7_days | Calculable, non implémenté. | N/A | CAUTION | P2 |
| matches_last_14_days | Calculable, non implémenté. | N/A | CAUTION | P2 |
| odds_opening | NOT AVAILABLE. | N/A | REJECTED | P2 |
| injuries | NOT AVAILABLE. | N/A | REJECTED | P2 |
| lineups | NOT AVAILABLE. | N/A | REJECTED | P2 |
| odds_closing | NOT AVAILABLE. | N/A | REJECTED | P3 |
| implied_probability | NOT AVAILABLE. | N/A | REJECTED | P3 |
| odds_movement | NOT AVAILABLE. | N/A | REJECTED | P3 |
| suspensions | NOT AVAILABLE. | N/A | REJECTED | P3 |
| weather | NOT AVAILABLE. | N/A | REJECTED | P3 |

## 25. Historical Reconstruction

"Que savait Xfoot avant une date T ?" est reconstructible pour les 25 features ML (`build_feature_snapshot`, wrapper de `live_features.build_live_features`, testé colonne par colonne) et pour Dixon-Coles via `research.py::build_dixon_coles_walk_forward` (retrain tronqué). Pas reconstructible pour Elo à une date arbitraire (seul l'état final est persisté).

## 26. Tests

Voir `api/test_feature_registry.py` — complétude/cohérence du registre, cutoff, rejet d'information future, normalisation équipe/ligue, valeurs manquantes, reproductibilité, snapshot, sécurité DB.

## 27. Database Safety

Lecture seule sur `match`/`match_stats`/`model_predictions`/`model_versions`/`team_ratings` — aucune écriture, aucune migration. Vérifié par test dédié (comptes de lignes identiques avant/après).

## 28. Production Isolation

Aucun modèle, endpoint, scheduler, dashboard, ni prédiction historique modifiés par cette phase.

## 29. Limitations


- match.date n'a jamais d'heure de coup d'envoi (100% des lignes historiques à minuit) — toute règle de cutoff au jour près, jamais à l'heure près, pour l'historique persisté.

- Aucun identifiant canonique d'équipe — deux tables d'alias fuzzy indépendantes et non synchronisées (voir section Team Normalization).

- Les ratings Dixon-Coles/Elo en base (team_ratings, api/model_artifacts) ne sont que des snapshots les plus récents — aucune série temporelle n'est persistée ; seule research.py::build_dixon_coles_walk_forward reconstruit un état historique fidèle à une date arbitraire (recherche/backtest uniquement).

- La base `match` de CET environnement ne couvre que 5 ligue(s) (Bundesliga, LaLiga, Ligue1, PremierLeague, SerieA) — 6 ligue(s) présente(s) dans data/all_leagues_raw_with_stats.csv (ChampionsLeague, ConferenceLeague, EuropaLeague, MLS, PrimeiraLiga, SaudiProLeague) n'y sont PAS chargées. Couverture match_stats calculée ci-dessus (0.9999) porte donc UNIQUEMENT sur ce sous-ensemble déjà chargé, pas sur le CSV source complet (dont la couverture réelle est nettement plus faible, ~57%, sur les ligues ajoutées plus récemment).

- Aucune donnée de marché (cotes), contextuelle (blessures/compositions/météo) n'existe dans le dépôt — documenté comme SOURCE CANDIDATE (§45), aucune intégration décidée dans cette phase.

## 30. Recommendations Phase 8B

1. `league_standing` et `season` (P1) : dérivables sans nouvelle source externe, candidates directes.
2. `matches_last_7/14_days` (P2) : même mécanisme que days_since_last_match, faible effort.
3. Odds/injuries (P2) : nécessitent un fournisseur externe — décision séparée, hors périmètre ici.
4. Unifier les deux tables d'alias équipe (team_name_matching.py / main.py) — dette technique identifiée, pas un risque de fuite mais un risque de divergence silencieuse.

---

### DATA FOUNDATION

🟢 READY

### FEATURE REGISTRY

🟢 READY

### EXTERNAL DATA

- odds (P2, nécessite un fournisseur externe)
- injuries (P2, endpoint API-Football /injuries jamais appelé)
- league_standing (P1, calculable par agrégation, aucune source externe requise)
- season (P1, dérivable par règle documentée, aucune source externe requise)

### PRODUCTION

**NO PRODUCTION CHANGES**

---

PHASE 8A — XFOOT DATA INTELLIGENCE & FEATURE REGISTRY V1 TERMINÉE. AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.
