#requires -Version 7.0
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Import-Module (Join-Path $root 'scripts/setup/Lifecycle.psm1') -Force
$result = Start-CaptainSystem -Root $root
Write-Host $result.Message
if ($result.Status -ne 'Ready') { exit 1 }
