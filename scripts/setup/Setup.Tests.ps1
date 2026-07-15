BeforeAll {
    Import-Module "$PSScriptRoot/Common.psm1" -Force
    if (Test-Path "$PSScriptRoot/Preflight.psm1") {
        Import-Module "$PSScriptRoot/Preflight.psm1" -Force
    }
}

Describe 'Preflight' {
    It 'marks an absent executable as installable' {
        $result = Test-SetupExecutable -Name 'Python' -MinimumVersion '3.11' -Resolver { $null }

        $result.Status | Should -Be 'Missing'
        $result.Remediation | Should -Be 'Install'
    }

    It 'reports a port owner without killing it' {
        $result = Test-SetupPort -Port 3457 -ConnectionProvider { [pscustomobject]@{ OwningProcess = 4242 } }

        $result.Status | Should -Be 'Invalid'
        $result.Data.OwningProcess | Should -Be 4242
    }

    It 'uses only approved winget package identifiers' {
        $calls = [Collections.Generic.List[object]]::new()

        $result = Install-SetupPrerequisite -Name 'Git' -ConfirmInstall { $true } -CommandRunner {
            param($filePath, $argumentList)
            $calls.Add([pscustomobject]@{ FilePath = $filePath; ArgumentList = $argumentList })
            [pscustomobject]@{ ExitCode = 0; Output = 'ok' }
        }

        $result.Status | Should -Be 'Ready'
        $calls[0].FilePath | Should -Be 'winget'
        $calls[0].ArgumentList -join ' ' | Should -Match 'Git\.Git'
    }

    It 'requires at least four gigabytes of free disk space' {
        $result = Test-SetupDiskSpace -Path $TestDrive -DriveProvider { [pscustomobject]@{ Free = 3GB } }

        $result.Status | Should -Be 'Invalid'
        $result.Remediation | Should -Be 'Manual'
    }

    It 'reports unavailable package sources without changing the system' {
        $result = Test-SetupNetwork -Probe { $false }

        $result.Status | Should -Be 'Failed'
        $result.Remediation | Should -Be 'Retry'
    }

    It 'requires both Docker engine and Compose v2' {
        $runner = {
            param($filePath, $argumentList)
            if ($argumentList[0] -eq 'info') { return [pscustomobject]@{ ExitCode = 0; Output = 'ready' } }
            return [pscustomobject]@{ ExitCode = 0; Output = 'Docker Compose version v1.29.0' }
        }

        $result = Test-DockerRuntime -CommandRunner $runner

        $result.Status | Should -Be 'Invalid'
        $result.Remediation | Should -Be 'Install'
    }
}

Describe 'Common setup contracts' {
    It 'creates a stable result object' {
        $result = New-SetupResult -Component 'Python' -Status 'Missing' -Message 'Python 3.11 fehlt' -Remediation 'Install'

        $result.PSObject.Properties.Name | Should -Be @('Component', 'Status', 'Message', 'Remediation', 'Data')
        $result.Status | Should -Be 'Missing'
    }

    It 'redacts every supplied secret before writing a log' {
        $path = Join-Path $TestDrive 'setup.log'

        Write-SetupLog -Path $path -Message 'token=abc password=xyz' -Secrets @('abc', 'xyz')

        (Get-Content $path -Raw).Trim() | Should -Match 'token=\*\*\* password=\*\*\*$'
    }

    It 'round-trips a non-secret checkpoint' {
        $path = Join-Path $TestDrive 'checkpoint.json'

        Save-SetupCheckpoint -Path $path -Stages @{ Preflight = 'Complete' }

        (Get-SetupCheckpoint -Path $path).Preflight | Should -Be 'Complete'
    }
}
