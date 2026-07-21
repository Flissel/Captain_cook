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
$baseEnvironmentAllowlist = @(
    'TEST_MARIADB_DSN'
    'CAPTAIN_FACTORY_MODEL'
)
$n8nEnvironmentAllowlist = @(
    'CAPTAIN_N8N_URL'
    'CAPTAIN_N8N_API_KEY'
    'CAPTAIN_N8N_MCP_TOKEN'
)

function Import-AllowlistedEnvironmentFile {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string[]]$AllowedNames
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($trimmed) -or $trimmed.StartsWith('#')) {
            continue
        }
        $match = [regex]::Match(
            $trimmed,
            '^(?:export\s+)?(?<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?<value>.*)$'
        )
        if (-not $match.Success) {
            throw 'An allowlisted environment file contains an invalid assignment.'
        }
        $name = $match.Groups['name'].Value
        if ($AllowedNames -cnotcontains $name) {
            continue
        }
        $existing = Get-Item -LiteralPath ("Env:" + $name) -ErrorAction SilentlyContinue
        if ($null -ne $existing) {
            continue
        }
        $value = $match.Groups['value'].Value.Trim()
        if (
            $value.Length -ge 2 -and
            (($value.StartsWith("'") -and $value.EndsWith("'")) -or
             ($value.StartsWith('"') -and $value.EndsWith('"')))
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable(
            $name,
            $value,
            [EnvironmentVariableTarget]::Process
        )
    }
}

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

function Assert-RedactedJsonFile {
    param([Parameter(Mandatory)][string]$Path)

    $raw = Get-Content -Raw -LiteralPath $Path
    if (
        $raw -match '(?i)"(?:api[_-]?key|access[_-]?token|token|authorization|password|secret|raw[_-]?prompt|private(?:[_-][a-z0-9]+)*|(?:[a-z0-9]+[_-])*path)"\s*:' -or
        $raw -match '(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+' -or
        $raw -match '(?i)(?:[A-Z]:[\\/]|\\\\[^\\]|/(?:home|Users|tmp|var|etc)/)'
    ) {
        throw 'A gate JSON file contains forbidden sensitive material.'
    }
    return $raw
}

function Assert-ExactPropertyNames {
    param(
        [Parameter(Mandatory)][object]$Value,
        [Parameter(Mandatory)][string[]]$ExpectedNames,
        [Parameter(Mandatory)][string]$Label
    )

    if ($null -eq $Value) {
        throw "$Label is missing."
    }
    $actual = @($Value.psobject.Properties.Name | Sort-Object)
    $expected = @($ExpectedNames | Sort-Object)
    if ([string]::Join('|', $actual) -cne [string]::Join('|', $expected)) {
        throw "$Label does not match the exact live-report contract."
    }
}

function Assert-GatewayPromotion {
    param(
        [Parameter(Mandatory)][object]$Payload,
        [Parameter(Mandatory)][object]$GatewayPromotion
    )

    Assert-ExactPropertyNames -Value $GatewayPromotion -ExpectedNames @(
        'projection_status', 'release_decision', 'promotion_block'
    ) -Label 'gateway_promotion'
    Assert-ExactPropertyNames -Value $GatewayPromotion.release_decision -ExpectedNames @(
        'job_id', 'correlation_id', 'status', 'reasons', 'evaluation_id',
        'evaluation_ref', 'tool_gaps'
    ) -Label 'gateway_promotion.release_decision'
    Assert-ExactPropertyNames -Value $GatewayPromotion.promotion_block -ExpectedNames @(
        'schema', 'event_id', 'job_id', 'correlation_id', 'causation_id', 'occurred_at',
        'producer', 'subject_version', 'attempt', 'phase', 'role', 'status',
        'artifact_refs', 'evidence_refs', 'assertion_ids', 'lease_id'
    ) -Label 'gateway_promotion.promotion_block'

    $decision = $GatewayPromotion.release_decision
    $block = $GatewayPromotion.promotion_block
    if (
        [string]$GatewayPromotion.projection_status -cne 'ready_to_use' -or
        [string]$decision.status -cne 'ready' -or
        [string]$block.schema -cne 'captain.agent-factory-block.v1' -or
        [string]$block.phase -cne 'capability_promoted' -or
        [string]$block.producer -cne 'captain' -or
        [string]$block.status -cne 'succeeded'
    ) {
        throw 'Release mode requires an authoritative Captain Gateway promotion.'
    }
    if (
        [string]$decision.job_id -cne [string]$Payload.job_id -or
        [string]$decision.correlation_id -cne [string]$Payload.correlation_id -or
        [string]$block.job_id -cne [string]$Payload.job_id -or
        [string]$block.correlation_id -cne [string]$Payload.correlation_id -or
        [int]$block.subject_version -ne [int]$Payload.subject_version -or
        [int]$block.attempt -ne [int]$Payload.attempt
    ) {
        throw 'Gateway promotion evidence is not bound to the live Factory report.'
    }
    if (@($block.evidence_refs).Count -eq 0) {
        throw 'The Captain promotion block requires nonempty evidence_refs.'
    }
    if (
        $null -eq $decision.evaluation_id -or
        [string]::IsNullOrWhiteSpace([string]$decision.evaluation_id) -or
        $null -eq $decision.evaluation_ref
    ) {
        throw 'The Gateway release decision requires accepted evaluation evidence.'
    }
    $matchingEvaluationRefs = @(
        $block.artifact_refs | Where-Object {
            [string]$_.uri -ceq [string]$decision.evaluation_ref.uri -and
            [string]$_.sha256 -ceq [string]$decision.evaluation_ref.sha256 -and
            [string]$_.media_type -ceq [string]$decision.evaluation_ref.media_type
        }
    )
    if ($matchingEvaluationRefs.Count -eq 0) {
        throw 'The Captain promotion block must contain the accepted evaluation reference.'
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
    $raw = Assert-RedactedJsonFile -Path $report.FullName
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
    $gatewayPromotion = $null
    $gatewayPromotionProperty = $payload.psobject.Properties['gateway_promotion']
    if ($null -ne $gatewayPromotionProperty) {
        $gatewayPromotion = $gatewayPromotionProperty.Value
    }
    if ($ExpectedMode -eq 'demo' -and $null -ne $gatewayPromotion) {
        throw 'Demo mode cannot contain a Gateway promotion.'
    }
    if ($ExpectedMode -eq 'release') {
        Assert-GatewayPromotion -Payload $payload -GatewayPromotion $gatewayPromotion
    }
    return $digest
}

if ($MaxCostUsd -le 0) {
    throw 'MaxCostUsd must be a positive amount.'
}
$decimalBits = [decimal]::GetBits($MaxCostUsd)
$fractionalDigits = ($decimalBits[3] -shr 16) -band 0xFF
if ($fractionalDigits -gt 2) {
    throw 'MaxCostUsd must have at most two fractional decimal places.'
}

if ($WithN8n) {
    Import-AllowlistedEnvironmentFile -Path (Join-Path $root '.env.captain-n8n') `
        -AllowedNames $n8nEnvironmentAllowlist
    Import-AllowlistedEnvironmentFile -Path (Join-Path $root '.env') `
        -AllowedNames ($baseEnvironmentAllowlist + $n8nEnvironmentAllowlist)
}
else {
    foreach ($name in $n8nEnvironmentAllowlist) {
        [Environment]::SetEnvironmentVariable(
            $name,
            $null,
            [EnvironmentVariableTarget]::Process
        )
    }
    Import-AllowlistedEnvironmentFile -Path (Join-Path $root '.env') `
        -AllowedNames $baseEnvironmentAllowlist
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
$preflightRaw = Assert-RedactedJsonFile -Path $preflightPath
try {
    $preflight = $preflightRaw | ConvertFrom-Json -Depth 100
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
$pytestExitCode = 1
try {
    & $python @pytestArguments *> $null
    $pytestExitCode = $LASTEXITCODE
}
catch {
    $pytestExitCode = 1
}
if ($pytestExitCode -ne 0) {
    throw 'Factory live validation failed without releasing test output.'
}

$reportDigest = Assert-RedactedReport -Directory $reportDirectory -ExpectedMode $Mode
Write-Output "Hermes six-skill Factory $Mode gate passed; report sha256=$reportDigest."
exit 0
