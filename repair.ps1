#requires -Version 7.0
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$checkpoint = Join-Path $PSScriptRoot '.captain-cook/checkpoint.json'
if (Test-Path $checkpoint) {
    $state = Get-Content $checkpoint -Raw | ConvertFrom-Json -AsHashtable
    foreach ($key in @($state.Keys)) { if ($state[$key] -ne 'Complete') { $state.Remove($key) } }
    $state | ConvertTo-Json | Set-Content $checkpoint -Encoding utf8
}
& (Join-Path $PSScriptRoot 'setup.ps1')
exit $LASTEXITCODE
