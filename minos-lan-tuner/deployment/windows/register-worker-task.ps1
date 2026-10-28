# Registers a logon task that starts the Python worker. Edit the three paths
# before running. This script was not executed on Windows in this build.
# Run from an elevated PowerShell:  .\register-worker-task.ps1

$Python = "C:\Python311\python.exe"
$Tuner = "C:\minos\minos_subnet\minos-lan-tuner"
$Config = "C:\minos\worker.yaml"

$Action = New-ScheduledTaskAction -Execute $Python -Argument "-m apps.worker.cli start --config `"$Config`"" -WorkingDirectory $Tuner
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "MinosLanWorker" -Action $Action -Trigger $Trigger -Settings $Settings -Force
Write-Output "Registered MinosLanWorker. Put WORKER_TOKEN in the user environment, not in worker.yaml."
