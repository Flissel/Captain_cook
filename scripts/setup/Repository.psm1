Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'Common.psm1')

function Invoke-HermesRepositoryProbe {
    param(
        [Parameter(Mandatory)][scriptblock] $Probe,
        [Parameter(Mandatory)][string] $Root
    )

    try {
        [pscustomobject]@{
            Succeeded = $true
            Present = [bool](& $Probe $Root)
        }
    }
    catch {
        [pscustomobject]@{
            Succeeded = $false
            Present = $false
        }
    }
}

function New-HermesRepositoryProbeFailure {
    New-SetupResult -Component 'Repository' -Status 'Failed' `
        -Message 'Das Hermes-Submodul konnte nicht sicher geprüft werden.' -Remediation 'Retry'
}

function Initialize-SetupSubmodules {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Root,
        [scriptblock] $CommandRunner = {
            param($filePath, $argumentList, $workingDirectory)
            Common\Invoke-SetupCommand -FilePath $filePath -ArgumentList $argumentList -WorkingDirectory $workingDirectory
        },
        [scriptblock] $HermesProbe = {
            param($candidateRoot)
            Test-Path -LiteralPath (Join-Path $candidateRoot 'hermes-agent/pyproject.toml')
        }
    )

    $probeResult = Invoke-HermesRepositoryProbe -Probe $HermesProbe -Root $Root
    if (-not $probeResult.Succeeded) {
        return New-HermesRepositoryProbeFailure
    }
    if ($probeResult.Present) {
        return New-SetupResult -Component 'Repository' -Status 'Ready' `
            -Message 'Das Hermes-Submodul ist bereits vorhanden.' -Remediation 'None'
    }

    try {
        $commandResult = & $CommandRunner 'git' @('submodule', 'update', '--init', '--recursive') $Root
        if ($null -eq $commandResult -or
            $null -eq $commandResult.PSObject.Properties['ExitCode'] -or
            [int]$commandResult.ExitCode -ne 0) {
            return New-SetupResult -Component 'Repository' -Status 'Failed' `
                -Message 'Die Git-Submodule konnten nicht initialisiert werden.' -Remediation 'Retry'
        }
    }
    catch {
        return New-SetupResult -Component 'Repository' -Status 'Failed' `
            -Message 'Die Git-Submodule konnten nicht initialisiert werden.' -Remediation 'Retry'
    }

    $probeResult = Invoke-HermesRepositoryProbe -Probe $HermesProbe -Root $Root
    if (-not $probeResult.Succeeded) {
        return New-HermesRepositoryProbeFailure
    }
    if (-not $probeResult.Present) {
        return New-SetupResult -Component 'Repository' -Status 'Missing' `
            -Message 'Git meldet Erfolg, aber hermes-agent/pyproject.toml fehlt weiterhin.' `
            -Remediation 'Manual'
    }

    New-SetupResult -Component 'Repository' -Status 'Ready' `
        -Message 'Die deklarierten Git-Submodule sind initialisiert.' -Remediation 'None'
}

Export-ModuleMember -Function 'Initialize-SetupSubmodules'
