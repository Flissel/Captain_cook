#requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('start', 'verify')]
    [string]$Action,

    [switch]$ConfirmProviderCost,

    [string]$MaxCostUsd,

    [string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot),

    [string]$PythonExecutable = (Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\Scripts\python.exe'),

    [string]$EntrypointModule = 'agenten.agent_factory.capability_live_entrypoint',

    [string]$EvidencePath,

    [string]$RecordingWindowTitle,

    [string]$FfmpegPath = 'ffmpeg'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Resolve-ExistingLeaf {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    $resolved = Resolve-Path -LiteralPath $Path -ErrorAction SilentlyContinue
    if ($null -eq $resolved -or -not (Test-Path -LiteralPath $resolved.Path -PathType Leaf)) {
        throw "$Description is missing"
    }
    return [System.IO.Path]::GetFullPath($resolved.Path)
}

function Assert-RepositoryOwnedPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Repository,

        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    $prefix = $Repository.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if (-not $Path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Description must be repository-owned"
    }
}

function Resolve-ProviderBudget {
    param([Parameter(Mandatory = $true)][string]$RawValue)

    if ($RawValue -notmatch '^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$') {
        throw 'MaxCostUsd must be a finite invariant decimal'
    }
    $parsed = [decimal]::Zero
    $style = [System.Globalization.NumberStyles]::AllowDecimalPoint
    $culture = [System.Globalization.CultureInfo]::InvariantCulture
    if (-not [decimal]::TryParse($RawValue, $style, $culture, [ref]$parsed)) {
        throw 'MaxCostUsd must be a finite invariant decimal'
    }
    if ($parsed -lt [decimal]::Parse('0.03', $culture)) {
        throw 'MaxCostUsd must fund three positive reservations'
    }
    return $parsed
}

$repository = [System.IO.Path]::GetFullPath(
    (Resolve-Path -LiteralPath $RepositoryRoot -ErrorAction Stop).Path
).TrimEnd('\', '/')
$moduleRoot = [System.IO.Path]::GetFullPath(
    (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot) -ErrorAction Stop).Path
).TrimEnd('\', '/')
if ($EntrypointModule -notmatch '^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$') {
    throw 'Capability live entrypoint module is invalid'
}
$entrypointRelative = $EntrypointModule.Replace('.', [System.IO.Path]::DirectorySeparatorChar) + '.py'
$entrypoint = Resolve-ExistingLeaf `
    -Path (Join-Path $moduleRoot $entrypointRelative) `
    -Description 'Capability live entrypoint module'
Assert-RepositoryOwnedPath `
    -Path $entrypoint `
    -Repository $moduleRoot `
    -Description 'Capability live entrypoint module'
$python = Resolve-ExistingLeaf -Path $PythonExecutable -Description 'Python executable'

function Invoke-CapabilityLiveEntrypoint {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    Push-Location -LiteralPath $moduleRoot
    try {
        & $python -m $EntrypointModule @Arguments *> $null
        return $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
}

if ([string]::IsNullOrWhiteSpace($EvidencePath)) {
    $EvidencePath = Join-Path $repository '.captain-cook\live-demo\current\final-evidence.json'
}
$evidence = [System.IO.Path]::GetFullPath($EvidencePath)
Assert-RepositoryOwnedPath -Path $evidence -Repository $repository -Description 'Capability live evidence'

if ($Action -eq 'verify') {
    $existingEvidence = Resolve-ExistingLeaf -Path $evidence -Description 'Capability live evidence'
    $exitCode = Invoke-CapabilityLiveEntrypoint -Arguments @(
        '--evidence-path', $existingEvidence, '--verify-only'
    )
    if ($exitCode -ne 0) {
        throw "Capability live evidence verification failed with exit code $exitCode"
    }
    [ordered]@{
        schema = 'captain.capability-live-wrapper-result.v1'
        status = 'ready_to_use'
        evidence_path = [System.IO.Path]::GetRelativePath($repository, $existingEvidence).Replace('\', '/')
    } | ConvertTo-Json -Compress
    exit 0
}

if (-not $ConfirmProviderCost.IsPresent) {
    throw 'Explicit -ConfirmProviderCost authorization is required'
}
if ([string]::IsNullOrWhiteSpace($MaxCostUsd)) {
    throw 'Explicit -MaxCostUsd is required'
}
$budget = Resolve-ProviderBudget -RawValue $MaxCostUsd

$inputDefinitions = @(
    [ordered]@{
        relative = 'examples/live-demo/sequential-n8n/TO_BE_BUILT.md'
        pattern = 'sequential'
    },
    [ordered]@{
        relative = 'examples/live-demo/selector-review/TO_BE_BUILT.md'
        pattern = 'selector_group_chat'
    },
    [ordered]@{
        relative = 'examples/live-demo/swarm-api-gap/TO_BE_BUILT.md'
        pattern = 'swarm'
    }
)
$inputs = @()
foreach ($definition in $inputDefinitions) {
    $candidate = Resolve-ExistingLeaf `
        -Path (Join-Path $repository $definition.relative) `
        -Description "Canonical live input $($definition.relative)"
    Assert-RepositoryOwnedPath -Path $candidate -Repository $repository -Description 'Canonical live input'
    $inputs += $candidate
}

New-Item -ItemType Directory -Path (Split-Path -Parent $evidence) -Force | Out-Null

$ffmpegProcess = $null
if (-not [string]::IsNullOrWhiteSpace($RecordingWindowTitle)) {
    $ffmpeg = Get-Command -Name $FfmpegPath -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $ffmpeg) {
        throw 'FFmpeg is required when -RecordingWindowTitle is supplied'
    }
    $windows = @(
        Get-Process | Where-Object {
            $_.MainWindowTitle -ceq $RecordingWindowTitle
        }
    )
    if ($windows.Count -ne 1) {
        throw 'Recording requires exactly one named window'
    }
    $recordingRoot = Join-Path $repository '.captain-cook\live-demo\recordings'
    New-Item -ItemType Directory -Path $recordingRoot -Force | Out-Null
    $recordingPath = Join-Path $recordingRoot 'capability-live-demo.mp4'
    $ffmpegArguments = @(
        '-hide_banner', '-loglevel', 'error', '-f', 'gdigrab', '-framerate', '30',
        '-i', "title=$RecordingWindowTitle", '-y', $recordingPath
    )
    $ffmpegProcess = Start-Process `
        -FilePath $ffmpeg.Source `
        -ArgumentList $ffmpegArguments `
        -WindowStyle Hidden `
        -PassThru
    Start-Sleep -Milliseconds 250
    $ffmpegProcess.Refresh()
    if ($ffmpegProcess.HasExited) {
        throw 'FFmpeg recording failed before provider execution'
    }
}

try {
    $arguments = @(
        '--repository-root', $repository,
        '--input', $inputs[0], '--pattern', $inputDefinitions[0].pattern,
        '--input', $inputs[1], '--pattern', $inputDefinitions[1].pattern,
        '--input', $inputs[2], '--pattern', $inputDefinitions[2].pattern,
        '--max-cost-usd', $MaxCostUsd,
        '--evidence-path', $evidence
    )
    $exitCode = Invoke-CapabilityLiveEntrypoint -Arguments $arguments
    if ($exitCode -ne 0) {
        throw "Capability live entrypoint failed with exit code $exitCode"
    }
    $verifyExitCode = Invoke-CapabilityLiveEntrypoint -Arguments @(
        '--evidence-path', $evidence, '--verify-only'
    )
    if ($verifyExitCode -ne 0) {
        throw "Capability live evidence verification failed with exit code $verifyExitCode"
    }
}
finally {
    if ($null -ne $ffmpegProcess -and -not $ffmpegProcess.HasExited) {
        Stop-Process -Id $ffmpegProcess.Id -ErrorAction SilentlyContinue
        $ffmpegProcess.WaitForExit(3000)
    }
}

[ordered]@{
    schema = 'captain.capability-live-wrapper-result.v1'
    status = 'ready_to_use'
    input_count = 3
    maximum_cost_usd = $budget.ToString('0.00', [System.Globalization.CultureInfo]::InvariantCulture)
    evidence_path = [System.IO.Path]::GetRelativePath($repository, $evidence).Replace('\', '/')
} | ConvertTo-Json -Compress
