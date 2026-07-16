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
    if (Test-Path "$PSScriptRoot/StageValidation.psm1") {
        Import-Module "$PSScriptRoot/StageValidation.psm1" -Force
    }
    if (Test-Path "$PSScriptRoot/Lifecycle.psm1") {
        Import-Module "$PSScriptRoot/Lifecycle.psm1" -Force
    }
}

Describe 'Guided orchestration' {
    It 'resumes at the first incomplete stage' {
        $visited = [Collections.Generic.List[string]]::new()

        $result = Invoke-GuidedSetup -Root $TestDrive -Checkpoint @{ Preflight = 'Complete'; Configuration = 'Incomplete' } -StageValidator { $true } -StageRunner {
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
            'N8N_MODE=external'
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

Describe 'Checkpoint revalidation and targeted repair' {
    It 'returns the first invalid stage and every successor' {
        Get-InvalidatedSetupStages -Stages (Get-SetupStages) -FirstInvalidStage 'Hermes' |
            Should -Be @('Hermes', 'Minibook', 'Services', 'Verification')
    }

    It 'rejects an unknown invalid stage' {
        { Get-InvalidatedSetupStages -Stages (Get-SetupStages) -FirstInvalidStage 'Unknown' } |
            Should -Throw '*Unbekannte Setup-Stage: Unknown*'
    }

    It 'validates file-backed stages inside the supplied root' {
        New-Item -ItemType File -Force -Path (Join-Path $TestDrive '.env') | Out-Null
        New-Item -ItemType File -Force -Path (Join-Path $TestDrive '.captain-cook/demo-run.json') | Out-Null
        $hermes = Join-Path $TestDrive '.captain-cook/hermes/Scripts/hermes.exe'
        New-Item -ItemType File -Force -Path $hermes | Out-Null
        $minibookPython = Join-Path $TestDrive 'minibook/.venv/Scripts/python.exe'
        $minibookBuild = Join-Path $TestDrive 'minibook/frontend/.next'
        New-Item -ItemType File -Force -Path $minibookPython | Out-Null
        New-Item -ItemType Directory -Force -Path $minibookBuild | Out-Null
        $context = @{ Root = $TestDrive }

        Test-SetupStage -Stage 'Configuration' -Context $context | Should -BeTrue
        Test-SetupStage -Stage 'Captain' -Context $context | Should -BeTrue
        Test-SetupStage -Stage 'Hermes' -Context $context | Should -BeTrue
        Test-SetupStage -Stage 'Minibook' -Context $context | Should -BeTrue

        Remove-Item -LiteralPath $hermes
        Test-SetupStage -Stage 'Hermes' -Context $context | Should -BeFalse
        Remove-Item -LiteralPath $minibookBuild
        Test-SetupStage -Stage 'Minibook' -Context $context | Should -BeFalse
    }

    It 'exports only the public stage-validation contract' {
        @((Get-Command -Module StageValidation).Name | Sort-Object) |
            Should -Be @('Get-InvalidatedSetupStages', 'Test-SetupStage')
        { Test-SetupStage -Stage 'Unknown' -Context @{ Root = $TestDrive } } |
            Should -Throw '*Unbekannte Setup-Stage: Unknown*'
    }

    It 'fails closed when standalone verification has no status provider' {
        $modulePath = (Resolve-Path "$PSScriptRoot/StageValidation.psm1").Path.Replace("'", "''")
        $probe = @"
`$ErrorActionPreference = 'Stop'
Import-Module '$modulePath' -Force
`$result = Test-SetupStage -Stage 'Verification' -Context @{ Root = 'unused' }
if (`$result -ne `$false) { exit 11 }
`$invalidProviderResult = Test-SetupStage -Stage 'Verification' -Context @{ Root = 'unused'; SystemStatusProvider = 'not-a-scriptblock' }
if (`$invalidProviderResult -ne `$false) { exit 12 }
"@

        $output = & pwsh -NoProfile -Command $probe 2>&1
        $exitCode = $LASTEXITCODE

        $exitCode | Should -Be 0
        $output -join "`n" | Should -Not -Match 'Get-CaptainSystemStatus|CommandNotFoundException'
    }

    It 'aggregates existing service-health results without starting services' {
        InModuleScope StageValidation -Parameters @{ CandidateRoot = $TestDrive } {
            Mock Read-DotEnv { @{ MAILPIT_WEB_PORT = '8025'; MAILPIT_SMTP_PORT = '1025'; N8N_URL = 'http://localhost:5678' } }
            Mock Get-ServiceHealth {
                @(
                    New-SetupResult -Component 'Mailpit' -Status 'Ready' -Message 'ok' -Remediation 'None'
                    New-SetupResult -Component 'n8n' -Status 'Failed' -Message 'down' -Remediation 'Retry'
                )
            }

            $result = Get-CaptainServiceHealth -Root $CandidateRoot

            $result.Status | Should -Be 'Failed'
            $result.Data.Results.Count | Should -Be 2
            $result.Message | Should -Be 'down'
            Test-SetupStage -Stage 'Services' -Context @{ Root = $CandidateRoot } |
                Should -BeFalse
            Should -Invoke Get-ServiceHealth -Times 2 -Exactly

            $providerState = [pscustomobject]@{ Calls = 0 }
            $systemStatusProvider = {
                param($root)
                $providerState.Calls++
                New-SetupResult -Component 'System' -Status 'Ready' -Message 'ok' -Remediation 'None'
            }
            Test-SetupStage -Stage 'Verification' -Context @{
                Root = $CandidateRoot
                SystemStatusProvider = $systemStatusProvider
            } |
                Should -BeTrue
            $providerState.Calls | Should -Be 1
        }
    }

    It 'revalidates completed stages and persists invalidation before rerunning successors' {
        $called = [Collections.Generic.List[string]]::new()
        $validated = [Collections.Generic.List[string]]::new()
        $observed = [pscustomobject]@{ Checkpoint = $null }
        $checkpoint = @{}
        Get-SetupStages | ForEach-Object { $checkpoint[$_] = 'Complete' }
        $checkpointPath = Join-Path $TestDrive '.captain-cook/checkpoint.json'
        Save-SetupCheckpoint -Path $checkpointPath -Stages $checkpoint

        $result = Invoke-GuidedSetup -Root $TestDrive -Checkpoint $checkpoint `
            -StageValidator {
                param($stage, $context)
                [void]$validated.Add($stage)
                $stage -ne 'Minibook'
            } `
            -StageRunner {
                param($stage, $context)
                if ($null -eq $observed.Checkpoint) {
                    $observed.Checkpoint = Get-SetupCheckpoint -Path $checkpointPath
                }
                [void]$called.Add($stage)
                [pscustomobject]@{ Status = 'Complete'; Message = 'ok' }
            }

        $result.Status | Should -Be 'Ready'
        $validated | Should -Be @('Preflight', 'Configuration', 'Captain', 'Hermes', 'Minibook')
        $called | Should -Be @('Minibook', 'Services', 'Verification')
        $observed.Checkpoint.Captain | Should -Be 'Complete'
        @($observed.Checkpoint.PSObject.Properties.Name) | Should -Not -Contain 'Minibook'
        @($observed.Checkpoint.PSObject.Properties.Name) | Should -Not -Contain 'Verification'
    }

    It 'does not rerun completed stages when every validator succeeds' {
        $validated = [Collections.Generic.List[string]]::new()
        $checkpoint = @{}
        Get-SetupStages | ForEach-Object { $checkpoint[$_] = 'Complete' }

        $result = Invoke-GuidedSetup -Root $TestDrive -Checkpoint $checkpoint `
            -StageValidator {
                param($stage, $context)
                [void]$validated.Add($stage)
                $true
            } `
            -StageRunner { throw 'A healthy completed stage must not rerun.' }

        $result.Status | Should -Be 'Ready'
        $validated | Should -Be (Get-SetupStages)
    }

    It 'preserves the positional StageRunner facade' {
        $stageRunnerPosition = @(
            (Get-Command Invoke-GuidedSetup).Parameters.StageRunner.Attributes |
                Where-Object { $_ -is [Management.Automation.ParameterAttribute] }
        )[0].Position
        $called = [Collections.Generic.List[string]]::new()
        $runner = {
            param($stage, $context)
            [void]$called.Add($stage)
            [pscustomobject]@{ Status = 'Complete'; Message = 'ok' }
        }

        $result = Invoke-GuidedSetup $TestDrive @{} $runner -StageValidator { $true }

        $stageRunnerPosition | Should -Be 2
        $result.Status | Should -Be 'Ready'
        $called | Should -Be (Get-SetupStages)
    }

    It 'repairs from the first broken completed stage and preserves stable result data' {
        $checkpoint = @{}
        Get-SetupStages | ForEach-Object { $checkpoint[$_] = 'Complete' }
        $checkpointPath = Join-Path $TestDrive '.captain-cook/checkpoint.json'
        Save-SetupCheckpoint -Path $checkpointPath -Stages $checkpoint
        $observed = [pscustomobject]@{ Checkpoint = $null; RunnerCalls = 0 }

        $result = Repair-CaptainSystem -Root $TestDrive -StageValidator {
            param($stage, $context)
            $stage -ne 'Hermes'
        } -SetupRunner {
            $observed.RunnerCalls++
            $observed.Checkpoint = Get-SetupCheckpoint -Path $checkpointPath
            New-SetupResult -Component 'Setup' -Status 'Ready' -Message 'repaired' -Remediation 'None' -Data @{ Existing = 'kept' }
        }

        $result.PSObject.Properties.Name | Should -Be @('Component', 'Status', 'Message', 'Remediation', 'Data')
        $result.Status | Should -Be 'Ready'
        $result.Data.Existing | Should -Be 'kept'
        $result.Data.InvalidatedStages | Should -Be @('Hermes', 'Minibook', 'Services', 'Verification')
        $observed.RunnerCalls | Should -Be 1
        $observed.Checkpoint.Captain | Should -Be 'Complete'
        @($observed.Checkpoint.PSObject.Properties.Name) | Should -Not -Contain 'Hermes'
        @($observed.Checkpoint.PSObject.Properties.Name) | Should -Not -Contain 'Verification'
    }

    It 'fails closed for null and malformed setup-runner results' {
        $cases = @(
            [pscustomobject]@{ Name = 'null'; Value = $null }
            [pscustomobject]@{
                Name = 'missing Data'
                Value = [pscustomobject]@{
                    Component = 'Setup'
                    Status = 'Ready'
                    Message = 'incomplete contract'
                    Remediation = 'None'
                }
            }
        )

        for ($index = 0; $index -lt $cases.Count; $index++) {
            $case = $cases[$index]
            $caseRoot = Join-Path $TestDrive "runner-result-$index"
            $checkpoint = @{}
            Get-SetupStages | ForEach-Object { $checkpoint[$_] = 'Complete' }
            Save-SetupCheckpoint -Path (Join-Path $caseRoot '.captain-cook/checkpoint.json') -Stages $checkpoint
            $runnerValue = $case.Value
            $observed = [pscustomobject]@{ Result = $null }

            {
                $observed.Result = Repair-CaptainSystem -Root $caseRoot `
                    -StageValidator { param($stage, $context) $stage -ne 'Hermes' } `
                    -SetupRunner { $runnerValue }
            } | Should -Not -Throw -Because $case.Name

            $observed.Result.PSObject.Properties.Name |
                Should -Be @('Component', 'Status', 'Message', 'Remediation', 'Data')
            $observed.Result.Component | Should -Be 'Setup'
            $observed.Result.Status | Should -Be 'Failed'
            $observed.Result.Remediation | Should -Be 'Retry'
            $observed.Result.Message | Should -Match 'Setup-Runner'
            $observed.Result.Data.InvalidatedStages |
                Should -Be @('Hermes', 'Minibook', 'Services', 'Verification')
        }
    }

    It 'is idempotent when every completed stage remains healthy' {
        $checkpoint = @{}
        Get-SetupStages | ForEach-Object { $checkpoint[$_] = 'Complete' }
        $checkpointPath = Join-Path $TestDrive '.captain-cook/checkpoint.json'
        Save-SetupCheckpoint -Path $checkpointPath -Stages $checkpoint
        $before = Get-Content -LiteralPath $checkpointPath -Raw
        $runnerState = [pscustomobject]@{ Calls = 0 }

        $result = Repair-CaptainSystem -Root $TestDrive -StageValidator { $true } -SetupRunner {
            $runnerState.Calls++
            New-SetupResult -Component 'Setup' -Status 'Ready' -Message 'already healthy' -Remediation 'None'
        }

        $result.Status | Should -Be 'Ready'
        @($result.Data.InvalidatedStages).Count | Should -Be 0
        $runnerState.Calls | Should -Be 1
        (Get-Content -LiteralPath $checkpointPath -Raw) | Should -BeExactly $before
    }

    It 'keeps repair.ps1 as a thin lifecycle facade' {
        $source = Get-Content "$PSScriptRoot/../../repair.ps1" -Raw

        $source | Should -Match 'Import-Module.*scripts/setup/Lifecycle\.psm1'
        $source | Should -Match 'Repair-CaptainSystem\s+-Root\s+\$PSScriptRoot'
        $source | Should -Match 'Write-Host\s+\$result\.Message'
        $source | Should -Not -Match 'ConvertFrom-Json|Set-Content|\.Remove\('
    }
}

Describe 'Lifecycle entry points' {
    It 'provides every root command' {
        $root = Resolve-Path "$PSScriptRoot/../.."

        foreach ($scriptName in @('setup.ps1', 'start.ps1', 'stop.ps1', 'status.ps1', 'repair.ps1')) {
            Test-Path (Join-Path $root $scriptName) | Should -BeTrue
        }
    }

    It 'prepares the Captain demo before starting dependent services' {
        (Get-Command Start-CaptainSystem).Definition | Should -Match 'Install-Captain'
    }
}

Describe 'Onboarding documentation' {
    It 'documents the only setup command and all lifecycle commands' {
        $readme = Get-Content "$PSScriptRoot/../../README.md" -Raw

        foreach ($command in @('.\setup.ps1', '.\start.ps1', '.\stop.ps1', '.\status.ps1', '.\repair.ps1')) {
            $readme | Should -Match ([regex]::Escape($command))
        }
    }

    It 'warns that lifecycle commands never delete Docker volumes' {
        $readme = Get-Content "$PSScriptRoot/../../README.md" -Raw

        $readme | Should -Match 'Docker-Volumes.*nie.*gelöscht'
    }

    It 'ships an executable acceptance script' {
        Test-Path "$PSScriptRoot/../acceptance/setup-smoke.ps1" | Should -BeTrue
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

    It 'does not treat a committed demo artifact as an installed Captain runtime' {
        New-Item -ItemType Directory -Force -Path (Join-Path $TestDrive 'artifacts') | Out-Null
        Set-Content -LiteralPath (Join-Path $TestDrive 'artifacts/demo-run.json') -Value '{}'
        $calls = [Collections.Generic.List[string]]::new()

        Install-Captain -Root $TestDrive -CommandRunner {
            param($commandPath, $commandArguments, $commandDirectory)
            $calls.Add($commandPath)
            [pscustomobject]@{ ExitCode = 0; Output = 'ok' }
        } | Out-Null

        $calls.Count | Should -BeGreaterThan 0
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

    It 'uses the Windows npm command shim for Minibook' {
        New-Item -ItemType Directory -Force -Path (Join-Path $TestDrive 'minibook/frontend') | Out-Null
        Set-Content -LiteralPath (Join-Path $TestDrive 'minibook/requirements.txt') -Value 'fastapi'
        Set-Content -LiteralPath (Join-Path $TestDrive 'minibook/frontend/package-lock.json') -Value '{}'
        $calls = [Collections.Generic.List[object]]::new()

        Install-Minibook -Root $TestDrive -CommandRunner {
            param($commandPath, $commandArguments, $commandDirectory)
            $calls.Add([pscustomobject]@{ FilePath = $commandPath; Arguments = $commandArguments })
            [pscustomobject]@{ ExitCode = 0; Output = 'ok' }
        } | Out-Null

        @($calls.FilePath | Where-Object { $_ -like 'npm*' } | Select-Object -Unique) | Should -Be @('npm.cmd')
    }

    It 'skips an already built Minibook installation by default' {
        New-Item -ItemType Directory -Force -Path (Join-Path $TestDrive 'minibook/.venv/Scripts') | Out-Null
        New-Item -ItemType Directory -Force -Path (Join-Path $TestDrive 'minibook/frontend/.next') | Out-Null
        New-Item -ItemType File -Force -Path (Join-Path $TestDrive 'minibook/.venv/Scripts/python.exe') | Out-Null
        $calls = [Collections.Generic.List[string]]::new()

        $result = Install-Minibook -Root $TestDrive -CommandRunner {
            param($commandPath, $commandArguments, $commandDirectory)
            $calls.Add($commandPath)
            [pscustomobject]@{ ExitCode = 0; Output = 'ok' }
        }

        $result.Status | Should -Be 'Ready'
        $calls.Count | Should -Be 0
    }

    It 'writes the Minibook backend configuration from setup values' {
        $result = Initialize-MinibookConfiguration -Root $TestDrive -BackendUrl 'http://localhost:3456' -PublicUrl 'http://localhost:3457'

        $result.Status | Should -Be 'Ready'
        $content = Get-Content (Join-Path $TestDrive 'minibook/config.yaml') -Raw
        $content | Should -Match 'port: 3456'
        $content | Should -Match 'public_url: "http://localhost:3457"'
    }

    It 'offers a safe alternate Minibook agent name when Hermes is taken' {
        $name = Get-AvailableMinibookAgentName -BaseUrl 'http://localhost:3457' -PreferredName 'Hermes' -AgentListProvider {
            Write-Output -NoEnumerate @([pscustomobject]@{ name = 'Hermes' })
        } -AlternateNameProvider { 'Hermes-Captain-PC' }

        $name | Should -Be 'Hermes-Captain-PC'
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
        $result = Test-HttpService -Name 'Mailpit' -Uri 'http://localhost:8025/api/v1/info' -AttemptCount 1 -Probe { $false }

        $result.Status | Should -Be 'Failed'
        $result.Remediation | Should -Be 'Retry'
    }

    It 'retries a transient public endpoint failure' {
        $state = [pscustomobject]@{ Attempts = 0 }

        $result = Test-HttpService -Name 'Mailpit' -Uri 'http://localhost:8025/api/v1/info' -AttemptCount 3 -DelayMilliseconds 0 -Probe {
            $state.Attempts++
            $state.Attempts -ge 2
        }

        $result.Status | Should -Be 'Ready'
        $state.Attempts | Should -Be 2
    }

    It 'does not expose the database password in process arguments' {
        $calls = [Collections.Generic.List[object]]::new()

        Test-MariaDbService -Root $TestDrive -User 'captain' -Password 'top-secret-value' -CommandRunner {
            param($filePath, $argumentList)
            $calls.Add([pscustomobject]@{ ArgumentList = $argumentList })
            [pscustomobject]@{ ExitCode = 0; Output = '1' }
        } | Out-Null

        $calls[0].ArgumentList -join ' ' | Should -Not -Match 'top-secret-value'
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
