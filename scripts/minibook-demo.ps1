#requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position=0)]
    [ValidateSet("start", "bootstrap", "status", "stop")]
    [string]$Action,
    [switch]$RecoverDemoCredentials,
    [scriptblock]$HealthProbe,
    [scriptblock]$IdentityProbe,
    [scriptblock]$AgentRegistrationProbe,
    [scriptblock]$ServiceStartProbe
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root '.env'
$stateDir = Join-Path $root '.captain-cook'
$pidFile = Join-Path $stateDir 'minibook-demo.pid'
$serviceName = 'captain-demo-service'

function Get-MinibookConfiguredPort {
    param([string]$ConfigPath)
    $recovery = 'Minibook endpoint is not configured. Set MINIBOOK_BACKEND_URL or configure an integer port in minibook/config.yaml.'
    if (-not (Test-Path $ConfigPath -PathType Leaf)) { throw $recovery }
    foreach ($line in [IO.File]::ReadAllLines($ConfigPath)) {
        if ($line -match '^\s*port\s*:\s*([0-9]+)\s*(?:#.*)?$') {
            $port = [int]0
            if ([int]::TryParse($Matches[1], [ref]$port) -and $port -ge 1 -and $port -le 65535) {
                return $port
            }
            throw $recovery
        }
    }
    throw $recovery
}
function Resolve-MinibookBaseUrl {
    param([string]$ConfiguredUrl, [string]$ConfigPath)
    if (-not [string]::IsNullOrWhiteSpace($ConfiguredUrl)) {
        try {
            $candidate = [uri]$ConfiguredUrl
        } catch {
            throw 'Minibook demo endpoint must be a local HTTP URL.'
        }
    } else {
        $port = Get-MinibookConfiguredPort -ConfigPath $ConfigPath
        $candidate = [uri]"http://127.0.0.1:$port"
    }
    if (-not $candidate.IsAbsoluteUri -or $candidate.Scheme -ne 'http' -or -not $candidate.IsLoopback) {
        throw 'Minibook demo endpoint must be a local HTTP URL.'
    }
    return $candidate.AbsoluteUri.TrimEnd('/')
}

if ($Action -ne 'stop') {
    $baseUrl = Resolve-MinibookBaseUrl -ConfiguredUrl $env:MINIBOOK_BACKEND_URL -ConfigPath (Join-Path $root 'minibook\config.yaml')
}
if ($null -eq $HealthProbe) {
    $HealthProbe = {
        param([string]$HealthUrl)
        (Invoke-WebRequest $HealthUrl -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200
    }
}
if ($null -eq $IdentityProbe) {
    $IdentityProbe = {
        param([string]$IdentityUrl, [string]$ApiKey)
        Invoke-RestMethod $IdentityUrl -Headers @{Authorization="Bearer $ApiKey"} -TimeoutSec 5
    }
}
if ($null -eq $AgentRegistrationProbe) {
    $AgentRegistrationProbe = {
        param([string]$RegistrationUrl, [string]$Name)
        Invoke-RestMethod $RegistrationUrl -Method Post -ContentType 'application/json' -Body (@{name=$Name}|ConvertTo-Json) -TimeoutSec 5
    }
}
if ($null -eq $ServiceStartProbe) {
    $ServiceStartProbe = {
        param([string]$PythonPath, [string]$WorkingDirectory, [string]$ProjectionApiKey)
        Start-Process $PythonPath -ArgumentList 'run.py' -WorkingDirectory $WorkingDirectory -WindowStyle Hidden -PassThru
    }
}

function Read-Env {
    $values = [ordered]@{}
    if (Test-Path $envFile) { foreach ($line in [IO.File]::ReadAllLines($envFile)) {
        if ($line -match '^CAPTAIN_DEMO_MINIBOOK_API_KEY=(.*)$') { $values['CAPTAIN_DEMO_MINIBOOK_API_KEY'] = $Matches[1] }
        if ($line -match '^CAPTAIN_DEMO_MINIBOOK_PROJECTION_API_KEY=(.*)$') { $values['CAPTAIN_DEMO_MINIBOOK_PROJECTION_API_KEY'] = $Matches[1] }
    }}
    $values
}
function Save-Env($values) {
    $lines = [Collections.Generic.List[string]]::new()
    $written = @{
        CAPTAIN_DEMO_MINIBOOK_API_KEY = $false
        CAPTAIN_DEMO_MINIBOOK_PROJECTION_API_KEY = $false
    }
    if (Test-Path $envFile) { foreach ($line in [IO.File]::ReadAllLines($envFile)) {
        if ($line -match '^CAPTAIN_DEMO_MINIBOOK_API_KEY=') {
            $lines.Add("CAPTAIN_DEMO_MINIBOOK_API_KEY=$($values['CAPTAIN_DEMO_MINIBOOK_API_KEY'])")
            $written['CAPTAIN_DEMO_MINIBOOK_API_KEY'] = $true
        } elseif ($line -match '^CAPTAIN_DEMO_MINIBOOK_PROJECTION_API_KEY=') {
            $lines.Add("CAPTAIN_DEMO_MINIBOOK_PROJECTION_API_KEY=$($values['CAPTAIN_DEMO_MINIBOOK_PROJECTION_API_KEY'])")
            $written['CAPTAIN_DEMO_MINIBOOK_PROJECTION_API_KEY'] = $true
        } else { $lines.Add($line) }
    }}
    foreach ($name in $written.Keys) {
        if (-not $written[$name] -and $values.Contains($name)) {
            $lines.Add("$name=$($values[$name])")
        }
    }
    [IO.File]::WriteAllLines($envFile, $lines, [Text.UTF8Encoding]::new($false))
}
function Test-Health {
    try { [bool](& $HealthProbe "$baseUrl/health") } catch { $false }
}
function Test-Identity($values) {
    $apiKey = [string]$values['CAPTAIN_DEMO_MINIBOOK_API_KEY']
    if ([string]::IsNullOrWhiteSpace($apiKey)) {
        throw 'Minibook demo credential is unavailable; use the explicit recovery workflow.'
    }
    try {
        $me = & $IdentityProbe "$baseUrl/api/v1/agents/me" $apiKey
        if ($me.name -ne $serviceName) { throw 'Configured Minibook key belongs to another identity.' }
    } catch {
        throw 'Configured CAPTAIN_DEMO_MINIBOOK_API_KEY is invalid; refusing to replace it.'
    }
}
function Start-Service($values) {
    if (Test-Health) { Write-Host '[ready] Minibook local instance'; return }
    $projectionApiKey = [string]$values['CAPTAIN_DEMO_MINIBOOK_PROJECTION_API_KEY']
    if ([string]::IsNullOrWhiteSpace($projectionApiKey)) {
        throw 'Minibook projection credential is unavailable; run bootstrap first.'
    }
    $env:MINIBOOK_PROJECTION_API_KEY = $projectionApiKey
    New-Item -ItemType Directory -Force $stateDir | Out-Null
    $python = Join-Path $root '.venv\Scripts\python.exe'; if (-not (Test-Path $python)) { $python = (Get-Command python.exe -CommandType Application).Source }
    $process = & $ServiceStartProbe $python (Join-Path $root 'minibook') $projectionApiKey
    $identity = @{ pid=$process.Id; started_at=$process.StartTime.ToUniversalTime().ToString('o'); executable=$process.Path }
    [IO.File]::WriteAllText($pidFile, ($identity | ConvertTo-Json -Compress))
    foreach ($attempt in 1..60) { if (Test-Health) { Write-Host '[ready] Minibook local instance'; return }; Start-Sleep -Milliseconds 500 }
    throw 'Minibook local instance did not become healthy.'
}
function Recover-ServiceCredential($values) {
    $database = Join-Path $root 'minibook\data\minibook.db'
    if (-not (Test-Path $database -PathType Leaf)) { throw 'Minibook demo identity exists but its local database is unavailable for explicit recovery.' }
    $python = Join-Path $root '.venv\Scripts\python.exe'; if (-not (Test-Path $python)) { $python = (Get-Command python.exe -CommandType Application).Source }
    $query = "import sqlite3, sys; con=sqlite3.connect(sys.argv[1]); rows=con.execute('SELECT api_key FROM agents WHERE name = ?', (sys.argv[2],)).fetchall(); sys.stdout.write(rows[0][0] if len(rows) == 1 else '')"
    $apiKey = (& $python -c $query $database $serviceName).Trim()
    if ([string]::IsNullOrWhiteSpace($apiKey)) { throw 'Minibook demo identity recovery was ambiguous or unavailable.' }
    try {
        $me = & $IdentityProbe "$baseUrl/api/v1/agents/me" $apiKey
        if ($me.name -ne $serviceName) { throw 'Recovered Minibook key belongs to another identity.' }
    } catch { throw 'Recovered Minibook key did not validate against the local service.' }
    $values['CAPTAIN_DEMO_MINIBOOK_API_KEY'] = $apiKey
    Save-Env $values
    Write-Host '[ready] Minibook demo service credential recovered locally (credential redacted)'
}
function Initialize-ProjectionCredential($values, [switch]$Persist) {
    $projectionApiKey = [string]$values['CAPTAIN_DEMO_MINIBOOK_PROJECTION_API_KEY']
    if ([string]::IsNullOrWhiteSpace($projectionApiKey)) {
        $projectionApiKey = [Convert]::ToHexString(
            [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
        ).ToLowerInvariant()
        $values['CAPTAIN_DEMO_MINIBOOK_PROJECTION_API_KEY'] = $projectionApiKey
        if ($Persist) { Save-Env $values }
        return $true
    }
    return $false
}
function Bootstrap-Service {
    $values = Read-Env
    $apiKey = [string]$values['CAPTAIN_DEMO_MINIBOOK_API_KEY']
    if ($apiKey) {
        $projectionKeyWasMissing = Initialize-ProjectionCredential $values
        Start-Service $values
        try {
            $me = & $IdentityProbe "$baseUrl/api/v1/agents/me" $apiKey
            if ($me.name -ne $serviceName) { throw 'Configured Minibook key belongs to another identity.' }
        } catch { throw 'Configured CAPTAIN_DEMO_MINIBOOK_API_KEY is invalid; refusing to replace it.' }
        if ($projectionKeyWasMissing) { Save-Env $values }
        Write-Host '[ready] Minibook demo service account reused (credential redacted)'; return
    }
    $null = Initialize-ProjectionCredential $values -Persist
    Start-Service $values
    try {
        $created = & $AgentRegistrationProbe "$baseUrl/api/v1/agents" $serviceName
    } catch {
        if ($RecoverDemoCredentials) { Recover-ServiceCredential $values; return }
        throw 'Minibook demo identity exists or registration failed; use -RecoverDemoCredentials to restore its local key.'
    }
    if (-not $created.api_key) { throw 'Minibook registration returned no API key.' }
    $values['CAPTAIN_DEMO_MINIBOOK_API_KEY'] = [string]$created.api_key
    Save-Env $values
    Write-Host '[ready] Minibook demo service account created locally (credential redacted)'
}
function Stop-Service {
    if (-not (Test-Path $pidFile)) { Write-Host '[ready] no managed Minibook process'; return }
    try { $identity = Get-Content $pidFile -Raw | ConvertFrom-Json } catch { throw 'Invalid managed Minibook PID file.' }
    $process = Get-Process -Id ([int]$identity.pid) -ErrorAction SilentlyContinue
    if ($process) {
        $sameStart = $process.StartTime.ToUniversalTime().Ticks -eq ([DateTimeOffset]$identity.started_at).UtcDateTime.Ticks
        $sameExecutable = [IO.Path]::GetFullPath($process.Path) -eq [IO.Path]::GetFullPath([string]$identity.executable)
        if (-not $sameStart -or -not $sameExecutable) { throw 'PID no longer belongs to the managed Minibook process.' }
        Stop-Process -Id $process.Id -ErrorAction Stop
    }
    Remove-Item $pidFile -Force
    Write-Host '[ready] managed Minibook process stopped'
}
switch ($Action) {
    start { Start-Service (Read-Env) }
    bootstrap { Bootstrap-Service }
    status {
        if (-not (Test-Health)) { throw 'Minibook is not healthy.' }
        Test-Identity (Read-Env)
        Write-Host '[ready] Minibook healthy and identity validated'
    }
    stop { Stop-Service }
}
