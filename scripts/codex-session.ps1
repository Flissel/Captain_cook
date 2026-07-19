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
    [string] $SessionId,

    [Parameter(Mandatory, ParameterSetName = "Run")]
    [string] $StatePath,

    [Parameter(ParameterSetName = "Run")]
    [ValidateSet("read-only", "workspace-write")]
    [string] $Sandbox = "workspace-write",

    [Parameter(Mandatory, ParameterSetName = "Cancel")]
    [string] $CancelStatePath,

    [Parameter(Mandatory, ParameterSetName = "Cancel")]
    [ValidateSet("operator", "timeout", "shutdown", "claim_lost", "captain_revoked")]
    [string] $CancellationReason
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($PSVersionTable.PSVersion.Major -lt 7) {
    throw "PowerShell 7 or newer is required."
}

if ($PSCmdlet.ParameterSetName -eq "Cancel") {
    $state = Get-Content -LiteralPath $CancelStatePath -Raw -Encoding UTF8 |
        ConvertFrom-Json -ErrorAction Stop
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
$startInfo.ArgumentList.Add("-a")
$startInfo.ArgumentList.Add("never")
$startInfo.ArgumentList.Add("exec")
$startInfo.ArgumentList.Add("--sandbox")
$startInfo.ArgumentList.Add($Sandbox)
$startInfo.ArgumentList.Add("--json")
$startInfo.ArgumentList.Add($Prompt)

$process = [System.Diagnostics.Process]::new()
$process.StartInfo = $startInfo
try {
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
    $resolvedStatePath = [IO.Path]::GetFullPath($StatePath)
    $temporaryStatePath = "$resolvedStatePath.tmp"
    [IO.File]::WriteAllText(
        $temporaryStatePath,
        ($identity | ConvertTo-Json -Compress),
        [Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $temporaryStatePath -Destination $resolvedStatePath -Force

    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    [void] $stderrTask.GetAwaiter().GetResult()
    [Console]::Out.Write($stdout)
    exit $process.ExitCode
} finally {
    $process.Dispose()
}
