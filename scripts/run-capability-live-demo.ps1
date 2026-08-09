#requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('start', 'verify')]
    [string]$Action,

    [switch]$ConfirmProviderCost,

    [string]$MaxCostUsd,

    [double]$TimeoutSeconds = 1800,

    [string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot),

    [string]$PythonExecutable = (Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\Scripts\python.exe'),

    [string]$LifecycleScript = (Join-Path $PSScriptRoot 'capability-demo.ps1')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repository = [IO.Path]::GetFullPath($RepositoryRoot)
$python = [IO.Path]::GetFullPath($PythonExecutable)
$lifecycle = [IO.Path]::GetFullPath($LifecycleScript)
if (-not (Test-Path -LiteralPath $repository -PathType Container)) {
    throw 'Repository root is unavailable.'
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Python executable is unavailable.'
}
if (-not (Test-Path -LiteralPath $lifecycle -PathType Leaf)) {
    throw 'Canonical capability demo lifecycle is unavailable.'
}

if ($Action -eq 'verify') {
    & $lifecycle status `
        -RepositoryRoot $repository `
        -PythonExecutable $python `
        -TimeoutSeconds $TimeoutSeconds
    exit $LASTEXITCODE
}

if (-not $ConfirmProviderCost.IsPresent) {
    throw 'Explicit -ConfirmProviderCost authorization is required.'
}
if ([string]::IsNullOrWhiteSpace($MaxCostUsd) -or $MaxCostUsd -notmatch '^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$') {
    throw 'MaxCostUsd must be a finite invariant decimal.'
}
$parsed = [decimal]::Zero
$style = [Globalization.NumberStyles]::AllowDecimalPoint
$culture = [Globalization.CultureInfo]::InvariantCulture
if (-not [decimal]::TryParse($MaxCostUsd, $style, $culture, [ref]$parsed)) {
    throw 'MaxCostUsd must be a finite invariant decimal.'
}
if ($parsed -lt [decimal]::Parse('0.03', $culture) -or $parsed -gt [decimal]::Parse('6.00', $culture)) {
    throw 'Maximum total provider cost is USD 6.00.'
}

& $lifecycle run `
    -AllowPaidRun `
    -MaxCostUsd $MaxCostUsd `
    -RepositoryRoot $repository `
    -PythonExecutable $python `
    -TimeoutSeconds $TimeoutSeconds
exit $LASTEXITCODE
