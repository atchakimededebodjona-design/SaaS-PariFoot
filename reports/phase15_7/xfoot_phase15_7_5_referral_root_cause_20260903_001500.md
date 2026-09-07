# PHASE 15.7.5 — DIAGNOSTIC RACINE DE L'ATTRIBUTION REFERRAL

Généré : 2026-09-03 00:15:00 UTC

## Compte diagnostiqué

`moimeme@test.com` / user_id=241, créé via `https://www.xfoot.site/promoter-2` (URL canonique validée en Phase 15.7.4), PROMOTEUR affiché = « — ».

## Cause racine — démontrée par preuve exécutée

**`captureReferralFromUrl()` (frontend-design/api.js) lit exclusivement `window.location.search`.** La réécriture `.htaccess` qui sert `/promoter-2` est **interne** (`RewriteRule ... [L,QSA]`, **sans** le flag `[R]`) — donc **aucune redirection HTTP** n'est jamais envoyée au navigateur (confirmé par `curl` : `200 OK` direct, **zéro** header `Location`). Le fichier `.htaccess` documente lui-même cette intention : *« jamais une redirection visible (302/301), le visiteur voit toujours https://www.xfoot.site/{slug} dans sa barre d'adresse »*.

**Conséquence directe** : `window.location.search` reste **toujours vide** pour un visiteur arrivant via `/promoter-2` — le `?ref=promoter-2` ajouté par la règle de réécriture n'existe **que côté serveur**, jamais transmis au client. Seul `window.location.pathname` contient réellement `/promoter-2`, mais **aucune ligne de `captureReferralFromUrl()` ne lit jamais `pathname`**.

### Preuve exécutée (pas seulement déduite)

Script Node.js chargeant le **vrai fichier** `frontend-design/api.js` (non modifié), exécuté face à des objets `window`/`location`/`fetch` simulés reproduisant fidèlement ce que `curl` a prouvé :

| Scénario | pathname | search | Appels `fetch()` | `localStorage['xfoot_referral_slug']` |
|---|---|---|---|---|
| **RÉEL** (production actuelle) | `/promoter-2` | `""` | **0** | **`null`** |
| Contrôle hypothétique (si redirection externe) | `/login.html` | `?ref=promoter-2` | 1 → `.../referral/resolve/promoter-2` | `"promoter-2"` |

**100% déterministe** — se produit pour tout visiteur, tout navigateur, toute fenêtre, indépendamment de l'état de session. Explique intégralement les 4 échecs réels (236, 238, 239, **241**), y compris ce dernier, survenu **après** le déploiement du correctif Phase 15.7 (`dbf2124`) — ce correctif était réel mais réglait un problème secondaire (bypass par token résiduel), insuffisant seul car la capture ne pouvait de toute façon jamais réussir.

## 1. Audit de la chaîne frontend

| Étape | État |
|---|---|
| A. Arrivée `/promoter-2` | `.htaccess` interne, aucune redirection HTTP (confirmé) |
| B. Exécution `captureReferralFromUrl()` | S'exécute bien, mais retourne immédiatement |
| C. Valeur extraite du pathname | **Jamais extraite** — aucune ligne ne lit `location.pathname` |
| D. Stockage localStorage | **Aucun** — jamais atteint |
| E. Clé localStorage | `xfoot_referral_slug` (jamais écrite ici) |
| F. Appel `attributeReferralIfPresent()` | Correctement positionné (avant redirection), mais sur localStorage vide |
| G-J. POST `/referral/attribute` | **Jamais émis** (`if (!slug) return;`) |
| K. Nettoyage localStorage | Non applicable (rien à nettoyer) |

## 2. Parcours réellement suivi

`login.html` reste le **seul** fichier avec formulaire d'inscription (confirmé, Phase 15.7.4). Le compte 241 l'a nécessairement suivi. `attributeReferralIfPresent()` y est correctement câblée. **H8 (parcours différent) écartée avec certitude.**

## 3. Contrat backend `POST /referral/attribute`

Déjà testé exhaustivement et prouvé correct en Phase 15.7 (24/24, reproduction fidèle du format réel). Le backend **n'est pas** en cause — il n'est simplement **jamais appelé** dans le scénario réel.

## 4. Vérification DB user 241

**NOT_VERIFIABLE** (aucun accès DB/admin). **Non nécessaire** : la cause démontrée est universelle et déterministe, elle explique le cas 241 sans lecture DB directe.

## 5. Logs/Network navigateur

Non reproductible par cet assistant (aucun outil de navigateur) — remplacé par la preuve exécutée Node.js ci-dessus, qui teste le même contrat fidèlement.

## 6. Vérification localStorage

Clés cohérentes entre capture et attribution (`xfoot_referral_slug` des deux côtés). Aucune divergence de nom, aucune suppression prématurée — le seul problème est que la clé n'est **jamais écrite**.

## 7. Vérification temporelle

Rupture précise identifiée à l'**Étape 2 (capture referral)** — tout ce qui suit (inscription, token, attribution, redirection) est fonctionnellement correct mais sans effet, faute de slug à transmettre.

## 8. Hypothèses testées

| # | Hypothèse | Verdict |
|---|---|---|
| H1 | captureReferralFromUrl() ne s'exécute pas | Partiellement vraie en effet, fausse en cause — s'exécute mais retourne vide |
| H2 | Capture OK, localStorage non conservé | Faux — capture jamais effectuée |
| H3 | localStorage OK, attribute jamais appelée | Faux — appelée, mais localStorage vide |
| H4-H6 | Payload/POST incorrects ou refusés | Non applicable — POST jamais émis |
| H7 | Backend crée, Admin n'affiche pas | Écartée (déjà prouvée fausse Phase 15.7) |
| H8 | Parcours différent | Écartée |
| **H9 (nouvelle)** | **Source du slug incorrecte (search vs pathname), réécriture interne** | **Confirmée avec preuve exécutée — cause racine réelle** |

## 9. Vérification affichage Admin

`PROMOTEUR="—"` correspond bien à l'**absence réelle** de `ReferralAttribution`, pas à un bug d'affichage — re-confirmé par le code et le test Phase 15.7. `REFERRAL_ADMIN_DISPLAY_BUG` écarté avec certitude.

## 10. Tests

Tests backend existants réutilisés (99/99 + 24/24, Phase 15.7) — confirment le mécanisme serveur intact, mais ne pouvaient **pas**, par construction, révéler cette cause (un test Python/TestClient n'a pas de `window.location`). Nouveau script de preuve : **diagnostic uniquement**, écrit dans le répertoire scratchpad de session, **jamais ajouté au dépôt**, charge le vrai `api.js` en lecture seule. Aucune modification de code pour faire passer un test.

## Pistes de correction pour une phase future (NON implémentées)

- **Option A (recommandée)** : faire lire à `captureReferralFromUrl()` le slug depuis `window.location.pathname` en repli — cohérent avec l'intention documentée de garder l'URL propre sans redirection.
- **Option B (déconseillée)** : transformer la réécriture en redirection externe (`[R=302]`) — romprait l'objectif documenté du fichier lui-même et dégraderait l'UX.

## Verdict

**`REFERRAL_ROOT_CAUSE_IDENTIFIED`**

Démontré par preuve exécutée : le vrai code source, face aux conditions réelles vérifiées par `curl`, ne déclenche jamais la capture. Mécanisme déterministe, explique les 4 échecs réels. Toutes les hypothèses alternatives testées et écartées.

---

**Aucune action effectuée** : code NON, `.htaccess` NON, Railway NON, DB NON, compte créé NON, paiement NON, commit NON, push NON.

**PHASE 15.7.5 — DIAGNOSTIC RACINE REFERRAL : CAUSE RACINE DÉMONTRÉE PAR PREUVE EXÉCUTÉE (SCRIPT NODE.JS CHARGEANT LE VRAI api.js NON MODIFIÉ). captureReferralFromUrl() NE LIT JAMAIS window.location.pathname, UNIQUEMENT window.location.search — QUI RESTE TOUJOURS VIDE POUR TOUT VISITEUR ARRIVANT VIA /promoter-2, CAR LA RÉÉCRITURE .htaccess EST INTERNE (JAMAIS DE REDIRECTION HTTP, CONFIRMÉ PAR curl : AUCUN HEADER Location). CE MÉCANISME EST 100% DÉTERMINISTE ET EXPLIQUE À LUI SEUL LES 4 ÉCHECS RÉELS OBSERVÉS (236, 238, 239, 241). LE CORRECTIF PHASE 15.7 (dbf2124) RESTE VALIDE POUR SON PROBLÈME PROPRE MAIS ÉTAIT INSUFFISANT SEUL. TOUTES LES AUTRES HYPOTHÈSES (BACKEND, AFFICHAGE ADMIN, MAUVAIS PARCOURS) SONT ÉCARTÉES AVEC PREUVE. AUCUNE MODIFICATION DE CODE, .htaccess, RAILWAY, DB EFFECTUÉE. AUCUN COMPTE CRÉÉ, AUCUN PAIEMENT, AUCUN COMMIT, AUCUN PUSH. VERDICT : REFERRAL_ROOT_CAUSE_IDENTIFIED — LA CORRECTION ELLE-MÊME EST VOLONTAIREMENT LAISSÉE À UNE PHASE SÉPARÉE, CONFORMÉMENT À L'INSTRUCTION EXPLICITE DE NE PAS CORRIGER MAINTENANT.**
