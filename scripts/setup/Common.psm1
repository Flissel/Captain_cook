Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function New-SetupResult {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string] $Component,

        [Parameter(Mandatory)]
        [ValidateSet('Ready', 'Missing', 'Invalid', 'Failed', 'Skipped', 'RestartRequired')]
        [string] $Status,

        [Parameter(Mandatory)]
        [string] $Message,

        [Parameter(Mandatory)]
        [ValidateSet('None', 'Install', 'Configure', 'Retry', 'Restart', 'Manual')]
        [string] $Remediation,

        [hashtable] $Data = @{}
    )

    [pscustomobject][ordered]@{
        Component   = $Component
        Status      = $Status
        Message     = $Message
        Remediation = $Remediation
        Data        = $Data
    }
}

function Invoke-SetupCommand {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string] $FilePath,

        [string[]] $ArgumentList = @(),

        [string] $WorkingDirectory = $PWD.Path
    )

    Push-Location $WorkingDirectory
    try {
        $output = & $FilePath @ArgumentList 2>&1
        [pscustomobject]@{
            ExitCode = $LASTEXITCODE
            Output   = $output -join [Environment]::NewLine
        }
    }
    finally {
        Pop-Location
    }
}

function Write-SetupLog {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string] $Path,

        [Parameter(Mandatory)]
        [string] $Message,

        [string[]] $Secrets = @()
    )

    $safeMessage = $Message
    foreach ($secret in ($Secrets | Where-Object { -not [string]::IsNullOrEmpty($_) })) {
        $safeMessage = $safeMessage.Replace($secret, '***')
    }

    $directory = Split-Path $Path -Parent
    if (-not [string]::IsNullOrEmpty($directory)) {
        New-Item -ItemType Directory -Force -Path $directory | Out-Null
    }
    Add-Content -LiteralPath $Path -Value "$(Get-Date -Format o) $safeMessage"
}

function Save-SetupCheckpoint {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string] $Path,

        [Parameter(Mandatory)]
        [hashtable] $Stages
    )

    $directory = Split-Path $Path -Parent
    if (-not [string]::IsNullOrEmpty($directory)) {
        New-Item -ItemType Directory -Force -Path $directory | Out-Null
    }
    $Stages | ConvertTo-Json | Set-Content -LiteralPath $Path -Encoding utf8
}

function Get-SetupCheckpoint {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Path)

    if (Test-Path -LiteralPath $Path) {
        return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    }
    return [pscustomobject]@{}
}

function Test-InteractiveSession {
    [CmdletBinding()]
    param()

    [Environment]::UserInteractive -and -not [Console]::IsInputRedirected
}

Export-ModuleMember -Function @(
    'New-SetupResult',
    'Invoke-SetupCommand',
    'Write-SetupLog',
    'Save-SetupCheckpoint',
    'Get-SetupCheckpoint',
    'Test-InteractiveSession'
)
