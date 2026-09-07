# PHASE 15.8-DIAG — DIAGNOSTIC PAIEMENT CHARIOW CONFIRMÉ MAIS NON TRAITÉ

Généré : 2026-09-03 01:20:00 UTC

## 1. Faits établis (sources séparées)

| Source | État |
|---|---|
| **Chariow** | `pi_ih0s0eixhgm1`, 1500 FCFA, produit « xfoot 1 mois », statut **Terminé** |
| **Xfoot — Admin Abonnés** | USER 245, PROMOTEUR=`promoter-2`, PLAN=`—`, MONTANT=`—`, STATUT=`NONE` |
| **Xfoot — Admin Gains** | 0 vente, 0 FCFA revenu, 0 FCFA commission |

## 2. Contrat webhook audité — découverte critique

`POST /billing/pulse` compare le champ racine `event` du corps JSON à la chaîne exacte `"successful.sale"`.

**Comportement critique découvert** (`api/app/billing/router.py:590-595`) : pour **tout** `event_type` non reconnu, le code **ne fait rien**, mais retourne quand même `HTTP 200 {"received": true}` **et marque la delivery comme traitée**. Aucune erreur, aucun log, aucune trace ne distingue ce cas d'un traitement réussi — ni pour Chariow, ni pour Xfoot.

**Second point de silence** (`_handle_successful_sale`, ligne 619-621) : même si `event == "successful.sale"`, si `sale.custom_metadata.user_id` est absent, la fonction retourne silencieusement, sans rien signaler.

## 3-4. Webhook reçu ?

**NOT_VERIFIABLE** par cet assistant — aucun accès aux logs Railway ni au dashboard Chariow. **Action humaine recommandée** : consulter les logs Railway autour de l'horodatage du paiement (`POST /billing/pulse`), et l'historique de livraison des Pulses dans Chariow si disponible.

## 5. Mapping utilisateur — précédent historique documenté

Le webhook attend `sale.custom_metadata.user_id`. **Ce dépôt contient une trace documentée d'un bug réel de cette exacte nature** (`api/app/billing/router.py:271-279, 356-363`) : des achats antérieurs avaient une licence Chariow **sans aucune metadata du tout**, nécessitant un repli sur `customer.email`. Ceci prouve que le nommage/structure exact sous lequel Chariow échote ses metadata **n'est pas une hypothèse fiable à 100%** — déjà constaté au moins une fois dans l'histoire de ce projet.

## 6. Mapping produit — écarté comme cause

Le traitement du Pulse `successful.sale` ne relit **jamais** `PRODUCT_IDS`/`CHARIOW_PRODUCT_ID_MONTHLY` — il utilise exclusivement `sale.custom_metadata.plan` (valeur déjà choisie par Xfoot au checkout). Un éventuel souci de configuration Railway affecterait le **montant facturé** (déjà vérifié correct : 1500 FCFA), mais **pas** le traitement du webhook lui-même.

## 7. Traitement du statut — distinction critique

« Terminé » est un **label d'interface** Chariow (français, pour l'humain) — **pas** le champ JSON `event` comparé par le code. Aucune preuve dans ce dépôt qu'un Pulse `successful.sale` **réel** (non fabriqué par nos propres tests) ait jamais été inspecté avant ce paiement. Tous les tests existants **fabriquent** eux-mêmes la valeur `"event":"successful.sale"`.

**Risque identifié** : si Chariow envoie en réalité une chaîne différente, le webhook l'ignore silencieusement — symptôme identique à ce qui est observé.

## 8-11. Idempotence / Ledger / ProviderSubscription

Majoritairement **NOT_VERIFIABLE** (aucun accès DB). Déduction : une ligne `ProviderSubscription(user_id=245)` existe très probablement déjà (créée au checkout, cohérent avec la présence de 245 dans Admin Abonnés) — écartant le chemin « sub is None », renforçant par élimination les hypothèses `event_type`/`custom_metadata`.

## 9. État Referral

L'attribution `245 → promoter-2` est déjà visible (indépendante du paiement, comme prouvé en Phase 15.7.5). Le flux commission n'a simplement **jamais été atteint** — le mécanisme lui-même n'est pas en cause.

## 12. Timing asynchrone

Aucune queue/worker trouvée dans `api/app/billing/` — traitement **entièrement synchrone**. Seule source légitime de délai : la livraison elle-même côté Chariow (externe, non vérifiable).

## 13-14. Aucun replay, aucune correction

Confirmé — diagnostic de lecture de code uniquement.

## Cause racine la plus probable (non démontrée avec certitude)

| # | Hypothèse |
|---|---|
| A (leading) | `event` reçu ≠ `"successful.sale"` exactement — jamais validé contre un événement réel avant ce paiement |
| B | `event` correct mais `sale.custom_metadata.user_id` absent — précédent documenté dans ce projet |
| C | Webhook jamais reçu (signature/`CHARIOW_PULSE_SECRET` mal configuré, ou Pulse jamais configuré côté Chariow) |

**Aucune des trois ne peut être confirmée ou infirmée sans accès aux logs Railway ou à l'historique Chariow.**

## Verdict

**`WEBHOOK_DIAGNOSTIC_BLOCKED`**

Défaut de conception réel et démontré (pas une hypothèse) : tout `event_type` non reconnu ou `custom_metadata` manquant produit un 200 silencieux, cohérent à 100% avec les symptômes, renforcé par un précédent documenté. Mais aucun accès aux logs/dashboard externes ne permet de confirmer laquelle des 3 hypothèses s'applique à ce paiement précis.

## Action requise humaine

1. Logs Railway autour de l'horodatage du paiement — chercher `POST /billing/pulse`.
2. Historique de livraison des Pulses dans le dashboard Chariow — corps JSON exact envoyé, en particulier le champ `event` et la structure de `custom_metadata`.
3. Selon ce qui est trouvé : A (event différent), B (event correct, metadata mal structuré), ou C (jamais reçu).

---

**Aucune correction effectuée. Aucun second paiement. Aucun replay. Aucune modification DB/backend/Railway/Chariow/code. Aucun commit, aucun push.**

**PHASE 15.8-DIAG — DIAGNOSTIC WEBHOOK : AUDIT EXHAUSTIF DU CODE RÉVÈLE UN DÉFAUT DE CONCEPTION RÉEL — TOUT event_type NON RECONNU OU CUSTOM_METADATA MANQUANT PRODUIT UN HTTP 200 SILENCIEUX, SANS AUCUNE TRACE D'ERREUR, EXACTEMENT COHÉRENT AVEC LES SYMPTÔMES OBSERVÉS. RENFORCÉ PAR UN BUG HISTORIQUE DOCUMENTÉ DE MÊME NATURE (metadata/custom_metadata) DÉJÀ RENCONTRÉ DANS CE PROJET. AUCUN ACCÈS AUX LOGS RAILWAY NI AU DASHBOARD CHARIOW POUR CONFIRMER LAQUELLE DES 3 HYPOTHÈSES EXPLIQUE CE PAIEMENT PRÉCIS. VERDICT : WEBHOOK_DIAGNOSTIC_BLOCKED — ACTION HUMAINE REQUISE AVANT TOUTE DÉCISION DE CORRECTION.**
