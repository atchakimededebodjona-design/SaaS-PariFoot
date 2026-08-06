# Foot Prediction API — Dixon-Coles

API FastAPI qui sert des prédictions 1X2 / probabilité de plus-moins de buts à partir de modèles
Dixon-Coles déjà entraînés (un par ligue). Elle ne dépend que de
`numpy`/`scipy.stats` pour les prédictions — aucune optimisation
(`scipy.optimize`) au moment de la requête, donc démarrage rapide et
réponses en quelques millisecondes. Inclut aussi un système
d'authentification JWT (inscription/connexion) et de facturation Chariow
(licences), tous deux indépendants des prédictions — voir sections
Authentification et Facturation plus bas.

## Architecture

```
export_model_artifacts.py   # entraînement (scipy.optimize), à lancer périodiquement
        │
        ▼
model_artifacts/<league>.json   # attack, defense, home_advantage, rho, teams...
        │
        ▼
api/main.py                 # sert les prédictions, aucune dépendance à l'entraînement
        │
        ▼
api/app/                    # authentification + facturation (indépendantes des prédictions)
  ├── core/
  │   ├── database.py           # moteur SQLAlchemy/SQLModel, SQLite par défaut
  │   ├── security_config.py    # SECRET_KEY, algorithme JWT, durée de vie du token
  │   └── chariow_config.py     # clés API Chariow, Product ID, secret des Pulses
  ├── models/
  │   ├── user.py                # modèle User + schémas Pydantic (register/read/token)
  │   └── subscription.py        # modèle Subscription (licence) + ProcessedPulseDelivery
  ├── auth/
  │   ├── security.py           # hashing bcrypt, émission/vérification JWT
  │   └── router.py              # /auth/register, /auth/login, /auth/me
  └── billing/
      ├── router.py              # /billing/checkout, /subscription, /pulse
      └── dependencies.py        # require_active_subscription — protège les endpoints premium
```

Rafraîchir les modèles (après chaque journée de championnat, ou via cron) :

```bash
python export_model_artifacts.py
```

Installer les dépendances :

```bash
pip install -r api/requirements.txt
```

Lancer l'API en local — **depuis le dossier `api/`** (nécessaire pour que
les imports `from app.core...` / `from app.auth...` résolvent
correctement le package `api/app/`) :

```bash
cd api
uvicorn main:app --reload --port 8000 --env-file .env
```

`--env-file .env` charge `JWT_SECRET_KEY` depuis `api/.env` (voir section
Authentification ci-dessous) — sans ce fichier, l'API démarre quand même
mais avec la clé de développement par défaut (à ne jamais utiliser en
production).

Documentation interactive : http://localhost:8000/docs

Lancer les tests (TestClient, pas de serveur réseau requis) :

```bash
python api/test_main.py             # endpoints de prédiction
python api/test_auth.py             # authentification (base SQLite isolée, jamais api/app.db)
python api/test_chariow_billing.py  # facturation Chariow (mocks + signatures Pulse réelles, base isolée)
python api/test_premium.py          # protection des endpoints premium par require_active_subscription
```

## Résolution des noms d'équipes

Les noms d'équipes fournis dans l'URL/le body sont résolus dans cet ordre,
avant tout calcul :

1. **Exact** — le nom correspond tel quel à une équipe connue.
2. **Normalisé** — insensible à la casse et aux accents (`"st etienne"` →
   `"St Etienne"`).
3. **Alias** — dictionnaire `TEAM_ALIASES` couvrant les abréviations
   médiatiques et noms officiels complets des 5 ligues (`"PSG"` → `"Paris
   SG"`, `"Bayern"` → `"Bayern Munich"`, `"AC Milan"` → `"Milan"`, etc.).
4. **Flou (fuzzy)** — dernier recours pour les fautes de frappe. **Jamais**
   résolu silencieusement : renvoie une erreur 404 avec des suggestions,
   pour ne jamais prédire un match sur la mauvaise équipe sans que
   l'appelant s'en aperçoive.

Chaque réponse de prédiction indique la méthode utilisée par équipe via
`home_team_resolution` / `away_team_resolution` (`"exact"` | `"normalized"`
| `"alias"`).

## Endpoints

### `GET /health`

Vérifie que l'API tourne et liste les ligues chargées en mémoire.

**Réponse :**
```json
{
  "status": "ok",
  "leagues_loaded": ["Bundesliga", "LaLiga", "Ligue1", "PremierLeague", "SerieA"],
  "checked_at": "2026-08-03T18:01:40.536481+00:00"
}
```

### `GET /leagues`

Liste les ligues disponibles, leurs équipes, et la date d'entraînement/de
données du modèle.

**Réponse (extrait) :**
```json
{
  "Ligue1": {
    "teams": ["Ajaccio", "Amiens", "Angers", "..."],
    "trained_at": "2026-08-03T18:00:13.160478+00:00",
    "data_up_to": "2025-05-17T00:00:00"
  }
}
```

### `GET /predictions/{league}/{home_team}/{away_team}`

Prédiction 1X2 + probabilité de plus/moins de 2.5 buts + scores exacts les plus probables pour un
seul match. `home_team`/`away_team` acceptent nom exact, alias, ou variante
accents/casse (voir résolution ci-dessus).

**Requête :** `GET /predictions/Ligue1/PSG/Marseille`

**Réponse :**
```json
{
  "league": "Ligue1",
  "home_team": "Paris SG",
  "away_team": "Marseille",
  "home_win": 0.6179,
  "draw": 0.196,
  "away_win": 0.1861,
  "over_2_5": 0.7184,
  "under_2_5": 0.2816,
  "most_likely_scores": [
    {"home_goals": 2, "away_goals": 1, "probability": 0.0923},
    {"home_goals": 1, "away_goals": 1, "probability": 0.0812},
    {"home_goals": 3, "away_goals": 1, "probability": 0.0747},
    {"home_goals": 2, "away_goals": 0, "probability": 0.0712},
    {"home_goals": 2, "away_goals": 2, "probability": 0.0598}
  ],
  "model_trained_at": "2026-08-03T18:00:13.160478+00:00",
  "model_data_up_to": "2025-05-17T00:00:00",
  "home_team_resolution": "alias",
  "away_team_resolution": "exact"
}
```

**Erreur (équipe non reconnue) → HTTP 404 :**
```json
{
  "detail": "Équipe domicile non reconnue : 'Marseil'. Vouliez-vous dire : ['Marseille'] ?"
}
```

### `POST /predictions/batch`

Prédictions pour plusieurs matchs en un seul appel (ex. une journée
complète de championnat, potentiellement multi-ligues). Chaque match est
traité indépendamment : une équipe non reconnue sur un match ne bloque pas
les autres.

**Requête :**
```json
[
  {"league": "Ligue1", "home_team": "PSG", "away_team": "Marseille"},
  {"league": "Ligue1", "home_team": "Xyzabc", "away_team": "Lyon"},
  {"league": "Bundesliga", "home_team": "Bayern", "away_team": "Dortmund"}
]
```

**Réponse :**
```json
[
  {
    "league": "Ligue1",
    "home_team_input": "PSG",
    "away_team_input": "Marseille",
    "ok": true,
    "prediction": { "...": "MatchPrediction complet, voir ci-dessus" },
    "error": null,
    "suggestions": []
  },
  {
    "league": "Ligue1",
    "home_team_input": "Xyzabc",
    "away_team_input": "Lyon",
    "ok": false,
    "prediction": null,
    "error": "Équipe domicile non reconnue : 'Xyzabc'.",
    "suggestions": []
  },
  {
    "league": "Bundesliga",
    "home_team_input": "Bayern",
    "away_team_input": "Dortmund",
    "ok": true,
    "prediction": { "...": "..." },
    "error": null,
    "suggestions": []
  }
]
```

### `GET /ratings/{league}`

Classement des équipes d'une ligue par `net_rating` (attack - defense).

**Requête :** `GET /ratings/Bundesliga`

**Réponse (extrait) :**
```json
[
  {"team": "Bayern Munich", "attack": 0.7963, "defense": 0.2273, "net_rating": 0.569}
]
```

## Authentification

Système JWT indépendant des endpoints de prédiction (`/health`,
`/leagues`, `/predictions/*`, `/ratings/*` restent **publics**, sans
token — l'auth n'a touché à aucun d'entre eux). Utilisateurs stockés dans
une base SQL (SQLite en développement, cf. section suivante).

### `POST /auth/register`

**Requête :**
```json
{"email": "alice@example.com", "password": "correct-horse-battery-staple"}
```

**Réponse (201) :**
```json
{"id": 1, "email": "alice@example.com", "is_active": true, "created_at": "2026-08-03T20:07:18.801126+00:00"}
```

`hashed_password` n'est jamais exposé dans la réponse. Un email déjà
enregistré renvoie **400** (`"Un compte existe déjà avec cet email"`).

### `POST /auth/login`

Formulaire OAuth2 standard (`application/x-www-form-urlencoded`, pas de
JSON) — le champ s'appelle `username` par convention OAuth2 même si on y
met un email ; ça permet aussi de tester directement depuis le bouton
"Authorize" de `/docs`.

**Requête :** `username=alice@example.com&password=correct-horse-battery-staple`

**Réponse (200) :**
```json
{"access_token": "eyJhbGciOiJIUzI1NiIs...", "token_type": "bearer"}
```

Email inconnu OU mot de passe incorrect → **401** avec exactement le même
message (`"Email ou mot de passe incorrect"`) dans les deux cas — pour ne
jamais laisser deviner quels emails sont enregistrés.

### `GET /auth/me`

Nécessite `Authorization: Bearer <token>`. Sans token ou token
invalide/expiré → **401**. Avec un token valide → **200** avec les infos
du compte (jamais `hashed_password`).

## Facturation (Chariow)

Licences gérées via un lien de checkout Chariow (hébergé) + Pulses
(webhooks) — **aucune donnée de paiement (Mobile Money, carte...) ne
transite ni n'est stockée par ce backend**. La SEULE source de vérité sur
l'état réel d'une licence est le Pulse Chariow signé (`/billing/pulse`) —
jamais ce qu'un client affirmerait de son propre statut.

Tous les endpoints `/billing/*` sauf `/pulse` nécessitent
`Authorization: Bearer <token>` (obtenu via `/auth/login`).

⚠️ **PAS de renouvellement automatique.** Contrairement à un abonnement
Stripe classique, un produit Licence Chariow n'est **pas** prélevé
automatiquement à échéance (confirmé via la documentation/interface
Chariow — les produits sont créés en mode "Paiement unique", prix fixe ;
le mode "Prix libre" est exclu de l'API de checkout). **Renouveler = repasser
par `POST /billing/checkout` avec le même plan**, avant ou après expiration
— c'est exactement le même appel qu'un premier achat, aucun endpoint séparé.
Le champ `days_until_expiry` (voir plus bas) permet d'afficher un compte à
rebours côté frontend pour inciter au renouvellement avant expiration.

### `POST /billing/checkout`

**Requête :**
```json
{
  "plan": "monthly",
  "first_name": "Awa", "last_name": "Koné",
  "phone_number": "0700000000", "phone_country_code": "+225"
}
```
Contrairement à Stripe Checkout (qui ne demandait que le plan), Chariow a
besoin des informations client dès la création du lien — à collecter côté
frontend (page `/billing`, formulaire avant redirection).

**Réponse (200) :** `{"checkout_url": "https://chariow.com/checkout/..."}`
— rediriger l'utilisateur vers cette URL (page Chariow hébergée, Mobile
Money natif). Plan inconnu ou Product ID non configuré → **400**. Champ
client manquant → **422**.

### `GET /billing/subscription`

**Réponse (200) :**
```json
{"status": "active", "plan": "monthly", "is_active": true,
 "current_period_end": "2026-09-02T20:23:24+00:00", "days_until_expiry": null}
```
Avant tout achat : `{"status": "none", "plan": null, "is_active": false, "current_period_end": null, "days_until_expiry": null}`.
`days_until_expiry` reflète la dernière valeur reçue via le Pulse
`license.nearing_expiry` — remis à `null` à chaque nouvel achat/renouvellement.

### `POST /billing/pulse`

Reçoit les Pulses (webhooks) Chariow : `successful.sale` (achat ou
renouvellement réussi — active la licence), `license.activated`
(confirmation, complète `successful.sale`), `license.expired`,
`license.revoked`, `license.nearing_expiry` (met à jour `days_until_expiry`).

Signature `x-pulse-signature` (HMAC-SHA256 hex du corps brut avec
`CHARIOW_PULSE_SECRET`) vérifiée à chaque requête — absente/invalide →
**400**, événement **jamais** traité. Déduplication sur le header
`x-pulse-delivery-id` : une delivery déjà vue est ignorée silencieusement
(Chariow peut la renvoyer après un timeout/5xx). Ne nécessite PAS de token
JWT (appelé par Chariow, pas par un utilisateur) mais la vérification de
signature joue exactement ce rôle de sécurité.

### ⚠️ Point non vérifié en conditions réelles

L'appel de création de lien de checkout
(`app/billing/router.py::_create_chariow_checkout_link` — endpoint
`POST /checkout`, structure `data.step`/`data.payment.checkout_url`) est
confirmé via la doc officielle (chariow.dev/en/guides/checkout). Reste à
lever avant de considérer l'intégration fiable pour de vrais clients :

- **Mode test des clés API** — vérifier dans Paramètres → Clés API du
  dashboard Chariow s'il existe des clés de test distinctes des clés live
  avant de configurer une vraie clé en développement.

### Tester en local

1. Démarrer l'API (`cd api && uvicorn main:app --reload --port 8000 --env-file .env`).
2. Créer un produit Licence de test dans le dashboard Chariow (mode
   "Paiement unique", prix fixe) et un Pulse pointant vers l'API locale
   exposée via un tunnel (ex. [ngrok](https://ngrok.com/) :
   `ngrok http 8000`, puis `https://<sous-domaine>.ngrok.io/billing/pulse`).
3. Renseigner `CHARIOW_PULSE_SECRET` dans `api/.env` avec le secret défini
   au moment de créer le Pulse, et redémarrer l'API.
4. Déclencher un vrai achat de test pour observer les événements reçus, ou
   reproduire les scénarios sans réseau via `python api/test_chariow_billing.py`
   (signatures HMAC réelles, appel de checkout mocké).
5. Vérifier via `GET /billing/subscription` (avec un token) que le statut a
   changé.

### Créer les 2 Product ID

1. Dashboard Chariow → **Paramètres → Clés API** : copier la clé dans
   `CHARIOW_API_KEY`.
2. Dashboard Chariow → **Produits** → créer un produit Licence "mensuel"
   et un produit Licence "annuel", chacun en mode **Paiement unique** avec
   un **prix fixe** (le mode "Prix libre" n'est pas utilisable pour le
   checkout via API) — copier leurs identifiants respectivement dans
   `CHARIOW_PRODUCT_ID_MONTHLY` et `CHARIOW_PRODUCT_ID_YEARLY`.

## Codes d'erreur

| Code | Cas |
|---|---|
| 200 | Prédiction résolue (via exact, normalisé, ou alias), ou requête auth/billing réussie |
| 400 | Inscription avec un email déjà utilisé, plan de facturation inconnu, ou signature de Pulse invalide |
| 401 | Connexion avec email/mot de passe incorrect, ou endpoint protégé (`/auth/me`, `/billing/*`) sans token valide |
| 402 | Accès à un endpoint premium (via `require_active_subscription`) sans abonnement actif |
| 404 | Ligue inconnue, ou équipe non résolue avec confiance (fuzzy/aucune correspondance) — le corps inclut des suggestions si disponibles |

## Notes pour un backend consommateur (ex. Node/Express)

- Les endpoints de **prédiction** (`/health`, `/leagues`, `/predictions/*`,
  `/ratings/*`) restent en lecture seule et **publics**, sans
  authentification — inchangé par l'ajout du système JWT. Si une
  restriction est nécessaire un jour, elle se ferait explicitement via
  `Depends(get_current_user)` sur les routes concernées, pas par défaut.
- Les endpoints **`/auth/*`** appliquent l'authentification décrite
  ci-dessus ; un token JWT (`Authorization: Bearer <token>`) obtenu via
  `/auth/login` est requis pour `/auth/me`.
- `most_likely_scores` est limité aux 5 scores les plus probables (top_n
  fixé côté serveur) et à un maximum de 8 buts par équipe dans le calcul de
  la matrice — au-delà, la probabilité est négligeable.
- Le endpoint batch traite les matchs séquentiellement ; pour une journée
  de championnat complète (~10 matchs par ligue), le temps de réponse reste
  de l'ordre de quelques dizaines de millisecondes (pas de ré-entraînement,
  uniquement du calcul de matrice Poisson déjà vectorisable).
- `model_trained_at` / `model_data_up_to` sont renvoyés sur chaque
  prédiction pour que le consommateur puisse afficher la fraîcheur du
  modèle utilisé, ou déclencher une alerte si `data_up_to` devient trop
  ancien par rapport à la date du jour.

## Configuration — JWT_SECRET_KEY

`api/app/core/security_config.py` lit `JWT_SECRET_KEY` depuis
l'environnement, avec une valeur par défaut **volontairement non sécurisée**
(`dev-only-insecure-key-CHANGE-ME-IN-PRODUCTION`) qui ne doit jamais servir
en dehors du développement local.

**Développement local** — une vraie clé a été générée et placée dans
`api/.env` (fichier gitignoré, jamais commité) :

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Chargée automatiquement au lancement via `--env-file` (pas de
`python-dotenv` importé dans le code applicatif — c'est uvicorn qui lit le
fichier) :

```bash
cd api
uvicorn main:app --reload --port 8000 --env-file .env
```

`api/.env.example` documente le format (à copier en `.env` sur une
nouvelle machine de dev).

**Production** — définir `JWT_SECRET_KEY` comme variable d'environnement
au niveau de la plateforme d'hébergement (jamais dans un fichier commité) :
un garde-fou dans `security_config.py` lève une `RuntimeError` au démarrage
si `ENV=production` est positionné et que `JWT_SECRET_KEY` a été laissée à
sa valeur par défaut — pour ne jamais démarrer silencieusement avec une
clé de dev en prod.

## Configuration — DATABASE_URL

SQLite (`api/app.db`) suffit en développement — aucune dépendance externe.
Pour basculer vers PostgreSQL/Supabase (recommandé en production), il
suffit de définir la variable d'environnement `DATABASE_URL`, **rien à
changer dans le code** (SQLModel/SQLAlchemy abstrait la différence) :

```bash
export DATABASE_URL="postgresql://user:password@host:5432/dbname"
```

Avec Supabase spécifiquement : récupérer la "Connection string" (mode
"Session" ou "Transaction" selon le pooling voulu) depuis Project Settings
→ Database, et l'utiliser telle quelle comme `DATABASE_URL`. Le driver
PostgreSQL (`psycopg2-binary` ou `psycopg[binary]`) doit être ajouté à
`api/requirements.txt` avant ce basculement — pas encore nécessaire pour le
développement SQLite actuel.
