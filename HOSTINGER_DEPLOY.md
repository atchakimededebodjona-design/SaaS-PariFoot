# Héberger le frontend sur Hostinger

Portée de ce ticket : `frontend-design/` (site statique HTML/CSS/JS, aucun
build) sur Hostinger. L'API FastAPI + Postgres restent sur **Railway** (voir
[api/README.md § Déploiement](api/README.md#déploiement-railway)) —
Hostinger ne sert que des fichiers statiques, ce n'est pas fait pour faire
tourner Python/uvicorn (sauf VPS, hors scope ici).

**Je n'ai pas accès à ton compte Railway ni Hostinger** (pas de CLI
authentifiée, pas d'accès dashboard) — les étapes ci-dessous sont à
exécuter par toi. Le code, lui, est prêt côté frontend (voir "Ce que ce
ticket a changé" en bas).

## Prérequis — déjà fait ✅

L'API tourne déjà en production sur Railway (projet **luminous-adventure**,
service **SaaS-PariFoot**, Postgres attaché) avec un domaine custom
**api.xfoot.site** — vérifié directement (`GET /health` → `200 ok`, 5
ligues chargées). `ALLOWED_ORIGINS` autorise déjà `https://xfoot.site`
(vérifié via une requête CORS `OPTIONS` — `access-control-allow-origin:
https://xfoot.site` déjà présent). Rien à faire côté Railway pour ce
ticket, seule l'étape 3 (upload) reste à faire.

## 1. Choisir une offre Hostinger

Un site purement statique (pas de PHP, pas de base de données côté
Hostinger) n'a besoin ni de VPS ni d'accès SSH — l'offre la moins chère
avec **hébergement web + File Manager/FTP** suffit (ex. "Premium Web
Hosting" au moment d'écrire ceci ; le nom exact des offres change parfois
côté Hostinger). Pas besoin d'un plan "Node.js"/"Python" — ces fichiers
sont servis tels quels, sans exécution serveur.

## 2. Domaine

Domaine retenu : **xfoot.site** (déjà acheté sur Hostinger —
hpanel.hostinger.com/domain/xfoot.site/domain-overview).

## 3. Uploader les fichiers

Dans le dashboard Hostinger → **Fichiers → Gestionnaire de fichiers** (ou
FTP) → dossier `public_html/` de **xfoot.site** → uploader tout le contenu
de `frontend-design/` (`index.html`, `login.html`, `billing.html`,
`styles.css`, `api.js`) **à la racine de `public_html/`**, pas dans un
sous-dossier — sinon `https://xfoot.site/` ne trouvera pas `index.html`
directement.

## 4. Brancher le frontend sur l'API — déjà fait ✅

- `frontend-design/api.js` : `PRODUCTION_API_URL = "https://api.xfoot.site"`.
- `ALLOWED_ORIGINS` (Railway) : autorise déjà `https://xfoot.site`.

Si `https://www.xfoot.site` doit aussi marcher (avec le sous-domaine
`www`), vérifier que `ALLOWED_ORIGINS` côté Railway l'inclut aussi — sinon
seul `https://xfoot.site` (sans `www`) fonctionnera.

## 5. Vérifier

1. `https://xfoot.site/login.html` doit charger la page (HTTPS géré
   automatiquement par Hostinger — certificat gratuit à activer dans le
   dashboard si ce n'est pas déjà fait par défaut).
2. Se connecter avec un compte existant → doit rediriger vers
   `index.html` avec le vrai email et les vraies équipes (si ça échoue,
   ouvrir la console du navigateur : une erreur CORS pointe vers l'étape 4
   côté Railway, une erreur réseau/404 vers `PRODUCTION_API_URL` mal
   rempli).
3. Lancer une prédiction → doit afficher un résultat (compte abonné) ou la
   bannière premium (402, compte non abonné) — mêmes comportements que
   testés en local.

## Ce que ce ticket a changé (déjà fait)

- `frontend-design/api.js` : `API_BASE_URL` ne se dérive plus
  aveuglément de `window.location.hostname` — seulement pour les hôtes
  locaux/LAN (dev). Sur un vrai domaine, utilise
  `PRODUCTION_API_URL = "https://api.xfoot.site"`.
- `HOSTINGER_DEPLOY.md` (ce fichier).

**Non touché** : rien côté API/Railway (déjà en place), aucun build tool
ajouté au frontend (reste du HTML/CSS/JS servi tel quel). **Il reste
uniquement l'étape 3 (upload sur Hostinger) à faire toi-même.**
