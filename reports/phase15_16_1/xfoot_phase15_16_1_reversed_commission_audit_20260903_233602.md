# PHASE 15.16.1 — AUDIT DES COMMISSIONS REVERSÉES + RECHERCHE ADMIN PAR PROMOTEUR

Généré : 2026-09-03 23:36:02 UTC — **audit + implémentation locale uniquement, aucun commit/push/déploiement.**

## PARTIE A — Audit de « Commissions reversées »

**Définition exacte trouvée dans le code** (`api/app/referral/stats.py::compute_admin_totals`) : somme des `ReferralCommission.commission_amount` dont le statut est `REVERSED`. Ce statut n'est atteint **que** via `commission_service.py::reverse_commissions_for_subscription`, elle-même appelée **uniquement** depuis le handler du Pulse Chariow `license.revoked` (`billing/router.py:812`) — c'est-à-dire un **remboursement/révocation d'abonnement côté client**, jamais un versement de commission à un promoteur.

**Aucun lien de code avec `PromoterWithdrawal`** — vérifié explicitement (aucune référence à cette table ni au mot « withdrawal » dans `compute_admin_totals`).

**Preuve par test** : un promoteur avec une commission `ACCRUED` de 600 FCFA, intégralement versée via un retrait `PAID` (jamais remboursée), produit `reversed_commissions == 0` **et** `total_commissions (ACCRUED) == 600` — comportement identique et prouvé au cas réel `promoter-2`.

**Conclusion : `0 FCFA` est la valeur mathématiquement et définitionnellement correcte** pour `promoter-2` — aucun remboursement Chariow n'a eu lieu sur cet abonnement. Le retrait de 600 FCFA est un concept totalement distinct, jamais compté par ce compteur, et ce n'est pas censé l'être. **Aucun bug de calcul.** La confusion observée était purement lexicale : « reversées » (remboursées) ressemble à « versées » (payées aux promoteurs) en français, alors que ce sont deux concepts opposés.

**Aucune modification de logique effectuée.**

## PARTIE B — Sources de vérité

| Donnée | Source |
|---|---|
| A. Commissions générées | `ReferralCommission.commission_amount` WHERE `status='ACCRUED'` (inchangé) |
| B. Commissions remboursées | `ReferralCommission.commission_amount` WHERE `status='REVERSED'` (inchangé) |
| C. Retraits PENDING | `PromoterWithdrawal.amount` WHERE `status='PENDING'` (Phase 15.14, inchangé) |
| D. Retraits PAID | `PromoterWithdrawal.amount` WHERE `status='PAID'` (Phase 15.14, inchangé) |
| E. Disponible | `accrued - paid - pending`, clampé à 0 (Phase 15.14, inchangé) |

**Nouveau champ, purement dérivé** : `commission_total_requested` = somme de `PromoterWithdrawal.amount` tous statuts confondus (PENDING+PAID+REJECTED) — ajouté à `compute_promoter_available_amount()`, propagé par `compute_promoter_stats()`. Aucun compteur mutable créé.

## PARTIE C — Recherche admin par promoteur

**Réutilisation stricte** : `GET /admin/promoters?q=<slug ou email>` (Phase 14, déjà existant, déjà `require_admin`) réutilisé **tel quel** pour la recherche — aucune duplication de logique. Seul ajout : `GET /admin/withdrawals` accepte désormais un paramètre optionnel `promoter_id` (en plus de `status_filter` déjà existant), sur un endpoint **déjà** réservé à `require_admin` — pur confort de vue, n'élargit aucun accès (l'admin voit déjà toutes les demandes sans filtre). Testé : 401 sans authentification, 403 pour un utilisateur normal, 403 pour un promoteur non-admin.

## PARTIE D — Résumé financier par promoteur

**Aucun nouvel endpoint créé.** `GET /admin/promoters/{promoter_id}` (Phase 14, déjà existant) fait déjà `**compute_promoter_stats(...)` — désormais enrichi de `commission_total_requested`, donc le résumé complet (généré / demandé / versé / en attente / disponible) est automatiquement exposé, **toujours calculé côté serveur**, jamais par le frontend.

## PARTIE E — Distinction des concepts / libellés

`admin-earnings.html` : « Commissions reversées » → **« Commissions remboursées (client) »**. Clarifie sans changer le sens métier ni la valeur affichée (0 reste 0, calcul API inchangé — aucun renommage de champ API, aucune rupture de contrat). **Non fait** : le libellé « Commissions versées aux promoteurs » n'a **pas** été utilisé pour ce compteur car ce n'est **pas** ce qu'il représente — ce concept est désormais exposé séparément, correctement, sous « Total versé » dans le nouveau résumé par promoteur (Partie D).

## PARTIE F — Isolation

Admin peut rechercher tous les promoteurs (testé). Isolation promoteur A / B inchangée (`get_current_promoter`, non touché par cette phase). `promoter_id` client n'est qu'un filtre de confort sur une route déjà `require_admin`, testé non contournable par un non-admin.

## PARTIE G — Tests

`api/test_phase15_16_1_admin_promoter_search.py` — **41/41 assertions réussies** (audit Partie A + les 15 scénarios numérotés + matrice de sécurité). **TEST 15** reproduit exactement les montants du cas réel `promoter-2` (600 généré / 600 versé / 0 en attente / 0 disponible) avec un promoteur de test **entièrement isolé** — la vraie production n'est jamais touchée.

**Régression complète** (13 suites existantes) : **0 échec**.

## PARTIE H — Protection du test réel

Aucun appel de production effectué durant cette phase — audit et développement entièrement locaux, sur bases de test isolées (jamais `api/app.db`, jamais la production). Le retrait réel de `promoter-2` n'a été ni recréé, ni modifié, ni supprimé, ni re-crédité.

## PARTIE I — Isolation IA

6 tables strictement identiques (`match=12459, match_stats=12459, model_predictions=3610, model_versions=15, team_ratings=568, prediction_log=9`). `api/app/ai/readiness/gates.py` reste la modification étrangère pré-existante, non touchée.

## PARTIE J — Déploiement

Aucun commit, push, déploiement, migration de production, modification Railway/Chariow — non autorisé pour cette phase, respecté intégralement.

## Fichiers modifiés/créés

**Backend modifié** : `app/referral/withdrawal_service.py`, `app/referral/stats.py`, `app/referral/admin_router.py`.
**Backend nouveau** : `test_phase15_16_1_admin_promoter_search.py`.
**Frontend modifié** : `admin-withdrawals.html` (recherche + résumé), `admin-earnings.html` (libellé clarifié).

## Verdict

**`PROMOTER_WITHDRAWAL_SEARCH_IMPLEMENTED_TESTS_PASS`**

Sous-conclusion de l'audit (Partie A) : **`AUDIT_PASS_NO_FIX_REQUIRED`** — le compteur « Commissions reversées » était déjà mathématiquement correct (0 pour promoter-2), seule sa formulation prêtait à confusion ; corrigée par un changement de libellé, sans toucher au calcul ni à l'API.

---

**PHASE 15.16.1 — AUDIT TERMINÉ AVEC PREUVE DE CODE ET DE TEST : « COMMISSIONS REVERSÉES » NE MESURE QUE LES REMBOURSEMENTS CLIENTS (ReferralCommission.status='REVERSED', VIA license.revoked), STRICTEMENT SANS RAPPORT AVEC LES RETRAITS PROMOTEURS — 0 FCFA EST LA VALEUR CORRECTE POUR promoter-2, AUCUN BUG. SEULE UNE CLARIFICATION DE LIBELLÉ A ÉTÉ APPLIQUÉE (« Commissions remboursées (client) »), AUCUN CALCUL NI CHAMP API MODIFIÉ. NOUVELLE RECHERCHE ADMIN PAR PROMOTEUR (SLUG/EMAIL) IMPLÉMENTÉE EN RÉUTILISANT UN ENDPOINT DÉJÀ EXISTANT, AVEC RÉSUMÉ FINANCIER PAR PROMOTEUR (GÉNÉRÉ/DEMANDÉ/VERSÉ/EN ATTENTE/DISPONIBLE) TOUJOURS CALCULÉ CÔTÉ SERVEUR, AUCUN NOUVEL ENDPOINT CRÉÉ. 41/41 TESTS DÉDIÉS, RÉGRESSION COMPLÈTE À 0 ÉCHEC, 6 TABLES IA INCHANGÉES, AUCUNE ACTION DE PRODUCTION, LE RETRAIT RÉEL DE promoter-2 RESTE STRICTEMENT INCHANGÉ. AUCUN COMMIT, AUCUN PUSH, AUCUN DÉPLOIEMENT. VERDICT : PROMOTER_WITHDRAWAL_SEARCH_IMPLEMENTED_TESTS_PASS (SOUS-AUDIT : AUDIT_PASS_NO_FIX_REQUIRED).**
