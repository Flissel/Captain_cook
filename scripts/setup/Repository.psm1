Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'Common.psm1')

function Invoke-HermesRepositoryProbe {
    param(
        [AllowNull()][object] $Probe,
        [Parameter(Mandatory)][string] $Root
    )

    if ($Probe -isnot [scriptblock]) {
        return [pscustomobject]@{
            Succeeded = $false
            Present = $false
        }
    }

    try {
        $probeOutput = @(& $Probe $Root)
    }
    catch {
        return [pscustomobject]@{
            Succeeded = $false
            Present = $false
        }
    }

    if ($probeOutput.Count -ne 1 -or $probeOutput[0] -isnot [bool]) {
        return [pscustomobject]@{
            Succeeded = $false
            Present = $false
        }
    }

    [pscustomobject]@{
        Succeeded = $true
        Present = $probeOutput[0]
    }
}

function New-HermesRepositoryProbeFailure {
    New-SetupResult -Component 'Repository' -Status 'Failed' `
        -Message 'Das Hermes-Submodul konnte nicht sicher geprüft werden.' -Remediation 'Retry'
}

function Test-SetupIntegerScalar {
    param([AllowNull()][object] $Value)

    $Value -is [sbyte] -or
    $Value -is [byte] -or
    $Value -is [int16] -or
    $Value -is [uint16] -or
    $Value -is [int32] -or
    $Value -is [uint32] -or
    $Value -is [int64] -or
    $Value -is [uint64]
}

function Initialize-SetupSubmodules {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Root,
        [scriptblock] $CommandRunner = {
            param($filePath, $argumentList, $workingDirectory)
            Common\Invoke-SetupCommand -FilePath $filePath -ArgumentList $argumentList -WorkingDirectory $workingDirectory
        },
        [AllowNull()][object] $HermesProbe = {
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
        $commandOutput = @(& $CommandRunner 'git' @('submodule', 'update', '--init', '--recursive') $Root)
        $commandResult = if ($commandOutput.Count -eq 1) { $commandOutput[0] } else { $null }
        $exitCodeProperty = if ($null -eq $commandResult) {
            $null
        }
        else {
            $commandResult.PSObject.Properties['ExitCode']
        }
        if ($null -eq $exitCodeProperty -or
            -not (Test-SetupIntegerScalar -Value $exitCodeProperty.Value) -or
            $exitCodeProperty.Value -ne 0) {
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
