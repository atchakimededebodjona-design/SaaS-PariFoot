# PHASE 15.6-R2 — DIAGNOSTIC ATTRIBUTION REFERRAL RÉELLE

Généré : 2026-09-02 22:30:00 UTC

## Compte diagnostiqué

`demo@payement.com` / user_id=236 — visible dans Admin → Abonnés, statut paiement NONE, **colonne PROMOTEUR = « — »**.

## Prémisse déjà établie

Contrairement au cas précédent (Phase 15.6-R, compte inconnu), **236 EST DÉJÀ visible dans Admin → Abonnés**. D'après le diagnostic de code de la phase précédente, cela prouve qu'une ligne `ProviderSubscription` existe déjà (le checkout a été atteint). **L'hypothèse « jamais passé par le checkout » est donc écartée pour ce compte** — le problème est ailleurs.

## Étape 1 — L'attribution existe-t-elle pour 236 ?

Recherche directe **NOT_VERIFIABLE** (aucun accès DB). Mais une déduction solide est possible via le code d'`admin_router.py:159-181` :

```python
attribution = attributions.get(sub.user_id)
promoter = promoters_by_id.get(attribution.promoter_id) if attribution else None
promoter_slug = promoter.slug if promoter else None
```

`promoters_by_id` est construit **directement** à partir des `promoter_id` trouvés dans les attributions existantes — donc si une `ReferralAttribution` existe, son promoteur sera trouvé, **sauf** si ce `Promoter` avait été supprimé (aucun endpoint `DELETE` sur `Promoter` n'existe dans tout le code — quasi impossible).

**Conclusion à haute confiance** : `PROMOTEUR = « — »` signifie très probablement qu'**aucune ligne `ReferralAttribution` n'existe** pour `converted_user_id=236`. Ce n'est **pas** un bug d'affichage admin.

## Étape 2 — Où le processus échoue-t-il ?

Le mécanisme normal : `captureReferralFromUrl()` (au chargement de `login.html`) → `localStorage` → `attributeReferralIfPresent()` (awaité avant redirection) → `POST /referral/attribute`.

### Point de défaillance n°1 — bypass par un token pré-existant

```js
// login.html:73-82
if (getToken()) {
    window.location.href = "dashboard.html";
} else {
    captureReferralFromUrl();
}
```

Si le navigateur utilisé pour visiter `/promoter-2` avait déjà un token JWT valide en `localStorage` (ex. session laissée ouverte de `payment@test.com`, testé dans les étapes précédentes de cette même conversation), `login.html` redirige **immédiatement** vers `dashboard.html` **sans jamais appeler `captureReferralFromUrl()`** — le `?ref=promoter-2` est silencieusement ignoré, aucune erreur visible.

### Point de défaillance n°2 — slug consommé par une connexion intermédiaire

`attributeReferralIfPresent()` s'exécute à **chaque** soumission réussie du formulaire — connexion **ou** inscription, sans distinction. Son `finally` efface **toujours** `xfoot_referral_slug`/`xfoot_referral_captured_at`, que l'attribution ait réussi ou non. Scénario plausible : visite de `/promoter-2` (slug capturé) → une simple connexion à `payment@test.com` dans le même navigateur, avant l'inscription → consomme et efface le slug → inscription de `demo@payement.com` juste après, sans repasser par `/promoter-2` → plus rien à attribuer.

`clearToken()` (bouton Déconnexion) ne touche **jamais** ces clés de referral — une simple déconnexion entre deux comptes ne réinitialise pas ce risque.

### Conditions métier écartées

Self-referral (236≠206), promoteur inactif (ACTIVE confirmé), fenêtre de 30 jours, regex `.htaccess` (« promoter-2 » matche correctement) — toutes écartées.

**Conclusion** : aucune preuve d'erreur réseau ou de rejet métier — la cause la plus probable est un **problème de séquencement côté navigateur**, pas un bug du mécanisme lui-même (audité et jugé correct).

## Étape 3 — Bug d'affichage admin ?

**Non applicable** — l'Étape 1 conclut que la ligne elle-même est probablement absente, pas que l'affichage la masquerait à tort.

## Étape 4 — Promoteur 206

`POST /referral/resolve/promoter-2` (sans `visitor_id`, zéro écriture) → `200 {"valid": true}` — confirme existence + ACTIVE. `user_id=206` non exposé par cet endpoint public (par design), NOT_VERIFIABLE directement.

## Étape 5 — Comparaison 209 vs 236

Les deux comptes sont visibles dans Admin → Abonnés (donc tous deux passés par le checkout). Aucun des deux n'a d'attribution confirmée par observation directe. Si les deux manquent effectivement d'attribution, cela pointe vers un problème **systématique de séquencement de test** (même navigateur, jamais repassé proprement par `/promoter-2` juste avant chaque inscription) plutôt qu'un bug isolé sur un seul compte.

## Étape 6 — Checkout — ⚠️ point ouvert toujours non résolu

Code correct. **Rappel** : le statut de la correction Railway (`CHARIOW_PRODUCT_ID_MONTHLY`, Phase 15.6-CORRECTION) n'a toujours pas été confirmé comme effectué. Point distinct, toujours bloquant.

## Étape 7 — Futur webhook

Confirmé inchangé : lecture fraîche par `converted_user_id`. Si attribution absente : **commission = 0 FCFA**, pas 600 FCFA — le paiement lui-même resterait valide pour l'utilisateur, mais sans aucune commission pour `promoter-2`.

## Étapes 8-10 — Tests / IA / Git

65/65 tests, 0 échec réel. 6 tables IA strictement identiques. `HEAD=eeb01ec` inchangé, aucune modification.

## Réponses explicites

1. **user_id 236 existe** : OUI (confirmé indirectement)
2. **ReferralAttribution pour 236 existe** : très probablement NON
3. N/A
4. **Étape d'échec** : séquencement navigateur — token résiduel ou slug consommé par une connexion intermédiaire
5. **Pourquoi admin affiche « — »** : parce que la ligne d'attribution est probablement absente, pas un bug de requête
6. **Pourquoi `/promoter-2` n'a pas produit l'attribution** : mécanisme sain, défaillance probable côté séquence d'actions du testeur
7. **236 prêt pour un paiement attribué** : **NON** — payer maintenant donnerait très probablement 0 FCFA de commission, pas 600 FCFA

## Verdict

**`REFERRAL_ATTRIBUTION_MISSING`**

Compte existant, checkout atteint, mais très probablement aucune `ReferralAttribution` — cause la plus probable : séquencement navigateur (token résiduel ou slug consommé entre-temps), pas un bug du mécanisme applicatif lui-même.

## Recommandation

**Ne pas payer avec ce compte.** Pour re-tester proprement : ouvrir une fenêtre de navigation privée/incognito, visiter `https://www.xfoot.site/promoter-2` **en premier**, s'inscrire **immédiatement** avec un email jamais utilisé, sans se connecter à aucun autre compte entre-temps. Vérifier ensuite dans Admin → Abonnés que la colonne PROMOTEUR affiche bien `promoter-2` **avant** tout paiement.

---

**Rappel** : aucune correction effectuée, aucune donnée créée/modifiée, aucun paiement, aucun commit, aucun push.
