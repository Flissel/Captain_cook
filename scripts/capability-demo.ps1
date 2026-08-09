#requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('init', 'start', 'status', 'run', 'resume', 'stop')]
    [string]$Action,

    [switch]$AllowPaidRun,

    [string]$MaxCostUsd = '6.00',

    [double]$TimeoutSeconds = 1800,

    [string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot),

    [string]$PythonExecutable = (Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\Scripts\python.exe')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repository = [IO.Path]::GetFullPath($RepositoryRoot)
if (-not (Test-Path -LiteralPath $repository -PathType Container)) {
    throw 'Repository root is unavailable.'
}
$python = [IO.Path]::GetFullPath($PythonExecutable)
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Python executable is unavailable.'
}
$entrypoint = Join-Path $PSScriptRoot 'capability-demo.py'
if (-not (Test-Path -LiteralPath $entrypoint -PathType Leaf)) {
    throw 'Capability demo entrypoint is unavailable.'
}

$cliArguments = @(
    $entrypoint,
    $Action,
    '--repository-root',
    $repository,
    '--max-cost-usd',
    $MaxCostUsd,
    '--timeout-seconds',
    $TimeoutSeconds.ToString([Globalization.CultureInfo]::InvariantCulture)
)
if ($AllowPaidRun.IsPresent) {
    $cliArguments += '--allow-paid-run'
}

& $python @cliArguments
$code = $LASTEXITCODE
exit $code
