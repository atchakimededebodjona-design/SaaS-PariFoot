# PHASE 15.6-DIAG — INCIDENT CHECKOUT PRICING

Généré : 2026-09-02 21:05:00 UTC

## 1. Preuve de référence

Xfoot affiche **Mensuel = 1500 FCFA**. Chariow (checkout.orqex.com) affiche **NET TO PAY : FCFA 1,000** (capture utilisateur). Aucun paiement effectué.

## 2. Inspection du code — commit `eeb01ec`

Diff exact de `eeb01ec` sur `router.py` : **uniquement un commentaire** (`plan: str`), aucun changement fonctionnel.

Diff exact sur `chariow_config.py` :
```diff
 PRODUCT_IDS = {
+    "biweekly": os.environ.get("CHARIOW_PRODUCT_ID_BIWEEKLY", ""),
     "monthly": os.environ.get("CHARIOW_PRODUCT_ID_MONTHLY", ""),
     "yearly": os.environ.get("CHARIOW_PRODUCT_ID_YEARLY", ""),
 }
```

**Point capital** : la ligne `"monthly": os.environ.get("CHARIOW_PRODUCT_ID_MONTHLY", "")` **existait déjà, à l'identique, avant `eeb01ec`**. Ce commit n'a jamais changé quelle variable d'environnement « monthly » lit — il a seulement ajouté la clé `biweekly`.

## 3. Matrice de mapping

| Plan Xfoot | Product ID attendu | Code source |
|---|---|---|
| 2 semaines / 1000 FCFA | `prd_1gje3jzz` | ✅ correct (nouvelle clé) |
| **1 mois / 1500 FCFA** | **`prd_sgvapilx`** | Code correct — lit `CHARIOW_PRODUCT_ID_MONTHLY`, mais résout à un montant de 1000 FCFA en production |
| 1 an / 28000 FCFA | `prd_f90jpbh3` | Non testé cet incident |

## 4. Variables d'environnement (sans exposer de secret)

| Variable | État |
|---|---|
| `CHARIOW_API_KEY` | PRESENT (le checkout a fonctionné techniquement) |
| `CHARIOW_PRODUCT_ID_MONTHLY` | PRESENT indirectement confirmé (sinon `router.py:245` aurait renvoyé 400 « Plan inconnu ou non configuré ») — **valeur exacte NOT_VERIFIABLE** (pas d'accès Railway), mais le comportement observé (1000 FCFA) est cohérent avec l'ANCIEN Product ID jamais mis à jour. |
| `CHARIOW_PRODUCT_ID_BIWEEKLY` | Hors périmètre direct de cet incident, non vérifié |

## 5. Le commit `eeb01ec` — précision critique

`eeb01ec` documente `prd_sgvapilx` dans les commentaires/`.env.example` comme la valeur **attendue** à configurer manuellement dans Railway pour `CHARIOW_PRODUCT_ID_MONTHLY`. **Un commit Git ne modifie jamais une variable d'environnement d'une plateforme d'hébergement** — cela exige une action manuelle distincte dans le dashboard Railway, qui n'a jamais été confirmée comme effectuée dans aucune des Phases 15.1 à 15.5 (toutes confirment explicitement « Railway non modifié »).

## 6. Backend production

`/health` → 200. Aucun endpoint de version/configuration n'existe (confirmé, non créé pour cette phase). `RAILWAY_COMMIT` : **NOT_VERIFIABLE**.

## 7. Parcours checkout réel

```
billing.html (plan="monthly")
  → POST /billing/checkout {plan:"monthly", ...}
  → create_checkout_session → PRODUCT_IDS["monthly"]
  → _create_chariow_checkout_link → POST api.chariow.com/v1/checkout {product_id: <valeur CHARIOW_PRODUCT_ID_MONTHLY>, custom_metadata:{user_id, plan}}
  → checkout_url renvoyée → checkout.orqex.com
```

Le parcours a fonctionné techniquement (pas d'erreur 400/500) — confirmant que la variable **existe** et n'est pas vide, mais résout vers un `product_id` dont le prix Chariow est 1000 FCFA.

## 8. Hypothèse principale — confirmée par élimination

`prd_sgvapilx` est bien le Product ID du mensuel 1500 FCFA (vérifié publiquement en Phase 15.2). Le checkout production n'a **pas** utilisé ce Product ID — sinon Chariow aurait affiché 1500 FCFA (produit à prix FIXE). Le montant observé (1000 FCFA) est **exactement** le prix de l'ancienne offre mensuelle légitime — élément fortement corroborant, pas une coïncidence.

## 9. Cache / Déploiement

| | |
|---|---|
| Commit local | `eeb01ec` |
| `origin/main` | `eeb01ec` (identique) |
| Configuration attendue | `CHARIOW_PRODUCT_ID_MONTHLY=prd_sgvapilx` sur Railway |
| Comportement observé | Checkout mensuel résout à 1000 FCFA |

**Conclusion : `BACKEND_PRICING_MAPPING_STALE`.** Le code déployé est correct et à jour ; la configuration Railway (valeur de la variable) n'a, à notre connaissance documentée, jamais été mise à jour.

## 10. Frontend ≠ Backend — confirmation explicite

`billing.html` affichant « 1500 FCFA » est un **texte statique HTML**, indépendant de la valeur réellement transmise à Chariow. La chaîne diverge **au niveau de la valeur de la variable Railway `CHARIOW_PRODUCT_ID_MONTHLY`**, jamais dans le code source.

## 11. Ancien prix 1000 FCFA — distinction

`1000 FCFA` est légitime à deux endroits distincts : (a) l'ancienne offre « 1000 FCFA/mois » (produit Chariow conservé, Product ID jamais commité dans ce dépôt), (b) la nouvelle offre légitime « 1000 FCFA/2 semaines » (`prd_1gje3jzz`). Le critère de distinction est le Product ID, pas le montant — et le mensuel résout actuellement vers le mauvais des deux.

## 12. Tests

**65/65, 0 échec réel.** `test_phase15_1_new_offers.py` (17/17) mocke `_create_chariow_checkout_link` et vérifie seulement que `PRODUCT_IDS["monthly"]` est bien **passé en argument** — ces tests ne peuvent **pas** détecter une valeur Railway incorrecte (aucun appel réseau réel, par design). Ceci explique pourquoi 65/65 tests verts n'a jamais pu révéler cet incident : le bug n'est pas dans le code testé, il est dans une configuration externe non testable localement.

## 13. IA

6 tables strictement inchangées (12459/12459/3610/15/568/9).

## Réponses explicites

1. **Product ID attendu pour le mensuel** : `prd_sgvapilx`
2. **Product ID utilisé par le code local** : aucun codé en dur — lit `CHARIOW_PRODUCT_ID_MONTHLY` (dont `prd_sgvapilx` est la valeur documentée comme attendue)
3. **Product ID production** : résout à 1000 FCFA — valeur exacte NOT_VERIFIABLE, très probablement l'ancien Product ID
4. **Ancienne variable d'environnement** : NON — même nom de variable avant/après, c'est sa **valeur** qui est probablement obsolète
5. **Backend production à jour** : le CODE oui (origin/main=eeb01ec) ; la CONFIGURATION Railway, très probablement NON
6. **Où la chaîne diverge** : entre le code (correct) et la valeur réelle de `CHARIOW_PRODUCT_ID_MONTHLY` sur Railway
7. **Pourquoi Chariow affiche 1000 FCFA** : le product_id envoyé est très probablement l'ancien, jamais reconfiguré lors du déploiement Phase 15.4
8. **Correction nécessite modification du code** : NON
9. **Correction nécessite modification Railway** : OUI — reconfigurer `CHARIOW_PRODUCT_ID_MONTHLY=prd_sgvapilx`, puis redéployer/redémarrer (env lu une fois au démarrage du process)
10. **Correction nécessite modification Chariow** : NON — `prd_sgvapilx` existe déjà et est correctement configuré à 1500 FCFA
11. **Nouvelle phase corrective nécessaire** : OUI — (a) reconfigurer `CHARIOW_PRODUCT_ID_MONTHLY` sur Railway, (b) vérifier par précaution `CHARIOW_PRODUCT_ID_BIWEEKLY`/`_YEARLY` (même risque potentiel non testé ici), (c) re-tester le checkout mensuel en lecture seule avant tout nouveau test de paiement réel

## Verdict Final

**`BACKEND_PRICING_MAPPING_STALE`** (implique aussi `CHECKOUT_PRICING_ROOT_CAUSE_IDENTIFIED` et `CORRECTION_REQUIRED`)

Cause racine identifiée avec un niveau de confiance élevé par élimination structurelle. La valeur exacte de la variable Railway reste `NOT_VERIFIABLE` par cet assistant (aucun accès dashboard), donc la cause est identifiée avec un haut degré de confiance mais pas prouvée à 100 % par accès direct.

**Checkout : FAIL / BLOCKED.** Xfoot = 1500 FCFA, Chariow = 1000 FCFA. Aucun paiement ne doit être tenté tant que cette divergence n'est pas corrigée et re-vérifiée.

---

## Sortie finale

```
PHASE 15.6-DIAG — CHECKOUT PRICING

Prix Xfoot affiché : 1500 FCFA
Prix Chariow observé : 1000 FCFA

Product ID mensuel attendu : prd_sgvapilx
Product ID réellement utilisé localement : aucun codé en dur — lit CHARIOW_PRODUCT_ID_MONTHLY (valeur attendue = prd_sgvapilx)
Product ID production : NOT_VERIFIABLE (très probablement l'ancien Product ID, cohérent avec le prix 1000 FCFA observé)

Backend mapping (code) : PASS
Configuration Railway : STALE (haute confiance, non prouvée par accès direct)

Cause racine : variable d'environnement Railway CHARIOW_PRODUCT_ID_MONTHLY jamais
reconfigurée de l'ancien Product ID (1000 FCFA/mois) vers prd_sgvapilx (1500 FCFA/mois)
lors du déploiement Phase 15.4 — le code, lui, est correct et inchangé dans sa logique.

Correction nécessaire : OUI (action Railway, hors code)

Paiement effectué : NON
Modification Chariow : NON
Modification DB : NON
Modification code : NON
Commit : NON
Push : NON

Tests : 65/65
IA : UNCHANGED

VERDICT : BACKEND_PRICING_MAPPING_STALE
```

**STOP.** Aucune correction effectuée. Aucun commit. Aucun push. Aucune modification de produit Chariow. En attente de décision sur la phase corrective séparée.
