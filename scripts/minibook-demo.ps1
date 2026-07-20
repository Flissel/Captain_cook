#requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position=0)]
    [ValidateSet("start", "bootstrap", "status", "stop")]
    [string]$Action
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
        if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { $values[$Matches[1]] = $Matches[2] }
    }}
    $values
}
function Save-Env($values) {
    $lines = foreach ($item in $values.GetEnumerator()) { '{0}={1}' -f $item.Key,$item.Value }
    [IO.File]::WriteAllLines($envFile, $lines, [Text.UTF8Encoding]::new($false))
}
function Test-Health {
    try { (Invoke-WebRequest "$baseUrl/health" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200 } catch { $false }
}
function Start-Service {
    if (Test-Health) { Write-Host '[ready] Minibook local instance'; return }
    New-Item -ItemType Directory -Force $stateDir | Out-Null
    $python = Join-Path $root '.venv\Scripts\python.exe'; if (-not (Test-Path $python)) { $python = (Get-Command python).Source }
    $process = Start-Process $python -ArgumentList 'run.py' -WorkingDirectory (Join-Path $root 'minibook') -WindowStyle Hidden -PassThru
    $identity = @{ pid=$process.Id; started_at=$process.StartTime.ToUniversalTime().ToString('o'); executable=$process.Path }
    [IO.File]::WriteAllText($pidFile, ($identity | ConvertTo-Json -Compress))
    foreach ($attempt in 1..60) { if (Test-Health) { Write-Host '[ready] Minibook local instance'; return }; Start-Sleep -Milliseconds 500 }
    throw 'Minibook local instance did not become healthy.'
}
function Bootstrap-Service {
    Start-Service
    $values = Read-Env
    $apiKey = [string]$values['CAPTAIN_DEMO_MINIBOOK_API_KEY']
    if ($apiKey) {
        try {
            $me = Invoke-RestMethod "$baseUrl/api/v1/agents/me" -Headers @{Authorization="Bearer $apiKey"} -TimeoutSec 5
            if ($me.name -ne $serviceName) { throw 'Configured Minibook key belongs to another identity.' }
            Write-Host '[ready] Minibook demo service account reused (credential redacted)'; return
        } catch { throw 'Configured CAPTAIN_DEMO_MINIBOOK_API_KEY is invalid; refusing to replace it.' }
    }
    try {
        $created = Invoke-RestMethod "$baseUrl/api/v1/agents" -Method Post -ContentType 'application/json' -Body (@{name=$serviceName}|ConvertTo-Json) -TimeoutSec 5
    } catch { throw 'Minibook demo identity exists or registration failed; restore its local key instead of rotating implicitly.' }
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
    start { Start-Service }
    bootstrap { Bootstrap-Service }
    status { if (-not (Test-Health)) { throw 'Minibook is not healthy.' }; Write-Host '[ready] Minibook healthy' }
    stop { Stop-Service }
}
