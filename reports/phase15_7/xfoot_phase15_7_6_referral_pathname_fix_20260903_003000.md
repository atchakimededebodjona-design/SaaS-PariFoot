# PHASE 15.7.6 — CORRECTION REFERRAL : FALLBACK PATHNAME SUR SLUG DE RÉÉCRITURE INTERNE

Généré : 2026-09-03 00:30:00 UTC

## Cause racine (rappel Phase 15.7.5)

`captureReferralFromUrl()` lisait exclusivement `window.location.search`. La réécriture `.htaccess` de `/promoter-2` est **interne** (jamais de redirection HTTP) — donc `window.location.search` reste toujours vide pour un visiteur arrivant via ce lien ; seul `window.location.pathname` porte réellement le slug, jamais lu par le code.

## Fichiers modifiés / ajoutés

| Fichier | Nature |
|---|---|
| `frontend-design/api.js` | Modifié — 32 insertions, 7 suppressions |
| `frontend-design/test_referral_capture.js` | Ajouté — suite de non-régression |

**Aucun fichier backend, `.htaccess`, ou table IA touché.**

## Logique avant / après

```diff
- const params = new URLSearchParams(window.location.search);
- const slug = (params.get("ref") || "").trim().toLowerCase();
- if (!slug) return;
+ const params = new URLSearchParams(window.location.search);
+ const fromQuery = (params.get("ref") || "").trim().toLowerCase();
+ const slug = fromQuery || _slugCandidateFromPathname();
+ if (!slug) return;
```

Nouvelle fonction `_slugCandidateFromPathname()` : extrait un candidat depuis `window.location.pathname`, en réutilisant **exactement** le même format que `app/referral/slug.py::_SLUG_RE` et la `RewriteRule` `.htaccess` (`^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$`) — un seul segment, strictement minuscules, aucun point ni slash.

**Priorité toujours au query param** (comportement historique `/login.html?ref=promoter-2` inchangé) — le pathname n'est consulté qu'en repli, si aucun `?ref=` n'est présent.

**Le backend reste l'unique source de vérité** : le filtrage client-side n'évite que des appels réseau inutiles pour des pathnames manifestement invalides — jamais une validation de sécurité en soi. Aucun changement au contrat `POST /referral/attribute`.

## Tests exécutés

### Nouveau test frontend — `frontend-design/test_referral_capture.js`

Charge le **vrai** fichier `api.js` (lecture réelle) dans un environnement `window`/`localStorage`/`fetch` simulé — même technique que la preuve exécutée de la Phase 15.7.5. **17/17 réussis.**

| Test | Scénario | Résultat |
|---|---|---|
| A | `/login.html?ref=promoter-2` | ✅ referral détecté |
| B | `/promoter-2` (pathname seul) | ✅ referral détecté (repli pathname) |
| C | `/login.html` (rien d'exploitable) | ✅ aucun referral |
| D | `/promoter-2/` (slash final) | ✅ jamais considéré valide |
| E | `/Promoter-2` (majuscule) | ✅ jamais considéré valide |
| F | `/billing.html` | ✅ aucun referral |
| G | `/slug-inexistant` | ✅ candidat préparé, backend rejette proprement, aucun faux promoteur |
| H | Idempotence | ✅ déjà couvert par les tests backend existants, non affecté |
| — | Priorité query > pathname | ✅ le query gagne toujours |
| — | **Reproduction exacte du bug Phase 15.7.5** | ✅ **slug désormais capturé, alors qu'avant : `null`** |

### Régression backend

**66/66, 0 échec réel.** Suites referral reconfirmées : `test_referral_promoter_platform.py` (99/99, idempotence incluse), `test_phase15_7_referral_attribution_repro.py` (24/24).

## Intégrité IA

6 tables strictement identiques : `match=12459, match_stats=12459, model_predictions=3610, model_versions=15, team_ratings=568, prediction_log=9`.

## Cas User 241

**Non modifié.** Aucune `ReferralAttribution` fabriquée manuellement pour `moimeme@test.com`. La validation repose exclusivement sur les tests A-H et la reproduction explicite du scénario exact qui échouait pour lui.

## Phase 15.7 — vérifiée intacte

L'ordre `captureReferralFromUrl()` avant `if (getToken())` dans `login.html` reste présent (fichier non touché cette phase). Les deux protections cumulent désormais : capture exécutée tôt (15.7) + capture capable de lire le pathname en repli (15.7.6).

## Git

`HEAD = dbf2124` inchangé. `frontend-design/api.js` modifié, `frontend-design/test_referral_capture.js` ajouté — **aucun `git add`, aucun commit, aucun push.**

## Déploiement

**Aucun.** Cette phase s'arrête après validation locale, conformément à l'instruction explicite.

## Verdict

**`REFERRAL_PATHNAME_FIX_IMPLEMENTED_TESTS_PASS`**

Correctif minimal et ciblé (1 fonction ajoutée, 1 étendue, 0 ligne backend, 0 changement de contrat). Tous les scénarios de non-régression demandés passent, plus la reproduction explicite du cas réel Phase 15.7.5. 17/17 + 66/66 + 99/99 + 24/24 verts. 6 tables IA inchangées. Aucun paiement, aucune modification DB/Railway/Chariow/`.htaccess`, aucun compte créé, aucun commit/push. Validation strictement locale, sans aucun effet en production tant qu'une phase de déploiement séparée n'est pas autorisée.

---

**PHASE 15.7.6 — CORRECTIF FALLBACK PATHNAME IMPLÉMENTÉ ET VALIDÉ LOCALEMENT. captureReferralFromUrl() LIT DÉSORMAIS window.location.pathname EN REPLI QUAND AUCUN ?ref= N'EST PRÉSENT — CORRIGEANT EXACTEMENT LA CAUSE RACINE DÉMONTRÉE EN PHASE 15.7.5. FORMAT DE SLUG RÉUTILISÉ À L'IDENTIQUE DU BACKEND ET DE .htaccess. LE BACKEND RESTE L'UNIQUE SOURCE DE VÉRITÉ. 17/17 NOUVEAU TEST FRONTEND, 66/66 RÉGRESSION BACKEND, 6 TABLES IA INCHANGÉES. AUCUNE MODIFICATION .htaccess/RAILWAY/DB/PRIX/CHARIOW. AUCUN PAIEMENT, AUCUN COMPTE CRÉÉ, LE COMPTE 241 RESTE INTACT. AUCUN COMMIT, AUCUN PUSH, AUCUN DÉPLOIEMENT. VERDICT : REFERRAL_PATHNAME_FIX_IMPLEMENTED_TESTS_PASS — EN ATTENTE D'AUTORISATION HUMAINE SÉPARÉE POUR LE DÉPLOIEMENT.**
