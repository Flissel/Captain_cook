Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'Common.psm1')
Import-Module (Join-Path $PSScriptRoot 'Configuration.psm1')
Import-Module (Join-Path $PSScriptRoot 'Preflight.psm1')
Import-Module (Join-Path $PSScriptRoot 'Services.psm1')

function Test-MinibookInstallation {
    param([Parameter(Mandatory)][string] $Root)

    (Test-Path -LiteralPath (Join-Path $Root 'minibook/.venv/Scripts/python.exe')) -and
        (Test-Path -LiteralPath (Join-Path $Root 'minibook/frontend/.next'))
}

function Get-CaptainServiceHealth {
    param([Parameter(Mandatory)][string] $Root)

    $configuration = Read-DotEnv -Path (Join-Path $Root '.env')
    $results = @(Get-ServiceHealth -Configuration $configuration)
    $failed = @($results | Where-Object Status -ne 'Ready')
    if ($failed.Count -gt 0) {
        return New-SetupResult -Component 'Services' -Status 'Failed' -Message ($failed.Message -join ' ') -Remediation 'Retry' -Data @{ Results = $results }
    }
    New-SetupResult -Component 'Services' -Status 'Ready' -Message 'Alle Captain-Dienste sind gesund.' -Remediation 'None' -Data @{ Results = $results }
}

function Get-InvalidatedSetupStages {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string[]] $Stages,
        [Parameter(Mandatory)][string] $FirstInvalidStage
    )

    $index = [Array]::IndexOf($Stages, $FirstInvalidStage)
    if ($index -lt 0) { throw "Unbekannte Setup-Stage: $FirstInvalidStage" }
    @($Stages[$index..($Stages.Count - 1)])
}

function Test-SetupStage {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Stage,
        [Parameter(Mandatory)][hashtable] $Context
    )

    switch ($Stage) {
        'Preflight' { return -not (@(Get-PreflightResults) | Where-Object Status -ne 'Ready') }
        'Configuration' { return Test-Path -LiteralPath (Join-Path $Context.Root '.env') }
        'Captain' { return Test-Path -LiteralPath (Join-Path $Context.Root '.captain-cook/demo-run.json') }
        'Hermes' { return Test-Path -LiteralPath (Join-Path $Context.Root '.captain-cook/hermes/Scripts/hermes.exe') }
        'Minibook' { return (Test-MinibookInstallation -Root $Context.Root) }
        'Services' { return (Get-CaptainServiceHealth -Root $Context.Root).Status -eq 'Ready' }
        'Verification' { return (Get-CaptainSystemStatus -Root $Context.Root).Status -eq 'Ready' }
        default { throw "Unbekannte Setup-Stage: $Stage" }
    }
}

Export-ModuleMember -Function @('Get-InvalidatedSetupStages', 'Test-SetupStage')
