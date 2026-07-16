#requires -Version 7.0
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'scripts/setup/Lifecycle.psm1') -Force
$result = Repair-CaptainSystem -Root $PSScriptRoot
Write-Host $result.Message
if ($result.Status -ne 'Ready') { exit 1 }
