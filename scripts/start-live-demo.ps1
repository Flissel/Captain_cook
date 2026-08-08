#requires -Version 7.0
[CmdletBinding()]
param(
    [switch]$RunDemo,
    [switch]$ConfirmProviderCost,
    [double]$MaxCostUsd,
    [string]$RecordingWindowTitle,
    [string]$ServiceCommand = (Join-Path $PSScriptRoot 'live-demo-services.ps1'),
    [string]$MinibookCommand = (Join-Path $PSScriptRoot 'minibook-demo.ps1'),
    [string]$CapabilityCommand = (Join-Path $PSScriptRoot 'run-capability-live-demo.ps1')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:MaxCostUsdBound = $PSBoundParameters.ContainsKey('MaxCostUsd')
$script:StartInvocationLine = $MyInvocation.Line

function Get-RawMaxCostUsd {
    foreach ($candidate in @([Environment]::CommandLine, $script:StartInvocationLine)) {
        if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
        $match = [regex]::Match(
            $candidate,
            '(?i)(?:^|\s)-MaxCostUsd(?:\s+|:)(?:"([^"]+)"|''([^'']+)''|(\S+))'
        )
        if ($match.Success) {
            foreach ($index in 1..3) {
                if ($match.Groups[$index].Success) { return $match.Groups[$index].Value }
            }
        }
    }
    return $null
}

function Assert-PaidDemoAuthorization {
    $hasBudget = $script:MaxCostUsdBound
    if (-not $RunDemo) {
        if ($ConfirmProviderCost -or $hasBudget -or -not [string]::IsNullOrWhiteSpace($RecordingWindowTitle)) {
            throw 'Provider-cost options require -RunDemo.'
        }
        return $null
    }
    if (-not $ConfirmProviderCost) {
        throw 'Provider-backed execution requires -ConfirmProviderCost.'
    }
    if (-not $hasBudget) {
        throw 'Provider-backed execution requires -MaxCostUsd with a positive finite invariant-culture value.'
    }
    $rawBudget = Get-RawMaxCostUsd
    if ([string]::IsNullOrWhiteSpace($rawBudget) -or -not [regex]::IsMatch($rawBudget, '^[0-9]+(?:\.[0-9]+)?$')) {
        throw 'MaxCostUsd must use an invariant-culture decimal point.'
    }
    if ([double]::IsNaN($MaxCostUsd) -or [double]::IsInfinity($MaxCostUsd) -or $MaxCostUsd -le 0) {
        throw 'MaxCostUsd must be positive and finite.'
    }
    return $MaxCostUsd.ToString('0.00', [Globalization.CultureInfo]::InvariantCulture)
}

function Invoke-LiveDemoChild {
    param(
        [string]$Name,
        [string]$Command,
        [string]$Action,
        [string[]]$AdditionalArguments = @()
    )
    if (-not (Test-Path $Command -PathType Leaf)) {
        throw "$Name command is unavailable."
    }
    $global:LASTEXITCODE = 0
    & $Command $Action @AdditionalArguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Name $Action failed."
    }
}

$totalCostCeiling = Assert-PaidDemoAuthorization

# The Captain-owned helper runs docker compose config during its start action.
Invoke-LiveDemoChild -Name 'Captain services' -Command $ServiceCommand -Action 'start'
Invoke-LiveDemoChild -Name 'Captain services' -Command $ServiceCommand -Action 'status'
Invoke-LiveDemoChild -Name 'Minibook' -Command $MinibookCommand -Action 'start'
Invoke-LiveDemoChild -Name 'Minibook' -Command $MinibookCommand -Action 'status'

if ($RunDemo) {
    $capabilityArguments = @('-ConfirmProviderCost', '-MaxCostUsd', $totalCostCeiling)
    if (-not [string]::IsNullOrWhiteSpace($RecordingWindowTitle)) {
        $capabilityArguments += @('-RecordingWindowTitle', $RecordingWindowTitle)
    }
    Invoke-LiveDemoChild -Name 'Capability demo' -Command $CapabilityCommand -Action 'start' -AdditionalArguments $capabilityArguments
}

Write-Host '[ready] Live demo infrastructure is healthy'
