#requires -Version 7.0
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '../..')
Import-Module (Join-Path $root 'scripts/setup/Configuration.psm1') -Force
Import-Module (Join-Path $root 'scripts/setup/Services.psm1') -Force

$failures = [Collections.Generic.List[string]]::new()
function Test-AcceptanceItem {
    param([string] $Name, [scriptblock] $Check)
    try {
        if (& $Check) { Write-Host "PASS $Name" -ForegroundColor Green; return }
        throw 'Prüfung lieferte false'
    }
    catch {
        $failures.Add("$Name`: $($_.Exception.Message)")
        Write-Host "FAIL $Name" -ForegroundColor Red
    }
}

$config = Read-DotEnv -Path (Join-Path $root '.env')
$hermesHome = if ($env:HERMES_HOME) { $env:HERMES_HOME } elseif ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'hermes' } else { Join-Path $HOME 'AppData/Local/hermes' }
$hermesConfig = Read-DotEnv -Path (Join-Path $hermesHome '.env')

Test-AcceptanceItem 'Captain Offline-Demo' {
    $python = Join-Path $root '.venv/Scripts/python.exe'
    & $python (Join-Path $root 'main.py') demo --output (Join-Path $root 'artifacts/demo-run.json') *> $null
    $LASTEXITCODE -eq 0 -and (Test-Path (Join-Path $root 'artifacts/demo-run.json'))
}
Test-AcceptanceItem 'Hermes CLI' {
    & (Join-Path $root '.captain-cook/hermes/Scripts/hermes.exe') --help *> $null
    $LASTEXITCODE -eq 0
}
Test-AcceptanceItem 'Hermes Minibook Skill' { Test-Path (Join-Path $hermesHome 'skills/minibook/SKILL.md') }
Test-AcceptanceItem 'Minibook Health' { (Invoke-WebRequest "$($config.MINIBOOK_BACKEND_URL)/health" -TimeoutSec 10).StatusCode -eq 200 }
Test-AcceptanceItem 'Minibook Version' { (Invoke-RestMethod "$($config.MINIBOOK_PUBLIC_URL)/api/v1/version" -TimeoutSec 10).version }
Test-AcceptanceItem 'Hermes Minibook Identity' {
    $headers = @{ Authorization = "Bearer $($hermesConfig.MINIBOOK_API_KEY)" }
    -not [string]::IsNullOrWhiteSpace([string](Invoke-RestMethod "$($config.MINIBOOK_PUBLIC_URL)/api/v1/agents/me" -Headers $headers -TimeoutSec 10).name)
}
Test-AcceptanceItem 'Mailpit API' { (Invoke-WebRequest "http://localhost:$($config.MAILPIT_WEB_PORT)/api/v1/info" -TimeoutSec 10).StatusCode -eq 200 }
Test-AcceptanceItem 'Mailpit SMTP' { (Test-TcpService -Name SMTP -ComputerName localhost -Port ([int]$config.MAILPIT_SMTP_PORT)).Status -eq 'Ready' }
Test-AcceptanceItem 'MariaDB Query' { (Test-MariaDbService -Root $root -User $config.MARIADB_USER -Password $config.MARIADB_PASSWORD).Status -eq 'Ready' }
Test-AcceptanceItem 'n8n Host Health' { (Invoke-WebRequest "$([string]$config.N8N_URL.TrimEnd('/'))/healthz" -TimeoutSec 30).StatusCode -eq 200 }
if ($config.N8N_MODE -eq 'owned') {
    Test-AcceptanceItem 'n8n Container Health' {
        docker compose --project-directory $root --env-file (Join-Path $root '.env') --profile owned-n8n exec -T n8n wget -q --spider http://127.0.0.1:5678/healthz
        $LASTEXITCODE -eq 0
    }
}
Test-AcceptanceItem 'Detailed Status' { & (Join-Path $root 'status.ps1') -Detailed *> $null; $LASTEXITCODE -eq 0 }
Test-AcceptanceItem 'Stop and Start' {
    & (Join-Path $root 'stop.ps1') *> $null
    if ($LASTEXITCODE -ne 0) { return $false }
    & (Join-Path $root 'start.ps1') *> $null
    $LASTEXITCODE -eq 0
}
Test-AcceptanceItem 'Idempotent Setup' { & (Join-Path $root 'setup.ps1') -CheckOnly *> $null; $LASTEXITCODE -eq 0 }
Test-AcceptanceItem 'Repair Command' { & (Join-Path $root 'repair.ps1') *> $null; $LASTEXITCODE -eq 0 }
Test-AcceptanceItem 'No Generated Git Files' { -not (git -C $root status --short | Where-Object { $_ -match '\.env|\.captain-cook' }) }
Test-AcceptanceItem 'No Secrets In Logs' {
    $logs = Get-ChildItem (Join-Path $root '.captain-cook') -Filter '*.log' -Recurse -ErrorAction SilentlyContinue
    $content = $logs | Get-Content -Raw -ErrorAction SilentlyContinue
    foreach ($secret in @($config.MARIADB_PASSWORD, $config.MARIADB_ROOT_PASSWORD, $hermesConfig.MINIBOOK_API_KEY)) {
        if ($secret -and $content -match [regex]::Escape($secret)) { return $false }
    }
    $true
}

if ($failures.Count) {
    Write-Host "`n$($failures.Count) Acceptance-Prüfung(en) fehlgeschlagen:" -ForegroundColor Red
    $failures | ForEach-Object { Write-Host "- $_" }
    exit 1
}
Write-Host "`nAlle Setup-Acceptance-Prüfungen sind bestanden." -ForegroundColor Green
