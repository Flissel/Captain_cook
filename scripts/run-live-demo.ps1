#requires -Version 7.0
[CmdletBinding()]
param(
    [switch]$LiveProviders,
    [string]$EnvFile = (Join-Path (Split-Path -Parent $PSScriptRoot) '.env')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

& (Join-Path $PSScriptRoot 'demo-preflight.ps1') -EnvFile $EnvFile
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not $LiveProviders) {
    Write-Host '[ready] infrastructure only; live providers were not requested'
    exit 0
}

foreach ($line in [IO.File]::ReadAllLines($EnvFile)) {
    if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2] }
}
Write-Host '[running] explicit provider-backed Gate E'
& (Join-Path $root 'scripts/run-gate-e.ps1')
exit $LASTEXITCODE
