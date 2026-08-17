[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [Alias('Input')]
    [string] $InputPath,

    [string] $EvidenceRoot,

    [string] $Python,

    [string] $ResumeMetadata
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$expectedInput = [IO.Path]::GetFullPath(
    (Join-Path $repositoryRoot 'tests/fixtures/agent_factory/sales_pipeline_brief.v3.json')
)
$resolvedInput = (Resolve-Path -LiteralPath $InputPath -ErrorAction Stop).Path
if (-not [string]::Equals($resolvedInput, $expectedInput, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Factory V3 live accepts only the frozen sales_pipeline_brief fixture.'
}

if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = Join-Path $repositoryRoot '.venv/Scripts/python.exe'
}
$resolvedPython = (Resolve-Path -LiteralPath $Python -ErrorAction Stop).Path
if (-not (Test-Path -LiteralPath $resolvedPython -PathType Leaf)) {
    throw 'Factory V3 live requires a concrete Python executable.'
}

if ([string]::IsNullOrWhiteSpace($EvidenceRoot)) {
    $EvidenceRoot = Join-Path $repositoryRoot '.captain-cook/private/factory-v3-live/runs'
}
$resolvedEvidenceRoot = [IO.Path]::GetFullPath($EvidenceRoot)
if (
    [string]::Equals($resolvedEvidenceRoot, [IO.Path]::GetPathRoot($resolvedEvidenceRoot), [StringComparison]::OrdinalIgnoreCase) -or
    [string]::Equals($resolvedEvidenceRoot, $repositoryRoot, [StringComparison]::OrdinalIgnoreCase)
) {
    throw 'Factory V3 live evidence root is too broad.'
}
[void](New-Item -ItemType Directory -Path $resolvedEvidenceRoot -Force)

$runId = $null
if (-not [string]::IsNullOrWhiteSpace($ResumeMetadata)) {
    $resolvedResume = (Resolve-Path -LiteralPath $ResumeMetadata -ErrorAction Stop).Path
    if ([IO.Path]::GetFileName($resolvedResume) -cne 'resume-metadata.json') {
        throw 'Factory V3 resume requires canonical signed metadata.'
    }
    $runId = [Guid]::Parse([IO.Path]::GetFileName([IO.Path]::GetDirectoryName($resolvedResume))).ToString()
    $expectedResume = [IO.Path]::GetFullPath(
        (Join-Path (Join-Path $resolvedEvidenceRoot $runId) 'resume-metadata.json')
    )
    if (-not [string]::Equals($resolvedResume, $expectedResume, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Factory V3 resume metadata is outside its durable run scope.'
    }
} else {
    $runId = [Guid]::NewGuid().ToString()
}

$runRoot = [IO.Path]::GetFullPath((Join-Path $resolvedEvidenceRoot $runId))
$workspaceRoot = Join-Path $runRoot 'workspace'
$casRoot = Join-Path $runRoot 'cas'
$stateRoot = Join-Path $runRoot 'state'
$preflightPath = Join-Path $runRoot 'preflight.json'
foreach ($path in @($workspaceRoot, $casRoot, $stateRoot)) {
    [void](New-Item -ItemType Directory -Path $path -Force)
}

$arguments = @(
    '-m', 'gateway.agent_factory_live_operator',
    '--repository-root', $repositoryRoot,
    '--input', $resolvedInput,
    '--workspace-root', $workspaceRoot,
    '--cas-root', $casRoot,
    '--state-root', $stateRoot,
    '--run-id', $runId,
    '--output', $preflightPath
)
if (-not [string]::IsNullOrWhiteSpace($ResumeMetadata)) {
    $arguments += @('--resume-metadata', $resolvedResume)
}

Push-Location -LiteralPath $repositoryRoot
try {
    & $resolvedPython @arguments
    $preflightExit = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($preflightExit -ne 0) {
    exit $preflightExit
}

# A zero exit is intentionally unreachable until a Captain-owned live runner
# enforces exact writable roots, exact argv, network denial, and provider cost.
# Do not substitute the existing PowerShell runner, deterministic Task-9 engine,
# or a caller-provided executable here.
[ordered] @{
    schema = 'captain.factory-v3-live-preflight-summary.v1'
    run_id = $runId
    status = 'runtime_enforcement_unavailable'
    unmet_checks = @('runtime_enforcement')
} | ConvertTo-Json -Compress
exit 5
