[CmdletBinding()]
param(
    [string]$ProjectId = "",
    [string]$RunId = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function New-RandomCredential {
    $bytes = [byte[]]::new(32)
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    } finally {
        $generator.Dispose()
    }
    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function Get-FreeLoopbackPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    try {
        $listener.Start()
        return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    } finally {
        $listener.Stop()
    }
}

function Set-ProcessEnvironmentValue {
    param([string]$Name, [AllowNull()][object]$Value)
    if ($null -eq $Value) {
        [System.Environment]::SetEnvironmentVariable($Name, $null, "Process")
        return
    }
    [System.Environment]::SetEnvironmentVariable($Name, [string]$Value, "Process")
}

foreach ($name in @("OPENAI_API_KEY", "CAPTAIN_N8N_API_KEY", "CAPTAIN_N8N_PORT")) {
    if (-not (Get-Item -Path ("Env:" + $name) -ErrorAction SilentlyContinue).Value) {
        throw "$name is required for the real Gate E release run."
    }
}

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$composeFile = Join-Path $repoRoot "docker-compose.test.yml"
$docker = (Get-Command docker -ErrorAction Stop).Source
$projectName = "captain-gate-e-$([guid]::NewGuid().ToString('N').Substring(0, 12))"
$environmentNames = @("MARIADB_TEST_PASSWORD", "MARIADB_TEST_ROOT_PASSWORD", "MARIADB_TEST_PORT", "TEST_MARIADB_DSN", "REQUIRE_MARIADB_TESTS", "COMPOSE_DISABLE_ENV_FILE")
$previousEnvironment = @{}
foreach ($name in $environmentNames) {
    $previousEnvironment[$name] = [System.Environment]::GetEnvironmentVariable($name, "Process")
}

$started = $false
$primaryError = $null
try {
    $password = New-RandomCredential
    $rootPassword = New-RandomCredential
    $port = Get-FreeLoopbackPort
    Set-ProcessEnvironmentValue -Name "MARIADB_TEST_PASSWORD" -Value $password
    Set-ProcessEnvironmentValue -Name "MARIADB_TEST_ROOT_PASSWORD" -Value $rootPassword
    Set-ProcessEnvironmentValue -Name "MARIADB_TEST_PORT" -Value $port
    Set-ProcessEnvironmentValue -Name "COMPOSE_DISABLE_ENV_FILE" -Value "1"

    & $docker compose --project-name $projectName --file $composeFile up -d --wait
    if ($LASTEXITCODE -ne 0) { throw "Isolated Gate E MariaDB startup failed with exit code $LASTEXITCODE" }
    $started = $true

    $encodedPassword = [System.Uri]::EscapeDataString($password)
    Set-ProcessEnvironmentValue -Name "TEST_MARIADB_DSN" -Value "mariadb://captain_test:${encodedPassword}@127.0.0.1:${port}/captain_test"
    Set-ProcessEnvironmentValue -Name "REQUIRE_MARIADB_TESTS" -Value "1"

    & (Join-Path $PSScriptRoot "run-gate-e.ps1") -ProjectId $ProjectId -RunId $RunId
    if ($LASTEXITCODE -ne 0) { throw "Gate E release run failed with exit code $LASTEXITCODE" }
} catch {
    $primaryError = $_
} finally {
    if ($started) {
        & $docker compose --project-name $projectName --file $composeFile down --remove-orphans
        if ($LASTEXITCODE -ne 0 -and $null -eq $primaryError) {
            $primaryError = [System.InvalidOperationException]::new("Isolated Gate E MariaDB cleanup failed with exit code $LASTEXITCODE")
        }
    }
    foreach ($name in $environmentNames) {
        Set-ProcessEnvironmentValue -Name $name -Value $previousEnvironment[$name]
    }
}

if ($null -ne $primaryError) { throw $primaryError }
