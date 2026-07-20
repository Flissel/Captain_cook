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
$rootEnv = Join-Path $root '.env'
$n8nEnv = Join-Path $root '.env.captain-n8n'
$testCompose = Join-Path $root 'docker-compose.test.yml'
$stateDir = Join-Path $root '.captain-cook'
$gatewayPid = Join-Path $stateDir 'gateway-demo.pid'
$evidence = Join-Path $stateDir 'evidence/live-demo-services.json'
$project = 'captain-cook-live-demo'

function Read-Env([string]$Path) {
    $values = [ordered]@{}
    if (Test-Path $Path) { foreach ($line in [IO.File]::ReadAllLines($Path)) {
        if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith('#')) { continue }
        if ($line -notmatch '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { throw "Invalid local environment line in $([IO.Path]::GetFileName($Path))." }
        if ($values.Contains($Matches[1])) { throw "Duplicate local environment key: $($Matches[1])" }
        $values[$Matches[1]] = $Matches[2]
    }}
    $values
}
function Save-Env($Values, [string]$Path) {
    $lines = foreach ($item in $Values.GetEnumerator()) { '{0}={1}' -f $item.Key,$item.Value }
    [IO.File]::WriteAllLines($Path, $lines, [Text.UTF8Encoding]::new($false))
}
function New-Secret([int]$Bytes=32) {
    $buffer = [byte[]]::new($Bytes)
    [Security.Cryptography.RandomNumberGenerator]::Fill($buffer)
    [Convert]::ToBase64String($buffer).TrimEnd('=').Replace('+','-').Replace('/','_')
}
function Set-Missing($Values, [string]$Name, [scriptblock]$Factory) {
    if (-not $Values.Contains($Name) -or [string]::IsNullOrWhiteSpace([string]$Values[$Name])) { $Values[$Name] = & $Factory }
}
function Initialize-LocalEnvironment {
    $values = Read-Env $rootEnv
    Set-Missing $values 'MARIADB_PASSWORD' { New-Secret }
    Set-Missing $values 'MARIADB_ROOT_PASSWORD' { New-Secret }
    Set-Missing $values 'MARIADB_TEST_PASSWORD' { New-Secret }
    Set-Missing $values 'MARIADB_TEST_ROOT_PASSWORD' { New-Secret }
    Set-Missing $values 'CAPTAIN_GATEWAY_TOKEN' { New-Secret }
    Set-Missing $values 'WORKER_GATEWAY_TOKEN' { New-Secret }
    Set-Missing $values 'MARIADB_TEST_PORT' { '33306' }
    Set-Missing $values 'GATEWAY_PORT' { '8090' }
    $escapedPassword = [Uri]::EscapeDataString([string]$values['MARIADB_TEST_PASSWORD'])
    $values['TEST_MARIADB_DSN'] = "mariadb://captain_test:${escapedPassword}@127.0.0.1:$($values['MARIADB_TEST_PORT'])/captain_test"
    $values['LEDGER_DSN'] = $values['TEST_MARIADB_DSN']
    $values['CAPTAIN_GATEWAY_URL'] = "http://127.0.0.1:$($values['GATEWAY_PORT'])"
    Save-Env $values $rootEnv
    Write-Host '[ready] local Gateway/captain_test settings initialized (values redacted)'
    $values
}
function Sync-CaptainN8nEnvironment($Values) {
    if (-not (Test-Path $n8nEnv)) { throw 'Captain n8n credential file is missing after bootstrap.' }
    $source = Read-Env $n8nEnv
    foreach ($name in @('CAPTAIN_N8N_PORT','CAPTAIN_N8N_API_KEY','CAPTAIN_N8N_MCP_TOKEN','CAPTAIN_N8N_MCP_BROKER_URL')) {
        if (-not $source.Contains($name) -or [string]::IsNullOrWhiteSpace([string]$source[$name])) { throw "Captain n8n bootstrap did not provide $name." }
        $Values[$name] = $source[$name]
    }
    $Values['CAPTAIN_N8N_URL'] = "http://127.0.0.1:$($Values['CAPTAIN_N8N_PORT'])"
    Save-Env $Values $rootEnv
    Write-Host '[ready] Captain n8n credentials inherited (values redacted)'
}
function Set-ProcessEnvironment($Values) {
    foreach ($item in $Values.GetEnumerator()) { [Environment]::SetEnvironmentVariable([string]$item.Key,[string]$item.Value,'Process') }
}
function Initialize-CaptainN8n($Values) {
    $n8n = Join-Path $PSScriptRoot 'captain-n8n.ps1'
    $running = @(& docker ps --filter 'label=com.docker.compose.project=captain-n8n-builder' --filter 'label=com.docker.compose.service=n8n' --format '{{.ID}}')
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the Captain n8n project.' }
    if ($running.Count -gt 0) {
        if (-not (Test-Path $n8nEnv)) { throw 'Captain n8n is running, but .env.captain-n8n is missing. Restore its local owner/API/MCP credentials; they cannot be reconstructed safely.' }
        & $n8n bootstrap
    } else {
        & $n8n init
        & $n8n start
        & $n8n bootstrap
    }
    Sync-CaptainN8nEnvironment $Values
}
function Start-Gateway($Values) {
    try { if ((Invoke-WebRequest "$($Values['CAPTAIN_GATEWAY_URL'])/healthz" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200) { Write-Host '[ready] Gateway already healthy'; return } } catch {}
    New-Item -ItemType Directory -Force $stateDir | Out-Null
    $python = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) { $python = (& python -c 'import sys; print(sys.executable)').Trim() }
    if (-not (Test-Path $python -PathType Leaf)) { throw 'A concrete Python 3.11 executable is required for the managed Gateway.' }
    Set-ProcessEnvironment $Values
    $process = Start-Process $python -ArgumentList '-m','gateway.app' -WorkingDirectory $root -WindowStyle Hidden -PassThru
    @{pid=$process.Id;started_at=$process.StartTime.ToUniversalTime().ToString('o');executable=$process.Path} | ConvertTo-Json -Compress | Set-Content $gatewayPid -Encoding utf8
    foreach ($attempt in 1..60) { try { if ((Invoke-WebRequest "$($Values['CAPTAIN_GATEWAY_URL'])/healthz" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) { Write-Host '[ready] Gateway database=captain_test'; return } } catch {}; Start-Sleep -Milliseconds 500 }
    throw 'Gateway did not become healthy against captain_test.'
}
function Stop-ManagedGateway {
    if (-not (Test-Path $gatewayPid)) { Write-Host '[ready] no managed Gateway process'; return }
    try { $identity = Get-Content $gatewayPid -Raw | ConvertFrom-Json } catch { throw 'Invalid managed Gateway PID file.' }
    $process = Get-Process -Id ([int]$identity.pid) -ErrorAction SilentlyContinue
    if ($process) {
        $recordedStart = ([DateTimeOffset]$identity.started_at).UtcDateTime
        if ($process.StartTime.ToUniversalTime().Ticks -ne $recordedStart.Ticks -or [IO.Path]::GetFullPath($process.Path) -ne [IO.Path]::GetFullPath([string]$identity.executable)) { throw 'PID no longer belongs to the managed Gateway process.' }
        & taskkill.exe /PID $process.Id /T /F *> $null
        if ($LASTEXITCODE -ne 0) { throw 'Managed Gateway process tree could not be stopped.' }
    }
    Remove-Item $gatewayPid -Force
    Write-Host '[ready] managed Gateway stopped'
}
function Invoke-Health {
    & (Join-Path $PSScriptRoot 'minibook-demo.ps1') status
    & (Join-Path $PSScriptRoot 'demo-preflight.ps1') -EnvFile $rootEnv
    $summary = [ordered]@{schema='captain.live-demo-services.v1';checked_at=(Get-Date).ToUniversalTime().ToString('o');status='ready';secrets='redacted';database='captain_test';services=@('gateway','minibook','captain-n8n-rest','captain-n8n-mcp','mailpit')}
    New-Item -ItemType Directory -Force (Split-Path $evidence -Parent) | Out-Null
    $summary | ConvertTo-Json -Depth 4 | Set-Content $evidence -Encoding utf8
    Write-Host '[ready] redacted .captain-cook/evidence/live-demo-services.json'
}
Push-Location $root
try {
    switch ($Action) {
        start {
            $values = Initialize-LocalEnvironment
            Set-ProcessEnvironment $values
            Initialize-CaptainN8n $values
            docker compose --project-name $project --env-file $rootEnv --file $testCompose up -d --wait mariadb-test
            if ($LASTEXITCODE -ne 0) { throw 'Isolated captain_test MariaDB failed to start.' }
            Start-Gateway $values
            docker compose --env-file $rootEnv up -d --wait mailpit
            if ($LASTEXITCODE -ne 0) { throw 'Captain Mailpit failed to start.' }
            & (Join-Path $PSScriptRoot 'minibook-demo.ps1') bootstrap
            Invoke-Health
        }
        health { Invoke-Health }
        stop {
            & (Join-Path $PSScriptRoot 'minibook-demo.ps1') stop
            Stop-ManagedGateway
            docker compose --env-file $rootEnv stop mailpit
            docker compose --project-name $project --env-file $rootEnv --file $testCompose stop mariadb-test
            if ($LASTEXITCODE -ne 0) { throw 'Captain demo container stop failed.' }
            if (Test-Path $n8nEnv) {
                & (Join-Path $PSScriptRoot 'captain-n8n.ps1') stop
            } else {
                Write-Host '[ready] existing Captain n8n left running because its local credential file is unavailable'
            }
            Write-Host '[ready] only Captain-managed demo services stopped; no volumes removed'
        }
    }
} finally { Pop-Location }
