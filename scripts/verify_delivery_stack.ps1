[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$EnvPath = Join-Path $RepoRoot ".env"

function Read-DotEnv {
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Missing local environment file: $Path"
    }

    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*#' -or $line -notmatch '=') {
            continue
        }
        $key, $value = $line -split '=', 2
        $values[$key.Trim()] = $value.Trim()
    }
    return $values
}

$config = Read-DotEnv -Path $EnvPath
$n8nUrl = if ($config.N8N_URL) { $config.N8N_URL } else { "http://localhost:15678" }
$mailpitUrl = if ($config.MAILPIT_URL) { $config.MAILPIT_URL } else { "http://localhost:8025" }
$smtpPort = if ($config.MAILPIT_SMTP_PORT) { [int]$config.MAILPIT_SMTP_PORT } else { 1025 }

Write-Host "Checking external n8n..."
$n8n = Invoke-WebRequest -Uri "$($n8nUrl.TrimEnd('/'))/healthz" -UseBasicParsing -TimeoutSec 5
if ($n8n.StatusCode -ne 200) {
    throw "n8n health check failed."
}

Write-Host "Checking Mailpit API and SMTP..."
$mailpit = Invoke-WebRequest -Uri "$($mailpitUrl.TrimEnd('/'))/api/v1/info" -UseBasicParsing -TimeoutSec 5
if ($mailpit.StatusCode -ne 200) {
    throw "Mailpit API check failed."
}
$smtp = Test-NetConnection -ComputerName localhost -Port $smtpPort -WarningAction SilentlyContinue
if (-not $smtp.TcpTestSucceeded) {
    throw "Mailpit SMTP check failed on port $smtpPort."
}

foreach ($required in ("MARIADB_DATABASE", "MARIADB_USER", "MARIADB_PASSWORD")) {
    if (-not $config[$required]) {
        throw "Missing required value $required in .env."
    }
}

Write-Host "Checking authenticated MariaDB query..."
Push-Location $RepoRoot
try {
    docker compose exec -T -e "MYSQL_PWD=$($config.MARIADB_PASSWORD)" mariadb mariadb `
        --user=$($config.MARIADB_USER) --database=$($config.MARIADB_DATABASE) `
        --batch --skip-column-names --execute="SELECT 1" *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "MariaDB query failed."
    }

    Write-Host "Checking container-to-host n8n access via host.docker.internal:15678..."
    cmd /c "docker run --rm --network captain-cook_default curlimages/curl:8.16.0 --fail --silent http://host.docker.internal:15678/healthz >nul 2>&1"
    if ($LASTEXITCODE -ne 0) {
        throw "Container-to-host n8n check failed."
    }
}
finally {
    Pop-Location
}

Write-Host "Delivery stack verification passed."
