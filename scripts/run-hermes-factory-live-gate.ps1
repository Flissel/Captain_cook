#requires -Version 7.0
[CmdletBinding()]
param(
    [ValidateSet('demo', 'release')]
    [string]$Mode = 'demo',
    [Parameter(Mandatory)]
    [decimal]$MaxCostUsd,
    [string]$Model,
    [switch]$WithN8n
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$skillNames = @(
    'captain-factory-discover'
    'captain-factory-brief-codex'
    'captain-factory-execute-team'
    'captain-factory-evaluate-team'
    'captain-factory-improve-team'
    'captain-factory-report-captain'
)
$entrypointModule = 'agenten.agent_factory.factory_live_entrypoint'
$root = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path

function Assert-CommandAvailable {
    param([Parameter(Mandatory)][string]$Name)

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command is unavailable: $Name"
    }
}

function Invoke-QuietCommand {
    param(
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string[]]$Arguments,
        [Parameter(Mandatory)][string]$FailureMessage
    )

    & $Executable @Arguments *> $null
    if ($LASTEXITCODE -ne 0) {
        throw $FailureMessage
    }
}

function Get-RequiredEnvironmentValue {
    param([Parameter(Mandatory)][string]$Name)

    $item = Get-Item -LiteralPath ("Env:" + $Name) -ErrorAction SilentlyContinue
    if ($null -eq $item -or [string]::IsNullOrWhiteSpace([string]$item.Value)) {
        throw "$Name is required."
    }
    return [string]$item.Value
}

function Assert-IsolatedDatabase {
    param([Parameter(Mandatory)][string]$Dsn)

    $match = [regex]::Match(
        $Dsn,
        '^[A-Za-z][A-Za-z0-9+.-]*://[^/]+/(?<database>[^/?#]+)(?:[?#].*)?$'
    )
    if (-not $match.Success) {
        throw 'TEST_MARIADB_DSN must be a valid DSN for the isolated captain_test database.'
    }
    $database = [System.Uri]::UnescapeDataString($match.Groups['database'].Value)
    if ($database -cne 'captain_test') {
        throw 'TEST_MARIADB_DSN must target the exact isolated captain_test database.'
    }
}

function Assert-RedactedReport {
    param(
        [Parameter(Mandatory)][string]$Directory,
        [Parameter(Mandatory)][string]$ExpectedMode
    )

    $reports = @(Get-ChildItem -LiteralPath $Directory -Filter 'sha256-*.json' -File)
    if ($reports.Count -ne 1) {
        throw 'The live gate must emit exactly one content-addressed JSON report.'
    }
    $report = $reports[0]
    $digest = (Get-FileHash -LiteralPath $report.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($report.BaseName -cne "sha256-$digest") {
        throw 'The live report filename does not match its SHA-256 digest.'
    }
    $raw = Get-Content -Raw -LiteralPath $report.FullName
    if (
        $raw -match '(?i)"(?:api[_-]?key|token|authorization|password|secret)"\s*:' -or
        $raw -match '(?i)(?:[A-Z]:\\Users\\|/home/|/Users/)'
    ) {
        throw 'The live report contains a secret-like field or an absolute user path.'
    }
    try {
        $payload = $raw | ConvertFrom-Json -Depth 100
    }
    catch {
        throw 'The live report is not valid JSON.'
    }
    if ([string]$payload.mode -cne $ExpectedMode) {
        throw 'The live report mode does not match the requested gate mode.'
    }
    if ($ExpectedMode -eq 'demo' -and [string]$payload.terminal_status -cne 'demo_ready') {
        throw 'Demo mode may emit demo_ready only.'
    }
    if ($ExpectedMode -eq 'release' -and [string]$payload.terminal_status -cne 'ready_to_use') {
        throw 'Release mode requires the Captain ready_to_use terminal projection.'
    }
    return $digest
}

if ($MaxCostUsd -le 0) {
    throw 'MaxCostUsd must be a positive amount.'
}
if ([string]::IsNullOrWhiteSpace($Model)) {
    $Model = [string]$env:CAPTAIN_FACTORY_MODEL
}
if ([string]::IsNullOrWhiteSpace($Model)) {
    throw 'Model or CAPTAIN_FACTORY_MODEL is required.'
}

$databaseDsn = Get-RequiredEnvironmentValue -Name 'TEST_MARIADB_DSN'
Assert-IsolatedDatabase -Dsn $databaseDsn
if ($WithN8n) {
    foreach ($name in @('CAPTAIN_N8N_URL', 'CAPTAIN_N8N_API_KEY', 'CAPTAIN_N8N_MCP_TOKEN')) {
        Get-RequiredEnvironmentValue -Name $name | Out-Null
    }
}

foreach ($command in @('docker', 'hermes', 'codex')) {
    Assert-CommandAvailable -Name $command
}
Invoke-QuietCommand -Executable 'docker' -Arguments @('version', '--format', '{{.Server.Version}}') `
    -FailureMessage 'Docker Engine is not reachable.'
Invoke-QuietCommand -Executable 'docker' -Arguments @('compose', 'version') `
    -FailureMessage 'Docker Compose is not available.'
$mariaDbContainers = @(
    & docker ps --filter 'label=com.docker.compose.service=mariadb-test' --format '{{.ID}}' 2>$null
)
if ($LASTEXITCODE -ne 0 -or $mariaDbContainers.Count -ne 1) {
    throw 'Exactly one running mariadb-test service is required.'
}

$enabledSkills = & hermes skills list --enabled-only 2>&1
if ($LASTEXITCODE -ne 0) {
    throw 'Hermes enabled-skill discovery failed.'
}
$enabledSkillText = [string]::Join([Environment]::NewLine, @($enabledSkills))
foreach ($skillName in $skillNames) {
    if ([regex]::Matches($enabledSkillText, [regex]::Escape($skillName)).Count -ne 1) {
        throw "Hermes must expose released skill exactly once: $skillName"
    }
}
$bundle = & hermes bundles show captain-agent-factory-loop 2>&1
if ($LASTEXITCODE -ne 0) {
    throw 'Hermes captain-agent-factory-loop bundle is unavailable.'
}
$bundleText = [string]::Join([Environment]::NewLine, @($bundle))
foreach ($skillName in $skillNames) {
    if ([regex]::Matches($bundleText, [regex]::Escape($skillName)).Count -ne 1) {
        throw "Hermes bundle must bind released skill exactly once: $skillName"
    }
}
Invoke-QuietCommand -Executable 'codex' -Arguments @('login', 'status') `
    -FailureMessage 'Codex authentication is unavailable.'

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    $python = 'python'
}
Assert-CommandAvailable -Name $python

$costText = $MaxCostUsd.ToString('0.00', [System.Globalization.CultureInfo]::InvariantCulture)
$runId = [guid]::NewGuid().ToString('N')
$reportRoot = Join-Path ([System.IO.Path]::GetTempPath()) 'captain-cook\hermes-factory-live-gate'
$reportDirectory = Join-Path $reportRoot $runId
New-Item -ItemType Directory -Path $reportDirectory -Force | Out-Null
$preflightPath = Join-Path $reportDirectory 'preflight.json'
$preflightArguments = @(
    '-m', $entrypointModule, 'preflight',
    '--mode', $Mode,
    '--max-cost-usd', $costText,
    '--model', $Model,
    '--repository-root', $root,
    '--report-directory', $reportDirectory,
    '--output', $preflightPath
)
if ($WithN8n) {
    $preflightArguments += '--with-n8n'
}
& $python @preflightArguments *> $null
if ($LASTEXITCODE -ne 0) {
    throw 'Factory live preflight failed; inspect the redacted external preflight report.'
}
if (-not (Test-Path -LiteralPath $preflightPath -PathType Leaf)) {
    throw 'Factory live preflight did not emit its redacted confirmation.'
}
try {
    $preflight = Get-Content -Raw -LiteralPath $preflightPath | ConvertFrom-Json -Depth 100
}
catch {
    throw 'Factory live preflight confirmation is invalid JSON.'
}
if (
    [string]$preflight.schema -cne 'captain.hermes-six-skill-factory-preflight.v1' -or
    $preflight.prerequisites_confirmed -ne $true -or
    [string]$preflight.database_name -cne 'captain_test' -or
    $preflight.services_verified -ne $true -or
    $preflight.codex_authenticated -ne $true -or
    $preflight.skills_verified -ne $true
) {
    throw 'Factory live preflight did not confirm every required prerequisite.'
}
$digestProperties = @($preflight.skill_digests.psobject.Properties)
if ($digestProperties.Count -ne 6) {
    throw 'Factory live preflight must confirm exactly six released skill digests.'
}
foreach ($skillName in $skillNames) {
    $property = $preflight.skill_digests.psobject.Properties[$skillName]
    if ($null -eq $property -or [string]$property.Value -notmatch '^[0-9a-f]{64}$') {
        throw "Factory live preflight did not confirm a valid digest for $skillName."
    }
}

$env:CAPTAIN_FACTORY_GATE_MODE = $Mode
$env:CAPTAIN_FACTORY_MAX_COST_USD = $costText
$env:CAPTAIN_FACTORY_MODEL = $Model
$env:CAPTAIN_FACTORY_WITH_N8N = $(if ($WithN8n) { '1' } else { '0' })
$env:CAPTAIN_FACTORY_REPORT_DIRECTORY = $reportDirectory
$env:CAPTAIN_FACTORY_PREFLIGHT_PATH = $preflightPath
$env:CAPTAIN_FACTORY_PREREQUISITES_CONFIRMED = '1'

$pytestArguments = @(
    '-m', 'pytest', '-q', '--no-cov', '-m', 'live',
    'tests/live/test_hermes_six_skill_factory_live.py', '-rs'
)
& $python @pytestArguments
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$reportDigest = Assert-RedactedReport -Directory $reportDirectory -ExpectedMode $Mode
Write-Output "Hermes six-skill Factory $Mode gate passed; report sha256=$reportDigest."
exit 0
