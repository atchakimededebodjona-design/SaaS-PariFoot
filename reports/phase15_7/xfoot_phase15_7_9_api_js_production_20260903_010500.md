# PHASE 15.7.9 — VÉRIFICATION PRODUCTION api.js APRÈS UPLOAD HOSTINGER

Généré : 2026-09-03 01:05:00 UTC

## 1. Référence locale (commit `70970ee`)

SHA256 : `6960e66df3c9d240565148f5ec7591a84547c6f47cd7b17aff1261c09cbb79a5` — 9702 octets.

## 2. Vérification production — requêtes multiples

| Requête | URL | Status | Cache | SHA256 | Taille |
|---|---|---|---|---|---|
| 1-3 (avec paramètre anti-cache) | `.../api.js?cachebust=...` | 200 | `x-hcdn-cache-status: MISS` | `6960e66d...` **(identique local)** | 9702 |
| 4 (URL réelle, sans paramètre) | `.../api.js` | 200 | `x-hcdn-cache-status: HIT`, `Age: 92466` | `48395c21...` (ancien) | 8122 |

`Last-Modified` de la copie cachée : `Tue, 01 Sep 2026 22:06:31 GMT` — **antérieur** à l'upload (`Thu, 03 Sep 2026 00:35:50 GMT`, confirmé sur la version origine).

## 3. Comparaison — diagnostic clair

**Upload réussi côté origine Hostinger** : la requête qui contourne le cache CDN renvoie un contenu **strictement identique** (SHA256 + `diff` byte-à-byte vide) au fichier du commit `70970ee`.

**Mais l'URL réellement utilisée par les visiteurs** (`https://www.xfoot.site/api.js`, sans paramètre) **sert toujours l'ancien fichier**, mis en cache ~26 heures avant l'upload, encore valide selon son `Cache-Control: max-age=604800` (7 jours) — **exactement le même comportement que l'incident déjà documenté en Phase 14.9/14.9R** pour ce même fichier.

## 4. Vérification du correctif — confirmé présent et fonctionnel

Dans la version origine (cache-bustée, identique au commit) :
- `_SLUG_CANDIDATE_RE` ✅
- `_slugCandidateFromPathname()` ✅
- Priorité au query `?ref=` préservée ✅
- Fallback correctement câblé (`slug = fromQuery || _slugCandidateFromPathname()`) ✅

**Preuve exécutée** : script Node.js chargeant ce contenu réel face à `pathname="/promoter-2"`, `search=""` → **1 appel `fetch` vers `/referral/resolve/promoter-2`, `localStorage['xfoot_referral_slug'] = "promoter-2"`**. Le fichier réellement uploadé corrige bien le bug de la Phase 15.7.5 — confirmé par exécution, pas seulement par lecture.

## 5. Diagnostic cache

Preuve claire et suffisante d'un cache CDN persistant : `x-hcdn-cache-status: HIT`, `Age: 92466` secondes, `Last-Modified` de la copie cachée antérieur à l'upload. **Aucune modification ni second upload tenté** — diagnostic uniquement.

## 6. Purge CDN

**Cet assistant n'a aucune capacité de purge** (aucun accès au dashboard Hostinger/hPanel). Aucune manipulation destructive ou globale tentée.

## 7. Aucun test fonctionnel utilisateur

Aucun compte créé, aucun paiement — cette phase valide exclusivement `api.js local == api.js production (origine)`.

## Verdict

**`HOSTINGER_CDN_PURGE_REQUIRED`**

Upload prouvé réussi côté origine (SHA256 + diff identiques, correctif confirmé fonctionnel par exécution réelle). L'URL réelle de production sert encore une copie CDN en cache, antérieure à l'upload de ~26h, qui restera servie jusqu'à expiration naturelle (jusqu'à 7 jours) sauf purge manuelle — action strictement humaine.

## Action requise

Purger le cache CDN Hostinger (hcdn) pour `https://www.xfoot.site/api.js` depuis le dashboard Hostinger (hPanel). Une fois fait, prévenir cet assistant pour re-vérifier que l'URL réelle sert désormais `SHA256=6960e66d...`.

---

**PHASE 15.7.9 — VÉRIFICATION PRODUCTION api.js : UPLOAD CONFIRMÉ RÉUSSI CÔTÉ ORIGINE HOSTINGER (SHA256 ET CONTENU IDENTIQUES BYTE-À-BYTE AU COMMIT 70970ee VIA REQUÊTE CONTOURNANT LE CACHE, CORRECTIF PATHNAME CONFIRMÉ PRÉSENT ET FONCTIONNEL PAR EXÉCUTION RÉELLE). MAIS L'URL RÉELLE DE PRODUCTION (SANS PARAMÈTRE) SERT ENCORE UNE COPIE CDN EN CACHE, ANTÉRIEURE À L'UPLOAD DE ~26 HEURES (Age=92466s, x-hcdn-cache-status=HIT) — MÊME INCIDENT QUE LA PHASE 14.9. AUCUNE CAPACITÉ DE PURGE CDN DISPONIBLE POUR CET ASSISTANT. AUCUNE MODIFICATION DE CODE, COMMIT, PUSH, .htaccess, RAILWAY, DB, CHARIOW, PRIX, OU TABLE IA. AUCUN COMPTE CRÉÉ, AUCUN PAIEMENT. VERDICT : HOSTINGER_CDN_PURGE_REQUIRED — ACTION HUMAINE REQUISE : PURGER LE CACHE CDN DEPUIS LE DASHBOARD HOSTINGER POUR https://www.xfoot.site/api.js.**
