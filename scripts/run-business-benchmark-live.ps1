#requires -Version 7
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('claims', 'renewal', 'all')]
    [string]$Profile,

    [string]$PythonPath = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$allowedEnvironment = @(
    'CAPTAIN_BENCHMARK_PROVIDER',
    'CAPTAIN_BENCHMARK_MODEL',
    'CAPTAIN_BENCHMARK_SUITE_VERSION',
    'CAPTAIN_BENCHMARK_CANDIDATE_ID',
    'CAPTAIN_BENCHMARK_JOB_ID',
    'CAPTAIN_BENCHMARK_MAX_USD',
    'CAPTAIN_JOB_REMAINING_USD',
    'CAPTAIN_JOB_ALLOWED_MODELS',
    'CAPTAIN_RUNTIME_URL',
    'CAPTAIN_BENCHMARK_PROVIDER_SECRET'
)
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$evidenceBase = Join-Path $repositoryRoot '.captain-cook/evidence/business-benchmarks'
$runRoot = Join-Path $evidenceBase ([DateTimeOffset]::UtcNow.ToString('yyyyMMddTHHmmssZ'))
$previousProfile = [Environment]::GetEnvironmentVariable('CAPTAIN_BENCHMARK_PROFILE', 'Process')
$previousEvidenceRoot = [Environment]::GetEnvironmentVariable('CAPTAIN_BENCHMARK_EVIDENCE_ROOT', 'Process')

try {
    $resolvedPython = $PythonPath
    if ([string]::IsNullOrWhiteSpace($resolvedPython)) {
        $pythonCommand = Get-Command python -CommandType Application -ErrorAction SilentlyContinue
        if ($null -eq $pythonCommand) {
            throw 'TODO_TOOL.v1: validated Python 3.11 interpreter is unavailable'
        }
        $resolvedPython = $pythonCommand.Source
    }
    if (-not (Test-Path -LiteralPath $resolvedPython -PathType Leaf)) {
        throw 'TODO_TOOL.v1: validated Python interpreter path is not a file'
    }
    & $resolvedPython -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)"
    if ($LASTEXITCODE -ne 0) {
        throw 'TODO_TOOL.v1: validated Python interpreter could not execute'
    }
    foreach ($name in $allowedEnvironment) {
        $value = [Environment]::GetEnvironmentVariable($name, 'Process')
        if ([string]::IsNullOrWhiteSpace($value)) {
            throw "Required allowlisted environment setting is missing: $name"
        }
    }

    [Environment]::SetEnvironmentVariable('CAPTAIN_BENCHMARK_PROFILE', $Profile, 'Process')
    [Environment]::SetEnvironmentVariable(
        'CAPTAIN_BENCHMARK_EVIDENCE_ROOT',
        $runRoot,
        'Process'
    )

    # The selected live test runs deterministic validation first, then health and
    # secret-existence checks. It fails (never skips) when the production bundle
    # is unavailable, before any provider effect.
    & $resolvedPython -m pytest `
        -q -m live --no-cov tests/live/test_business_benchmark_live.py
    if ($LASTEXITCODE -ne 0) {
        throw "Business benchmark live gate failed with exit code $LASTEXITCODE"
    }

    [ordered]@{
        schema = 'captain.business-benchmark-live-runner-result.v1'
        profile = $Profile
        evidence_root = $runRoot
        status = 'completed'
    } | ConvertTo-Json -Compress
}
finally {
    [Environment]::SetEnvironmentVariable(
        'CAPTAIN_BENCHMARK_PROFILE', $previousProfile, 'Process'
    )
    [Environment]::SetEnvironmentVariable(
        'CAPTAIN_BENCHMARK_EVIDENCE_ROOT', $previousEvidenceRoot, 'Process'
    )
}
