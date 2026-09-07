# PHASE 15.9 — DURCISSEMENT DU WEBHOOK CHARIOW + RÉCUPÉRATION CONTRÔLÉE

Généré : 2026-09-03 02:00:00 UTC

## PARTIE A — Correction du webhook

### Défaut constaté (confirmé)

Deux points de silence identifiés en Phase 15.8-DIAG, **confirmés ici par test exécuté** (pas seulement par lecture) :
1. Tout `event_type` non reconnu → `HTTP 200` + marqué traité, sans aucune trace.
2. `successful.sale` sans `custom_metadata.user_id` → retour silencieux, même en cas de `custom_metadata` totalement absente.

### Correction appliquée — `api/app/billing/router.py` (seul fichier backend touché)

**`chariow_pulse` :**
- `json.JSONDecodeError` → **400 explicite** (jamais une 500 générique).
- Champ `event` absent → **400 explicite** (un vrai Pulse Chariow porte toujours ce champ).
- `event_type` présent mais non géré → **200 conservé** (décision assumée, voir ci-dessous) **mais désormais journalisé explicitement**.

**`_handle_successful_sale` :**
- `user_id` non numérique ou `ProviderSubscription` introuvable → `logger.warning()` explicite.
- **Nouveau repli EMAIL** : réutilise `_find_provider_subscription_by_email` — **mécanisme déjà existant et déjà testé** pour les Pulses `license.*`, aucun nouveau système de confiance inventé.
- Aucun repli ne fonctionne → `logger.warning("IGNORÉ : ...")` explicite, puis `return` (toujours 200 à Chariow, mais désormais 100% diagnosticable).
- `sub.plan = custom_metadata.get("plan") or sub.plan` — ne jamais écraser un plan déjà connu par une valeur absente en repli email.
- Bug introduit puis corrigé **avant tout test** : `recompute_entitlement` utilise désormais `sub.user_id` (fiable dans les deux chemins), pas la variable locale `user_id` qui n'existait pas en repli email.

### Décision de conception assumée — pourquoi 200 reste 200 pour un type non géré

Le code documentait déjà que Chariow réessaie indéfiniment un Pulse non-200. Changer ce comportement pour *tout* type non reconnu risquerait une tempête de retry pour de futurs types légitimes (ex. remboursements) que Xfoot ne gère simplement pas encore. **Distinction retenue** : type présent mais inconnu → 200 (accusé honnête) + log ; `event` absent ou JSON invalide (payload structurellement non conforme) → 400. **Jugement d'ingénierie explicite, présenté ici pour validation humaine plutôt que décidé silencieusement.**

### Tests

| Suite | Résultat |
|---|---|
| `test_phase15_9_webhook_hardening.py` (14 scénarios demandés + variantes) | **31/31** |
| Régression complète | **67/67** (66 préexistants + le nouveau) |

Capture **réelle** des logs Python (pas une relecture de code) pour confirmer la journalisation effective. Sécurité/idempotence reconfirmées : signature invalide → 401, delivery_id rejoué (même contenu ou contradictoire) → jamais de retraitement, 3 envois du même événement → exactement 1 commission. Aucun secret journalisé.

### Isolation IA

6 tables strictement identiques : `match=12459, match_stats=12459, model_predictions=3610, model_versions=15, team_ratings=568, prediction_log=9`.

### Git

**Aucun commit, aucun push** — aucune autorisation explicite donnée pour cette phase précise.

---

## PARTIE B — Récupération du paiement réel `pi_ih0s0eixhgm1`

### B1 — Recherche d'une voie existante

**`POST /billing/activate-license`** — endpoint **déjà existant**, jamais créé pour cette phase : *« filet de sécurité si la redirection post-paiement échoue/tarde et que le Pulse successful.sale/license.activated n'est pas encore arrivé »* — exactement le scénario actuel. Aucun autre mécanisme de synchronisation/réconciliation Chariow trouvé.

### B2 — Le mécanisme officiel identifié

L'utilisateur **authentifié** (donc USER 245 lui-même) colle sa clé de licence Chariow (reçue par email). Le backend appelle `GET {CHARIOW_API_BASE_URL}/licenses/{key}` — un **vrai appel en lecture seule à l'API officielle Chariow**, jamais un scraping. Vérification d'appartenance par `metadata.user_id` ou repli `customer.email`. Met à jour `ProviderSubscription` via le flux métier normal, aucune table modifiée directement.

**⚠️ Gap réel découvert** : `activate-license` **n'appelle jamais** `create_commission_for_confirmed_payment()` (confirmé — un seul point d'appel dans tout le code, exclusivement dans `_handle_successful_sale`). **Conséquence : même en cas de succès, l'abonnement de USER 245 deviendrait actif, mais aucune commission ne serait créée pour `promoter-2`.** Ce chemin est donc **partiel**, pas complet.

**Action requise** : USER 245 (`oktest@test.com`) doit se connecter lui-même et coller sa clé de licence — action strictement humaine, hors de portée de cet assistant (aucune authentification possible en son nom, et ce serait de toute façon usurper une action réservée au titulaire du compte).

### B3 — Replay officiel Chariow

**Non vérifiable** par cet assistant (aucun accès dashboard Chariow). Même s'il existait, **le rejouer maintenant ne changerait rien** : le code en production est encore l'ancien (la Partie A est validée localement uniquement, non déployée) — un replay recevrait le même traitement défaillant. **Un replay officiel ne deviendrait utile qu'après déploiement de la Partie A.** Aucun replay tenté.

### B4-B5 — Verdict de récupération

**Aucune récupération complète immédiate possible.** Ce qui manque précisément :
1. Action humaine de USER 245 (activate-license) — hors de portée de cet assistant, et de toute façon partielle (pas de commission).
2. Confirmation d'un replay officiel Chariow — accès dashboard manquant.
3. Autorisation de déployer la Partie A — condition préalable à tout replay utile.
4. Une extension d'`activate-license` pour y ajouter la création de commission serait une vraie correction utile, mais **excède le périmètre de cette phase** (Partie B interdit d'inventer une nouvelle voie) — proposée comme recommandation séparée.

### B6-B10

**Non applicable** — aucune récupération effectuée, aucune donnée fabriquée. Aucun remboursement réel.

---

## PARTIE C — Limitations

- Aucun accès logs Railway, aucun accès dashboard Chariow, aucun outil de navigateur, aucune autorisation de déploiement cette phase.
- **Aucune preuve manquante inventée. Aucun paiement Xfoot déclaré traité uniquement parce que Chariow affiche « Terminé ».**

## Verdict

**`WEBHOOK_FIXED_REAL_PAYMENT_REQUIRES_OFFICIAL_REPLAY`**

Partie A complète et validée localement (31/31 + 67/67, 6 tables IA inchangées, 1 seul fichier backend + 1 fichier de test). Partie B bloquée par des conditions hors de portée de cette phase (action de USER 245, confirmation Chariow, autorisation de déploiement).

---

**PHASE 15.9 — PARTIE A TERMINÉE ET VALIDÉE LOCALEMENT : WEBHOOK DURCI (1 SEUL FICHIER api/app/billing/router.py + 1 NOUVEAU FICHIER DE TEST), 31/31 TESTS DÉDIÉS, 67/67 RÉGRESSION COMPLÈTE, 6 TABLES IA INCHANGÉES, AUCUNE RÉGRESSION. PARTIE B : AUCUNE RÉCUPÉRATION EFFECTUÉE — UN MÉCANISME OFFICIEL EXISTE (POST /billing/activate-license, LECTURE SEULE VIA L'API CHARIOW RÉELLE) MAIS EST PARTIEL (NE CRÉE PAS LA COMMISSION, GAP DÉCOUVERT) ET NÉCESSITE UNE ACTION DE USER 245 LUI-MÊME ; UN REPLAY OFFICIEL CHARIOW ÉVENTUEL NE SERAIT UTILE QU'APRÈS DÉPLOIEMENT DE LA CORRECTION, NON AUTORISÉ CETTE PHASE. AUCUNE DONNÉE FABRIQUÉE, AUCUN DEUXIÈME PAIEMENT, AUCUNE MODIFICATION DB/RAILWAY/CHARIOW, AUCUN COMMIT, AUCUN PUSH. VERDICT : WEBHOOK_FIXED_REAL_PAYMENT_REQUIRES_OFFICIAL_REPLAY — EN ATTENTE DE DÉCISIONS HUMAINES SÉPARÉES (DÉPLOIEMENT DE LA PARTIE A, ET/OU ACTION DE USER 245 VIA activate-license, ET/OU EXTENSION FUTURE DE CE ENDPOINT POUR COUVRIR LA COMMISSION).**
