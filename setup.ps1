#requires -Version 7.0
[CmdletBinding()]
param([switch] $CheckOnly)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Import-Module (Join-Path $root 'scripts/setup/Common.psm1') -Force
Import-Module (Join-Path $root 'scripts/setup/Preflight.psm1') -Force
Import-Module (Join-Path $root 'scripts/setup/Lifecycle.psm1') -Force

Write-Host 'Captain Cook – einfache Einrichtung' -ForegroundColor Cyan
Write-Host 'Fehlende Komponenten werden erklärt und nur nach deiner Zustimmung installiert.'

$prerequisites = [ordered]@{ Git='git'; Python='python'; Node='node'; Docker='docker' }
foreach ($item in $prerequisites.GetEnumerator()) {
    if (Get-Command $item.Value -ErrorAction SilentlyContinue) { continue }
    Write-Host "`n$($item.Key) fehlt und wird für das Gesamtsystem benötigt." -ForegroundColor Yellow
    if ($CheckOnly) { Write-Error "$($item.Key) fehlt."; exit 1 }
    $answer = Read-Host "$($item.Key) jetzt benutzergeführt installieren? [J/n]"
    if ($answer -notin @('', 'j', 'J', 'ja', 'Ja', 'y', 'Y', 'yes')) { Write-Error "$($item.Key) bleibt unvollständig."; exit 1 }
    $installed = Install-SetupPrerequisite -Name $item.Key -ConfirmInstall { $true }
    Write-Host $installed.Message
    if ($installed.Status -ne 'Ready') { exit 1 }
    $confirmation = Confirm-InstalledPrerequisite -Name $item.Key
    Write-Host $confirmation.Remediation
    if ($confirmation.Status -eq 'RestartRequired') { Write-Host $confirmation.Message; exit 1 }
    if ($confirmation.Status -ne 'Ready') { exit 1 }
}

$checkpointPath = Join-Path $root '.captain-cook/checkpoint.json'
$checkpoint = Get-SetupCheckpoint -Path $checkpointPath
$result = Invoke-GuidedSetup -Root $root -Checkpoint $checkpoint
Write-Host $result.Message -ForegroundColor $(if ($result.Status -eq 'Ready') {'Green'} else {'Red'})
if ($result.Status -ne 'Ready') { Write-Host 'Behebe den Hinweis und starte .\repair.ps1 oder .\setup.ps1 erneut.'; exit 1 }
Write-Host 'Minibook: http://localhost:3457'
Write-Host 'Mailpit:  http://localhost:8025'
Write-Host 'n8n:      siehe N8N_URL in .env'
