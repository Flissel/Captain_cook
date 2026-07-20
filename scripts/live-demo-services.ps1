#requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position=0)]
    [ValidateSet("start", "health", "stop")]
    [string]$Action
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$n8n = Join-Path $PSScriptRoot 'captain-n8n.ps1'
$minibook = Join-Path $PSScriptRoot 'minibook-demo.ps1'
$evidence = Join-Path $root '.captain-cook/evidence/live-demo-services.json'

function Invoke-Health {
    & $n8n status | Out-Null
    & $minibook status
    & (Join-Path $PSScriptRoot 'demo-preflight.ps1') -EnvFile (Join-Path $root '.env')
    $summary = [ordered]@{ schema='captain.live-demo-services.v1'; checked_at=(Get-Date).ToUniversalTime().ToString('o'); status='ready'; secrets='redacted'; services=@('gateway-captain_test','minibook-local','captain-n8n-rest','captain-n8n-mcp','mailpit') }
    New-Item -ItemType Directory -Force (Split-Path $evidence -Parent) | Out-Null
    $summary | ConvertTo-Json -Depth 4 | Set-Content $evidence -Encoding utf8
    Write-Host '[ready] redacted .captain-cook/evidence/live-demo-services.json'
}
Push-Location $root
try {
    switch ($Action) {
        start {
            docker compose --env-file .env up -d --wait mailpit
            if ($LASTEXITCODE -ne 0) { throw 'Captain Mailpit failed to start.' }
            & $n8n init; & $n8n start; & $n8n bootstrap
            & $minibook bootstrap
            Invoke-Health
        }
        health { Invoke-Health }
        stop {
            & $minibook stop
            & $n8n stop
            docker compose --env-file .env stop mailpit
            if ($LASTEXITCODE -ne 0) { throw 'Captain Mailpit stop failed.' }
            Write-Host '[ready] only Captain-managed demo services stopped'
        }
    }
} finally { Pop-Location }
