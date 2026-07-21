#requires -Version 7.0
[CmdletBinding()]
param(
    [switch]$LiveProviders,
    [switch]$ConfirmProviderCost,
    [ValidateRange(0.01, 100.00)]
    [decimal]$MaxCostUsdPerInput = 1.00,
    [string[]]$InputIds = @(
        'sales_pipeline_brief',
        'proposal_refinement',
        'renewal_orchestration'
    ),
    [string]$ManifestPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'demo_inputs/agent_factory/manifest.json'),
    [string]$EvidenceDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) '.captain-cook/evidence'),
    [string]$CapabilityRunnerPath = (Join-Path $PSScriptRoot 'run-capability-factory-live.ps1'),
    [string]$ServiceRunnerPath = (Join-Path $PSScriptRoot 'live-demo-services.ps1'),
    [string]$CredentialSourceEnv = (Join-Path (Split-Path -Parent $PSScriptRoot) '.env'),
    [string]$SharedArtifactDirectory,
    [ValidateRange(60, 86400)]
    [int]$WallClockBudgetSeconds = 600,
    [switch]$RecordVideo,
    [string]$RecordingWindowTitle,
    [string]$FfmpegPath = 'ffmpeg'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$stateRoot = Join-Path $root '.captain-cook'
$recordingProcess = $null
$recordingPath = $null
$resolvedSharedArtifactDirectory = $null

function Get-DotEnvValue([string]$Path, [string]$Name) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -notmatch '^\s*(?:export\s+)?(?<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?<value>.*)\s*$') { continue }
        if ($Matches.name -ne $Name) { continue }
        $value = $Matches.value.Trim()
        if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        return $value
    }
    return $null
}

function Resolve-SharedArtifactDirectory([string]$ExplicitPath, [string]$EnvPath) {
    if (-not [string]::IsNullOrWhiteSpace($ExplicitPath)) {
        $configured = @($ExplicitPath)
    } else {
        $configured = @('CAPTAIN_RUNTIME_ARTIFACT_ROOT', 'MINIBOOK_CREATION_ARTIFACTS' | ForEach-Object {
            [Environment]::GetEnvironmentVariable($_, 'Process')
        } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        if ($configured.Count -eq 0) {
            $configured = @('CAPTAIN_RUNTIME_ARTIFACT_ROOT', 'MINIBOOK_CREATION_ARTIFACTS' | ForEach-Object {
                Get-DotEnvValue $EnvPath $_
            } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        }
    }
    if ($configured.Count -eq 0) {
        throw 'Shared capability artifact root is missing (CAPTAIN_RUNTIME_ARTIFACT_ROOT / MINIBOOK_CREATION_ARTIFACTS).'
    }
    $resolved = @($configured | ForEach-Object {
        $candidate = [string]$_
        if (-not [IO.Path]::IsPathFullyQualified($candidate)) { $candidate = Join-Path $root $candidate }
        [IO.Path]::GetFullPath($candidate)
    } | Sort-Object -Unique)
    if ($resolved.Count -ne 1) {
        throw 'Captain Runtime and Minibook must use the same capability artifact root.'
    }
    return $resolved[0]
}

function Resolve-Leaf([string]$Path, [string]$Label) {
    $candidate = [IO.Path]::GetFullPath($Path)
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw "$Label does not exist."
    }
    return $candidate
}

function Resolve-EvidenceDirectory([string]$Path) {
    $candidate = [IO.Path]::GetFullPath($Path)
    $relative = [IO.Path]::GetRelativePath($root, $candidate)
    $insideWorkspace = $relative -ne '..' -and -not $relative.StartsWith("..$([IO.Path]::DirectorySeparatorChar)")
    if ($insideWorkspace) {
        $allowedRelative = [IO.Path]::GetRelativePath($root, $stateRoot)
        if ($relative -ne $allowedRelative -and -not $relative.StartsWith("$allowedRelative$([IO.Path]::DirectorySeparatorChar)")) {
            throw 'Evidence inside the repository must remain below the gitignored .captain-cook/evidence directory.'
        }
    }
    return $candidate
}

function Resolve-SelectedInputs([string]$Path, [string[]]$SelectedIds) {
    if ($SelectedIds.Count -ne 3 -or @($SelectedIds | Select-Object -Unique).Count -ne 3) {
        throw 'Exactly three distinct demo inputs are required.'
    }
    $manifestFile = Resolve-Leaf $Path 'Demo input manifest'
    try { $manifest = Get-Content -LiteralPath $manifestFile -Raw | ConvertFrom-Json } catch {
        throw 'Demo input manifest is not valid JSON.'
    }
    if ($manifest.schema_name -ne 'captain.demo-input-manifest.v1') {
        throw 'Unsupported demo input manifest schema.'
    }
    $manifestDirectory = Split-Path -Parent $manifestFile
    $resolved = [Collections.Generic.List[object]]::new()
    foreach ($inputId in $SelectedIds) {
        $matches = @($manifest.inputs | Where-Object { $_.input_id -eq $inputId })
        if ($matches.Count -ne 1) { throw "Demo input id is missing or ambiguous: $inputId" }
        $entry = $matches[0]
        if ([string]::IsNullOrWhiteSpace([string]$entry.pattern)) {
            throw "Demo input has no conversation pattern: $inputId"
        }
        $inputPath = [IO.Path]::GetFullPath((Join-Path $manifestDirectory ([string]$entry.path)))
        $relative = [IO.Path]::GetRelativePath($manifestDirectory, $inputPath)
        if ($relative -eq '..' -or $relative.StartsWith("..$([IO.Path]::DirectorySeparatorChar)")) {
            throw "Demo input escapes its manifest directory: $inputId"
        }
        if (-not (Test-Path -LiteralPath $inputPath -PathType Leaf) -or [IO.Path]::GetFileName($inputPath) -ne 'TO_BE_BUILT.md') {
            throw "Canonical TO_BE_BUILT.md is missing for input: $inputId"
        }
        $resolved.Add([pscustomobject]@{
            input_id = [string]$entry.input_id
            pattern = [string]$entry.pattern
            path = $inputPath
        })
    }
    if (@($resolved.pattern | Select-Object -Unique).Count -ne 3) {
        throw 'The live demo requires three distinct AutoGen conversation patterns.'
    }
    return @($resolved.ToArray())
}

function ConvertFrom-CapabilityOutput([object[]]$Output) {
    $lines = @($Output | ForEach-Object { [string]$_ })
    for ($index = $lines.Count - 1; $index -ge 0; $index--) {
        $candidate = ($lines[$index..($lines.Count - 1)] -join "`n").Trim()
        if (-not $candidate.StartsWith('{')) { continue }
        try {
            $parsed = $candidate | ConvertFrom-Json
            if ($parsed.schema -eq 'captain.capability-factory-cli-result.v1') { return $parsed }
        } catch {}
    }
    throw 'Capability runner returned no valid redacted result.'
}

function Assert-RunResult(
    [object]$Result,
    [object]$SelectedInput,
    [Guid]$CorrelationId,
    [bool]$RequireCreationEvidence
) {
    $summary = $Result.summary
    if (
        $Result.status -ne 'ready_to_use' -or
        $summary.terminal_state -ne 'ready_to_use' -or
        $summary.execution_state -ne 'completed'
    ) { throw "Capability run did not reach ready_to_use for $($SelectedInput.input_id)." }
    if ([string]$summary.correlation_id -ne $CorrelationId.ToString()) {
        throw "Capability run changed correlation identity for $($SelectedInput.input_id)."
    }
    if (
        -not $summary.release_authority_job_id -or
        -not $summary.terminal_decision_id -or
        -not $summary.capability_id -or
        [int]$summary.capability_version -lt 1 -or
        -not $summary.execution_command_id -or
        -not $summary.execution_result_id -or
        @($summary.projection_event_ids).Count -lt 1 -or
        [string]$summary.package_sha256 -notmatch '^[0-9a-f]{64}$'
    ) { throw "Gateway promotion, execution, or Minibook projection identity is incomplete for $($SelectedInput.input_id)." }
    if (@($summary.unresolved_required_tool_gaps).Count -ne 0) {
        throw "Required tool gaps remain unresolved for $($SelectedInput.input_id)."
    }
    if (@($summary.projection_event_ids | ForEach-Object { [string]$_ }) -notcontains [string]$summary.execution_result_id) {
        throw "Minibook projection does not bind the execution result for $($SelectedInput.input_id)."
    }
    if ($summary.minibook_projection_verified -ne $true) {
        throw "Minibook projection result is not committed for $($SelectedInput.input_id)."
    }
    if (
        [string]$Result.digests.input_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$Result.digests.manifest_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [double]$Result.timings.duration_seconds -lt 0
    ) { throw "Capability result digest or duration evidence is invalid for $($SelectedInput.input_id)." }
    if ($RequireCreationEvidence) {
        if (
            $summary.execution_mode -ne 'created' -or
            -not $summary.creation_job_id -or
            -not $summary.recovery_id -or
            @($summary.e2e_batch_ids).Count -ne 3 -or
            @($summary.e2e_batch_ids | Select-Object -Unique).Count -ne 3 -or
            @($summary.release_evidence_sha256).Count -ne 4
        ) { throw "Capability run lacks controlled recovery evidence and three distinct E2E traces for $($SelectedInput.input_id)." }
    }
    return [ordered]@{
        input_id = [string]$SelectedInput.input_id
        pattern = [string]$SelectedInput.pattern
        correlation_id = [string]$summary.correlation_id
        terminal_state = [string]$summary.terminal_state
        execution_state = [string]$summary.execution_state
        execution_mode = [string]$summary.execution_mode
        capability_id = [string]$summary.capability_id
        capability_version = [int]$summary.capability_version
        release_authority_job_id = [string]$summary.release_authority_job_id
        terminal_decision_id = [string]$summary.terminal_decision_id
        execution_command_id = [string]$summary.execution_command_id
        execution_result_id = [string]$summary.execution_result_id
        projection_event_ids = @($summary.projection_event_ids | ForEach-Object { [string]$_ })
        minibook_projection_verified = [bool]$summary.minibook_projection_verified
        recovery_id = [string]$summary.recovery_id
        e2e_batch_ids = @($summary.e2e_batch_ids | ForEach-Object { [string]$_ })
        package_sha256 = [string]$summary.package_sha256
        input_sha256 = [string]$Result.digests.input_sha256
        manifest_sha256 = [string]$Result.digests.manifest_sha256
        duration_seconds = [double]$Result.timings.duration_seconds
    }
}

function Invoke-CapabilityRun([object]$SelectedInput, [Guid]$CorrelationId) {
    $runState = Join-Path $stateRoot "capability-live-demo/$CorrelationId"
    $arguments = @{
        InputPath = [string]$SelectedInput.path
        CorrelationId = $CorrelationId
        ArtifactDirectory = $resolvedSharedArtifactDirectory
        CheckpointDirectory = (Join-Path $runState 'checkpoints')
        WallClockBudgetSeconds = $WallClockBudgetSeconds
    }
    $global:LASTEXITCODE = 0
    $raw = @(& $CapabilityRunnerPath @arguments 6>&1)
    $succeeded = $?
    $nativeExitCode = Get-Variable LASTEXITCODE -ValueOnly -ErrorAction SilentlyContinue
    if (-not $succeeded -or ($nativeExitCode -is [int] -and $nativeExitCode -ne 0)) {
        throw "Capability runner failed closed for $($SelectedInput.input_id)."
    }
    return ConvertFrom-CapabilityOutput $raw
}

function Invoke-ServiceAction([ValidateSet('start', 'stop')][string]$Action) {
    $global:LASTEXITCODE = 0
    if ($Action -eq 'start') {
        & $ServiceRunnerPath start -RecoverDemoCredentials -CredentialSourceEnv $CredentialSourceEnv 6>&1 | Out-Null
    } else {
        & $ServiceRunnerPath stop 6>&1 | Out-Null
    }
    $succeeded = $?
    $nativeExitCode = Get-Variable LASTEXITCODE -ValueOnly -ErrorAction SilentlyContinue
    if (-not $succeeded -or ($nativeExitCode -is [int] -and $nativeExitCode -ne 0)) {
        throw "Captain service action failed closed: $Action"
    }
}

function Start-NamedWindowRecording([string]$WindowTitle, [string]$OutputPath) {
    if ([string]::IsNullOrWhiteSpace($WindowTitle)) {
        throw 'Recording requires an explicit named window title.'
    }
    $command = Get-Command $FfmpegPath -CommandType Application -ErrorAction SilentlyContinue
    if ($null -eq $command) { throw 'ffmpeg is unavailable; recording was not started.' }
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $command.Source
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true
    foreach ($argument in @(
        '-hide_banner', '-loglevel', 'error', '-y', '-f', 'gdigrab',
        '-framerate', '15', '-i', "title=$WindowTitle", '-c:v', 'libx264',
        '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', $OutputPath
    )) { $null = $info.ArgumentList.Add($argument) }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $info
    if (-not $process.Start()) { throw 'ffmpeg named-window recording did not start.' }
    return $process
}

function Stop-NamedWindowRecording([Diagnostics.Process]$Process) {
    if ($Process.HasExited) {
        if ($Process.ExitCode -ne 0) { throw 'ffmpeg named-window recording failed.' }
        return
    }
    $Process.StandardInput.WriteLine('q')
    if (-not $Process.WaitForExit(15000)) {
        $Process.Kill($true)
        throw 'ffmpeg named-window recording did not finalize in time.'
    }
    if ($Process.ExitCode -ne 0) { throw 'ffmpeg named-window recording failed.' }
}

$ManifestPath = Resolve-Leaf $ManifestPath 'Demo input manifest'
$CapabilityRunnerPath = Resolve-Leaf $CapabilityRunnerPath 'Capability runner'
$ServiceRunnerPath = Resolve-Leaf $ServiceRunnerPath 'Service runner'
$EvidenceDirectory = Resolve-EvidenceDirectory $EvidenceDirectory
$selectedInputs = Resolve-SelectedInputs $ManifestPath $InputIds

$configuredRuntimeSeconds = [Environment]::GetEnvironmentVariable('CAPTAIN_FACTORY_RUNTIME_SECONDS', 'Process')
if ([string]::IsNullOrWhiteSpace($configuredRuntimeSeconds)) {
    $configuredRuntimeSeconds = Get-DotEnvValue $CredentialSourceEnv 'CAPTAIN_FACTORY_RUNTIME_SECONDS'
}
if ($LiveProviders -and -not [string]::IsNullOrWhiteSpace($configuredRuntimeSeconds)) {
    $parsedRuntimeSeconds = 0
    if (-not [int]::TryParse($configuredRuntimeSeconds, [ref]$parsedRuntimeSeconds) -or $parsedRuntimeSeconds -ne $WallClockBudgetSeconds) {
        throw 'Factory runtime budget disagrees with the live orchestrator budget.'
    }
}

if (-not $LiveProviders) {
    [ordered]@{
        schema = 'captain.capability-live-demo-plan.v1'
        status = 'validated'
        message = 'live providers were not requested'
        input_ids = @($selectedInputs.input_id)
        patterns = @($selectedInputs.pattern)
    } | ConvertTo-Json -Depth 4
    exit 0
}
if (-not $ConfirmProviderCost) {
    throw 'Live provider execution requires -ConfirmProviderCost.'
}
if ($RecordVideo -and [string]::IsNullOrWhiteSpace($RecordingWindowTitle)) {
    throw 'Recording requires -RecordingWindowTitle so capture stays limited to one named window.'
}
$resolvedSharedArtifactDirectory = Resolve-SharedArtifactDirectory $SharedArtifactDirectory $CredentialSourceEnv

$previousCost = [Environment]::GetEnvironmentVariable('CAPTAIN_FACTORY_MAX_COST_USD', 'Process')
$costWasSet = $null -ne $previousCost
$costScale = ([decimal]::GetBits($MaxCostUsdPerInput)[3] -shr 16) -band 0x7F
if ($costScale -gt 2) {
    throw 'Factory cost budget accepts at most two decimal places.'
}
$costText = $MaxCostUsdPerInput.ToString('0.00', [Globalization.CultureInfo]::InvariantCulture)
$configuredCost = $previousCost
if ([string]::IsNullOrWhiteSpace($configuredCost)) {
    $configuredCost = Get-DotEnvValue $CredentialSourceEnv 'CAPTAIN_FACTORY_MAX_COST_USD'
}
if (-not [string]::IsNullOrWhiteSpace($configuredCost)) {
    $parsedConfiguredCost = [decimal]0
    $costStyle = [Globalization.NumberStyles]::AllowDecimalPoint
    if (
        -not [decimal]::TryParse(
            $configuredCost,
            $costStyle,
            [Globalization.CultureInfo]::InvariantCulture,
            [ref]$parsedConfiguredCost
        ) -or
        $parsedConfiguredCost -ne $MaxCostUsdPerInput
    ) {
        throw 'Factory cost budget disagrees with the live orchestrator budget.'
    }
}
[Environment]::SetEnvironmentVariable('CAPTAIN_FACTORY_MAX_COST_USD', $costText, 'Process')

try {
    New-Item -ItemType Directory -Force -Path $EvidenceDirectory | Out-Null
    if ($RecordVideo) {
        $recordingPath = Join-Path $EvidenceDirectory ("capability-live-demo-{0}.mp4" -f (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ'))
        $recordingProcess = Start-NamedWindowRecording $RecordingWindowTitle $recordingPath
    }

    Invoke-ServiceAction start
    $runs = [Collections.Generic.List[object]]::new()
    $firstInput = $selectedInputs[0]
    $firstCorrelation = [Guid]::NewGuid()
    $firstResult = Invoke-CapabilityRun $firstInput $firstCorrelation
    $first = Assert-RunResult $firstResult $firstInput $firstCorrelation $true
    $runs.Add($first)

    Invoke-ServiceAction stop
    Invoke-ServiceAction start
    $resumeResult = Invoke-CapabilityRun $firstInput $firstCorrelation
    $resume = Assert-RunResult $resumeResult $firstInput $firstCorrelation $false
    if ($resumeResult.summary.execution_mode -ne 'reused') {
        throw 'Restart/resume did not reuse released Gateway authority.'
    }
    foreach ($name in @('capability_id', 'capability_version', 'execution_command_id', 'execution_result_id')) {
        if ([string]$first[$name] -ne [string]$resume[$name]) {
            throw "restart/resume changed execution identity: $name"
        }
    }
    if ((@($first.projection_event_ids) -join ',') -ne (@($resume.projection_event_ids) -join ',')) {
        throw 'restart/resume changed execution identity: projection_event_ids'
    }

    foreach ($selectedInput in $selectedInputs[1..2]) {
        $correlation = [Guid]::NewGuid()
        $result = Invoke-CapabilityRun $selectedInput $correlation
        $runs.Add((Assert-RunResult $result $selectedInput $correlation $true))
    }

    $projectionVerified = (
        @($runs | Where-Object { $_.minibook_projection_verified -eq $true }).Count -eq $runs.Count -and
        $resume.minibook_projection_verified -eq $true
    )
    if (-not $projectionVerified) {
        throw 'Minibook projection verification is incomplete.'
    }

    if ($recordingProcess) {
        Stop-NamedWindowRecording $recordingProcess
        $recordingProcess = $null
        if (-not (Test-Path -LiteralPath $recordingPath -PathType Leaf) -or (Get-Item -LiteralPath $recordingPath).Length -lt 1) {
            throw 'Named-window recording produced no video artifact.'
        }
    }

    $restartEvidence = [ordered]@{
        correlation_id = [string]$resume.correlation_id
        capability_id = [string]$resume.capability_id
        capability_version = [int]$resume.capability_version
        execution_command_id = [string]$resume.execution_command_id
        execution_result_id = [string]$resume.execution_result_id
        projection_event_ids = @($resume.projection_event_ids)
    }
    $evidence = [ordered]@{
        schema = 'captain.capability-live-demo-evidence.v1'
        status = 'ready_to_use'
        created_at = (Get-Date).ToUniversalTime().ToString('o')
        commit_sha = (& git -C $root rev-parse HEAD).Trim()
        database = 'captain_test'
        input_count = 3
        maximum_cost_usd_per_input = $costText
        controlled_recovery_verified = $true
        restart_resume_verified = $true
        gateway_execution_verified = $true
        minibook_projection_verified = $projectionVerified
        runs = @($runs)
        restart_resume = $restartEvidence
        recording = if ($recordingPath) {
            [ordered]@{ status = 'recorded'; file_name = [IO.Path]::GetFileName($recordingPath) }
        } else {
            [ordered]@{ status = 'not_requested' }
        }
    }
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ')
    $evidencePath = Join-Path $EvidenceDirectory "capability-live-demo-$stamp.json"
    $temporaryPath = "$evidencePath.$([Guid]::NewGuid().ToString('N')).tmp"
    $evidence | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $temporaryPath -Encoding utf8
    Move-Item -LiteralPath $temporaryPath -Destination $evidencePath
    [ordered]@{
        schema = 'captain.capability-live-demo-result.v1'
        status = 'ready_to_use'
        evidence_path = $evidencePath
        recording_status = [string]$evidence.recording.status
    } | ConvertTo-Json -Depth 3
} finally {
    if ($recordingProcess) {
        try { Stop-NamedWindowRecording $recordingProcess } catch {}
    }
    if ($costWasSet) {
        [Environment]::SetEnvironmentVariable('CAPTAIN_FACTORY_MAX_COST_USD', $previousCost, 'Process')
    } else {
        [Environment]::SetEnvironmentVariable('CAPTAIN_FACTORY_MAX_COST_USD', $null, 'Process')
    }
}
