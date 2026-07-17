[CmdletBinding(DefaultParameterSetName = "Run")]
param(
    [Parameter(Mandatory, ParameterSetName = "Run")]
    [string] $Workspace,

    [Parameter(Mandatory, ParameterSetName = "Run")]
    [string] $Prompt,

    [Parameter(ParameterSetName = "Run")]
    [string] $CodexPath,

    [Parameter(Mandatory, ParameterSetName = "Cancel")]
    [ValidateRange(1, [int]::MaxValue)]
    [int] $CancelProcessId
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($PSCmdlet.ParameterSetName -eq "Cancel") {
    $cancelArguments = @("/PID", "$CancelProcessId", "/T", "/F")
    & taskkill.exe $cancelArguments *> $null
    exit $LASTEXITCODE
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
$startInfo.ArgumentList.Add("exec")
$startInfo.ArgumentList.Add("--json")
$startInfo.ArgumentList.Add($Prompt)

$process = [System.Diagnostics.Process]::new()
$process.StartInfo = $startInfo
try {
    if (-not $process.Start()) {
        throw "Codex process did not start."
    }
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
