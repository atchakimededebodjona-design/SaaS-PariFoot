# Ré-entraînement hebdomadaire sur Railway (Cron Job)

Remplace la planification actuelle (Planificateur de tâches Windows, sur la
machine de développement — voir [README_REFRESH_JOB.md](README_REFRESH_JOB.md))
par un **Cron Job Railway**, dans le même projet que le service web, pour ne
plus dépendre d'une machine locale.

Ce document est la procédure de configuration côté dashboard Railway —
**je n'ai pas d'accès à ton compte Railway (CLI non installée/authentifiée
sur cette machine)**, donc les étapes ci-dessous sont à exécuter par toi.
Le code, lui, est prêt (voir "Ce que ce ticket a changé" en bas de page).

## Décision de stockage — pourquoi Postgres pour les artefacts, un Volume pour le CSV

| Donnée | Stockage retenu | Pourquoi |
|---|---|---|
| `api/model_artifacts/*.json` (paramètres Dixon-Coles) | **Table Postgres `model_artifact`** | Le Cron Job et le service web sont **deux services Railway séparés, chacun avec son propre système de fichiers**. Un Volume Railway est attaché à un seul service (confirmé dans la doc Railway : *"Each service can only have a single volume"*) — un fichier écrit par le Cron Job sur son propre Volume ne serait donc **jamais vu par le service web**, qui continuerait à servir les anciens modèles indéfiniment. Postgres, lui, est déjà accessible aux deux services via `DATABASE_URL`. C'est la seule des trois options du ticket qui résout le vrai problème (distribuer le résultat au service qui sert les prédictions), pas seulement la persistance du job. |
| `data/all_leagues_raw_with_stats.csv` (historique brut) | **Volume Railway**, attaché uniquement au Cron Job | Contrairement aux artefacts, ce fichier n'est lu/écrit que par le Cron Job lui-même (`update_raw_data.py`) — jamais par le service web. La limite "1 volume = 1 service" ne pose donc aucun problème ici. Migrer aussi cette donnée vers Postgres maintenant créerait une table `match_history` jetable, tout de suite remplacée par les tables `matches`/`match_stats` prévues en Phase 2 de l'audit — préférable d'attendre. |

Hypothèse posée faute d'accès au dashboard : **une base Postgres est déjà
provisionnée sur Railway pour le service web** (le code le suppose déjà —
`DATABASE_URL` vers Postgres en production, voir `api/app/core/database.py`).
Si ce n'est pas encore le cas, il faut d'abord ajouter l'add-on Postgres au
projet avant l'étape 4 ci-dessous.

## Étapes de configuration

### 1. Créer le service Cron Job

Dans le projet Railway existant (celui du service web) : **New → GitHub Repo**
→ sélectionner le même dépôt. Ça crée un **second service** dans le même
projet (le service web n'est pas touché).

### 2. Root Directory = racine du dépôt (pas `api/`)

Dans **Settings** du nouveau service : laisser **Root Directory** vide (ou
`/`). C'est **différent du service web**, qui utilise `api/` comme racine
(pour ne déployer que l'API) — le Cron Job a besoin des scripts racine
(`refresh_and_retrain.py`, `update_raw_data.py`, ...) et de `data/`, absents
du sous-dossier `api/`.

> **Dépannage** — erreur `python: can't open file '/app/refresh_and_retrain.py':
> [Errno 2] No such file or directory` : Root Directory est resté sur `api`
> (copié depuis la config du service web) au lieu d'être vide. Railway ne
> récupère depuis GitHub QUE le sous-dossier indiqué en Root Directory —
> avec `api`, `refresh_and_retrain.py` (à la racine du dépôt) n'est jamais
> présent dans le conteneur, quelle que soit la Start Command. Aucun chemin
> relatif/absolu ne corrige ça : il faut vider le champ Root Directory.

### 3. Fichier de config = `railway.cron.json`

Toujours dans **Settings** : renseigner **Config-as-code file path** =
`railway.cron.json` (fichier ajouté à la racine du dépôt par ce ticket).
Nom volontairement distinct de `railway.json` pour ne jamais entrer en
conflit avec une config future du service web.

Ce fichier fixe :
- `deploy.cronSchedule` = `"0 4 * * 1"` — lundi 4h00 **UTC**. C'est le même
  créneau que l'actuel (lundi 4h, choisi car tous les matchs de la semaine
  sont joués) — **si tu es en Côte d'Ivoire/zone UTC+0, aucun ajustement
  n'est nécessaire ; si tu es dans un autre fuseau, ajuste l'heure en
  conséquence** (Railway évalue tous les cron en UTC, sans exception).
  Minimum autorisé par Railway : 5 minutes entre deux exécutions — très
  large marge pour un rythme hebdomadaire.
- `deploy.startCommand` = `python refresh_and_retrain.py --raw-file data_persisted/all_leagues_raw_with_stats.csv`
  — pointe explicitement vers le chemin monté par le Volume (étape 5), pour
  ne jamais toucher au `data/all_leagues_raw_with_stats.csv` commité dans
  le dépôt (celui-ci sert uniquement d'amorce, voir étape 6).

### 4. Variables d'environnement du Cron Job

Dans **Variables** de ce service :

| Variable | Valeur | Nécessaire ? |
|---|---|---|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` (référence à ton add-on Postgres existant — à adapter au nom réel du service Postgres dans ton projet) | **Oui** — c'est ce qui permet l'écriture dans `model_artifact` (§ décision de stockage) |
| `ALERT_WEBHOOK_URL` | URL d'un webhook entrant Slack ou Discord | Optionnel — active la notification d'échec (étape 7) |

Aucune variable Chariow (`CHARIOW_*`), `JWT_SECRET_KEY`, `ALLOWED_ORIGINS`
ni `FRONTEND_URL` n'est nécessaire — ce job ne touche ni à l'auth ni à la
facturation.

### 5. Attacher un Volume

Dans l'onglet **Volumes** de ce service : créer un volume, **Mount Path**
= `/app/data_persisted`.

Confirmé par la documentation officielle Railway (pas par un déploiement
réel, auquel je n'ai pas accès) : sans Dockerfile, Railway construit avec
Nixpacks, dont le `WORKDIR` de l'image est `/app` — c'est le répertoire de
travail depuis lequel `startCommand` s'exécute. Un Volume monté sur
`/app/data_persisted` est donc bien ce que voit le script à l'exécution
via le chemin relatif `data_persisted/...` utilisé dans `startCommand`.
Source : [docs.railway.com/volumes](https://docs.railway.com/volumes) —
*« if you are using nixpacks the default project directory would be
`/app` so your mount point would be `/app/temp_files` »* (exemple donné
par Railway lui-même, transposé ici à `data_persisted`).

### 6. Premier déploiement — amorçage automatique

Le Volume démarre **vide**. `refresh_and_retrain.py` le détecte
automatiquement (`_ensure_raw_file_seeded`, ajouté par ce ticket) : si
`data_persisted/all_leagues_raw_with_stats.csv` n'existe pas encore, il est
copié depuis `data/all_leagues_raw_with_stats.csv` (l'historique commité
dans le dépôt, 10 707 matchs) avant la mise à jour — **aucune action
manuelle requise**, contrairement à ce qu'un Volume vide impliquerait
normalement.

### 7. Alerte en cas d'échec

- **Minimum (déjà actif, sans configuration)** : tout échec (code de sortie
  1 ou 2) écrit une ligne `XFOOT_RETRAIN_JOB_FAILED` ou
  `XFOOT_RETRAIN_JOB_PARTIAL` dans les logs — visibles dans **ce service →
  Deployments → [la run] → Logs** sur Railway, et cette run apparaît comme
  échouée (exit code ≠ 0) dans l'historique du Cron Job.
- **Notification externe (optionnelle)** : si `ALERT_WEBHOOK_URL` est
  définie (étape 4), un message est posté sur ce webhook à chaque échec ou
  succès partiel — compatible tel quel avec un webhook entrant Slack
  (Slack → Apps → Incoming Webhooks) ou un webhook Discord (Paramètres du
  salon → Intégrations → Webhooks), sans code supplémentaire à écrire.

### 8. Test manuel

Une fois les étapes 1 à 5 faites : dans le service Cron Job sur Railway,
utiliser **Deploy → Run Now** (ou l'équivalent affiché dans l'interface —
le libellé exact peut varier selon la version du dashboard) pour déclencher
une exécution immédiate, sans attendre lundi 4h.

**Je n'ai pas pu déclencher ni observer cette exécution moi-même** (pas
d'accès Railway depuis cet environnement) — à faire côté utilisateur.
Points à vérifier dans les logs de cette run :

1. `Amorçage : ... -> data_persisted/... (fichier absent, ...)` — confirme
   que l'amorçage automatique (étape 6) a bien joué au premier run.
2. `[b] OK — 5 ligues entraînées`
3. `[Bundesliga] -> api\model_artifacts\Bundesliga.json + base` (répété pour
   les 5 ligues) — le `+ base` confirme l'écriture Postgres réussie.
4. `JOB TERMINÉ AVEC SUCCÈS COMPLET.`

Puis, côté service web (sans le redéployer) : les logs de démarrage
devraient afficher `Artefacts chargés depuis la base pour : [...]` au
prochain redémarrage naturel du service — le service web ne relit la base
qu'à son propre démarrage (voir `api/main.py::on_startup`), pas en continu ;
un redéploiement (ou redémarrage manuel) du service web après la première
exécution réussie du Cron Job rendra donc les modèles fraîchement
ré-entraînés visibles par l'API.

## Ce que ce ticket a changé (déjà fait, testé localement)

- `api/app/models/model_artifact.py` — nouvelle table `model_artifact`.
- `api/alembic/versions/117d1fc4bc85_add_model_artifact_table.py` — migration
  générée et vérifiée (`alembic upgrade head` puis `alembic check` : aucun
  écart résiduel) contre une base SQLite temporaire.
- `api/main.py` — au démarrage, après `init_db()`, tente de charger les
  artefacts depuis `model_artifact` et complète/écrase `LEAGUE_MODELS` pour
  les ligues trouvées en base ; toute ligue absente de la base reste servie
  depuis son fichier JSON local. Testé de bout en bout en local (base
  vide → comportement inchangé, 8/8 tests de `test_main.py` toujours au
  vert ; base peuplée → logs confirmant le chargement depuis la base, API
  toujours fonctionnelle).
- `refresh_and_retrain.py` — amorçage automatique du Volume vide, écriture
  Postgres best-effort après l'écriture JSON atomique existante (jamais
  bloquante), marqueurs de log `XFOOT_RETRAIN_JOB_FAILED`/`_PARTIAL`, alerte
  webhook optionnelle. Testé en local : `test_refresh_and_retrain.py`
  toujours au vert, et une exécution réelle (`--skip-refresh`) confirme
  l'écriture des 5 ligues en base SQLite locale ET le rechargement par
  `api/main.py` ensuite.
- `requirements.txt` (nouveau, racine) — dépendances du Cron Job, séparées
  de `api/requirements.txt`.
- `railway.cron.json` (nouveau, racine) — config du service Cron Job.

**Non touché** : `Procfile`, `api/requirements.txt`, le comportement du
service web pour un déploiement sans base peuplée (fallback fichiers
inchangé), la planification Windows existante (reste utilisable en parallèle
tant que le Cron Job Railway n'est pas validé en conditions réelles).
