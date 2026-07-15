BeforeAll {
    Import-Module "$PSScriptRoot/Common.psm1" -Force
    if (Test-Path "$PSScriptRoot/Preflight.psm1") {
        Import-Module "$PSScriptRoot/Preflight.psm1" -Force
    }
    if (Test-Path "$PSScriptRoot/Configuration.psm1") {
        Import-Module "$PSScriptRoot/Configuration.psm1" -Force
    }
    if (Test-Path "$PSScriptRoot/Services.psm1") {
        Import-Module "$PSScriptRoot/Services.psm1" -Force
    }
}

Describe 'Services' {
    It 'does not activate owned n8n in external mode' {
        $calls = [Collections.Generic.List[object]]::new()

        $result = Start-CaptainServices -Root $TestDrive -N8nMode 'External' -CommandRunner {
            param($filePath, $argumentList)
            $calls.Add([pscustomobject]@{ FilePath = $filePath; ArgumentList = $argumentList })
            [pscustomobject]@{ ExitCode = 0; Output = 'ok' }
        }

        $result.Status | Should -Be 'Ready'
        $calls[0].ArgumentList -join ' ' | Should -Not -Match 'owned-n8n'
    }

    It 'never adds a destructive Docker volume argument when stopping' {
        $calls = [Collections.Generic.List[object]]::new()

        Stop-CaptainServices -Root $TestDrive -N8nMode 'Owned' -CommandRunner {
            param($filePath, $argumentList)
            $calls.Add([pscustomobject]@{ FilePath = $filePath; ArgumentList = $argumentList })
            [pscustomobject]@{ ExitCode = 0; Output = 'ok' }
        } | Out-Null

        $arguments = $calls[0].ArgumentList -join ' '
        $arguments | Should -Match 'stop'
        $arguments | Should -Not -Match 'down|-v|volume|rm'
    }

    It 'reports an unhealthy public endpoint as retryable' {
        $result = Test-HttpService -Name 'Mailpit' -Uri 'http://localhost:8025/api/v1/info' -Probe { $false }

        $result.Status | Should -Be 'Failed'
        $result.Remediation | Should -Be 'Retry'
    }
}

Describe 'Configuration' {
    It 'preserves existing values and round-trips special characters' {
        $path = Join-Path $TestDrive '.env'
        Set-Content -LiteralPath $path -Value 'EXISTING=keep'

        Write-DotEnv -Path $path -Values @{ EXISTING = 'keep'; DB_PASSWORD = "a# b`"c" }

        $values = Read-DotEnv -Path $path
        $values.EXISTING | Should -Be 'keep'
        $values.DB_PASSWORD | Should -Be "a# b`"c"
    }

    It 'rejects a secret path tracked by Git' {
        $result = Test-TrackedSecretPath -Path '.env' -GitRunner { param($arguments) [pscustomobject]@{ ExitCode = 0; Output = '.env' } }

        $result | Should -BeFalse
    }

    It 'never adopts an external n8n endpoint without consent' {
        $mode = Resolve-N8nMode -Url 'http://localhost:15678' -Probe { $true } -ConfirmAdoption { $false }

        $mode | Should -Be 'Owned'
    }

    It 'generates a strong URL-safe secret' {
        $secret = New-SetupSecret

        $secret.Length | Should -BeGreaterOrEqual 40
        $secret | Should -Match '^[A-Za-z0-9_-]+$'
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
