[CmdletBinding(DefaultParameterSetName = "Run")]
param(
    [Parameter(Mandatory, ParameterSetName = "Run")]
    [string] $Workspace,

    [Parameter(Mandatory, ParameterSetName = "Run")]
    [string] $Prompt,

    [Parameter(ParameterSetName = "Run")]
    [string] $CodexPath,

    [Parameter(Mandatory, ParameterSetName = "Run")]
    [Parameter(Mandatory, ParameterSetName = "Cancel")]
    [Parameter(Mandatory, ParameterSetName = "Inspect")]
    [Parameter(Mandatory, ParameterSetName = "CancelIdentity")]
    [Parameter(Mandatory, ParameterSetName = "InspectIdentity")]
    [string] $SessionId,

    [Parameter(ParameterSetName = "Run")]
    [string] $StatePath,

    [Parameter(ParameterSetName = "Run")]
    [switch] $EmitState,

    [Parameter(Mandatory, ParameterSetName = "Run")]
    [DateTimeOffset] $DeadlineAt,

    [Parameter(ParameterSetName = "Run")]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')]
    [string] $ResumeThreadId,

    [Parameter(ParameterSetName = "Run")]
    [ValidatePattern('^http://127\.0\.0\.1:[0-9]{1,5}/v1$')]
    [string] $ProviderProxyUrl,

    [Parameter(ParameterSetName = "Run")]
    [ValidateSet("read-only", "workspace-write")]
    [string] $Sandbox = "workspace-write",

    [Parameter(Mandatory, ParameterSetName = "Cancel")]
    [string] $CancelStatePath,

    [Parameter(Mandatory, ParameterSetName = "Inspect")]
    [string] $InspectStatePath,

    [Parameter(Mandatory, ParameterSetName = "Cancel")]
    [Parameter(Mandatory, ParameterSetName = "CancelIdentity")]
    [ValidateSet("operator", "timeout", "shutdown", "claim_lost", "captain_revoked")]
    [string] $CancellationReason,

    [Parameter(Mandatory, ParameterSetName = "CancelIdentity")]
    [Parameter(Mandatory, ParameterSetName = "InspectIdentity")]
    [int] $ProcessId,

    [Parameter(Mandatory, ParameterSetName = "CancelIdentity")]
    [Parameter(Mandatory, ParameterSetName = "InspectIdentity")]
    [long] $ProcessStartTimeUtcTicks,

    [Parameter(Mandatory, ParameterSetName = "CancelIdentity")]
    [Parameter(Mandatory, ParameterSetName = "InspectIdentity")]
    [string] $ProcessExecutable
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($PSVersionTable.PSVersion.Major -lt 7) {
    throw "PowerShell 7 or newer is required."
}

if ($PSCmdlet.ParameterSetName -in @("Inspect", "InspectIdentity")) {
    $state = if ($PSCmdlet.ParameterSetName -eq "Inspect") {
        Get-Content -LiteralPath $InspectStatePath -Raw -Encoding UTF8 |
            ConvertFrom-Json -ErrorAction Stop
    } else {
        [pscustomobject] @{
            session_id = $SessionId
            pid = $ProcessId
            start_time_utc_ticks = $ProcessStartTimeUtcTicks
            executable = $ProcessExecutable
        }
    }
    if (
        $state.session_id -ne $SessionId -or
        [int] $state.pid -lt 1 -or
        [long] $state.start_time_utc_ticks -lt 1 -or
        [string]::IsNullOrWhiteSpace([string] $state.executable)
    ) {
        throw "Inspection state does not match the requested session."
    }
    $current = Get-Process -Id ([int] $state.pid) -ErrorAction SilentlyContinue
    $status = "lost"
    if ($null -ne $current) {
        $matches = $current.StartTime.ToUniversalTime().Ticks -eq [long] $state.start_time_utc_ticks -and
            [string]::Equals(
                [IO.Path]::GetFullPath($current.Path),
                [IO.Path]::GetFullPath([string] $state.executable),
                [StringComparison]::OrdinalIgnoreCase
            )
        $status = if ($matches) { "active" } else { "identity_mismatch" }
    }
    [pscustomobject] @{ session_id = $SessionId; status = $status } | ConvertTo-Json -Compress
    exit 0
}

if ($PSCmdlet.ParameterSetName -in @("Cancel", "CancelIdentity")) {
    $state = if ($PSCmdlet.ParameterSetName -eq "Cancel") {
        Get-Content -LiteralPath $CancelStatePath -Raw -Encoding UTF8 |
            ConvertFrom-Json -ErrorAction Stop
    } else {
        [pscustomobject] @{
            session_id = $SessionId
            pid = $ProcessId
            started_at_utc = "direct"
            start_time_utc_ticks = $ProcessStartTimeUtcTicks
            executable = $ProcessExecutable
        }
    }
    if (
        $state.session_id -ne $SessionId -or
        [int] $state.pid -lt 1 -or
        [string]::IsNullOrWhiteSpace([string] $state.started_at_utc) -or
        [long] $state.start_time_utc_ticks -lt 1 -or
        [string]::IsNullOrWhiteSpace([string] $state.executable)
    ) {
        throw "Cancellation state does not match the requested session."
    }

    $current = Get-Process -Id ([int] $state.pid) -ErrorAction Stop
    $currentStartedAt = $current.StartTime.ToUniversalTime()
    $currentExecutable = $current.Path
    if (
        $currentStartedAt.Ticks -ne [long] $state.start_time_utc_ticks -or
        -not [string]::Equals(
            [IO.Path]::GetFullPath($currentExecutable),
            [IO.Path]::GetFullPath([string] $state.executable),
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "Process identity no longer matches; refusing cancellation."
    }

    & taskkill.exe @("/PID", "$($state.pid)", "/T", "/F") *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Codex process tree cancellation failed."
    }
    [pscustomobject] @{
        session_id = $SessionId
        outcome = "cancelled"
        cancellation_reason = $CancellationReason
    } | ConvertTo-Json -Compress
    exit 0
}

if ($DeadlineAt.Offset -ne [TimeSpan]::Zero) {
    throw "Codex deadline must be a UTC timestamp."
}
if ([string]::IsNullOrWhiteSpace($StatePath) -eq (-not $EmitState)) {
    throw "Exactly one Codex state output mode must be configured."
}

$resolvedWorkspace = (Resolve-Path -LiteralPath $Workspace -ErrorAction Stop).Path
if (-not (Test-Path -LiteralPath $resolvedWorkspace -PathType Container)) {
    throw "Authorized workspace is not a directory."
}

if ($CodexPath) {
    $resolvedCodex = (Get-Item -LiteralPath $CodexPath -ErrorAction Stop).FullName
} else {
    $command = Get-Command -Name codex -CommandType Application -ErrorAction Stop |
        Select-Object -First 1
    $resolvedCodex = $command.Source
}

$startInfo = [System.Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = $resolvedCodex
$startInfo.WorkingDirectory = $resolvedWorkspace
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
$startInfo.ArgumentList.Add("--dangerously-bypass-approvals-and-sandbox")
if ($ProviderProxyUrl) {
    $startInfo.ArgumentList.Add("-c")
    $startInfo.ArgumentList.Add('model="gpt-5.6-terra"')
    $startInfo.ArgumentList.Add("-c")
    $startInfo.ArgumentList.Add('model_provider="captain_budget_proxy"')
    $startInfo.ArgumentList.Add("-c")
    $startInfo.ArgumentList.Add('model_reasoning_effort="high"')
    $startInfo.ArgumentList.Add("-c")
    $startInfo.ArgumentList.Add('model_providers.captain_budget_proxy.name="Captain localhost budget proxy"')
    $startInfo.ArgumentList.Add("-c")
    $startInfo.ArgumentList.Add(('model_providers.captain_budget_proxy.base_url="{0}"' -f $ProviderProxyUrl))
    $startInfo.ArgumentList.Add("-c")
    $startInfo.ArgumentList.Add('model_providers.captain_budget_proxy.env_key="CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN"')
    $startInfo.ArgumentList.Add("-c")
    $startInfo.ArgumentList.Add('model_providers.captain_budget_proxy.wire_api="responses"')
    $startInfo.ArgumentList.Add("-c")
    $startInfo.ArgumentList.Add('model_providers.captain_budget_proxy.request_max_retries=0')
    $startInfo.ArgumentList.Add("-c")
    $startInfo.ArgumentList.Add('model_providers.captain_budget_proxy.stream_max_retries=0')
}
if ($ResumeThreadId) {
    # Every value is a distinct ArgumentList entry so neither thread names nor
    # prompts cross a shell parsing boundary.
    $startInfo.ArgumentList.Add("exec")
    $startInfo.ArgumentList.Add("--ignore-user-config")
    $startInfo.ArgumentList.Add("--ignore-rules")
    $startInfo.ArgumentList.Add("resume")
    $startInfo.ArgumentList.Add("--json")
    $startInfo.ArgumentList.Add($ResumeThreadId)
    $startInfo.ArgumentList.Add($Prompt)
} else {
    $startInfo.ArgumentList.Add("exec")
    $startInfo.ArgumentList.Add("--ignore-user-config")
    $startInfo.ArgumentList.Add("--ignore-rules")
    $startInfo.ArgumentList.Add("--json")
    $startInfo.ArgumentList.Add($Prompt)
}

$process = [System.Diagnostics.Process]::new()
$process.StartInfo = $startInfo
try {
    if ([DateTimeOffset]::UtcNow -ge $DeadlineAt) {
        exit 124
    }
    if (-not $process.Start()) {
        throw "Codex process did not start."
    }
    $process.Refresh()
    $identity = [ordered] @{
        session_id = $SessionId
        pid = $process.Id
        started_at_utc = $process.StartTime.ToUniversalTime().ToString("O")
        start_time_utc_ticks = $process.StartTime.ToUniversalTime().Ticks
        executable = $resolvedCodex
    }
    if ($EmitState) {
        [Console]::Out.WriteLine(
            "CAPTAIN_PROCESS_STATE:" + ($identity | ConvertTo-Json -Compress)
        )
        [Console]::Out.Flush()
    } else {
        $resolvedStatePath = [IO.Path]::GetFullPath($StatePath)
        $temporaryStatePath = "$resolvedStatePath.tmp"
        [IO.File]::WriteAllText(
            $temporaryStatePath,
            ($identity | ConvertTo-Json -Compress),
            [Text.UTF8Encoding]::new($false)
        )
        Move-Item -LiteralPath $temporaryStatePath -Destination $resolvedStatePath -Force
    }

    $stderrTask = $process.StandardError.BaseStream.CopyToAsync(
        [IO.Stream]::Null,
        65536
    )
    while (($line = $process.StandardOutput.ReadLine()) -ne $null) {
        [Console]::Out.WriteLine($line)
        [Console]::Out.Flush()
    }
    $process.WaitForExit()
    [void] $stderrTask.GetAwaiter().GetResult()
    exit $process.ExitCode
} finally {
    $process.Dispose()
}
