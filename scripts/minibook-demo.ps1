#requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position=0)]
    [ValidateSet("start", "bootstrap", "status", "stop")]
    [string]$Action,
    [switch]$RecoverDemoCredentials
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root '.env'
$stateDir = Join-Path $root '.captain-cook'
$pidFile = Join-Path $stateDir 'minibook-demo.pid'
$baseUrl = 'http://127.0.0.1:3456'
$serviceName = 'captain-demo-service'

function Read-Env {
    $values = [ordered]@{}
    if (Test-Path $envFile) { foreach ($line in [IO.File]::ReadAllLines($envFile)) {
        if ($line -match '^(CAPTAIN_DEMO_MINIBOOK_API_KEY|MINIBOOK_API_KEY|MINIBOOK_PROJECTION_API_KEY)=(.*)$') { $values[$Matches[1]] = $Matches[2] }
    }}
    $values
}
function Save-Env($values) {
    $pending = [ordered]@{
        CAPTAIN_DEMO_MINIBOOK_API_KEY = [string]$values['CAPTAIN_DEMO_MINIBOOK_API_KEY']
        MINIBOOK_API_KEY = [string]$values['MINIBOOK_API_KEY']
    }
    $lines = [Collections.Generic.List[string]]::new()
    if (Test-Path $envFile) { foreach ($line in [IO.File]::ReadAllLines($envFile)) {
        if ($line -match '^(CAPTAIN_DEMO_MINIBOOK_API_KEY|MINIBOOK_API_KEY)=') {
            $name = $Matches[1]
            $lines.Add(('{0}={1}' -f $name,$pending[$name])); $pending.Remove($name)
        } else { $lines.Add($line) }
    }}
    foreach ($item in $pending.GetEnumerator()) { $lines.Add(('{0}={1}' -f $item.Key,$item.Value)) }
    [IO.File]::WriteAllLines($envFile, $lines, [Text.UTF8Encoding]::new($false))
}
function Test-Health {
    try { (Invoke-WebRequest "$baseUrl/health" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200 } catch { $false }
}
function Get-ManagedServiceProcess {
    if (-not (Test-Path $pidFile -PathType Leaf)) { return $null }
    try { $identity = Get-Content $pidFile -Raw | ConvertFrom-Json } catch { throw 'Invalid managed Minibook PID file.' }
    $process = Get-Process -Id ([int]$identity.pid) -ErrorAction SilentlyContinue
    if (-not $process) { Remove-Item $pidFile -Force; return $null }
    $sameStart = $process.StartTime.ToUniversalTime().Ticks -eq ([DateTimeOffset]$identity.started_at).UtcDateTime.Ticks
    $sameExecutable = [IO.Path]::GetFullPath($process.Path) -eq [IO.Path]::GetFullPath([string]$identity.executable)
    if (-not $sameStart -or -not $sameExecutable) { throw 'PID no longer belongs to the managed Minibook process.' }
    return $process
}
function Start-Service {
    $values = Read-Env
    $projectionKey = [string]$values['MINIBOOK_PROJECTION_API_KEY']
    if ([string]::IsNullOrWhiteSpace($projectionKey)) { throw 'Projection credential is required before starting Minibook.' }
    if (Test-Health) {
        $managed = Get-ManagedServiceProcess
        if (-not $managed) { throw 'Healthy Minibook endpoint is not the managed demo process.' }
        Stop-Process -Id $managed.Id -ErrorAction Stop
        Remove-Item $pidFile -Force
        Write-Host '[ready] managed Minibook restarted for current configuration'
    }
    New-Item -ItemType Directory -Force $stateDir | Out-Null
    $python = Join-Path $root '.venv\Scripts\python.exe'; if (-not (Test-Path $python)) { $python = (Get-Command python).Source }
    $previousProjectionKey = [Environment]::GetEnvironmentVariable('MINIBOOK_PROJECTION_API_KEY','Process')
    $previousMinibookUrl = [Environment]::GetEnvironmentVariable('MINIBOOK_URL','Process')
    try {
        [Environment]::SetEnvironmentVariable('MINIBOOK_PROJECTION_API_KEY',$projectionKey,'Process')
        [Environment]::SetEnvironmentVariable('MINIBOOK_URL',$baseUrl,'Process')
        $process = Start-Process $python -ArgumentList 'run.py' -WorkingDirectory (Join-Path $root 'minibook') -WindowStyle Hidden -PassThru
    } finally {
        [Environment]::SetEnvironmentVariable('MINIBOOK_PROJECTION_API_KEY',$previousProjectionKey,'Process')
        [Environment]::SetEnvironmentVariable('MINIBOOK_URL',$previousMinibookUrl,'Process')
    }
    $identity = @{ pid=$process.Id; started_at=$process.StartTime.ToUniversalTime().ToString('o'); executable=$python }
    [IO.File]::WriteAllText($pidFile, ($identity | ConvertTo-Json -Compress))
    foreach ($attempt in 1..60) { if (Test-Health) { Write-Host '[ready] Minibook local instance'; return }; Start-Sleep -Milliseconds 500 }
    throw 'Minibook local instance did not become healthy.'
}
function Recover-ServiceCredential($values) {
    $database = Join-Path $root 'minibook\data\minibook.db'
    if (-not (Test-Path $database -PathType Leaf)) { throw 'Minibook demo identity exists but its local database is unavailable for explicit recovery.' }
    $python = Join-Path $root '.venv\Scripts\python.exe'; if (-not (Test-Path $python)) { $python = (Get-Command python).Source }
    $query = "import sqlite3, sys; con=sqlite3.connect(sys.argv[1]); rows=con.execute('SELECT api_key FROM agents WHERE name = ?', (sys.argv[2],)).fetchall(); sys.stdout.write(rows[0][0] if len(rows) == 1 else '')"
    $apiKey = (& $python -c $query $database $serviceName).Trim()
    if ([string]::IsNullOrWhiteSpace($apiKey)) { throw 'Minibook demo identity recovery was ambiguous or unavailable.' }
    try {
        $me = Invoke-RestMethod "$baseUrl/api/v1/agents/me" -Headers @{Authorization="Bearer $apiKey"} -TimeoutSec 5
        if ($me.name -ne $serviceName) { throw 'Recovered Minibook key belongs to another identity.' }
    } catch { throw 'Recovered Minibook key did not validate against the local service.' }
    $values['CAPTAIN_DEMO_MINIBOOK_API_KEY'] = $apiKey
    $values['MINIBOOK_API_KEY'] = $apiKey
    Save-Env $values
    Write-Host '[ready] Minibook demo service credential recovered locally (credential redacted)'
}
function Bootstrap-Service {
    Start-Service
    $values = Read-Env
    if ($values['CAPTAIN_DEMO_MINIBOOK_API_KEY'] -and $values['MINIBOOK_API_KEY'] -and $values['CAPTAIN_DEMO_MINIBOOK_API_KEY'] -ne $values['MINIBOOK_API_KEY']) {
        throw 'Configured Minibook API key aliases do not match; refusing to choose one.'
    }
    $apiKey = if ($values['MINIBOOK_API_KEY']) { [string]$values['MINIBOOK_API_KEY'] } else { [string]$values['CAPTAIN_DEMO_MINIBOOK_API_KEY'] }
    if ($apiKey) {
        try {
            $me = Invoke-RestMethod "$baseUrl/api/v1/agents/me" -Headers @{Authorization="Bearer $apiKey"} -TimeoutSec 5
            if ($me.name -ne $serviceName) { throw 'Configured Minibook key belongs to another identity.' }
            $values['CAPTAIN_DEMO_MINIBOOK_API_KEY'] = $apiKey
            $values['MINIBOOK_API_KEY'] = $apiKey
            Save-Env $values
            Write-Host '[ready] Minibook demo service account reused (credential redacted)'; return
        } catch { throw 'Configured CAPTAIN_DEMO_MINIBOOK_API_KEY is invalid; refusing to replace it.' }
    }
    try {
        $created = Invoke-RestMethod "$baseUrl/api/v1/agents" -Method Post -ContentType 'application/json' -Body (@{name=$serviceName}|ConvertTo-Json) -TimeoutSec 5
    } catch {
        if ($RecoverDemoCredentials) { Recover-ServiceCredential $values; return }
        throw 'Minibook demo identity exists or registration failed; use -RecoverDemoCredentials to restore its local key.'
    }
    if (-not $created.api_key) { throw 'Minibook registration returned no API key.' }
    $values['CAPTAIN_DEMO_MINIBOOK_API_KEY'] = [string]$created.api_key
    $values['MINIBOOK_API_KEY'] = [string]$created.api_key
    Save-Env $values
    Write-Host '[ready] Minibook demo service account created locally (credential redacted)'
}
function Stop-Service {
    $process = Get-ManagedServiceProcess
    if (-not $process) { Write-Host '[ready] no managed Minibook process'; return }
    Stop-Process -Id $process.Id -ErrorAction Stop
    Remove-Item $pidFile -Force
    Write-Host '[ready] managed Minibook process stopped'
}
switch ($Action) {
    start { Start-Service }
    bootstrap { Bootstrap-Service }
    status { if (-not (Test-Health)) { throw 'Minibook is not healthy.' }; Write-Host '[ready] Minibook healthy' }
    stop { Stop-Service }
}
