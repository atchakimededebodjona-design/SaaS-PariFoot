# run_refresh_and_retrain.ps1 — Wrapper pour le Planificateur de tâches Windows
# =============================================================================
# Exécute refresh_and_retrain.py avec le bon dossier de travail et remonte
# le code de sortie du job Python tel quel (0=succès, 1=échec total,
# 2=succès partiel) — le Planificateur de tâches Windows peut se baser
# dessus pour déclencher une alerte si besoin.
#
# Voir README_REFRESH_JOB.md pour la configuration complète de la tâche
# planifiée (schtasks / interface graphique).

$ErrorActionPreference = "Stop"

$ProjectDir = "C:\Users\CHP SOTOUBOUA\SaaS parifoot"
$PythonExe = "C:\Python314\python.exe"   # ajuster si un venv dédié est utilisé

Set-Location $ProjectDir

& $PythonExe refresh_and_retrain.py
$ExitCode = $LASTEXITCODE

if ($ExitCode -eq 0) {
    Write-Host "refresh_and_retrain.py : succès complet."
} elseif ($ExitCode -eq 2) {
    Write-Host "refresh_and_retrain.py : succès partiel — voir logs/refresh_and_retrain.log"
} else {
    Write-Host "refresh_and_retrain.py : ÉCHEC (code $ExitCode) — voir logs/refresh_and_retrain.log"
}

exit $ExitCode
