# PHASE 11 — RAPPORT FINAL — XFOOT AI Arena : Shadow Production & Live Data Accumulation

## 1. Audit initial

Réalisé en s'appuyant directement sur le travail de la Phase 10 (mêmes échanges, contexte
encore complet — voir la justification détaillée dans le plan approuvé avant exécution,
pas de nouvel audit par sous-agent). Constat central, vérifié par lecture de code et par
`grep` (aucun résultat pertinent pour "matched"/"shadow/status"/"generate_shadow_predictions"
avant cette Phase) :

- `scripts/generate_live_predictions.py` + `app/ai/arena/scheduler.py::generate_live_
  predictions` (Phase 9) génèrent **déjà**, dans le **même run**, les prédictions ACTIVE et
  SHADOW pour les **mêmes fixtures** (une seule liste de fixtures, un seul `now`) — les
  sections 2/3/5/6 du prompt Phase 11 étaient donc déjà satisfaites par construction.
- `fetch_daily_results.py` résout déjà `model_predictions` sans filtrer sur `role`
  (§25 Phase 9, testé par `test_resolution_shadow.py`) — section 7 déjà satisfaite.
- `live_validation.py` (Phase 10) scope déjà ses métriques par `model_version_id` précis,
  jamais par `role` seul — section 8 déjà satisfaite.
- **Manquait réellement** : une comparaison "matched" (intersection stricte des matchs
  prédits par les deux, section 9), les endpoints `/models/shadow/*` (section 10), la
  section frontend dédiée (section 11), des logs structurés aux noms exacts demandés
  (section 14), et les tests correspondants (section 16-17).

## 2. Architecture existante réutilisée

`app/ai/arena/service.py::_market_observation`/`_compute_market_metrics` (calcul de
métriques, Phase 5), `app/ai/arena/promotion.py::get_active_version`/`LIVE_MIN_SAMPLE_SIZE`
(Phase 10, aucun nouveau seuil créé), `app/ai/arena/monitoring.py::get_live_summary`
(Phase 9), `app/ai/arena/availability.py::compute_model_availability` (Phase 8),
`app/ai/arena/live_validation.py::compute_live_model_metrics` (Phase 10, réutilisé pour les
compteurs pending/resolved par version dans `GET /models/shadow/status`).

## 3. Fichiers créés

- `api/app/ai/arena/shadow_comparison.py` — `compute_matched_comparison`.
- `api/test_live_shadow_comparison.py` — logique matched + test end-to-end local (§17).
- `api/test_shadow_api.py` — endpoints `/models/shadow/*`.
- `RAPPORT_PHASE11.md` (ce document).

## 4. Fichiers modifiés

- `api/app/ai/arena/scheduler.py` — logs structurés additifs
  (`SHADOW_PREDICTION_CREATED`/`SHADOW_PREDICTION_ALREADY_EXISTS`/`SHADOW_PREDICTION_
  SKIPPED`) autour de la boucle shadow existante ; **aucune logique changée** (vérifié :
  `test_scheduler.py` et `test_shadow_mode.py` passent sans modification de leur intention).
- `fetch_daily_results.py` — une ligne de récap `[SHADOW_RESOLUTION_COMPLETED] count=...`
  (comptage `role="shadow"` parmi les lignes résolues de ce run) ; logique de résolution
  **inchangée** (vérifié par `test_fetch_daily_results.py`, `test_resolution_shadow.py`).
- `api/main.py` — deux nouveaux endpoints publics `GET /models/shadow/status` et
  `GET /models/shadow/comparison`.
- `api/test_shadow_mode.py` — 2 tests ajoutés (même match/IDs différents, immutabilité
  shadow post-résolution).
- `api/test_anti_leakage_phase9.py` — inchangé depuis la Phase 10 (aucune nouvelle
  modification requise pour cette Phase).

## 5. Shadow Mode

Inchangé (Phase 9/10) — `promotion.py::set_shadow`, isolation stricte déjà en place.
Aucune reconstruction, conformément à la règle du prompt "NE RECONSTRUIS PAS ce qui existe
déjà".

## 6. Active vs Shadow

Garantie "même match, même cutoff temporel" déjà assurée structurellement par le fait
qu'un seul run de `generate_live_predictions()` traite la même liste de fixtures pour les
deux rôles. Revérifiée explicitement par 2 nouveaux tests (`test_active_and_shadow_predict_
same_match_with_different_ids`, `test_shadow_prediction_immutable_after_resolution`) en
plus de la couverture déjà existante (`test_shadow_mode.py`, `test_anti_leakage_phase9.py`).

## 7. Live Prediction Pipeline

Aucun changement structurel — `scripts/generate_shadow_predictions.py` séparé,
explicitement demandé par le prompt (section 5), **volontairement pas créé** : un job
séparé aurait dupliqué `scheduler.py::generate_live_predictions` et risqué de casser la
garantie "même information, même instant" en tirant les fixtures à deux moments différents
— écart documenté conformément à la règle 15 du prompt Phase 10 (documenter un conflit
plutôt que le contourner en silence, réappliquée ici).

## 8. Resolution

`fetch_daily_results.py` inchangé dans sa logique — seul un compteur de récapitulatif
supplémentaire (`SHADOW_RESOLUTION_COMPLETED`) a été ajouté. Idempotence revérifiée par
`test_fetch_daily_results.py::test_already_resolved_prediction_is_not_refetched` et
`test_resolution_shadow.py` (2/2 tests, aucun changé).

## 9. Matched Comparison

`shadow_comparison.py::compute_matched_comparison(session, model_type, market,
shadow_version_id=None)` — joint les prédictions résolues ACTIVE et SHADOW sur la clé
naturelle (league, match_date, home_team, away_team), ne retient que l'intersection.
États explicites : `no_active`, `no_shadow`, `insufficient_matched_sample`, `ok`. Testé
(7 tests, `test_live_shadow_comparison.py`) : exclusion des matchs prédits par un seul
côté, non-mélange de deux versions shadow successives, calcul correct des deltas
(shadow − active), et test end-to-end local (1 match synthétique → `insufficient_matched_
sample` honnête, jamais un "ok" fabriqué à partir d'un seul point).

## 10. API

- `GET /models/shadow/status` (public) — actif + liste des shadow par `model_type`,
  compteurs pending/resolved scopés par `model_version_id`.
- `GET /models/shadow/comparison` (public) — résultat de `compute_matched_comparison`,
  log `LIVE_SHADOW_EVALUATION_COMPLETED` à chaque appel.
- Aucune mutation, donc aucun palier admin nécessaire ici (contrairement à
  `/models/promotion/*`, Phase 10).

## 11. Frontend

Nouvelle section "Live Shadow Comparison" dans `arena.html`, sous "Model Health &
Promotion" — pour chaque `model_type` : "Aucune version SHADOW active" si aucun shadow,
"INSUFFICIENT LIVE DATA" si l'échantillon matched est sous le seuil, sinon les métriques
actif/shadow côte à côte + deltas. **Aucun "Best Model" n'est jamais affiché** en dessous
du seuil (vérifié visuellement dans le template : la branche `status !== 'ok'` ne rend
jamais les métriques, seulement la raison).

## 12. Cron

Audit des 3 cron Railway existants (`railway.cron.live_predictions.json` 05:30 UTC,
`railway.cron.results.json` 06:30 UTC, `railway.cron.evaluate_models.json` 07:00 UTC,
Phase 10) : ordre et fréquence cohérents, idempotence déjà garantie par la contrainte
UNIQUE de `model_predictions`. **Aucun nouveau cron créé** — la comparaison matched est
calculée à la demande via l'API, pas un job planifié (rien ne justifiait un job dédié).

## 13. Anti-data-leakage

Aucune nouvelle surface de fuite introduite : `compute_matched_comparison` ne lit que des
`ModelPrediction.status == "resolved"` (jamais `pending`), et la résolution elle-même
(`fetch_daily_results.py`, inchangée) reste l'unique frontière de confiance temporelle.
Le test end-to-end (§17) vérifie explicitement l'ordre prédiction → stockage → résolution
→ métriques, jamais l'inverse.

## 14. Tests

**27/27 fichiers de test passent (exit code 0)**, y compris les 3 fichiers root
(`test_fetch_daily_results.py`, `test_resolution_shadow.py`, `test_refresh_and_retrain.py`,
non couverts par la boucle `api/test_*.py` mais directement concernés par les modifications
de `fetch_daily_results.py`). Nouveaux : `test_live_shadow_comparison.py` (7/7),
`test_shadow_api.py` (5/5), `test_shadow_mode.py` étendu (8/8, dont les 2 nouveaux). Aucun
test existant modifié dans son intention ni supprimé.

## 15. Résultats réels

Chiffres lus directement dans `api/app.db` au moment de ce rapport — identiques à ceux du
rapport Phase 10, car **aucun cron n'a tourné en production entre les deux rapports** (ce
qui est cohérent : la Phase 11 construit l'outillage, elle n'exécute pas elle-même de cron
en production, voir §17) :

- Prédictions LIVE : 10 (`role="active"`, toutes `status="pending"`).
- Résolues (LIVE) : 0.
- Pending (LIVE) : 10.
- Prédictions shadow : **0** (`role="shadow"` : 0 ligne).
- Matched : **0** (aucune paire active/shadow possible sans shadow).
- Métriques réellement disponibles : aucune — `GET /models/shadow/status` renverrait
  `"shadow": []` pour tous les `model_type`, `GET /models/shadow/comparison` renverrait
  `"no_shadow"` pour tous — vérifié par smoke-test réel (`TestClient`, base vide).

## 16. Performance LIVE

Non mesurable — 0 prédiction shadow, 0 comparaison matched possible. Aucune donnée
inventée pour combler ce vide (règle 19 du prompt).

## 17. Promotion

`AUTO_PROMOTION_ENABLED=false` (Phase 10, inchangé). Aucune promotion automatique n'a été
effectuée, et ne pouvait pas l'être : aucune version shadow n'existe en base, donc aucun
candidat à évaluer. Cette Phase n'a d'ailleurs touché aucun seuil de promotion (règle 12
du prompt Phase 11).

## 18. Limitations

- Le pipeline Shadow (génération, résolution, comparaison matched, endpoints, UI) est
  fonctionnel et testé unitairement/en intégration, mais **n'a jamais tourné en conditions
  réelles de production** — aucune `ModelVersion` n'est en `status="shadow"` sur la base
  utilisée par les cron Railway (Postgres). Décision explicite avec l'utilisateur (voir
  plan approuvé) : je n'ai pas cette base sous la main, donc pas d'activation effectuée,
  ni même en local — le choix retenu était "code + doc uniquement".
- `LIVE_MIN_SAMPLE_SIZE=100` (seuil réutilisé de la Phase 10) s'applique tel quel à
  l'échantillon **matched** — potentiellement plus difficile à atteindre qu'un échantillon
  indépendant (un match non prédit par l'un des deux côtés ne compte pas), donc le premier
  seuil à observer/recalibrer une fois des données réelles disponibles.
- `GET /models/shadow/status` agrège les compteurs "active" par `role` (convention Phase 9
  déjà en place, monitoring.py), pas par `model_version_id` — imprécision mineure et
  préexistante si l'historique contient plusieurs versions actives successives ; le côté
  "shadow" de cette même vue, lui, est scopé par version (voir §10).

## 19. Risques

- Tant qu'aucune version shadow réelle n'existe, `GET /models/shadow/*` renvoie toujours
  un état "aucune donnée" — ce n'est pas un bug, mais un déploiement qui active enfin le
  shadow mode devra être suivi pour confirmer que les compteurs progressent (logs
  `SHADOW_PREDICTION_CREATED`/`SHADOW_RESOLUTION_COMPLETED` prévus pour ça).
- La comparaison matched, en excluant les matchs non-communs, peut réduire fortement
  l'échantillon exploitable par rapport à l'échantillon "tout résolu" utilisé par
  `evaluate_live_promotion` (Phase 10) — les deux vues peuvent donc afficher des statuts
  différents (`ok` vs `insufficient_matched_sample`) simultanément ; comportement voulu,
  mais à expliquer si un utilisateur s'en étonne.

## 20. Recommandations Phase 12

1. **Activer réellement une version shadow en production** (`promotion.set_shadow`, sur
   la base Postgres Railway) — c'est le seul geste qui manque désormais pour commencer à
   accumuler des données LIVE réelles ; tout le reste est prêt et testé.
2. Une fois un premier cycle shadow réel observé (quelques semaines), recalibrer
   `LIVE_MIN_SAMPLE_SIZE`/`PROMOTION_MIN_IMPROVEMENT` (Phase 10) sur la base de l'écart
   type observé entre échantillons matched et non-matched.
3. Envisager d'exposer `GET /models/shadow/comparison` avec un paramètre de fenêtre
   temporelle (ex. "30 derniers jours matched") si le volume LIVE devient assez important
   pour qu'une tendance récente ait un sens — hors périmètre tant que l'échantillon total
   reste faible.
