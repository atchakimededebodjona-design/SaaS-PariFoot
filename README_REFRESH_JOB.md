# Job de rafraîchissement périodique — Dixon-Coles

Transforme le ré-entraînement Dixon-Coles d'un script lancé à la main en
job automatisable, avec source de données actualisée, validation
automatique, et écriture atomique des artefacts.

## Structure

```
update_raw_data.py          # (a) télécharge saison courante+précédente, fusionne, dédoublonne
export_model_artifacts.py   # (b) entraîne Dixon-Coles par ligue — train_all_leagues() en mémoire
validate_artifacts.py       # (c) garde-fous : nb équipes, home_advantage, rho, cohérence historique
refresh_and_retrain.py      # (d)+(e) orchestrateur : écriture atomique + log récapitulatif
test_refresh_and_retrain.py # simule un CSV corrompu, vérifie que rien n'est altéré
run_refresh_and_retrain.ps1 # wrapper pour le Planificateur de tâches Windows
logs/refresh_and_retrain.log
```

### Enchaînement (`refresh_and_retrain.py`)

1. **Mise à jour des données** — télécharge `season-<courante>.csv` et
   `season-<précédente>.csv` pour les 5 ligues depuis
   `github.com/datasets/football-datasets` (mêmes noms d'équipes et mêmes
   colonnes que `data/all_leagues_raw_with_stats.csv` — vérifié par
   inspection directe du dépôt avant implémentation), fusionne avec
   l'historique local en dédoublonnant sur `(date, home_team, away_team)`.
   Les deux saisons (pas une seule) sont retéléchargées à chaque
   exécution : ça couvre la transition de saison (le fichier de la
   nouvelle saison peut ne pas encore exister sur le dépôt en tout début
   d'année — 404 traité comme un cas normal, pas une erreur) et rend
   l'opération idempotente.
2. **Ré-entraînement** — `export_model_artifacts.train_all_leagues()`,
   entièrement en mémoire, rien n'est encore écrit sur disque.
3. **Validation par ligue** (`validate_artifacts.validate_artifact`) :
   - nombre d'équipes en chute de plus de 30 % par rapport à l'artefact
     précédent → rejeté (probable bug de parsing)
   - `home_advantage` hors de `[0.05, 0.4]` → rejeté
   - `rho` hors de `[-0.3, 0.1]` → rejeté
   - nombre de matchs d'entraînement en baisse par rapport au cycle
     précédent → rejeté (l'historique ne devrait jamais rétrécir)
   - dérive de plus de 0.1 sur `home_advantage`/`rho` d'un cycle à l'autre
     → signalé en avertissement, mais PAS bloquant

   Ces bornes ont été fixées à partir des valeurs réellement observées sur
   les 5 ligues (`home_advantage` entre 0.133 et 0.241, `rho` entre -0.128
   et +0.018) — largement à l'intérieur des fourchettes retenues.

   **Chaque ligue est validée indépendamment** : si une seule ligue
   échoue, son ancien artefact reste en place tel quel, les 4 autres sont
   mises à jour normalement.
4. **Écriture atomique** — chaque artefact validé est écrit dans
   `<league>.json.tmp` puis renommé vers `<league>.json` (`Path.replace`,
   atomique y compris sous Windows) : jamais d'écrasement direct, jamais
   de fichier à moitié écrit en cas de crash.
5. **Log récapitulatif** — ligues mises à jour / conservées, nombre de
   matchs, `home_advantage`/`rho`/nombre d'équipes par ligue, durée
   d'exécution. Écrit à la fois dans `logs/refresh_and_retrain.log` et sur
   la sortie standard.

### Codes de sortie

| Code | Signification |
|---|---|
| 0 | Succès complet — toutes les ligues mises à jour |
| 1 | Échec total (étape a ou b) — **aucun** artefact touché |
| 2 | Succès partiel — au moins une ligue a échoué la validation et a gardé son ancien artefact, les autres ont été mises à jour |

## Test manuel avant automatisation

```bash
# 1. Exécution complète (télécharge les données, ré-entraîne, valide, écrit)
python refresh_and_retrain.py
# -> vérifier logs/refresh_and_retrain.log et le code de sortie ($LASTEXITCODE / $?)

# 2. Ré-entraînement seul, sans re-télécharger (utile pour tester b/c/d rapidement)
python refresh_and_retrain.py --skip-refresh

# 3. Mise à jour des données seule, sans ré-entraîner (pour vérifier la
#    connectivité et le contenu récupéré avant de faire confiance au job complet)
python update_raw_data.py

# 4. Test de résilience à un CSV corrompu (n'importe JAMAIS les vrais
#    fichiers du projet — tout se passe dans un dossier temporaire)
python test_refresh_and_retrain.py
```

Après une exécution manuelle réussie, vérifier que l'API voit bien les
nouvelles données :

```bash
uvicorn api.main:app --port 8000 &
curl http://localhost:8000/leagues   # data_up_to doit refléter la mise à jour
```

## Planification

Une fois par semaine suffit (les championnats ne jouent pas tous les
jours) — un bon créneau est un jour où toutes les rencontres de la semaine
sont jouées (ex. lundi matin, après le week-end).

### Linux/macOS — cron

```cron
# Tous les lundis à 04h00
0 4 * * 1 cd /path/to/SaaS-parifoot && /usr/bin/python3 refresh_and_retrain.py >> logs/cron.log 2>&1
```

### Windows — Planificateur de tâches

Le développement se fait sous Windows (`C:\Users\CHP SOTOUBOUA\...`), deux options :

**Option A — ligne de commande (`schtasks`)**, à exécuter dans un terminal
avec les droits nécessaires :

```powershell
schtasks /create /tn "DixonColes_RefreshAndRetrain" `
  /tr "powershell.exe -ExecutionPolicy Bypass -File \"C:\Users\CHP SOTOUBOUA\SaaS parifoot\run_refresh_and_retrain.ps1\"" `
  /sc weekly /d MON /st 04:00 /rl LIMITED
```

**Option B — interface graphique** (Planificateur de tâches Windows) :

1. Ouvrir "Planificateur de tâches" → "Créer une tâche de base..."
2. Nom : `DixonColes_RefreshAndRetrain`
3. Déclencheur : Hebdomadaire, tous les lundis, 04:00
4. Action : "Démarrer un programme"
   - Programme : `powershell.exe`
   - Arguments : `-ExecutionPolicy Bypass -File "C:\Users\CHP SOTOUBOUA\SaaS parifoot\run_refresh_and_retrain.ps1"`
5. Dans les propriétés avancées : cocher "Exécuter que l'utilisateur soit
   connecté ou non" si le job doit tourner même sans session ouverte.

`run_refresh_and_retrain.ps1` se charge de se positionner dans le bon
dossier et de remonter le code de sortie (0/1/2) — le Planificateur de
tâches peut s'en servir pour déclencher une notification en cas d'échec
(action supplémentaire conditionnée au code de sortie, ou simplement en
surveillant `logs/refresh_and_retrain.log`).

## Gestion des échecs — ce qui est garanti

- Si l'étape (a) échoue (réseau, dépôt source indisponible, schéma
  changé) : le job s'arrête immédiatement, code 1, **aucun** artefact
  n'est touché — l'API continue de servir les prédictions basées sur les
  anciennes données.
- Si l'étape (b) échoue (CSV local corrompu ou illisible, optimiseur qui
  ne converge pas) : même comportement, code 1, aucun artefact touché.
- Si l'étape (c) rejette une ou plusieurs ligues (paramètres
  implausibles, chute suspecte du nombre d'équipes) : ces ligues gardent
  leur ancien artefact, les autres sont mises à jour, code 2. Le log
  détaille précisément la raison du rejet pour chaque ligue concernée.
- Aucun scénario ne peut laisser un fichier `model_artifacts/<league>.json`
  partiellement écrit ou corrompu (écriture atomique via fichier
  temporaire + renommage).

Vérifié par `test_refresh_and_retrain.py` : un CSV volontairement corrompu
en entrée du ré-entraînement produit bien un échec total (code 1), laisse
les 5 artefacts existants byte-identiques avant/après, et l'API rechargée
sur ces artefacts répond normalement.
