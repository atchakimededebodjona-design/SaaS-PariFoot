# RAPPORT PHASE 10 — XFOOT AI Arena : Live Validation, Model Promotion & Production Hardening

## 0. Préambule — écart entre le prompt fourni et le périmètre réellement livré

Le prompt Phase 10 initial (rédigé pour Antigravity) a été relu avant exécution. Un audit du
dépôt (agent Explore) a montré que **la Phase 9 avait déjà implémenté la quasi-totalité du
pipeline demandé** : scheduler de prédictions LIVE + cron, cron de résolution des résultats
(déjà idempotent et agnostique au `role`), ModelOrchestrator/EnsembleEngine/WeightStrategy,
`compute_model_availability`, monitoring LIVE multi-fenêtres + `ModelHealth`
(HEALTHY/WARNING/DEGRADED/INSUFFICIENT_DATA/UNAVAILABLE), mode SHADOW complet et isolé,
retraining continu XGBoost/LightGBM, endpoints GET de consultation, et une UI santé dans
`arena.html`. Ce constat a été présenté à l'utilisateur avant toute écriture de code
(voir plan approuvé) : la Phase 10 livrée ci-dessous couvre donc uniquement ce qui manquait
réellement, plutôt que de recréer ce qui existait déjà — conformément à la règle 8 du
prompt ("NE PAS créer de doublons").

Deux écarts volontaires par rapport à la lettre du prompt, documentés plutôt que contournés
en silence (règle 15 du prompt) :

- **Pas de `config.py` centralisé unique.** Les seuils déjà en place (`MIN_BENCHMARK_
  SAMPLE_SIZE`, `PROMOTION_MIN_VALIDATION_SAMPLE`, `MIN_MONITORING_SAMPLE`, etc.) restent
  chacun dans leur module, par convention déjà établie en Phase 5-9 (constante documentée
  en haut du module qui l'utilise). Les migrer dans un fichier central pour cette Phase
  aurait ajouté un risque de régression pour un gain cosmétique. Les **nouveaux** seuils
  Phase 10 (`LIVE_MIN_SAMPLE_SIZE`, `PROMOTION_MIN_IMPROVEMENT`, `AUTO_PROMOTION_ENABLED`)
  suivent la même convention, dans `app/ai/arena/promotion.py`.
- **Pas de seuils absolus** (`PROMOTION_MAX_LOG_LOSS`/`PROMOTION_MAX_BRIER`/`PROMOTION_MIN_
  ACCURACY`). Tout le reste du dépôt (benchmark, health, promotion offline) compare
  toujours un modèle à un autre ou à lui-même dans le temps, jamais à une valeur absolue —
  un seuil absolu de log loss n'aurait aucun fondement statistique tant qu'on ne sait pas
  ce qui est atteignable sur ce sport/ce jeu de ligues. La porte de promotion LIVE Phase 10
  reste donc relative (candidat vs version active, marge minimale d'amélioration).

## 1. Fichiers créés

- `api/app/ai/arena/live_validation.py` — métriques LIVE scopées par `model_version_id`
  précise (jamais mélangées entre deux versions successives du même `model_type`).
- `api/app/models/model_promotion_event.py` — modèle `ModelPromotionEvent` (table
  `model_promotion_events`, append-only).
- `api/alembic/versions/d3f6a1b8c452_phase10_model_promotion_events.py` — migration créant
  cette table (chaînée après `b4d1e7f92a6c`, tête Phase 9).
- `api/app/auth/admin.py` — dépendance `require_admin` (allowlist `ADMIN_EMAILS`).
- `scripts/evaluate_live_models.py` — cron d'évaluation périodique, mode `evaluate only`
  par défaut.
- `railway.cron.evaluate_models.json` — cron Railway associé (07:00 UTC, après la
  résolution des résultats à 06:30).
- `api/test_live_validation.py`, `api/test_live_promotion.py`, `api/test_promotion_api.py`.
- `RAPPORT_PHASE10.md` (ce document).

## 2. Fichiers modifiés

- `api/app/ai/arena/promotion.py` — ajout de `evaluate_live_promotion`,
  `LivePromotionDecision`, `get_active_version` (rendue publique), et des constantes
  `LIVE_MIN_SAMPLE_SIZE`/`PROMOTION_MIN_IMPROVEMENT`/`AUTO_PROMOTION_ENABLED`.
  `evaluate_promotion`/`apply_promotion`/`set_shadow` (Phase 9) **inchangés**.
- `api/main.py` — 4 nouveaux endpoints `/models/promotion/*` (voir §10), mise à jour du
  commentaire Phase 9 qui notait l'absence de palier admin.
- `api/alembic/env.py` — enregistrement de `ModelPromotionEvent` pour l'autogénération
  (`alembic check` échouait sans cette ligne — corrigé et revérifié, voir §14).
- `api/test_anti_leakage_phase9.py` — 2 tests ajoutés à la suite existante (pas de nouveau
  fichier "data leakage" dédié, celui-ci est déjà l'endroit prévu pour ça).
- `frontend-design/arena.html` — nouvelle section "Model Health & Promotion".

## 3. Architecture finale

```
ModelVersion.status="shadow" (Phase 9, set_shadow, CLI/évaluation manuelle)
        ↓
scheduler.py::generate_live_predictions()  →  model_predictions (role="shadow")
        ↓
fetch_daily_results.py  (résolution, déjà agnostique au role — inchangé)
        ↓
live_validation.py::compute_live_model_metrics(model_version_id, market)
        ↓
promotion.py::evaluate_live_promotion(model_version_id, market)
        ├─ insufficient_data / already_active / rejected / no_clear_gain / eligible
        ↓
model_promotion_events (append-only, TOUJOURS écrit, y compris rejet)
        ↓
   ┌────────────────────┬─────────────────────────────┐
   │ scripts/evaluate_   │ POST /models/promotion/      │
   │ live_models.py      │ evaluate | promote (admin)   │
   │ (cron, AUTO_        │ (humain, ré-évalue toujours  │
   │ PROMOTION_ENABLED   │ côté serveur avant d'appliquer)│
   │ =false par défaut)  │                               │
   └────────────────────┴─────────────────────────────┘
        ↓ (si éligible et autorisé)
promotion.py::apply_promotion()  →  ModelVersion.status="active"
```

## 4. Scheduler

Inchangé (Phase 9) : `scripts/generate_live_predictions.py` + `railway.cron.live_predictions.json`
(05:30 UTC quotidien) génère déjà les prédictions actives et shadow. Ajout Phase 10 :
`scripts/evaluate_live_models.py` + `railway.cron.evaluate_models.json` (07:00 UTC,
après la résolution des résultats à 06:30) évalue périodiquement les candidats.

## 5. Live validation

`live_validation.compute_live_model_metrics(session, model_type, model_version_id, market)`
réutilise `service.py::_model_predictions_markets` (donc `_market_observation`/
`_compute_market_metrics`, la même formule que GET /models/benchmark et monitoring.py) —
filtré `prediction_source="live"` et **scopé à une seule `model_version_id`**, contrairement
à `monitoring.py` qui agrège par `role` sur tout l'historique d'un `model_type`. Cette
distinction est le cœur de la Phase 10 : elle seule permet de comparer CE candidat précis à
LA version active précise, sans jamais mélanger deux versions shadow successives.

## 6. Model Health

Réutilisé tel quel (Phase 9, `monitoring.py::compute_model_health`) — aucune réimplémentation.
La comparaison "candidat vs version active" demandée par la Phase 10 est un axe différent
(deux versions différentes, même fenêtre temporelle) de celui de `compute_model_health`
(une version, deux fenêtres temporelles) — les deux coexistent, chacun répondant à une
question distincte.

## 7. Shadow Mode

Inchangé (Phase 9) — `promotion.py::set_shadow`, isolation stricte déjà testée
(`test_shadow_mode.py`, `test_resolution_shadow.py`). La Phase 10 ajoute la **décision** de
sortie du mode shadow (`evaluate_live_promotion`), pas le mode shadow lui-même.

## 8. Promotion Engine

Deux moteurs distincts, jamais confondus :
- `evaluate_promotion` (Phase 9, inchangé) : offline, sur les métriques de VALIDATION de
  l'entraînement — utilisé par `scripts/retrain_ml_models.py --force`.
- `evaluate_live_promotion` (Phase 10, nouveau) : sur les performances LIVE réelles,
  candidat vs version active — deux portes : échantillon (`LIVE_MIN_SAMPLE_SIZE`, défaut
  100) puis marge d'amélioration réelle (`PROMOTION_MIN_IMPROVEMENT`, défaut 0.01 en log
  loss). Décisions possibles : `already_active`, `insufficient_data`, `rejected`,
  `no_clear_gain`, `eligible`.

## 9. Promotion History

`model_promotion_events` — une ligne PAR décision (`GET`/`POST evaluate`/`POST promote`
manuel, ou passage automatique de `evaluate_live_models.py`), jamais mise à jour ni
supprimée. Un rejet reste tracé exactement comme une promotion réussie (vérifié par
`test_promotion_api.py::test_promote_applies_and_is_idempotent_then_rejects`).

## 10. API

- `GET /models/promotion/status` (public) — décision LIVE actuelle pour chaque version
  SHADOW/CANDIDATE, sans rien écrire.
- `GET /models/promotion/history` (public) — historique paginé (`limit`, `model_type`,
  `decision`).
- `POST /models/promotion/evaluate` (**admin**) — évalue et journalise, n'applique jamais.
- `POST /models/promotion/promote` (**admin**) — ré-évalue TOUJOURS côté serveur avant
  d'appliquer (ne fait jamais confiance à une décision envoyée par le client), journalise
  dans tous les cas, y compris un rejet (400).
- `GET /models/health` inchangé (existait déjà, Phase 9).

Auth admin : `ADMIN_EMAILS` (CSV, env var) — décidé explicitement avec l'utilisateur plutôt
qu'une colonne DB ou une clé API statique (zéro migration sur `User`).

## 11. Frontend

Nouvelle section "Model Health & Promotion" dans `arena.html`, sous "Model Versions" —
liste des candidats avec leur décision (icône + badge), métriques candidat/baseline
côte à côte, raison en clair, historique récent, boutons "Lancer une évaluation"/
"Promouvoir" (réutilisent `apiFetch()` existant, qui attache déjà le Bearer token —
échouent proprement en 403 pour un utilisateur non-admin, aucun nouveau mécanisme d'auth
frontend nécessaire).

## 12. Railway

`railway.cron.evaluate_models.json` ajouté (07:00 UTC). `railway.cron.live_predictions.json`
et `railway.cron.results.json` (Phase 9) inchangés.

## 13. Anti-data-leakage

- Prédictions `status="pending"` jamais incluses dans une métrique (héritage direct de
  `_market_observation`, revérifié spécifiquement au niveau promotion par
  `test_promotion_never_uses_pending_predictions`).
- Deux versions shadow successives du même `model_type` ne partagent jamais un échantillon
  (`test_two_successive_shadow_versions_never_mixed_in_decision`) — le risque de fuite
  spécifique à ce module (scoping par `model_version_id`, pas par `role` seul).
- `evaluate_live_promotion` est une fonction pure, entièrement reconstructible depuis les
  données stockées (`test_promotion_decision_reconstructable_from_stored_data`).
- `POST /models/promotion/promote` ré-évalue toujours côté serveur (jamais de confiance
  dans une décision transmise par le client).
- La résolution (`fetch_daily_results.py`, inchangée) reste l'unique frontière de confiance
  temporelle : une prédiction ne peut être `status="resolved"` qu'une fois le résultat réel
  connu — aucun paramètre `evaluation_date` n'existe dans ce code qui permettrait de
  contourner cette frontière.

## 14. Tests

30 fichiers de tests dans `api/`, tous exécutés individuellement en régression complète
après implémentation : **25/25 fichiers de test passent (exit code 0)**, y compris les 3
nouveaux (`test_live_validation.py` 4/4, `test_live_promotion.py` 8/8,
`test_promotion_api.py` 6/6) et la suite étendue `test_anti_leakage_phase9.py` (10/10,
dont les 2 tests ajoutés). Aucun test existant modifié dans son intention ni supprimé.
`alembic upgrade head` puis `alembic check` : aucune dérive détectée entre le modèle
SQLModel et la chaîne de migrations (corrigé : `ModelPromotionEvent` devait être importé
dans `alembic/env.py` pour que l'autogénération le voie — sans quoi `alembic check`
signalait à tort une suppression de table).

## 15. Résultats réels observés (base de développement locale, au moment de ce rapport)

Chiffres lus directement dans `api/app.db` — jamais inventés :

- `model_predictions` : 3610 lignes au total, dont 3600 `source="backtest"` déjà résolues
  et 10 `source="live"` encore `status="pending"` (matchs pas encore joués).
- **0 prédiction LIVE résolue** à ce jour, et **0 ligne `role="shadow"`** — aucun cycle
  shadow n'a encore tourné en conditions réelles.
- `model_versions` : une version `active` par `model_type` (dixon_coles/elo/xgboost/
  lightgbm/ensemble), le reste `retired` — **aucune version `shadow`/`candidate`
  actuellement en base**.

## 16. Nombre de prédictions LIVE

10 (toutes `pending`, aucune résolue — voir §15).

## 17. Nombre de prédictions résolues

3600, mais **exclusivement issues de backtests historiques**, pas de production LIVE réelle.
0 prédiction LIVE résolue à ce jour.

## 18. Performance LIVE

Non mesurable pour l'instant — 0 échantillon LIVE résolu. `GET /models/promotion/status`
renverrait honnêtement une liste de candidats vide (aucune version shadow en base) ;
`GET /models/live-performance`/`GET /models/health` (Phase 9, inchangés) renvoient déjà
`INSUFFICIENT_DATA` partout dans cet état — comportement attendu, pas une régression.

## 19. Modèles éligibles

Aucun — pas de version shadow/candidate en base au moment de ce rapport, donc rien à
évaluer. Le pipeline (`evaluate_live_promotion`, endpoints, cron) est fonctionnel et
testé (voir §14) mais n'a encore rien à décider tant qu'aucun cycle shadow réel n'a
tourné en production.

## 20. Promotions effectuées

0 (aucune donnée réelle disponible pour en justifier une — voir §15-19).

## 21. Promotions refusées

0 pour la même raison. Le comportement de refus (insufficient_data/rejected/no_clear_gain)
est couvert par 8 tests unitaires (`test_live_promotion.py`) et 2 tests d'intégration API
(`test_promotion_api.py`) qui simulent ces scénarios avec des données construites.

## 22. Limitations

- `LIVE_MIN_SAMPLE_SIZE=100` et `PROMOTION_MIN_IMPROVEMENT=0.01` sont des seuils
  "bootstrap" (même statut que tous les seuils Phase 5-9 déjà en place) — jamais validés
  sur des cycles shadow réels puisqu'aucun n'a encore eu lieu. À recalibrer une fois des
  données réelles accumulées.
- `evaluate_live_promotion` ne compare que sur le marché `1X2` par défaut dans le cron
  (seul marché modélisé par tous les moteurs ML actuels) — les endpoints HTTP acceptent un
  `market` explicite pour les autres marchés si un modèle venait à les supporter.
- Aucune version shadow n'existe en base actuellement : l'ensemble du pipeline de
  promotion Phase 10 est testé unitairement/en intégration mais pas encore exercé en
  conditions réelles de production — c'est attendu, un premier cycle shadow doit d'abord
  tourner (voir §23 recommandations).
- `AUTO_PROMOTION_ENABLED=false` par défaut : aucune promotion automatique ne peut avoir
  lieu tant qu'un humain n'active pas explicitement cette variable, après avoir observé
  au moins un cycle d'évaluation manuelle.

## 23. Risques

- Un seul admin est réellement identifiable pour l'instant (`ADMIN_EMAILS` à renseigner en
  production) — sans cette variable positionnée, `POST /models/promotion/*` refuse tout le
  monde (403), y compris l'opérateur légitime : à vérifier au déploiement.
- `evaluate_live_promotion` compare uniquement le `log_loss` — `no_clear_gain` peut rejeter
  un candidat objectivement meilleur en accuracy/Brier mais marginal en log loss ; cohérent
  avec la priorité déjà établie (log loss > Brier > accuracy, Phase 5/9), mais à surveiller
  si un modèle futur a un profil de métriques très différent.

## 24. Recommandations Phase 11

1. Mettre une version XGBoost ou LightGBM en mode shadow (`promotion.set_shadow`, via un
   futur endpoint ou manuellement) et laisser tourner au moins un cycle complet
   (scheduler → résolution → évaluation) pour obtenir les premières données LIVE réelles
   avant de recalibrer `LIVE_MIN_SAMPLE_SIZE`/`PROMOTION_MIN_IMPROVEMENT`.
2. Ajouter un cron pour `scripts/retrain_ml_models.py` (actuellement CLI-only, non planifié)
   si un réentraînement périodique automatique est souhaité — hors périmètre de cette Phase.
3. Envisager un endpoint HTTP admin pour `set_shadow` (aujourd'hui CLI-only) si le flux
   d'observation manuelle via `arena.html` devient le mode opératoire principal.
