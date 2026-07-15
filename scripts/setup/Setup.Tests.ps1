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
    if (Test-Path "$PSScriptRoot/Components.psm1") {
        Import-Module "$PSScriptRoot/Components.psm1" -Force
    }
    if (Test-Path "$PSScriptRoot/Lifecycle.psm1") {
        Import-Module "$PSScriptRoot/Lifecycle.psm1" -Force
    }
}

Describe 'Guided orchestration' {
    It 'resumes at the first incomplete stage' {
        $visited = [Collections.Generic.List[string]]::new()

        $result = Invoke-GuidedSetup -Root $TestDrive -Checkpoint @{ Preflight = 'Complete'; Configuration = 'Incomplete' } -StageRunner {
            param($stage, $context)
            $visited.Add($stage)
            [pscustomobject]@{ Status = 'Complete'; Message = "$stage complete" }
        }

        $result.Status | Should -Be 'Ready'
        $visited[0] | Should -Be 'Configuration'
        $visited | Should -Not -Contain 'Preflight'
    }

    It 'does not report success when verification fails' {
        $result = Invoke-GuidedSetup -Root $TestDrive -Checkpoint @{} -StageRunner {
            param($stage, $context)
            if ($stage -eq 'Verification') { return [pscustomobject]@{ Status = 'Failed'; Message = 'verification failed' } }
            [pscustomobject]@{ Status = 'Complete'; Message = "$stage complete" }
        }

        $result.Status | Should -Be 'Failed'
        $result.Remediation | Should -Be 'Retry'
    }

    It 'uses the exact stable stage order' {
        Get-SetupStages | Should -Be @('Preflight', 'Configuration', 'Captain', 'Hermes', 'Minibook', 'Services', 'Verification')
    }

    It 'initializes missing local secrets without replacing existing values' {
        Set-Content -LiteralPath (Join-Path $TestDrive '.env.example') -Value @(
            'CAPTAIN_TIMEZONE=Europe/Berlin'
            'MARIADB_PASSWORD='
            'MARIADB_ROOT_PASSWORD='
            'N8N_MODE=owned'
        )
        Set-Content -LiteralPath (Join-Path $TestDrive '.env') -Value 'MARIADB_PASSWORD="keep-me"'

        $result = Initialize-SetupConfiguration -Root $TestDrive -SecretPathValidator { $true }

        $result.Status | Should -Be 'Ready'
        $values = Read-DotEnv -Path (Join-Path $TestDrive '.env')
        $values.MARIADB_PASSWORD | Should -Be 'keep-me'
        $values.MARIADB_ROOT_PASSWORD.Length | Should -BeGreaterOrEqual 40
    }

    It 'reports incomplete when any supplied health probe fails' {
        $result = Get-CaptainSystemStatus -Root $TestDrive -HealthProbes @(
            { New-SetupResult -Component 'Captain' -Status 'Ready' -Message 'ok' -Remediation 'None' },
            { New-SetupResult -Component 'Minibook' -Status 'Failed' -Message 'down' -Remediation 'Retry' }
        )

        $result.Status | Should -Be 'Failed'
        $result.Data.Results.Count | Should -Be 2
    }
}

Describe 'Lifecycle entry points' {
    It 'provides every root command' {
        $root = Resolve-Path "$PSScriptRoot/../.."

        foreach ($scriptName in @('setup.ps1', 'start.ps1', 'stop.ps1', 'status.ps1', 'repair.ps1')) {
            Test-Path (Join-Path $root $scriptName) | Should -BeTrue
        }
    }
}

Describe 'Components' {
    It 'skips Captain dependency installation after a healthy check' {
        $calls = [Collections.Generic.List[object]]::new()

        $result = Install-Captain -Root $TestDrive -HealthCheck { $true } -CommandRunner {
            param($filePath, $argumentList, $workingDirectory)
            $calls.Add($filePath)
        }

        $result.Status | Should -Be 'Ready'
        $calls.Count | Should -Be 0
    }

    It 'installs Hermes from the checked-out local source' {
        New-Item -ItemType Directory -Path (Join-Path $TestDrive 'hermes-agent') | Out-Null
        Set-Content -LiteralPath (Join-Path $TestDrive 'hermes-agent/pyproject.toml') -Value '[project]'
        $calls = [Collections.Generic.List[object]]::new()

        $result = Install-Hermes -Root $TestDrive -HealthCheck { $false } -CommandRunner {
            param($filePath, $argumentList, $workingDirectory)
            $calls.Add([pscustomobject]@{ FilePath = $filePath; ArgumentList = $argumentList })
            [pscustomobject]@{ ExitCode = 0; Output = 'ok' }
        }

        $result.Status | Should -Be 'Ready'
        $allArguments = ($calls.ArgumentList | ForEach-Object { $_ -join ' ' }) -join "`n"
        $allArguments | Should -Match '--editable'
        $allArguments | Should -Match 'hermes-agent'
        $allArguments | Should -Not -Match 'https?://'
    }

    It 'registers Hermes only when no valid Minibook identity exists' {
        $registrations = 0

        $result = Register-HermesIdentity -CurrentIdentityProbe { $true } -RegistrationRequest { $script:registrations++ }

        $result.Status | Should -Be 'Ready'
        $registrations | Should -Be 0
    }

    It 'copies the Minibook skill to the user profile' {
        $source = Join-Path $TestDrive 'source/SKILL.md'
        $destination = Join-Path $TestDrive 'profile/skills/minibook'
        New-Item -ItemType Directory -Path (Split-Path $source -Parent) | Out-Null
        Set-Content -LiteralPath $source -Value '# Minibook'

        $result = Install-MinibookSkill -Source $source -DestinationDirectory $destination

        $result.Status | Should -Be 'Ready'
        (Get-Content (Join-Path $destination 'SKILL.md') -Raw) | Should -Match 'Minibook'
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
