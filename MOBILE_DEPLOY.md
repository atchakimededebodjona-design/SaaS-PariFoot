# App Android (Play Store) — setup Capacitor

Portée : `mobile/` — empaquette le site statique existant
(`frontend-design/`, aucun changement de fond, pas de réécriture) dans une
app Android native via [Capacitor](https://capacitorjs.com/), pour publier
sur le Play Store. L'API reste sur Railway, inchangée.

**iOS/App Store repoussé** (décision explicite) : compiler pour l'App
Store nécessite Xcode, donc un Mac — indisponible sur cette machine
Windows. Le projet `mobile/` est structuré pour ajouter la plateforme iOS
plus tard (`npx cap add ios`) sans rien refaire, dès qu'un Mac ou un
service de build cloud (Codemagic, EAS Build, GitHub Actions macOS
runner...) est disponible.

**Je n'ai pas Android Studio/le SDK Android installés sur cette machine**
(vérifié : ni JDK, ni `ANDROID_HOME`, ni Android Studio détectés) — je ne
peux donc ni compiler d'APK, ni lancer l'app sur un émulateur/téléphone
moi-même. Ce qui est fait : tout ce qui ne nécessite pas de build (voir
"Ce qui a été fait" en bas). Ce qui reste : à toi, avec Android Studio
installé.

## ⚠️ À trancher AVANT de soumettre au Play Store : le paiement

`vip.html`/`billing.html` redirigent vers un paiement Chariow (Mobile
Money) hébergé **hors de l'app**. Google Play impose l'utilisation de
**Google Play Billing** pour tout contenu numérique consommé dans l'app
(ce qui inclut des prédictions IA débloquées par abonnement) — un bouton
qui renvoie vers un paiement externe pour ce même contenu est un motif de
**rejet direct** lors de la revue.

Deux options, à choisir avant la première soumission :

1. **Intégrer Google Play Billing** — travail non négligeable (facturation
   dupliquée avec Chariow, ou migration complète), mais permet un vrai
   achat in-app.
2. **App "en lecture seule" côté paiement** — dans la version app
   uniquement, remplacer le bouton "Débloquer l'accès VIP" par un message
   du type *"Gère ton abonnement depuis xfoot.site dans un navigateur"*,
   sans aucun flux de paiement dans l'app elle-même. C'est le chemin
   généralement toléré par Google (l'app ne vend rien elle-même), mais la
   tolérance dépend de la lecture stricte de la policy au moment de la
   revue — pas une garantie absolue.

**Rien n'a été fait ce ticket-ci sur ce point** — à décider avant de créer
la fiche Play Console, pas avant de tester l'app en local (le mode debug
n'est pas soumis à la revue Google).

## Ce qu'il te reste à faire

### 1. Installer les prérequis

- [Android Studio](https://developer.android.com/studio) (embarque le JDK
  et le SDK Android — rien d'autre à installer séparément).
- Au premier lancement, laisser Android Studio télécharger le SDK
  proposé par défaut (SDK Platform correspondant à `targetSdkVersion 36`,
  voir `mobile/android/variables.gradle`).

### 2. Ouvrir le projet

```bash
cd mobile
npx cap sync android   # à refaire à chaque modif de frontend-design/ ou de capacitor.config.json
npx cap open android   # lance Android Studio sur mobile/android
```

Dans Android Studio : laisser Gradle synchroniser (barre de progression en
bas), puis **Run ▶** avec un émulateur ou un téléphone branché en USB
(mode développeur + débogage USB activés) pour un premier test.

### 3. CORS — indispensable pour que l'app parle à l'API

L'app charge ses pages depuis `https://app.xfoot.site` (hostname virtuel
configuré dans `mobile/capacitor.config.json`, choisi pour être
identifiable côté serveur — **il n'a pas besoin d'exister en DNS**,
Capacitor intercepte ce hostname en interne pour servir les fichiers
locaux ; seuls les appels `fetch()` vers `api.xfoot.site` sortent
réellement sur le réseau, avec cet en-tête `Origin`).

Sur Railway, ajouter `https://app.xfoot.site` à `ALLOWED_ORIGINS` du
service web (même variable qui autorise déjà `https://xfoot.site`, voir
`HOSTINGER_DEPLOY.md`). Sans ça, toutes les requêtes API depuis l'app
échouent avec une erreur CORS silencieuse dans les logs du WebView.

### 4. Icône et écran de démarrage

L'app utilise encore l'icône par défaut de Capacitor (placeholder) — à
remplacer avant toute soumission. Le plus simple : générer un set complet
d'icônes/splash à partir d'un seul PNG source avec
[`@capacitor/assets`](https://github.com/ionic-team/capacitor-assets) :

```bash
cd mobile
npm install -D @capacitor/assets
# place un icon.png (1024x1024) et un splash.png (2732x2732) à la racine de mobile/
npx capacitor-assets generate --android
```

### 5. Signer l'app pour la release

Un APK/AAB de release doit être signé. Dans Android Studio : **Build →
Generate Signed Bundle / APK** → créer un nouveau keystore (`.jks`) la
première fois. **Garde ce fichier et son mot de passe en lieu sûr et
sauvegardé ailleurs que sur cette seule machine** — un keystore perdu rend
impossible toute mise à jour future de l'app publiée (Google Play exige la
même signature à chaque update ; il n'y a pas de "mot de passe oublié").

### 6. Compte Google Play Developer + fiche Play Console

- Créer un [compte Google Play Developer](https://play.google.com/console/signup)
  (25 $ US, paiement unique).
- Dans Play Console : nouvelle app → renseigner fiche (description,
  captures d'écran — le format `.app-container` en `max-width: 480px` de
  `styles.css` correspond déjà à un rendu mobile portrait), catégorie,
  questionnaire de classification du contenu, **politique de
  confidentialité (URL obligatoire)** — à rédiger/héberger si pas déjà
  fait, et déclaration sur les paiements (voir section ⚠️ ci-dessus).
- Upload du `.aab` signé (étape 5) dans une piste de test interne d'abord
  (recommandé), puis production une fois validé.

## Ce qui a été fait (ce ticket)

- `mobile/` (nouveau) — projet Capacitor : `package.json`,
  `capacitor.config.json` (appId `site.xfoot.app`, appName `xFoot`,
  `webDir` pointant directement vers `../frontend-design`, **aucune
  duplication des fichiers du site** — `npx cap sync` recopie à chaque
  fois l'état courant), et `android/` (projet Android Studio généré par
  `@capacitor/android`, `applicationId site.xfoot.app`, `minSdkVersion 24`
  / `targetSdkVersion 36`, `versionCode 1` / `versionName "1.0"`).
- `frontend-design/api.js` — correctif indispensable avant tout test réel :
  la WebView Capacitor sert les pages depuis un hostname qui matchait par
  erreur la détection "dev local" existante (`_LOCAL_HOSTNAME_RE`), ce qui
  aurait fait pointer l'app vers une API locale inexistante au lieu de
  `https://api.xfoot.site`. Détection ajoutée via `window.Capacitor.isNativePlatform()`
  (n'existe que dans une app empaquetée, jamais dans un navigateur
  classique) pour forcer `PRODUCTION_API_URL` dans ce cas, quel que soit le
  hostname.
- `mobile/.gitignore` — exclut `node_modules/` (`android/.gitignore`,
  généré par le template Capacitor, gère déjà les artefacts de build et le
  keystore).

**Non testé** : je n'ai pas pu builder ni lancer l'app (pas de JDK/SDK
Android sur cette machine) — à valider avec Android Studio installé,
suivant l'étape 2 ci-dessus.
