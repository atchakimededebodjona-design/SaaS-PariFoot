# PHASE 16.0 — AUDIT COMPLET DU MOTEUR IA PROSPECTIF (READ-ONLY)

Généré : 2026-09-04 22:29:50 UTC — **audit 100% read-only. Aucun code modifié, aucun commit, aucun push, aucune migration, aucun entraînement, aucune écriture DB, aucun changement Railway.**

Méthodologie : 3 audits de code approfondis (traçage du pipeline live depuis `main.py`, audit du pipeline shadow/track record, audit readiness/registre de modèles/rollback) + requêtes `SELECT` en lecture seule sur `api/app.db`. Chaque affirmation ci-dessous est sourcée par un `fichier:ligne` ou une requête SQL précise.

## Constat général

Le produit principal (l'endpoint de prédiction utilisé par les vrais utilisateurs) **fonctionne** et sert de vraies prédictions prospectives. Mais l'infrastructure sophistiquée bâtie en Phases 9 à 13 (shadow mode, track record, decision layer, readiness, rollback) est **intégralement construite et testée, mais structurellement déconnectée** de ce produit et de tout processus automatique. C'est la cause unique et précise des verdicts `NO_DATA`/`NO_GO` récurrents — pas un bug, mais une infrastructure jamais branchée.

## PARTIE A — Cartographie du pipeline (15 étapes)

| # | Étape | Existe | Appelée par le chemin live | Statut |
|---|---|---|---|---|
| 1 | Récupération fixtures | ✅ | Partiel | Vivante mais découplée de la prédiction (aucun lien serveur fixture↔prédiction) |
| 2 | Snapshot point-in-time | ✅ (2 impl.) | 1 seule (Arena) | `features/snapshot.py` orpheline ; `engine/live_features.py` vivante mais quasi jamais empruntée |
| 3 | Génération features | ✅ (3 impl.) | 1 seule (Arena) | Le produit principal ne génère aucune feature ML — scalaires pré-entraînés directs |
| 4 | Sélection du modèle | ✅ | ❌ | **Code mort** — le chemin live utilise une liste fixe codée en dur (`ModelOrchestrator.default_models()`) |
| 5 | model_version utilisée | ✅ (2 mécanismes) | ✅ | Aucune vraie sélection dynamique — dict en mémoire (produit) ou flag booléen statique (Arena) |
| 6 | Génération prédiction | ✅ (dupliquée) | Partiel | Le produit principal a sa PROPRE implémentation Dixon-Coles, indépendante du module "canonique" |
| 7 | Stockage prédiction | ✅ (2 tables) | ✅ | Vivant mais volume live extrême faible (9 + 10 lignes depuis le lancement) |
| 8 | Decision layer | ✅ | ❌ | **Entièrement orpheline** — `main.py` n'importe rien de `app.ai.decision` |
| 9 | Shadow record | ✅ | ❌ | **100% manuel** — aucun cron ne l'appelle, store vide (`{}`) |
| 10 | Résultat réel | ✅ | Partiel | Automatique pour `prediction_log` (5/9) ; les 10 prédictions live de `model_predictions` ne sont JAMAIS résolues |
| 11 | Évaluation | ✅ | ❌ | Code réel et correct, jamais exécuté sur données non vides (43/43 snapshots à 0) |
| 12 | Track record | ✅ | ❌ | `NOT_AVAILABLE` (0 observation) |
| 13 | Monitoring | ✅ | ❌ | Réel mais manuel, jamais cron'd |
| 14 | Readiness gate | ✅ | ❌ | 23 gates réels, jamais interrogés par le chemin live (couche d'audit pure) |
| 15 | Rollback | ✅ | ❌ | Code réel, capable d'écrire en DB, mais **0 exécution jamais** |

## PARTIE B — État des tables IA

| Table | Volume | Fraîcheur / constat |
|---|---|---|
| `match` | 12 459 | MIN=2019-08-09, MAX=**2026-05-24** — **0 ligne postérieure à aujourd'hui (2026-09-04)** |
| `match_stats` | 12 459 | 1:1 avec `match` |
| `model_predictions` | 3 610 | **3 600 `backtest`** (résolues trivialement) + **10 `live`** (toutes `pending`, jamais résolues) — fenêtre de génération de 5h seulement |
| `model_versions` | 15 | 5 `active`, 10 `retired`, **0 `shadow`/`candidate`** |
| `team_ratings` | 568 | Seulement 4 `model_version_id` distincts, calculés dans une fenêtre de ~3h — run ponctuel |
| `prediction_log` | 9 | 5/9 résolues automatiquement — volume minuscule depuis le lancement |
| `model_selection_decisions` | 3 | Toutes `status='insufficient_data'`, `calibration_verdict=NULL` |
| `shadow_selection_predictions` | 0 | Jamais alimentée |
| `model_promotion_events` | 0 | Aucun rollback ni promotion jamais appliqué |

## PARTIE C — Xfoot sait-il produire une vraie prédiction prospective aujourd'hui ?

**Partiellement oui.** Le produit principal génère de vraies prédictions pour de vraies fixtures à venir, avec une boucle prédiction→résultat réel qui fonctionne (`prediction_log`, 5/9 résolues automatiquement). Mais :
- Aucun lien serveur (identifiant de fixture) ne relie une prédiction à sa fixture d'origine — tout repose sur des chaînes de caractères envoyées par le client.
- Aucun snapshot par requête pour le produit principal — un artefact statique par ligue, rafraîchi seulement chaque semaine.
- Le chemin réellement point-in-time-safe (filtre `Match.date < as_of`, testé) existe (`engine/live_features.py`) mais n'est emprunté que par l'endpoint Arena, utilisé 2 fois au total depuis le lancement.
- Aucune des prédictions live (produit ou Arena) n'entre jamais dans le système de preuve (shadow) — même correctement résolues, elles ne nourrissent aucune métrique de confiance à long terme.

## PARTIE D — Track record (système de preuve)

Le code de calcul est réel et correct : `accuracy`, `log_loss`, `brier_score`, intervalle de confiance de Wilson, classification de maturité (seuils 10/30/100), comparaison production-vs-shadow par bootstrap + test de McNemar, calibration, stabilité. **Mais** : les 43/43 snapshots historiques enregistrés montrent `captured=0`, `NO_DATA`, `NO_GO` — aucune preuve qu'il ait jamais tourné sur une donnée réelle non vide.

## PARTIE E — Pourquoi le verdict reste NO_DATA (chaîne causale exacte, prouvée par le code)

1. `human_review.py::derive_phase10_verdict` vérifie **en premier** : `if future_fixtures == 0 and real_prospective_resolved == 0: return "NO_DATA"` — avant même de regarder le verdict du gate matrix.
2. `future_fixtures == 0` est vrai : 0 ligne dans `match` avec une date postérieure à aujourd'hui.
3. `real_prospective_resolved == 0` est vrai : le store Shadow est vide.
4. Le `"NO_GO"` sous-jacent du gate matrix (masqué par le `NO_DATA` ci-dessus) est lui-même causé par 4 des 9 gates **critiques** en échec : `PROVENANCE`, `ROLLBACK`, `MONITORING`, `TRACK_RECORD` — tous retombant sur la même racine : le store Shadow n'a jamais été alimenté, et `model_promotion_events` n'a jamais reçu une seule ligne.

Ce n'est pas une réponse générale — c'est prouvé ligne par ligne dans le code lu intégralement.

## PARTIE F — Modèles réellement présents

5 modèles `active` (un par type) : `xfoot-dixon-coles-v1` (seul réellement utilisé par le produit, via artefact mémoire), `xfoot-elo-v4`, `xfoot-xgboost-v3`, `xfoot-lightgbm-v3`, `xfoot-ensemble-v3` (ces 4 derniers exercés uniquement via l'Arena, 2 appels réels au total). 10 versions `retired`, désactivées manuellement après évaluation walk-forward "pas de gain clair". **0 version `shadow`/`candidate`** — confirme que le cron d'évaluation quotidien n'a jamais eu de version à évaluer.

## PARTIE G — Shadow pipeline : réponses précises

1. Créées automatiquement ? **Non** — 100% manuel.
2. Stockées ? Oui, mais dans un fichier JSON local, actuellement vide.
3. Comparées aux résultats ? Le code existe, mais seulement sur déclenchement manuel.
4. Le monitoring les consomme ? Oui côté code, mais le monitoring lui-même est manuel.
5. Le readiness les lit ? Oui — et rapporte honnêtement `NOT_AVAILABLE`/`NO_DATA` car le store est vide.

**Rupture de chaîne** : aucun code ne relie le seul pipeline vraiment automatique (`generate_live_predictions.py`, cron quotidien) à la capture Shadow. Un humain doit combler cet écart manuellement — ce qui n'a essentiellement jamais été fait.

## PARTIE H — Rollback & kill switch

- **Kill switch** : état actuel **ENABLED (non déclenché)**, persisté dans un fichier JSON absent (traité comme état par défaut sûr). 11 des 12 déclencheurs automatiques ne sont jamais levés par le code live — `main.py` n'importe rien de `app.ai.safety`.
- **Rollback** : code réel, capable d'écrire en DB, mais **jamais exécuté** (0 ligne dans `model_promotion_events`), et non branché dans aucun chemin live ou automatique.
- **Feature flags** : aucun `SHADOW_MODE`/`AI_MODE`/`PRODUCTION_MODE` n'existe. Le vocabulaire `MODE_0` à `MODE_4` est purement descriptif, jamais appliqué. `AUTO_PROMOTION_ENABLED` (défaut `false`) est le seul vrai interrupteur, sans effet sur le service live.
- **Conclusion** : toute la couche sécurité est une couche d'audit/rapport bien construite, mais totalement découplée du service réel.

## PARTIE I — Dette technique priorisée

**CRITIQUE** : (1) aucune capture Shadow automatique, (2) table `match` sans fixture future (source de données non reliée aux fixtures live), (3) decision layer jamais branché sur le trafic live, (4) rollback jamais exercé en conditions réelles.

**ÉLEVÉ** : (5) XGBoost/LightGBM dépendent d'un cycle de réentraînement/promotion non opérant, (6) aucun lien serveur fixture↔prédiction, (7) `model_selection.py` entièrement mort, (8) kill switch sans surveillance réelle.

**MOYEN** : (9) duplication Dixon-Coles, (10) duplication Elo, (11) volume live extrêmement faible, (12) confusion architecturale (`features/snapshot.py` orpheline vs `engine/live_features.py` réellement vivante).

**FAIBLE** : (13) gates ODDS/VALUE/LEGAL bloqués (intégration jamais construite, déjà déprioritisé), (14) filtrage exact du gate SECURITY non re-dérivé (sans rapport avec la chaîne NO_DATA).

*(Détail complet avec fichiers et dépendances dans le JSON accompagnant ce rapport.)*

## PARTIE J — Proposition Phase 16.1 (une seule)

**Brancher automatiquement la capture Shadow sur le cron déjà existant `generate_live_predictions.py`** : après l'écriture quotidienne des nouvelles prédictions, appeler `run_prospective_capture` pour les enregistrer dans le Shadow Decision Store, puis ajouter un second appel de résolution après le cron `fetch_daily_results.py` déjà existant.

**Pourquoi celle-ci et pas une autre** : c'est le seul changement qui attaque directement la cause racine (Partie E) sans nouvel entraînement, nouvelle table, ni modification de schéma — il réutilise deux fonctions déjà écrites et testées, et deux jobs cron déjà planifiés. Toutes les autres pistes dépendent de données réelles qui n'existeront que si cette capture commence à fonctionner en premier.

## Verdict

**`AI_PIPELINE_INCOMPLETE`**

Le moteur n'est ni cassé ni bloqué par un obstacle externe : le produit principal sert réellement de vraies prédictions. Mais l'infrastructure d'évaluation (shadow, track record, decision, readiness, rollback) est intégralement construite et testée, tout en étant structurellement déconnectée du seul pipeline automatique existant. Une phase suivante unique, bien délimitée et non bloquée est identifiée (Partie J).

---

**PHASE 16.0 — AUDIT READ-ONLY TERMINÉ : LE PRODUIT PRINCIPAL PRODUIT DE VRAIES PRÉDICTIONS PROSPECTIVES AVEC UNE BOUCLE DE RÉSOLUTION PARTIELLEMENT FONCTIONNELLE, MAIS L'INTÉGRALITÉ DE L'INFRASTRUCTURE SHADOW/TRACK RECORD/DECISION/READINESS/ROLLBACK DES PHASES 9-13 EST CONSTRUITE, TESTÉE, ET N'A JAMAIS TOURNÉ SUR UNE DONNÉE RÉELLE (STORE SHADOW VIDE, 0 ROLLBACK, 0 CAPTURE AUTOMATIQUE). CHAÎNE CAUSALE EXACTE DU VERDICT NO_DATA PROUVÉE LIGNE PAR LIGNE DANS human_review.py. AUCUN CODE MODIFIÉ, AUCUNE ÉCRITURE DB, AUCUN COMMIT, AUCUN PUSH. VERDICT : AI_PIPELINE_INCOMPLETE — UNE SEULE PROCHAINE PHASE (16.1) EST PROPOSÉE : BRANCHER LA CAPTURE SHADOW SUR LE CRON DÉJÀ EXISTANT.**
