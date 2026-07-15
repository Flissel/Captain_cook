#requires -Version 7.0
[CmdletBinding()]
param([switch] $Detailed)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Import-Module (Join-Path $root 'scripts/setup/Lifecycle.psm1') -Force
$result = Get-CaptainSystemStatus -Root $root
Write-Host $result.Message
if ($Detailed) { $result.Data.Results | Format-Table Component,Status,Message -AutoSize }
if ($result.Status -ne 'Ready') { exit 1 }
