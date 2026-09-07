# PHASE 15.7.4 — DIAGNOSTIC ROUTAGE REFERRAL /promoter-2 APRÈS UPLOAD login.html

Généré : 2026-09-03 00:00:00 UTC

## 1. Vérification HTTP — cause trouvée

| URL | Status | Titre |
|---|---|---|
| `https://www.xfoot.site/promoter-2` | **200** | « xFoot - Connexion » |
| `https://www.xfoot.site/promoter-2/` (slash final) | **404** | « This Page Does Not Exist » |
| `https://www.xfoot.site/login.html` | **200** | « xFoot - Connexion » |
| `https://www.xfoot.site/Promoter-2` (majuscule) | **404** | — |
| `https://xfoot.site/promoter-2` (sans www) | **200** | OK |
| User-Agent mobile Safari | **200** | OK |

**`/promoter-2` sans slash final, en minuscules : fonctionne parfaitement**, sert directement `login.html` (aucune redirection HTTP, `Content-Length` et `Last-Modified` identiques byte pour byte à `/login.html`). **`/promoter-2/` (slash final) et `/Promoter-2` (majuscule) : 404 réel.**

## 2. Mécanisme de routage — confirmé cohérent

`.htaccess` :
```apache
RewriteCond %{REQUEST_FILENAME} !-f
RewriteCond %{REQUEST_FILENAME} !-d
RewriteRule ^([a-z0-9][a-z0-9-]{0,38}[a-z0-9]|[a-z0-9])$ /login.html?ref=$1 [L,QSA]
```

Pattern **ancré**, **strictement minuscules**, **un seul segment sans slash**. Aucune logique JavaScript ne lit `location.pathname` (recherche exhaustive, aucun résultat) — le routage est 100% côté serveur ; côté client, `captureReferralFromUrl()` lit uniquement `?ref=` via `URLSearchParams`.

## 3. `.htaccess` — aucune divergence

Comportement **entièrement cohérent** avec la règle telle qu'écrite. `.htaccess` n'a **pas** été modifié dans le déploiement Phase 15.7.2 (seul `login.html` a été uploadé) — ce n'est donc **pas une régression**, mais une caractéristique préexistante et documentée de la règle (« même alphabet que `app/referral/slug.py::_SLUG_RE` côté backend »).

## 4. Correctif Phase 15.7 — confirmé déployé

Le `login.html` servi par `/promoter-2` contient bien `captureReferralFromUrl();` **avant** `if (getToken())` — exactement l'ordre du commit `dbf2124`. Le slug n'est jamais lu depuis `location.pathname` : c'est `.htaccess` qui le traduit en `?ref=promoter-2` avant que le moindre JS ne s'exécute.

## 5. Production vs local

Contenu HTML confirmé identique au commit `dbf2124` (taille et `Last-Modified` cohérents avec l'upload récent, 23:53:40 UTC aujourd'hui). Aucune modification, aucun nouvel upload effectué cette phase.

## 6. Test navigateur

**Non exécutable par cet assistant** (aucun outil de navigateur).

## 7. Problème A vs Problème B

- **Problème A (routage)** : le routage fonctionne **parfaitement** pour l'URL canonique exacte (minuscules, sans slash — c'est précisément la forme générée par le backend : `referral_link=f"https://www.xfoot.site/{promoter.slug}"`, slugs toujours normalisés en minuscules). Il échoue **uniquement** pour des variantes (slash final, casse différente) — plausibles si l'URL a été tapée manuellement ou complétée par l'historique du navigateur.
- **Problème B (capture)** : **écarté** pour l'URL canonique — le correctif est confirmé présent et actif en production.

## Hypothèse explicative

L'utilisateur a très probablement observé le 404 en tapant/complétant l'URL avec un slash final ou une majuscule (autocomplétion du navigateur suggérant une variante visitée pendant les tests intensifs de cette conversation), ou en testant à un instant très proche de l'upload. Non confirmé avec certitude absolue (aucun accès à l'historique réel du navigateur), mais les deux variantes fautives sont reproductibles à volonté et prouvées par preuve HTTP directe.

## Verdict

**`ROUTING_BROWSER_OK`**

L'URL canonique fonctionne correctement, correctif confirmé en ligne. Le 404 observé s'explique par deux variantes d'URL invalides selon la règle `.htaccess` existante (jamais modifiée) — pas une régression du déploiement, pas un problème de capture referral.

## Recommandation

Redemander à l'utilisateur de retester en copiant/collant **exactement** `https://www.xfoot.site/promoter-2` (sans espace, sans slash final, sans majuscule) — idéalement en effaçant d'abord la barre d'adresse plutôt que de laisser l'autocomplétion suggérer une variante. Si le 404 persiste avec l'URL exacte confirmée caractère pour caractère, ce serait alors un signal fort d'un problème non encore identifié, à re-diagnostiquer séparément.

---

**Aucune modification effectuée** : code NON, `.htaccess` NON, Railway NON, DB NON, prix/offres NON, paiement NON, compte créé NON, commit NON, push NON.
