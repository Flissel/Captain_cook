#requires -Version 7.0
[CmdletBinding()]
param(
    [string]$ProjectId = "",
    [string]$RunId = ""
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

foreach ($name in @(
    'TEST_MARIADB_DSN',
    'OPENAI_API_KEY',
    'CAPTAIN_N8N_API_KEY',
    'CAPTAIN_N8N_PORT'
)) {
    if (-not (Get-Item -Path ("Env:" + $name) -ErrorAction SilentlyContinue).Value) {
        throw "$name is required for the real Gate E release run."
    }
}
if (-not $env:CAPTAIN_N8N_URL) {
    $env:CAPTAIN_N8N_URL = "http://127.0.0.1:$($env:CAPTAIN_N8N_PORT)"
}
if (-not $ProjectId) { $ProjectId = "release-$([guid]::NewGuid().ToString('N').Substring(0, 16))" }
if (-not $RunId) { $RunId = "candidate-$([guid]::NewGuid().ToString('N').Substring(0, 20))" }

for ($index = 1; $index -le 3; $index++) {
    $env:CAPTAIN_GATE_A_PROJECT_ID = $ProjectId
    $env:CAPTAIN_GATE_A_RUN_ID = $RunId
    $env:CAPTAIN_GATE_A_RUN_INDEX = [string]$index
    & $python -m pytest -q --no-cov -m live tests/live/test_gate_a_codex_n8n.py -rs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$env:CAPTAIN_RELEASE_PROJECT_ID = $ProjectId
$env:CAPTAIN_RELEASE_RUN_ID = $RunId
& $python -m pytest -q --no-cov -m live tests/live/test_gate_e_release_decision.py -rs
exit $LASTEXITCODE
