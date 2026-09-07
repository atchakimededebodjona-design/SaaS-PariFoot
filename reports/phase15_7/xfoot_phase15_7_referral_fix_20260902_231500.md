# PHASE 15.7 — REFERRAL ATTRIBUTION FIX V1

Généré : 2026-09-02 23:15:00 UTC

## 1. Problème initial

3 comptes créés après passage par `https://www.xfoot.site/promoter-2` apparaissent dans Admin → Abonnés (donc ont atteint le checkout) mais avec **PROMOTEUR = « — »**.

## 2. Comptes de reproduction

`demo@payement.com` (236), `demo11@test.com` (238), `demo2@payement.com` (239).

## 3. Cause racine

### Le backend est rigoureusement disculpé

Un test de reproduction (`api/test_phase15_7_referral_attribution_repro.py`, TEST 1) rejoue **exactement** la séquence et le **format exact** du navigateur réel :

1. `POST /referral/resolve/promoter-2` **sans authentification** (mime `captureReferralFromUrl`)
2. Inscription + connexion
3. `POST /referral/attribute` **avec un `captured_at` au format JS exact** (`...Z`, jamais `+00:00` comme dans tous les autres fichiers de test du dépôt — écart de couverture comblé ici)

**Résultat : `ReferralAttribution` créée correctement, `promoter_id` exact, visible dans Admin → Abonnés avec `promoter_slug='promoter-2'`.** Ceci prouve que le backend n'a **aucun bug** dans ce mécanisme.

### Cause identifiée côté frontend

**Fichier** : `frontend-design/login.html`, lignes 73-82 (avant correctif)

```js
if (getToken()) {
    window.location.href = "dashboard.html";
} else {
    captureReferralFromUrl();
}
```

**Bug** : `captureReferralFromUrl()` n'était appelée que dans la branche `else`. Si un token JWT résiduel d'une session **précédente** (ex. un autre compte de test dans le même navigateur, jamais explicitement déconnecté) était déjà présent en `localStorage` au moment de visiter `/promoter-2`, `login.html` redirigeait **immédiatement** vers `dashboard.html` **sans jamais capturer le slug** — silencieusement, aucune erreur visible. Le slug était alors perdu pour de bon.

**Confiance** : élevée mais non confirmée à 100 %. C'est le **seul** écart de code réel trouvé après un audit exhaustif (capture, stockage, persistance, sécurité, conditions métier — toutes auditées sans anomalie), et il est cohérent avec l'historique de test de cette conversation (même navigateur utilisé intensivement pour `payment@test.com` juste avant). Non confirmé par observation directe des navigateurs ayant réellement servi à créer 236/238/239.

### Hypothèses écartées avec preuve

- Format `captured_at` ('Z' vs '+00:00') — testé directement avec Pydantic 2.13.4, parsé correctement dans les deux cas.
- Bug de résolution du slug côté backend — testé et confirmé fonctionnel à plusieurs reprises.
- Self-referral, promoteur inactif, fenêtre 30 jours, attribution déjà existante — toutes testées, fonctionnent comme attendu, n'expliquent pas ce cas.
- CORS cassé sur `/referral/*` — écarté (register/login/checkout, même politique CORS, fonctionnent).
- Bug d'affichage Admin → Abonnés — écarté par lecture de code ET empiriquement (TEST 2 du nouveau fichier).

## 4-6. Correctif

**Fichier modifié** : `frontend-design/login.html` (seul fichier backend/frontend touché). **Fichier ajouté** : `api/test_phase15_7_referral_attribution_repro.py`.

```diff
- if (getToken()) {
-     window.location.href = "dashboard.html";
- } else {
-     captureReferralFromUrl();
- }
+ captureReferralFromUrl();
+ if (getToken()) {
+     window.location.href = "dashboard.html";
+ }
```

**Justification** : capturer le referral est une opération sans effet de bord (écriture `localStorage` best-effort), indépendante de l'état de connexion — la conditionner à l'absence de token n'avait aucune raison de sécurité ou métier. Ce changement ne touche **aucune** ligne backend, **aucune** règle de sécurité, de self-referral, de fenêtre d'attribution ou de persistance (toutes restent dans `attribute_referral`, inchangé). `/promoter-2` continue de fonctionner à l'identique.

## 7-8. Tests

**Avant** : aucun test existant du dépôt ne reproduisait la séquence exacte (scénario purement navigateur, jamais couvert). **Après** : nouveau fichier — **24/24** (dont la reproduction fidèle du parcours réel + toutes les protections existantes). Régression complète : **66/66, 0 échec réel** (65 préexistants + 1 nouveau).

## 9-13. Parcours réel — NON EXÉCUTÉ

**Non exécuté par cet assistant** — aucun outil de navigateur disponible. L'Étape 8 (fenêtre privée, inscription immédiate, vérification Admin) nécessite une exécution humaine réelle, non simulable ici. C'est la **limite principale** de cette phase.

## 14-16. Checkout / Commission

Code correct (`prd_sgvapilx`, formule commission inchangée : `floor(1500 × 4000 / 10000) = 600`). **Rappel** : le point ouvert Railway (`CHARIOW_PRODUCT_ID_MONTHLY`, Phase 15.6-CORRECTION) reste non confirmé comme résolu — distinct de ce correctif, mais tout aussi bloquant avant paiement réel.

## 17. Intégrité IA

6 tables strictement identiques : `match=12459, match_stats=12459, model_predictions=3610, model_versions=15, team_ratings=568, prediction_log=9`.

## 18. Git

```
M frontend-design/login.html
?? api/test_phase15_7_referral_attribution_repro.py
```

Diff : `frontend-design/login.html | 20 ++++++++++++++------ (1 file changed, 14 insertions(+), 6 deletions(-))` — strictement limité au sujet referral. Aucun `git add .`/`-A`. **Aucun commit, aucun push.**

## 19. Limites

- Aucun accès navigateur — pas de validation réelle du parcours production.
- La cause identifiée est la seule trouvée après audit exhaustif, mais non confirmée comme LA cause exacte des 3 échecs réels.
- Un correctif frontend ne peut, par nature, jamais être prouvé à 100 % par un test backend Python.
- Point Railway toujours ouvert, indépendant.
- **Correctif local uniquement — sans aucun effet en production** tant qu'il n'est ni committé, ni poussé, ni uploadé manuellement sur Hostinger.

## 20. Recommandations

1. Valider via l'Étape 8 exactement comme spécifié (fenêtre privée, visite directe, inscription immédiate, aucune autre connexion entre-temps).
2. Si validé : demander une autorisation humaine distincte pour commit / push / upload Hostinger de `login.html`.
3. Ne pas oublier le point Railway ouvert avant tout paiement réel.
4. Envisager, en phase séparée avec approbation explicite, une robustesse accrue d'`attributeReferralIfPresent()` — non fait ici pour respecter le correctif le plus petit possible.

## Verdict

**`REFERRAL_FIX_IMPLEMENTED_TESTS_PASS`**

Backend disculpé par reproduction fidèle (24/24). Cause racine la plus probable corrigée par le changement le plus petit possible. 66/66 tests, 6 tables IA inchangées, diff limité, aucun commit/push. Le parcours réel de production reste à valider par un humain — seule preuve manquante avant un verdict pleinement positif.

---

**PHASE 15.7 — REFERRAL ATTRIBUTION FIX V1 : BACKEND RIGOUREUSEMENT DISCULPÉ PAR REPRODUCTION FIDÈLE DE LA SÉQUENCE ET DU FORMAT RÉELS (24/24). CAUSE RACINE LA PLUS PROBABLE IDENTIFIÉE (captureReferralFromUrl() BYPASSÉE SI UN TOKEN RÉSIDUEL EXISTE DÉJÀ EN LOCALSTORAGE, frontend-design/login.html) ET CORRIGÉE PAR LE CHANGEMENT LE PLUS PETIT POSSIBLE (1 SEUL FICHIER FRONTEND, 0 LIGNE BACKEND, 0 RÈGLE DE SÉCURITÉ TOUCHÉE). 66/66 TESTS VERTS, 6 TABLES IA INCHANGÉES, DIFF STRICTEMENT LIMITÉ AU REFERRAL. AUCUN PAIEMENT, AUCUNE COMMISSION CRÉÉE, AUCUNE ÉCRITURE CHARIOW, AUCUN COMMIT, AUCUN PUSH. LE PARCOURS RÉEL DE PRODUCTION (ÉTAPE 8) RESTE À VALIDER PAR UN HUMAIN — SEULE PREUVE MANQUANTE AVANT UN VERDICT PLEINEMENT POSITIF.**
