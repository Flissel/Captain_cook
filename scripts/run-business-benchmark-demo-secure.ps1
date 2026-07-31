#requires -Version 7
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$PythonPath = '',

    [ValidateSet('Build', 'Run')]
    [string]$Action = 'Run'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$demoRunner = Join-Path $PSScriptRoot 'run-business-benchmark-demo.ps1'
$secureKey = $null
$keyPointer = [IntPtr]::Zero
$plainKey = $null
$exitCode = 1

try {
    $secureKey = Read-Host 'OPENAI_API_KEY' -AsSecureString
    $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
    if ([string]::IsNullOrWhiteSpace($plainKey)) {
        throw 'OPENAI_API_KEY must not be empty.'
    }
    [Environment]::SetEnvironmentVariable('OPENAI_API_KEY', $plainKey, 'Process')
    & $demoRunner -Action $Action -PythonPath $PythonPath
    $exitCode = $LASTEXITCODE
}
finally {
    $nullString = [System.Management.Automation.Language.NullString]::Value
    [Environment]::SetEnvironmentVariable('OPENAI_API_KEY', $nullString, 'Process')
    $plainKey = $null
    if ($keyPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
    }
    if ($null -ne $secureKey) {
        $secureKey.Dispose()
    }
}

exit $exitCode
