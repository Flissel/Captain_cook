[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function New-RandomCredential {
    $bytes = [System.Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function Set-ProcessEnvironmentValue {
    param(
        [Parameter(Mandatory)]
        [string]$Name,
        [AllowNull()]
        [object]$Value
    )

    if ($null -eq $Value) {
        $nullString = [System.Management.Automation.Language.NullString]::Value
        [System.Environment]::SetEnvironmentVariable($Name, $nullString, "Process")
        return
    }
    [System.Environment]::SetEnvironmentVariable($Name, [string]$Value, "Process")
}

function Invoke-Pytest {
    param(
        [Parameter(Mandatory)]
        [string]$Python,
        [Parameter(Mandatory)]
        [string[]]$Arguments,
        [Parameter(Mandatory)]
        [string]$Label
    )

    $outputLines = @(
        & $Python @Arguments 2>&1 | ForEach-Object {
            $line = $_.ToString()
            Write-Host $line
            $line
        }
    )
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "$Label failed with exit code $exitCode"
    }
    return ,$outputLines
}

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$composeFile = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "docker-compose.test.yml"))
$dockerCommand = (Get-Command docker -ErrorAction Stop).Source
$localPython = if ($IsWindows) {
    Join-Path $repoRoot ".venv/Scripts/python.exe"
} else {
    Join-Path $repoRoot ".venv/bin/python"
}
$pythonCommand = if (Test-Path -LiteralPath $localPython) {
    [System.IO.Path]::GetFullPath($localPython)
} else {
    (Get-Command python -ErrorAction Stop).Source
}

$environmentNames = @(
    "MARIADB_TEST_PASSWORD",
    "MARIADB_TEST_ROOT_PASSWORD",
    "MARIADB_TEST_PORT",
    "TEST_MARIADB_DSN",
    "REQUIRE_MARIADB_TESTS",
    "COMPOSE_DISABLE_ENV_FILE"
)
$previousEnvironment = @{}
foreach ($name in $environmentNames) {
    $previousEnvironment[$name] = [System.Environment]::GetEnvironmentVariable($name, "Process")
}

$primaryError = $null
$cleanupErrors = [System.Collections.Generic.List[string]]::new()
$locationPushed = $false

try {
    $testPassword = New-RandomCredential
    $rootPassword = New-RandomCredential
    Set-ProcessEnvironmentValue -Name "MARIADB_TEST_PASSWORD" -Value $testPassword
    Set-ProcessEnvironmentValue -Name "MARIADB_TEST_ROOT_PASSWORD" -Value $rootPassword
    Set-ProcessEnvironmentValue -Name "MARIADB_TEST_PORT" -Value "33306"
    Set-ProcessEnvironmentValue -Name "COMPOSE_DISABLE_ENV_FILE" -Value "1"

    Push-Location -LiteralPath $repoRoot
    $locationPushed = $true

    & $dockerCommand compose --project-name captain-cook-test --file $composeFile up -d --wait
    $upExitCode = $LASTEXITCODE
    if ($upExitCode -ne 0) {
        throw "Isolated MariaDB startup failed with exit code $upExitCode"
    }

    $encodedUser = [System.Uri]::EscapeDataString("captain_test")
    $encodedPassword = [System.Uri]::EscapeDataString($testPassword)
    $encodedDatabase = [System.Uri]::EscapeDataString("captain_test")
    $testDsn = "mariadb://${encodedUser}:${encodedPassword}@127.0.0.1:33306/${encodedDatabase}"
    Set-ProcessEnvironmentValue -Name "TEST_MARIADB_DSN" -Value $testDsn
    Set-ProcessEnvironmentValue -Name "REQUIRE_MARIADB_TESTS" -Value "1"

    $selectedArguments = @(
        "-m", "pytest", "-q", "--no-cov",
        "tests/blockchain/test_mariadb_storage.py",
        "tests/gateway/test_gateway.py",
        "-rs"
    )
    $selectedOutput = Invoke-Pytest -Python $pythonCommand -Arguments $selectedArguments -Label "Selected MariaDB/gateway tests"
    $selectedText = $selectedOutput -join "`n"
    if ($selectedText -match "(?m)^SKIPPED \[" -or $selectedText -match "\b\d+ skipped\b") {
        throw "Selected MariaDB/gateway tests reported a skip"
    }
    if ($selectedText -notmatch "(?m)^22 passed(?:,| in )") {
        throw "Selected MariaDB/gateway tests did not report exactly 22 passed"
    }

    $fullArguments = @("-m", "pytest", "-q", "-rs", "-m", "not live")
    $fullOutput = Invoke-Pytest -Python $pythonCommand -Arguments $fullArguments -Label "Full coverage suite"
    $AllowedFullSuiteSkipPatterns = @(
        "^SKIPPED \[1\] tests/test_captain_supply_chain\.py:\d+: could not import 'autogen': No module named 'autogen'$",
        "^SKIPPED \[1\] tests/ledger_bridge/test_query\.py:\d+: autogen_core IS installed \(requirements\.txt pins it\); the no-autogen degradation path can't be exercised in-process in this environment$"
    )
    $fullSuiteSkipLines = @(
        $fullOutput |
            ForEach-Object { $_.Replace("\", "/") } |
            Where-Object { $_ -match "^SKIPPED \[\d+\]" }
    )
    foreach ($skipLine in $fullSuiteSkipLines) {
        $isAllowed = $false
        foreach ($pattern in $AllowedFullSuiteSkipPatterns) {
            if ($skipLine -match $pattern) {
                $isAllowed = $true
                break
            }
        }
        if (-not $isAllowed) {
            throw "Unexpected full-suite skip: $skipLine"
        }
    }
} catch {
    $primaryError = $_
} finally {
    try {
        & $dockerCommand compose --project-name captain-cook-test --file $composeFile down --remove-orphans
        $downExitCode = $LASTEXITCODE
        if ($downExitCode -ne 0) {
            $cleanupErrors.Add("Isolated MariaDB cleanup failed with exit code $downExitCode")
        }
    } catch {
        $cleanupErrors.Add("Isolated MariaDB cleanup failed: $($_.Exception.Message)")
    }

    if ($locationPushed) {
        try {
            Pop-Location
        } catch {
            $cleanupErrors.Add("Working-directory restoration failed: $($_.Exception.Message)")
        }
    }

    foreach ($name in $environmentNames) {
        try {
            Set-ProcessEnvironmentValue -Name $name -Value $previousEnvironment[$name]
        } catch {
            $cleanupErrors.Add("Environment restoration failed for ${name}: $($_.Exception.Message)")
        }
    }
}

if ($null -ne $primaryError) {
    if ($cleanupErrors.Count -gt 0) {
        throw "$($primaryError.Exception.Message); cleanup errors: $($cleanupErrors -join '; ')"
    }
    throw $primaryError
}
if ($cleanupErrors.Count -gt 0) {
    throw "Cleanup errors: $($cleanupErrors -join '; ')"
}
