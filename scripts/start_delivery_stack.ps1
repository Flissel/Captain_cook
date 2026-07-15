[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VibeMindCompose = Join-Path $env:USERPROFILE "Desktop\Vibemind_V1\vibemind-os\voice\docker-compose.n8n.yml"
$VerifyScript = Join-Path $PSScriptRoot "verify_delivery_stack.ps1"

docker info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Desktop is not available."
}

if (-not (Test-Path -LiteralPath $VibeMindCompose)) {
    throw "VibeMind n8n Compose file not found: $VibeMindCompose"
}

Write-Host "Starting the existing VibeMind n8n instance..."
docker compose -f $VibeMindCompose up -d --build
if ($LASTEXITCODE -ne 0) {
    throw "VibeMind n8n failed to start."
}

$n8nHealthy = $false
foreach ($attempt in 1..30) {
    try {
        $response = Invoke-WebRequest -Uri "http://localhost:15678/healthz" -UseBasicParsing -TimeoutSec 3
        if ($response.StatusCode -eq 200) {
            $n8nHealthy = $true
            break
        }
    }
    catch {
        Start-Sleep -Seconds 2
    }
}
if (-not $n8nHealthy) {
    throw "VibeMind n8n did not become healthy on http://localhost:15678."
}

Write-Host "Starting Captain Cook Mailpit and MariaDB..."
Push-Location $RepoRoot
try {
    docker compose --env-file .env up -d --wait
    if ($LASTEXITCODE -ne 0) {
        throw "Captain Cook delivery services failed to start."
    }

    & $VerifyScript
    if ($LASTEXITCODE -ne 0) {
        throw "Delivery stack verification failed."
    }
}
finally {
    Pop-Location
}
