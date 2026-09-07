# PHASE 15.6-R — DIAGNOSTIC INSCRIPTION + ATTRIBUTION REFERRAL

Généré : 2026-09-02 21:50:00 UTC

## Découverte principale — Étape 3 : pourquoi Admin → Abonnés peut exclure un compte valide

**`GET /admin/subscribers` (`api/app/referral/admin_router.py:146-183`) n'interroge PAS la table `user`.** Il interroge exclusivement :

```python
select(ProviderSubscription).where(ProviderSubscription.provider == "chariow")
```

Pour chaque ligne `ProviderSubscription` trouvée, le code va ensuite chercher le `User` correspondant. **Conséquence directe : un utilisateur n'apparaît dans « Admin → Abonnés » QUE s'il possède déjà une ligne `ProviderSubscription` — jamais simplement parce qu'il a un compte.**

Cette ligne n'est créée qu'à un seul endroit dans tout le code : `_get_or_create_provider_subscription` (`api/app/billing/router.py:111-133`), appelée **uniquement** à l'intérieur de `POST /billing/checkout` — c'est-à-dire au moment où l'utilisateur clique « Continuer vers le paiement », **jamais à l'inscription**.

**Comparaison avec `payment@test.com` (209)** : ce compte EST visible dans Admin → Abonnés, ce qui prouve qu'une ligne `ProviderSubscription` existe déjà pour lui — cohérent avec le fait qu'il a déjà été utilisé pour atteindre l'écran de checkout Chariow lors du diagnostic précédent (Phase 15.6, Étape 3 initiale). Cet appel à `POST /billing/checkout` a créé la ligne, même sans paiement complété.

**Conclusion probable (haute confiance, non confirmée par les données)** : si le nouveau compte s'est inscrit via `/promoter-2` mais n'a **pas encore** cliqué sur « Continuer vers le paiement », son absence d'Admin → Abonnés est **entièrement attendue et normale** — ce n'est très probablement **pas un bug**, mais la conséquence directe de la conception de cet endpoint (une liste d'abonnés/de checkouts engagés, pas une liste de tous les comptes inscrits).

## Étape 4 — Mécanisme du lien `/promoter-2` — audité, aucune anomalie

| Étape | Détail |
|---|---|
| Résolution du slug | `.htaccess` réécrit `/promoter-2` → sert `login.html` avec `?ref=promoter-2` (confirmé : 200, sans redirection HTTP) |
| Capture avant inscription | `captureReferralFromUrl()` (`login.html:81`, au chargement) — lit `?ref=`, appelle `POST /referral/resolve/{slug}` (public), stocke `slug`+`timestamp` en `localStorage` si valide — **purement côté client** à ce stade |
| Persistance serveur | `login.html:169-196` — à la soumission : `register()` → `login()` → **`await attributeReferralIfPresent()`** (bloque la redirection jusqu'à la fin de cet appel) → `POST /referral/attribute` |
| Fonction serveur | `attribute_referral` (`api/app/referral/router.py:100-138`) — crée `ReferralAttribution` si non déjà attribué, promoteur ACTIVE, pas de self-referral, dans la fenêtre de 30 jours |

**Aucune anomalie détectée dans cette chaîne.** Reste à confirmer avec des données réelles que l'appel a effectivement réussi pour ce compte précis (ex. coupure réseau pendant l'inscription — échec silencieux, best-effort par design).

## Étape 5 — Recherche `ReferralAttribution`

**NOT_VERIFIABLE** — aucun accès DB pour interroger `referral_attribution`, ni pour le nouveau compte ni pour re-confirmer 209.

## Étape 6 — Classification

**CAS G — AUDIT_BLOCKED** pour les faits de données. Mais les faits « visibilité admin » et « existence de l'attribution » sont **découplés** dans ce système — l'absence dans Admin → Abonnés ne prouve ni n'infirme l'existence d'une attribution.

## Étape 7 — Lecture webhook (re-confirmée)

```python
attribution = session.exec(select(ReferralAttribution).where(ReferralAttribution.converted_user_id == referred_user_id)).first()
```

Confirmé inchangé (`commission_service.py:56-58`) — lecture SQL fraîche à la confirmation du paiement. Commission attendue si attribution existe : **600 FCFA** (40% × 1500).

## Étape 8 — Configuration checkout — ⚠️ point ouvert distinct

Code local correct (`PRODUCT_IDS['monthly']` lit `CHARIOW_PRODUCT_ID_MONTHLY`, valeur attendue `prd_sgvapilx`). **Rappel important** : la Phase 15.6-DIAG avait établi avec haute confiance que cette variable sur Railway pointait encore vers l'ancien produit (1000 FCFA observé chez Chariow). La correction demandée (Phase 15.6-CORRECTION) **n'a pas été confirmée comme effectuée** avant cette phase. Ce point reste ouvert, indépendamment du sujet attribution traité ici.

## Étapes 9-11 — Tests / IA / Git

**65/65 tests, 0 échec réel.** Suites ciblées : `test_referral_promoter_platform.py` (99/99), `test_phase14_1_production_integration.py` (36/36). 6 tables IA strictement identiques. `HEAD=eeb01ec` inchangé, aucun commit, aucun push.

## Blockers

- Aucun accès DB production, aucun token pour aucun compte, aucun outil de navigateur.
- Email/user_id du nouveau compte non communiqués — non inventés, conformément à la règle.
- Statut de la correction Railway toujours non confirmé.

## Verdict

**`AUDIT_BLOCKED`**

Les faits de données ne peuvent pas être établis faute d'accès. Mais le diagnostic du code apporte une explication structurelle de haute valeur, probablement suffisante à elle seule : un compte fraîchement inscrit qui n'a pas encore tenté de checkout est **normalement absent** d'Admin → Abonnés, quel que soit le succès de son attribution referral — ce n'est très probablement pas un bug.

## Recommandation pour lever le blocage

Deux pistes, au choix :
1. Chercher le nouveau compte dans une liste d'**utilisateurs bruts** si une telle vue existe dans l'admin (pas seulement « Abonnés »).
2. Faire cliquer ce compte jusqu'à « Continuer vers le paiement » **sans payer** — cela créera la ligne `ProviderSubscription` et le fera apparaître dans Admin → Abonnés ; si `promoter_slug` y affiche déjà `promoter-2`, cela confirmera a posteriori que l'attribution avait bien été persistée dès l'inscription.

---

**Rappel** : aucune correction effectuée, aucune donnée créée/modifiée, aucun paiement, aucun commit, aucun push. Cette phase est un diagnostic uniquement — la décision suivante revient à l'utilisateur.
