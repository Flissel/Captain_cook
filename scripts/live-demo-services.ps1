#requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position=0)]
    [ValidateSet("start", "health", "stop")]
    [string]$Action,
    [switch]$RecoverDemoCredentials,
    [string]$CredentialSourceEnv
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$rootEnv = Join-Path $root '.env'
$n8nEnv = Join-Path $root '.env.captain-n8n'
$testCompose = Join-Path $root 'docker-compose.test.yml'
$stateDir = Join-Path $root '.captain-cook'
$gatewayPid = Join-Path $stateDir 'gateway-demo.pid'
$runtimePid = Join-Path $stateDir 'runtime-demo.pid'
$evidence = Join-Path $stateDir 'evidence/live-demo-services.json'
$project = 'captain-cook-live-demo'

function Read-Env([string]$Path, [string[]]$AllowedNames) {
    $values = [ordered]@{}
    if (Test-Path $Path) { foreach ($line in [IO.File]::ReadAllLines($Path)) {
        if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith('#')) { continue }
        if ($line -notmatch '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { throw "Invalid local environment line in $([IO.Path]::GetFileName($Path))." }
        $name = $Matches[1]
        if ($AllowedNames -notcontains $name) { continue }
        if ($values.Contains($name)) { throw "Duplicate local environment key: $name" }
        $values[$name] = $Matches[2]
    }}
    $values
}
function Save-Env($Values, [string]$Path) {
    $pending = [ordered]@{}; foreach ($item in $Values.GetEnumerator()) { $pending[$item.Key] = $item.Value }
    $lines = [Collections.Generic.List[string]]::new()
    if (Test-Path $Path) { foreach ($line in [IO.File]::ReadAllLines($Path)) {
        if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=') {
            $name = $Matches[1]
            if ($pending.Contains($name)) { $lines.Add(('{0}={1}' -f $name,$pending[$name])); $pending.Remove($name); continue }
        }
        $lines.Add($line)
    }}
    foreach ($item in $pending.GetEnumerator()) { $lines.Add(('{0}={1}' -f $item.Key,$item.Value)) }
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
    $allowed = @('MARIADB_PASSWORD','MARIADB_ROOT_PASSWORD','MARIADB_TEST_PASSWORD','MARIADB_TEST_ROOT_PASSWORD','CAPTAIN_GATEWAY_TOKEN','WORKER_GATEWAY_TOKEN','CAPTAIN_RUNTIME_TOKEN','MARIADB_TEST_PORT','GATEWAY_PORT','CAPTAIN_RUNTIME_PORT','TEST_MARIADB_DSN','LEDGER_DSN','CAPTAIN_GATEWAY_URL','CAPTAIN_RUNTIME_URL','N8N_API_KEY','N8N_MCP_TOKEN','CAPTAIN_N8N_PORT','CAPTAIN_N8N_API_KEY','CAPTAIN_N8N_MCP_TOKEN','CAPTAIN_N8N_MCP_BROKER_URL','CAPTAIN_N8N_URL')
    $values = Read-Env $rootEnv $allowed
    Set-Missing $values 'MARIADB_PASSWORD' { New-Secret }
    Set-Missing $values 'MARIADB_ROOT_PASSWORD' { New-Secret }
    Set-Missing $values 'MARIADB_TEST_PASSWORD' { New-Secret }
    Set-Missing $values 'MARIADB_TEST_ROOT_PASSWORD' { New-Secret }
    Set-Missing $values 'CAPTAIN_GATEWAY_TOKEN' { New-Secret }
    Set-Missing $values 'WORKER_GATEWAY_TOKEN' { New-Secret }
    Set-Missing $values 'CAPTAIN_RUNTIME_TOKEN' { New-Secret }
    Set-Missing $values 'MARIADB_TEST_PORT' { '33306' }
    Set-Missing $values 'GATEWAY_PORT' { '8090' }
    Set-Missing $values 'CAPTAIN_RUNTIME_PORT' { '8091' }
    $escapedPassword = [Uri]::EscapeDataString([string]$values['MARIADB_TEST_PASSWORD'])
    $values['TEST_MARIADB_DSN'] = "mariadb://captain_test:${escapedPassword}@127.0.0.1:$($values['MARIADB_TEST_PORT'])/captain_test"
    $values['LEDGER_DSN'] = $values['TEST_MARIADB_DSN']
    $values['CAPTAIN_GATEWAY_URL'] = "http://127.0.0.1:$($values['GATEWAY_PORT'])"
    $values['CAPTAIN_RUNTIME_URL'] = "http://127.0.0.1:$($values['CAPTAIN_RUNTIME_PORT'])"
    Save-Env $values $rootEnv
    Write-Host '[ready] local Gateway/captain_test settings initialized (values redacted)'
    $values
}
function Sync-CaptainN8nEnvironment($Values) {
    if (-not (Test-Path $n8nEnv)) { throw 'Captain n8n credential file is missing after bootstrap.' }
    $source = Read-Env $n8nEnv @('CAPTAIN_N8N_PORT','CAPTAIN_N8N_API_KEY','CAPTAIN_N8N_MCP_TOKEN','CAPTAIN_N8N_MCP_BROKER_URL')
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
function Recover-CaptainN8nEnvironment($Values, [string]$SourceEnv) {
    if ($SourceEnv -and [IO.Path]::GetFullPath($SourceEnv) -ne [IO.Path]::GetFullPath($rootEnv)) {
        if (-not (Test-Path $SourceEnv -PathType Leaf)) { throw 'The explicit credential source .env does not exist.' }
        $sourceAliases = Read-Env $SourceEnv @('N8N_API_KEY','N8N_MCP_TOKEN')
        foreach ($name in @('N8N_API_KEY','N8N_MCP_TOKEN')) {
            if ($sourceAliases.Contains($name)) { $Values[$name] = $sourceAliases[$name] }
        }
    }
    foreach ($name in @('N8N_API_KEY','N8N_MCP_TOKEN')) {
        if (-not $Values.Contains($name) -or [string]::IsNullOrWhiteSpace([string]$Values[$name])) { throw "Recovery requires a non-empty $name in the root .env." }
    }
    $baseUrl = 'http://127.0.0.1:5679'
    $authenticated = $false
    foreach ($attempt in 1..30) { try {
        $rest = Invoke-WebRequest "$baseUrl/api/v1/workflows?limit=1" -Headers @{'X-N8N-API-KEY'=[string]$Values['N8N_API_KEY']} -UseBasicParsing -TimeoutSec 5
        $body = '{"jsonrpc":"2.0","id":"credential-recovery","method":"tools/list","params":{}}'
        $mcp = Invoke-WebRequest "$baseUrl/mcp-server/http" -Method Post -Headers @{Authorization="Bearer $([string]$Values['N8N_MCP_TOKEN'])";Accept='application/json, text/event-stream'} -Body $body -ContentType 'application/json' -UseBasicParsing -TimeoutSec 5
        if ($rest.StatusCode -eq 200 -and $mcp.StatusCode -eq 200) { $authenticated=$true; break }
    } catch {}
        Start-Sleep -Seconds 1
    }
    if (-not $authenticated) { throw 'Captain n8n recovery credentials failed REST or MCP authentication.' }
    $recovered = [ordered]@{
        CAPTAIN_N8N_PORT='5679'; CAPTAIN_N8N_API_KEY=[string]$Values['N8N_API_KEY']; CAPTAIN_N8N_MCP_TOKEN=[string]$Values['N8N_MCP_TOKEN']; CAPTAIN_N8N_MCP_BROKER_URL='http://127.0.0.1:5680'
    }
    Save-Env $recovered $n8nEnv
    foreach ($item in $recovered.GetEnumerator()) { $Values[$item.Key] = $item.Value }
    $Values['CAPTAIN_N8N_URL'] = $baseUrl
    Save-Env $Values $rootEnv
    Write-Host '[ready] Captain n8n demo credentials recovered after REST/MCP verification (values redacted)'
}
function Initialize-CaptainN8n($Values, [switch]$Recover, [string]$SourceEnv) {
    $n8n = Join-Path $PSScriptRoot 'captain-n8n.ps1'
    $running = @(& docker ps --filter 'label=com.docker.compose.project=captain-n8n-builder' --filter 'label=com.docker.compose.service=n8n' --format '{{.ID}}')
    $existing = @(& docker ps -a --filter 'label=com.docker.compose.project=captain-n8n-builder' --filter 'label=com.docker.compose.service=n8n' --format '{{.ID}}')
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the Captain n8n project.' }
    if ($Recover -and $existing.Count -eq 0) { throw 'No existing Captain n8n builder is available for credential recovery.' }
    if ($Recover -and $running.Count -eq 0) {
        foreach ($service in @('postgres','n8n','mcp-broker')) {
            $containers = @(& docker ps -a --filter 'label=com.docker.compose.project=captain-n8n-builder' --filter "label=com.docker.compose.service=$service" --format '{{.ID}}')
            if ($containers.Count -gt 0) { & docker start @containers *> $null; if ($LASTEXITCODE -ne 0) { throw "Could not restart Captain n8n service $service." } }
        }
        Recover-CaptainN8nEnvironment $Values $SourceEnv
        return
    }
    if ($running.Count -gt 0) {
        if ($Recover) { Recover-CaptainN8nEnvironment $Values $SourceEnv; return }
        if (-not (Test-Path $n8nEnv)) { throw 'Captain n8n is running, but .env.captain-n8n is missing. Use -RecoverDemoCredentials only with already validated local demo aliases.' }
        & $n8n bootstrap
    } else {
        & $n8n init
        & $n8n start
        & $n8n bootstrap
    }
    Sync-CaptainN8nEnvironment $Values
}
function Stop-CaptainN8nContainers {
    $containers = @(& docker ps --filter 'label=com.docker.compose.project=captain-n8n-builder' --format '{{.ID}}')
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect Captain n8n containers.' }
    if ($containers.Count -gt 0) { & docker stop @containers *> $null; if ($LASTEXITCODE -ne 0) { throw 'Captain n8n containers could not be stopped.' } }
    Write-Host '[ready] labelled Captain n8n containers stopped; volumes preserved'
}
function Start-Gateway($Values) {
    try { if ((Invoke-WebRequest "$($Values['CAPTAIN_GATEWAY_URL'])/healthz" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200) { Write-Host '[ready] Gateway already healthy'; return } } catch {}
    $gatewayPort = [Uri]$Values['CAPTAIN_GATEWAY_URL'] | Select-Object -ExpandProperty Port
    $listener = Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $gatewayPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        $conflict = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
        if ($conflict -and $conflict.CommandLine -match '(?i)(^|\s)-m\s+gateway\.app(\s|$)') {
            & taskkill.exe /PID $listener.OwningProcess /T /F *> $null
            if ($LASTEXITCODE -ne 0) { throw 'Stale local Gateway process could not be stopped.' }
            Write-Host '[ready] stale local Gateway process stopped after failed health check'
        } else {
            throw 'Gateway port is occupied by a non-demo process; refusing to stop it.'
        }
    }
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
function Get-ManagedRuntimeProcess {
    if (-not (Test-Path $runtimePid)) { return $null }
    try { $identity = Get-Content $runtimePid -Raw | ConvertFrom-Json } catch { throw 'Invalid managed Runtime PID file.' }
    $process = Get-Process -Id ([int]$identity.pid) -ErrorAction SilentlyContinue
    if (-not $process) {
        Remove-Item $runtimePid -Force
        return $null
    }
    $recordedStart = ([DateTimeOffset]$identity.started_at).UtcDateTime
    if ($process.StartTime.ToUniversalTime().Ticks -ne $recordedStart.Ticks -or [IO.Path]::GetFullPath($process.Path) -ne [IO.Path]::GetFullPath([string]$identity.executable)) {
        throw 'PID no longer belongs to the managed Runtime process.'
    }
    return $process
}
function Start-Runtime($Values) {
    $runtimeUrl = [string]$Values['CAPTAIN_RUNTIME_URL']
    $managed = Get-ManagedRuntimeProcess
    if ($managed) {
        try {
            if ((Invoke-WebRequest "$runtimeUrl/health" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200) {
                Write-Host '[ready] Runtime already healthy with verified process identity'
                return
            }
        } catch {}
        throw 'Managed Runtime process exists but is not healthy.'
    }
    $runtimePort = ([Uri]$runtimeUrl).Port
    $listener = Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $runtimePort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) { throw 'Runtime port is occupied by an unmanaged process; refusing to reuse or stop it.' }
    New-Item -ItemType Directory -Force $stateDir | Out-Null
    $python = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) { $python = (& python -c 'import sys; print(sys.executable)').Trim() }
    if (-not (Test-Path $python -PathType Leaf)) { throw 'A concrete Python 3.11 executable is required for the managed Runtime.' }
    Set-ProcessEnvironment $Values
    $process = Start-Process $python -ArgumentList '-m','agenten.agent_runtime.runtime_entrypoint' -WorkingDirectory $root -WindowStyle Hidden -PassThru
    @{pid=$process.Id;started_at=$process.StartTime.ToUniversalTime().ToString('o');executable=$process.Path} | ConvertTo-Json -Compress | Set-Content $runtimePid -Encoding utf8
    foreach ($attempt in 1..60) {
        if (-not (Get-Process -Id $process.Id -ErrorAction SilentlyContinue)) { break }
        try { if ((Invoke-WebRequest "$runtimeUrl/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) { Write-Host '[ready] authenticated Runtime boundary'; return } } catch {}
        Start-Sleep -Milliseconds 500
    }
    Stop-ManagedRuntime
    throw 'Runtime did not become healthy.'
}
function Stop-ManagedRuntime {
    $process = Get-ManagedRuntimeProcess
    if (-not $process) { Write-Host '[ready] no managed Runtime process'; return }
    & taskkill.exe /PID $process.Id /T /F *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Managed Runtime process tree could not be stopped.' }
    Remove-Item $runtimePid -Force
    Write-Host '[ready] managed Runtime stopped'
}
function Assert-RuntimeConfiguration($Values) {
    $python = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) { $python = (& python -c 'import sys; print(sys.executable)').Trim() }
    if (-not (Test-Path $python -PathType Leaf)) { throw 'A concrete Python 3.11 executable is required for Runtime preflight.' }
    Set-ProcessEnvironment $Values
    & $python -c 'from agenten.agent_runtime.runtime_entrypoint import preflight_runtime; preflight_runtime()' *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Production Runtime configuration is unavailable; no services were started.' }
}
function Invoke-StartServices([switch]$RecoverDemoCredentials, [string]$SourceEnv) {
    $values = Initialize-LocalEnvironment
    Set-ProcessEnvironment $values
    Assert-RuntimeConfiguration $values
    Initialize-CaptainN8n $values -Recover:$RecoverDemoCredentials -SourceEnv $SourceEnv
    docker compose --project-name $project --env-file $rootEnv --file $testCompose up -d --wait mariadb-test
    if ($LASTEXITCODE -ne 0) { throw 'Isolated captain_test MariaDB failed to start.' }
    Start-Gateway $values
    Start-Runtime $values
    docker compose --env-file $rootEnv up -d --wait mailpit
    if ($LASTEXITCODE -ne 0) { throw 'Captain Mailpit failed to start.' }
    & (Join-Path $PSScriptRoot 'minibook-demo.ps1') bootstrap -RecoverDemoCredentials:$RecoverDemoCredentials
    Invoke-Health
}
function Invoke-Health {
    $values = Read-Env $rootEnv @('CAPTAIN_RUNTIME_URL')
    if (-not $values.Contains('CAPTAIN_RUNTIME_URL')) { throw 'Runtime URL is not configured.' }
    if (-not (Get-ManagedRuntimeProcess)) { throw 'Managed Runtime process is not running.' }
    if ((Invoke-WebRequest "$($values['CAPTAIN_RUNTIME_URL'])/health" -UseBasicParsing -TimeoutSec 3).StatusCode -ne 200) { throw 'Runtime health check failed.' }
    & (Join-Path $PSScriptRoot 'minibook-demo.ps1') status
    & (Join-Path $PSScriptRoot 'demo-preflight.ps1') -EnvFile $rootEnv
    $summary = [ordered]@{schema='captain.live-demo-services.v1';checked_at=(Get-Date).ToUniversalTime().ToString('o');status='ready';secrets='redacted';database='captain_test';services=@('gateway','runtime','minibook','captain-n8n-rest','captain-n8n-mcp','mailpit')}
    New-Item -ItemType Directory -Force (Split-Path $evidence -Parent) | Out-Null
    $summary | ConvertTo-Json -Depth 4 | Set-Content $evidence -Encoding utf8
    Write-Host '[ready] redacted .captain-cook/evidence/live-demo-services.json'
}
Push-Location $root
try {
    switch ($Action) {
        start {
            Invoke-StartServices -Recover:$RecoverDemoCredentials -SourceEnv $CredentialSourceEnv
        }
        health { Invoke-Health }
        stop {
            & (Join-Path $PSScriptRoot 'minibook-demo.ps1') stop
            Stop-ManagedRuntime
            Stop-ManagedGateway
            docker compose --env-file $rootEnv stop mailpit
            docker compose --project-name $project --env-file $rootEnv --file $testCompose stop mariadb-test
            if ($LASTEXITCODE -ne 0) { throw 'Captain demo container stop failed.' }
            Stop-CaptainN8nContainers
            Write-Host '[ready] only Captain-managed demo services stopped; no volumes removed'
        }
    }
} finally { Pop-Location }
