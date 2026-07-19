#requires -Version 7.0
[CmdletBinding()]
param(
    [int]$Port = 3456,
    [switch]$RequireProjectionCapability
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$minibook = Join-Path $root 'minibook'
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

function Import-DotEnv([string]$Path) {
    if (-not (Test-Path $Path)) { return }
    foreach ($rawLine in Get-Content -LiteralPath $Path) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith('#')) { continue }
        $separator = $line.IndexOf('=')
        if ($separator -lt 1) { continue }
        $name = $line.Substring(0, $separator)
        $value = $line.Substring($separator + 1)
        if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { continue }
        $normalized = $value.Trim().Trim('"').Trim("'")
        Set-Item -Path ("Env:" + $name.Trim()) -Value $normalized
    }
}

Import-DotEnv (Join-Path $root '.env')
if (-not $env:MINIBOOK_PROJECTION_API_KEY) {
    $userProjectionKey = [Environment]::GetEnvironmentVariable(
        'MINIBOOK_PROJECTION_API_KEY',
        'User'
    )
    if ($userProjectionKey) { $env:MINIBOOK_PROJECTION_API_KEY = $userProjectionKey }
}
if ($RequireProjectionCapability -and -not $env:MINIBOOK_PROJECTION_API_KEY) {
    throw 'MINIBOOK_PROJECTION_API_KEY is required for the Captain release projection.'
}

if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    Write-Output "Minibook already listens on port $Port."
} else {
    $config = Join-Path $minibook 'config.yaml'
    if (-not (Test-Path $config)) {
        @('public_url: "http://localhost:3457"', "port: $Port", 'database: "data/minibook.db"') |
            Set-Content -LiteralPath $config -Encoding utf8
    }
    & $python -m pip install -r (Join-Path $minibook 'requirements.txt') | Out-Host
    Start-Process -FilePath $python -ArgumentList 'run.py' -WorkingDirectory $minibook -WindowStyle Hidden
}

$deadline = (Get-Date).AddSeconds(30)
do {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://127.0.0.1:$Port/health"
        if ($response.StatusCode -eq 200) { Write-Output "Minibook is healthy at http://127.0.0.1:$Port/health"; exit 0 }
    } catch {}
    Start-Sleep -Milliseconds 500
} while ((Get-Date) -lt $deadline)
throw "Minibook did not become healthy on port $Port."
