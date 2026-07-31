#requires -Version 7
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Plan', 'Build', 'Run')]
    [string]$Action,

    [string]$PythonPath = '',

    [string]$HermesPythonPath = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Action = $Action.ToUpperInvariant()

function Test-NativeExecutableLaunch {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (
        -not (Test-Path -LiteralPath $Path -PathType Leaf) -or
        [IO.Path]::GetExtension($Path) -cne '.exe'
    ) {
        return $false
    }
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = [IO.Path]::GetFullPath($Path)
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.ArgumentList.Add("--version")
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            return $false
        }
        if (-not $process.WaitForExit(10000)) {
            $process.Kill($true)
            $process.WaitForExit()
            return $false
        }
        return $process.ExitCode -eq 0
    }
    catch {
        return $false
    }
    finally {
        $process.Dispose()
    }
}

function Resolve-LaunchableCodexExecutable {
    $candidates = [Collections.Generic.List[string]]::new()
    $applicationData = [Environment]::GetFolderPath('ApplicationData')
    $npmOptionalDependencies = Join-Path $applicationData 'npm\node_modules\@openai\codex\node_modules'
    if (Test-Path -LiteralPath $npmOptionalDependencies -PathType Container) {
        Get-ChildItem -Path (Join-Path $npmOptionalDependencies '@openai\codex-win32-*\vendor\*\bin\codex.exe') -File -ErrorAction SilentlyContinue |
            ForEach-Object { $candidates.Add($_.FullName) }
    }
    Get-Command codex.exe -CommandType Application -All -ErrorAction SilentlyContinue |
        ForEach-Object { $candidates.Add($_.Source) }
    foreach ($candidate in $candidates | Select-Object -Unique) {
        if (Test-NativeExecutableLaunch -Path $candidate) {
            return [IO.Path]::GetFullPath($candidate)
        }
    }
    throw 'TODO_TOOL.v1: no ProcessStart-launchable native Codex CLI is available'
}

function Assert-CodexUsesChatGptSubscription {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$CodexHome
    )

    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = [IO.Path]::GetFullPath($Path)
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.Environment['CODEX_HOME'] = [IO.Path]::GetFullPath($CodexHome)
    $startInfo.ArgumentList.Add('login')
    $startInfo.ArgumentList.Add('status')
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            throw 'Codex authentication inspection did not start.'
        }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(10000)) {
            $process.Kill($true)
            $process.WaitForExit()
            throw 'Codex authentication inspection timed out.'
        }
        [string[]]$status = @(
            $stdout.GetAwaiter().GetResult().Trim()
            $stderr.GetAwaiter().GetResult().Trim()
        ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
        if (
            $process.ExitCode -ne 0 -or
            $status.Count -ne 1 -or
            $status[0] -cne 'Logged in using ChatGPT'
        ) {
            throw 'TODO_TOOL.v1: Codex must use ChatGPT subscription authentication; metered API authentication is denied by the team cost cap.'
        }
    }
    finally {
        $process.Dispose()
    }
}

$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$rootEnvPath = Join-Path $repositoryRoot '.env'
$captainN8nEnvPath = Join-Path $repositoryRoot '.env.captain-n8n'
$provisionScript = Join-Path $PSScriptRoot 'provision-business-benchmark-demo.py'
$preflightScript = Join-Path $PSScriptRoot 'preflight-business-benchmark-demo.py'
$factoryRunner = Join-Path $PSScriptRoot 'run-agent-factory-business-demo.py'
$liveRunner = Join-Path $PSScriptRoot 'run-business-benchmark-live.ps1'
$serviceRunner = Join-Path $PSScriptRoot 'live-demo-services.ps1'
$benchmarkRuntimeEnvPath = Join-Path $repositoryRoot '.captain-cook/private/business-benchmarks/business-benchmark-runtime.env'
$canonicalRenewalWorkflow = Join-Path $repositoryRoot 'examples/business_benchmark_candidates/customer_renewal_orchestration_team/workflows/renewal_context_read.json'
$maximumUsdPerTeam = '0.30'
$maximumHermesUsd = '0.06'
$maximumTotalUsdPerTeam = '0.50'
$priorAttemptReserveUsdPerTeam = '0.06'
$userMaximumEurPerTeam = '1.00'
$seedVersion = 'business-benchmark-demo-2026-07-v21'

$rootEnvAllowlist = @(
    'CAPTAIN_GATEWAY_TOKEN',
    'WORKER_GATEWAY_TOKEN',
    'CAPTAIN_BENCHMARK_MODEL'
)
$benchmarkRuntimeAllowlist = @(
    'TEST_MARIADB_DSN',
    'MARIADB_BENCHMARK_PORT',
    'CAPTAIN_BENCHMARK_GATEWAY_URL'
)
$captainN8nAllowlist = @(
    'CAPTAIN_N8N_PORT',
    'CAPTAIN_N8N_API_KEY',
    'CAPTAIN_N8N_MCP_TOKEN',
    'CAPTAIN_N8N_MCP_BROKER_URL',
    'CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET'
)

function Read-AllowlistedEnvironment {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string[]]$AllowedNames
    )
    $values = [ordered]@{}
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required gitignored demo environment file is missing: $([IO.Path]::GetFileName($Path))"
    }
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith('#')) {
            continue
        }
        if ($line -notmatch '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            throw "Invalid local environment line in $([IO.Path]::GetFileName($Path))."
        }
        $name = $Matches[1]
        if ($AllowedNames -notcontains $name) {
            continue
        }
        if ($values.Contains($name)) {
            throw "Duplicate allowlisted local environment key: $name"
        }
        $values[$name] = $Matches[2].Trim().Trim('"').Trim("'")
    }
    return $values
}

function Merge-Environment {
    param(
        [Parameter(Mandatory = $true)][System.Collections.IDictionary]$Target,
        [Parameter(Mandatory = $true)][System.Collections.IDictionary]$Source
    )
    foreach ($entry in $Source.GetEnumerator()) {
        if ($Target.Contains($entry.Key)) {
            throw "Allowlisted demo environment key appears in multiple sources: $($entry.Key)"
        }
        $Target[$entry.Key] = [string]$entry.Value
    }
}

function Set-ProcessEnvironment {
    param([Parameter(Mandatory = $true)][System.Collections.IDictionary]$Values)
    foreach ($entry in $Values.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable(
            [string]$entry.Key,
            [string]$entry.Value,
            'Process'
        )
    }
}

function Resolve-Python311 {
    param([string]$ConfiguredPath)
    $resolved = $ConfiguredPath
    if ([string]::IsNullOrWhiteSpace($resolved)) {
        $candidate = Join-Path $repositoryRoot '.venv\Scripts\python.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $resolved = $candidate
        }
        else {
            $command = Get-Command python -CommandType Application -ErrorAction SilentlyContinue
            if ($null -eq $command) {
                throw 'TODO_TOOL.v1: validated Python 3.11 interpreter is unavailable'
            }
            $resolved = $command.Source
        }
    }
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw 'TODO_TOOL.v1: validated Python interpreter path is not a file'
    }
    & $resolved -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)' *> $null
    if ($LASTEXITCODE -ne 0) {
        throw 'TODO_TOOL.v1: validated Python interpreter must be Python 3.11'
    }
    return (Resolve-Path -LiteralPath $resolved).Path
}

function Resolve-HermesPython {
    param([string]$ConfiguredPath)

    $resolved = $ConfiguredPath
    if ([string]::IsNullOrWhiteSpace($resolved)) {
        $resolved = Join-Path (
            [Environment]::GetFolderPath('ApplicationData')
        ) 'uv\tools\hermes-agent\Scripts\python.exe'
    }
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw 'TODO_TOOL.v1: dedicated Hermes Python interpreter is unavailable'
    }
    return (Resolve-Path -LiteralPath $resolved).Path
}

function Assert-ExactCaptainTestDsn {
    param(
        [Parameter(Mandatory = $true)][string]$Dsn,
        [Parameter(Mandatory = $true)][string]$ExpectedPort
    )
    try {
        $uri = [Uri]$Dsn
        $port = [int]$ExpectedPort
    }
    catch {
        throw 'TEST_MARIADB_DSN must be a valid local captain_test DSN.'
    }
    if (
        $uri.Scheme -cne 'mariadb' -or
        $uri.Host -notin @('127.0.0.1', 'localhost') -or
        $uri.AbsolutePath.TrimEnd('/') -cne '/captain_test' -or
        $uri.Port -ne $port -or
        $port -lt 1024 -or
        $port -gt 65535
    ) {
        throw 'TEST_MARIADB_DSN must target the exact isolated local captain_test database.'
    }
}

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$Value)
    $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
    return [Convert]::ToHexString(
        [Security.Cryptography.SHA256]::HashData($bytes)
    ).ToLowerInvariant()
}

function Require-NonEmpty {
    param(
        [Parameter(Mandatory = $true)][object]$Value,
        [Parameter(Mandatory = $true)][string]$Field
    )
    if ([string]::IsNullOrWhiteSpace([string]$Value)) {
        throw "Provisioning result is missing typed field: $Field"
    }
    return [string]$Value
}

function New-FactoryDispatchCheckpoint {
    param(
        [Parameter(Mandatory = $true)][object[]]$Teams,
        [Parameter(Mandatory = $true)][string]$IssuedAt,
        [Parameter(Mandatory = $true)][string]$RenewalBatchId
    )
    $jobs = @(
        foreach ($team in $Teams) {
            [ordered]@{
                profile = [string]$team.profile
                job_id = [string]$team.job.job_id
                candidate_id = [string]$team.candidate_id
                missing_gateway_evidence = @($team.missing_gateway_evidence)
            }
        }
    )
    return [ordered]@{
        schema = 'captain.business-benchmark-demo-run.v1'
        status = 'factory_dispatch_required'
        database = 'captain_test'
        issued_at = $IssuedAt
        maximum_usd_per_team = $maximumUsdPerTeam
        jobs = $jobs
        renewal_batch_id = $RenewalBatchId
        instruction = 'Run the Captain Factory dispatch composition for both job IDs, then rerun this command.'
    }
}

function New-CandidatesReady {
    param(
        [Parameter(Mandatory = $true)][object[]]$Teams,
        [Parameter(Mandatory = $true)][string]$IssuedAt,
        [Parameter(Mandatory = $true)][string]$RenewalBatchId
    )
    return [ordered]@{
        schema = 'captain.business-benchmark-demo-run.v1'
        status = 'candidates_ready'
        database = 'captain_test'
        issued_at = $IssuedAt
        maximum_usd_per_team = $maximumUsdPerTeam
        jobs = @(
            foreach ($team in $Teams) {
                [ordered]@{
                    profile = [string]$team.profile
                    job_id = [string]$team.job.job_id
                    candidate_id = [string]$team.candidate_id
                }
            }
        )
        renewal_batch_id = $RenewalBatchId
    }
}

function New-DryRunPlan {
    param(
        [Parameter(Mandatory = $true)][object[]]$Teams,
        [Parameter(Mandatory = $true)][string]$IssuedAt
    )
    return [ordered]@{
        schema = 'captain.business-benchmark-demo-run.v1'
        status = 'planned'
        mode = 'dry_run'
        database = 'captain_test'
        issued_at = $IssuedAt
        suite_version = 21
        seed_version_id = $seedVersion
        maximum_usd_per_team = $maximumUsdPerTeam
        jobs = @(
            foreach ($team in $Teams) {
                [ordered]@{
                    profile = [string]$team.profile
                    job_id = [string]$team.job.job_id
                }
            }
        )
        effects = [ordered]@{
            provider_calls = $false
            live_service_calls = $false
            provisioning_apply = $false
            gateway_mutation = $false
            minibook_mutation = $false
        }
    }
}

function Test-ResolvedPreflightBindings {
    param(
        [Parameter(Mandatory = $true)][object]$Preflight,
        [Parameter(Mandatory = $true)][object[]]$Teams
    )

    if (
        $Preflight.PSObject.Properties.Name -notcontains 'jobs' -or
        @($Preflight.jobs).Count -ne 2
    ) {
        return $false
    }
    $expected = @(
        foreach ($team in $Teams) {
            "$([string]$team.job.job_id)|$([string]$team.candidate_id)"
        }
    ) | Sort-Object
    $actual = @(
        foreach ($scope in @($Preflight.jobs)) {
            if (
                $null -eq $scope -or
                $scope.PSObject.Properties.Name -notcontains 'job_id' -or
                $scope.PSObject.Properties.Name -notcontains 'candidate_id' -or
                [string]::IsNullOrWhiteSpace([string]$scope.job_id) -or
                [string]::IsNullOrWhiteSpace([string]$scope.candidate_id)
            ) {
                return $false
            }
            "$([string]$scope.job_id)|$([string]$scope.candidate_id)"
        }
    ) | Sort-Object
    return ($expected -join ',') -ceq ($actual -join ',')
}

function Test-CodexBuildInterruptedCheckpoint {
    param([Parameter(Mandatory = $true)][object]$Checkpoint)

    $expectedTopLevel = @(
        'schema', 'database', 'status', 'exit_code', 'reason', 'checkpoint_ref',
        'terminal_receipt_ref', 'next_resume_ordinal', 'captain_authorization_binding'
    ) | Sort-Object
    $actualTopLevel = @($Checkpoint.PSObject.Properties.Name | Sort-Object)
    if (($expectedTopLevel -join ',') -cne ($actualTopLevel -join ',')) {
        return $false
    }
    if (
        $Checkpoint.schema -cne 'captain.business-demo-factory-operator.v1' -or
        $Checkpoint.database -cne 'captain_test' -or
        $Checkpoint.status -cne 'codex_build_interrupted' -or
        $Checkpoint.reason -notin @(
            'codex_timed_out', 'runtime_cancelled', 'resume_authorization_required'
        )
    ) {
        return $false
    }
    if (
        ($Checkpoint.reason -ceq 'codex_timed_out' -and -not (Test-StrictInteger -Value $Checkpoint.exit_code -Expected 124)) -or
        ($Checkpoint.reason -ceq 'runtime_cancelled' -and -not (Test-StrictInteger -Value $Checkpoint.exit_code -Expected 130)) -or
        ($Checkpoint.reason -ceq 'resume_authorization_required' -and $null -ne $Checkpoint.exit_code) -or
        ($null -ne $Checkpoint.next_resume_ordinal -and [string]$Checkpoint.next_resume_ordinal -notmatch '^[12]$')
    ) {
        return $false
    }
    foreach ($expected in @(
        [ordered]@{ reference = $Checkpoint.checkpoint_ref; namespace = 'codex-checkpoint' },
        [ordered]@{ reference = $Checkpoint.terminal_receipt_ref; namespace = 'codex-terminal-receipt' }
    )) {
        $reference = $expected.reference
        $expectedReference = @('media_type', 'sha256', 'uri')
        $actualReference = @($reference.PSObject.Properties.Name | Sort-Object)
        if (
            ($expectedReference -join ',') -cne ($actualReference -join ',') -or
            $reference.media_type -cne 'application/json' -or
            [string]$reference.sha256 -notmatch '^[0-9a-f]{64}$' -or
            [string]$reference.uri -cne "artifact://factory/$($expected.namespace)/$($reference.sha256)"
        ) {
            return $false
        }
    }
    $binding = $Checkpoint.captain_authorization_binding
    $expectedBinding = @(
        'attempt', 'base_revision', 'brief_sha256', 'correlation_id', 'idempotency_key',
        'invocation_id', 'job_id', 'lease_id', 'scaffold_manifest_sha256',
        'subject_version', 'workspace_ref'
    ) | Sort-Object
    $actualBinding = @($binding.PSObject.Properties.Name | Sort-Object)
    if (
        ($expectedBinding -join ',') -cne ($actualBinding -join ',') -or
        -not (Test-CanonicalUuid -Value ([string]$binding.job_id)) -or
        -not (Test-CanonicalUuid -Value ([string]$binding.correlation_id)) -or
        -not (Test-CanonicalUuid -Value ([string]$binding.invocation_id)) -or
        [string]$binding.subject_version -notmatch '^[1-9][0-9]{0,8}$' -or
        [string]$binding.attempt -notmatch '^[1-9][0-9]{0,8}$' -or
        [string]$binding.idempotency_key -notmatch '^[0-9a-f]{64}$' -or
        [string]$binding.lease_id -notmatch '^[a-z0-9][a-z0-9._-]{0,127}$' -or
        -not (Test-FactoryWorkspaceReference -Value ([string]$binding.workspace_ref)) -or
        [string]$binding.base_revision -notmatch '^[0-9a-f]{40,64}$' -or
        [string]$binding.scaffold_manifest_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$binding.brief_sha256 -notmatch '^[0-9a-f]{64}$'
    ) {
        return $false
    }
    return $true
}

function Test-CanonicalUuid {
    param([Parameter(Mandatory = $true)][string]$Value)

    $parsed = [guid]::Empty
    return [guid]::TryParse($Value, [ref]$parsed) -and $parsed.ToString('D') -ceq $Value
}

function Test-StrictInteger {
    param(
        [Parameter(Mandatory = $true)][object]$Value,
        [Parameter(Mandatory = $true)][long]$Expected
    )

    return ($Value -is [int] -or $Value -is [long]) -and [long]$Value -eq $Expected
}

function Test-StrictNonzeroInteger {
    param([Parameter(Mandatory = $true)][object]$Value)

    return ($Value -is [int] -or $Value -is [long]) -and [long]$Value -ne 0
}

function Test-FactoryWorkspaceReference {
    param([Parameter(Mandatory = $true)][string]$Value)

    return (
        $Value -match '^workspace://business-benchmark-demo/(claims|renewal)/epoch-[0-9a-f]{16}$' -or
        $Value -match '^workspace://[a-z0-9][a-z0-9-]{0,127}/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/(dispatch_agent_architect|dispatch_tool_integrator|submit_forge_job|dispatch_build_validator|dispatch_real_case_tester|dispatch_quality_warden)/[1-9][0-9]{0,5}/[0-9]{8}T[0-9]{12}Z$'
    )
}

function New-InfrastructureCheckpoint {
    param([Parameter(Mandatory = $true)][string]$IssuedAt)
    return [ordered]@{
        schema = 'captain.business-benchmark-demo-run.v1'
        status = 'infrastructure_required'
        database = 'captain_test'
        issued_at = $IssuedAt
        component = 'captain_test_gateway_n8n'
        instruction = 'Restore or start only the Captain-owned benchmark infrastructure, then rerun this command.'
    }
}

function Get-HumanReviewCheckpoint {
    param(
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][string]$IssuedAt
    )
    $root = Join-Path $repositoryRoot '.captain-cook/private/business-benchmarks/human-review'
    $raw = @(
        & $Python -m agenten.agent_factory.business_benchmark_human_review_cli `
            --root $root list --status pending 2>$null
    )
    if ($LASTEXITCODE -ne 0 -or $raw.Count -eq 0) {
        return $null
    }
    try {
        $pending = ($raw -join [Environment]::NewLine) | ConvertFrom-Json -Depth 20
    }
    catch {
        return $null
    }
    if ([int]$pending.count -lt 1) {
        return $null
    }
    return [ordered]@{
        schema = 'captain.business-benchmark-demo-run.v1'
        status = 'human_review_required'
        database = 'captain_test'
        issued_at = $IssuedAt
        review_count = [int]$pending.count
        instruction = 'List pending reviews with the Captain human-review CLI; an operator must complete them explicitly, then rerun this command.'
    }
}

Push-Location $repositoryRoot
try {
    $python = Resolve-Python311 $PythonPath
    $issuedAt = [DateTimeOffset]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.fffZ')

    if ($Action -ceq 'PLAN') {
        $planArguments = @(
            $provisionScript,
            '--plan-only',
            '--workspace-root', $repositoryRoot,
            '--issued-at', $issuedAt,
            '--model', 'gpt-4.1-mini',
            '--maximum-usd-per-team', $maximumUsdPerTeam,
            '--suite-version', '21',
            '--seed-version-id', $seedVersion
        )
        $rawPlanProvisioning = @(& $python @planArguments)
        if ($LASTEXITCODE -ne 0) {
            throw 'Captain business benchmark dry-run provisioning failed closed.'
        }
        try {
            $planProvisioning = ($rawPlanProvisioning -join [Environment]::NewLine) |
                ConvertFrom-Json -Depth 100
        }
        catch {
            throw 'Captain business benchmark dry-run provisioning returned invalid JSON.'
        }
        $planTeams = @($planProvisioning.teams)
        $planProfiles = @($planTeams | ForEach-Object { [string]$_.profile } | Sort-Object)
        if (
            $planProvisioning.schema -cne 'captain.business-benchmark-demo-provisioning.v1' -or
            $planProvisioning.mode -cne 'dry_run' -or
            $planProvisioning.database -cne 'captain_test' -or
            $planTeams.Count -ne 2 -or
            ($planProfiles -join ',') -cne 'claims,renewal'
        ) {
            throw 'Captain business benchmark dry-run provisioning result is not canonical.'
        }
        foreach ($team in $planTeams) {
            $null = Require-NonEmpty $team.job.job_id "$($team.profile).job.job_id"
            if (
                [string]$team.job.execution_policy.max_cost_usd -cne $maximumUsdPerTeam -or
                @($team.job.execution_policy.allowed_models) -notcontains 'gpt-4.1-mini' -or
                [int]$team.suite.suite_version -ne 21
            ) {
                throw 'Dry-run team model or budget does not match the demo authority.'
            }
        }
        New-DryRunPlan -Teams $planTeams -IssuedAt $issuedAt |
            ConvertTo-Json -Compress -Depth 10
        exit 0
    }

    $hermesPython = Resolve-HermesPython $HermesPythonPath

    if ($Action -in @('BUILD', 'RUN')) {
        try {
            & $serviceRunner benchmark-start *> $null
            if ($LASTEXITCODE -ne 0) {
                throw 'Captain benchmark infrastructure returned a non-zero exit code.'
            }
        }
        catch {
            New-InfrastructureCheckpoint -IssuedAt $issuedAt |
                ConvertTo-Json -Compress -Depth 10
            exit 4
        }
    }

    $environment = [ordered]@{}
    Merge-Environment $environment (Read-AllowlistedEnvironment $rootEnvPath $rootEnvAllowlist)
    Merge-Environment $environment (Read-AllowlistedEnvironment $captainN8nEnvPath $captainN8nAllowlist)
    $benchmarkRuntime = Read-AllowlistedEnvironment $benchmarkRuntimeEnvPath $benchmarkRuntimeAllowlist
    foreach ($required in $benchmarkRuntimeAllowlist) {
        if (-not $benchmarkRuntime.Contains($required) -or [string]::IsNullOrWhiteSpace([string]$benchmarkRuntime[$required])) {
            throw "Required dedicated benchmark runtime setting is missing: $required"
        }
    }
    $environment['TEST_MARIADB_DSN'] = [string]$benchmarkRuntime['TEST_MARIADB_DSN']
    $environment['MARIADB_TEST_PORT'] = [string]$benchmarkRuntime['MARIADB_BENCHMARK_PORT']
    $environment['CAPTAIN_GATEWAY_URL'] = [string]$benchmarkRuntime['CAPTAIN_BENCHMARK_GATEWAY_URL']
    foreach ($required in @(
        'TEST_MARIADB_DSN',
        'MARIADB_TEST_PORT',
        'CAPTAIN_N8N_PORT',
        'CAPTAIN_N8N_API_KEY',
        'CAPTAIN_N8N_MCP_TOKEN',
        'CAPTAIN_N8N_MCP_BROKER_URL',
        'CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET'
    )) {
        if (-not $environment.Contains($required) -or [string]::IsNullOrWhiteSpace([string]$environment[$required])) {
            throw "Required allowlisted Captain demo setting is missing: $required"
        }
    }
    Assert-ExactCaptainTestDsn `
        -Dsn ([string]$environment['TEST_MARIADB_DSN']) `
        -ExpectedPort ([string]$environment['MARIADB_TEST_PORT'])

    $model = if (
        $environment.Contains('CAPTAIN_BENCHMARK_MODEL') -and
        -not [string]::IsNullOrWhiteSpace([string]$environment['CAPTAIN_BENCHMARK_MODEL'])
    ) {
        [string]$environment['CAPTAIN_BENCHMARK_MODEL']
    }
    else {
        'gpt-4.1-mini'
    }
    $codexCommand = Resolve-LaunchableCodexExecutable
    $pwshCommand = Get-Command pwsh.exe -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    $userProfileRoot = [Environment]::GetFolderPath('UserProfile')
    $codexHomePath = Join-Path $userProfileRoot '.codex'
    if (
        $null -eq $pwshCommand -or
        -not (Test-Path -LiteralPath $codexCommand -PathType Leaf) -or
        -not (Test-Path -LiteralPath $pwshCommand.Source -PathType Leaf) -or
        -not (Test-Path -LiteralPath $codexHomePath -PathType Container)
    ) {
        throw 'TODO_TOOL.v1: Codex CLI, PowerShell 7, or Codex home is unavailable'
    }
    Assert-CodexUsesChatGptSubscription `
        -Path $codexCommand `
        -CodexHome $codexHomePath
    $redactionVersion = 'benchmark-redaction-v1'
    $environment['CAPTAIN_BENCHMARK_PROVIDER'] = 'openai'
    $environment['CAPTAIN_BENCHMARK_MODEL'] = $model
    $environment['CAPTAIN_BENCHMARK_PROVIDER_SECRET'] = 'OPENAI_API_KEY'
    $environment['CAPTAIN_BENCHMARK_REDACTION_POLICY_VERSION'] = $redactionVersion
    $environment['CAPTAIN_BENCHMARK_REDACTION_POLICY_SHA256'] = Get-Sha256Hex `
        '{"redaction_policy_version":"benchmark-redaction-v1"}'
    $environment['CAPTAIN_BENCHMARK_CASE_MAX_COST_USD'] = '0.01'
    $environment['CAPTAIN_BENCHMARK_CASE_MAX_LATENCY_MS'] = '30000'
    $environment['CAPTAIN_BENCHMARK_MAX_COST_PER_CALL_USD'] = '0.01'
    $environment['CAPTAIN_BENCHMARK_PRICING_MINIMUM_COST_USD'] = '0'
    $environment['CAPTAIN_BENCHMARK_PRICING_INPUT_COST_PER_MILLION_USD'] = '0.40'
    $environment['CAPTAIN_BENCHMARK_PRICING_OUTPUT_COST_PER_MILLION_USD'] = '1.60'
    $environment['CAPTAIN_BENCHMARK_PRICING_VERSION'] = 'openai-demo-2026-07'
    $environment['CAPTAIN_BENCHMARK_PRICING_EFFECTIVE_AT'] = '2026-07-01T00:00:00Z'
    $environment['CAPTAIN_BENCHMARK_SEED_VERSION_ID'] = $seedVersion
    $environment['CAPTAIN_BENCHMARK_AUTHORITY_ROOT'] = Join-Path $repositoryRoot '.captain-cook/private/business-benchmarks'
    $environment['CAPTAIN_BENCHMARK_SKILL_ROOT'] = Join-Path $repositoryRoot 'agenten/agent_factory/skills'
    $environment['CAPTAIN_BENCHMARK_HUMAN_REVIEW_TIMEOUT_SECONDS'] = '0'
    $environment['CAPTAIN_BENCHMARK_RENEWAL_N8N_EVIDENCE_ROOT'] = Join-Path $repositoryRoot '.captain-cook/business-benchmark'
    $environment['CAPTAIN_BENCHMARK_RENEWAL_WORKFLOW_PATH'] = $canonicalRenewalWorkflow
    $environment['CAPTAIN_CODEX_EXECUTABLE'] = $codexCommand
    $environment['CAPTAIN_CODEX_AUTH_MODE'] = 'chatgpt_subscription'
    $environment['CAPTAIN_PWSH_EXECUTABLE'] = $pwshCommand.Source
    $environment['CAPTAIN_CODEX_HOME'] = $codexHomePath
    $environment['CAPTAIN_FACTORY_USER_MAX_EUR_PER_TEAM'] = $userMaximumEurPerTeam
    $environment['CAPTAIN_FACTORY_MAX_TOTAL_COST_USD_PER_TEAM'] = $maximumTotalUsdPerTeam
    $environment['CAPTAIN_FACTORY_CODEX_METERED_USD_PER_TEAM'] = '0'
    $environment['CAPTAIN_FACTORY_HERMES_MAX_TOTAL_USD'] = $maximumHermesUsd
    $environment['CAPTAIN_FACTORY_PRIOR_ATTEMPT_RESERVE_USD_PER_TEAM'] = $priorAttemptReserveUsdPerTeam
    $environment['N8N_MODE'] = 'captain-builder'
    $environment['CAPTAIN_N8N_URL'] = "http://127.0.0.1:$($environment['CAPTAIN_N8N_PORT'])"
    Set-ProcessEnvironment $environment

    $arguments = @(
        $provisionScript,
        '--workspace-root', $repositoryRoot,
        '--issued-at', $issuedAt,
        '--model', $model,
        '--maximum-usd-per-team', $maximumUsdPerTeam,
        '--suite-version', '21',
        '--seed-version-id', $seedVersion
    )
    if ($Action -in @('BUILD', 'RUN')) {
        $arguments += '--apply'
    }
    $rawProvisioning = @(& $python @arguments)
    if ($LASTEXITCODE -ne 0) {
        throw 'Captain business benchmark provisioning failed closed.'
    }
    try {
        $provisioning = ($rawProvisioning -join [Environment]::NewLine) | ConvertFrom-Json -Depth 100
    }
    catch {
        throw 'Captain business benchmark provisioning returned invalid JSON.'
    }
    $expectedMode = if ($Action -in @('BUILD', 'RUN')) { 'applied' } else { 'dry_run' }
    if (
        $provisioning.schema -cne 'captain.business-benchmark-demo-provisioning.v1' -or
        $provisioning.mode -cne $expectedMode -or
        $provisioning.database -cne 'captain_test' -or
        @($provisioning.teams).Count -ne 2
    ) {
        throw 'Captain business benchmark provisioning result is not canonical.'
    }
    $teams = @($provisioning.teams)
    $claims = @($teams | Where-Object { $_.profile -ceq 'claims' })
    $renewal = @($teams | Where-Object { $_.profile -ceq 'renewal' })
    if ($claims.Count -ne 1 -or $renewal.Count -ne 1) {
        throw 'Provisioning must return exactly one Claims and one Renewal team.'
    }
    $claims = $claims[0]
    $renewal = $renewal[0]
    foreach ($team in $teams) {
        $null = Require-NonEmpty $team.job.job_id "$($team.profile).job.job_id"
        $null = Require-NonEmpty $team.candidate_id "$($team.profile).candidate_id"
        if (
            [string]$team.job.execution_policy.max_cost_usd -cne $maximumUsdPerTeam -or
            @($team.job.execution_policy.allowed_models) -notcontains $model -or
            [int]$team.suite.suite_version -ne 21
        ) {
            throw 'Provisioned team model or budget does not match the demo authority.'
        }
        if (
            $Action -in @('BUILD', 'RUN') -and
            [decimal]$team.gateway_budget_remaining_usd -ne [decimal]$maximumUsdPerTeam
        ) {
            throw 'Applied team Gateway budget does not match the demo authority.'
        }
    }
    $renewalBatchId = Require-NonEmpty $renewal.work_batch.batch_id 'renewal.work_batch.batch_id'

    $environment['CAPTAIN_BENCHMARK_PROFILE'] = 'all'
    $environment['CAPTAIN_BENCHMARK_MAX_USD'] = '0.60'
    $environment['CAPTAIN_JOB_ALLOWED_MODELS'] = $model
    foreach ($binding in @(
        @('CLAIMS', $claims),
        @('RENEWAL', $renewal)
    )) {
        $prefix = "CAPTAIN_BENCHMARK_$($binding[0])"
        $team = $binding[1]
        $environment["${prefix}_SUITE_VERSION"] = [string]$team.suite.suite_version
        $environment["${prefix}_CANDIDATE_ID"] = [string]$team.candidate_id
        $environment["${prefix}_JOB_ID"] = [string]$team.job.job_id
        $environment["${prefix}_ATTEMPT"] = '1'
        $environment["${prefix}_MAX_USD"] = $maximumUsdPerTeam
        $environment["${prefix}_REMAINING_USD"] = [string]$team.gateway_budget_remaining_usd
    }
    $environment['CAPTAIN_BENCHMARK_RENEWAL_BATCH_ID'] = $renewalBatchId
    $environment['CAPTAIN_BENCHMARK_RENEWAL_WORKSPACE_REF'] = "workspace://business-benchmark-renewal/$renewalBatchId"
    $environment['CAPTAIN_BENCHMARK_EVIDENCE_ROOT'] = Join-Path $repositoryRoot '.captain-cook/evidence/business-benchmarks/preflight'
    Set-ProcessEnvironment $environment

    $rawPreflight = @(& $python $preflightScript)
    if ($LASTEXITCODE -ne 0) {
        throw 'Captain business benchmark default composition preflight failed closed.'
    }
    try {
        $preflight = ($rawPreflight -join [Environment]::NewLine) | ConvertFrom-Json -Depth 20
    }
    catch {
        throw 'Captain business benchmark default composition preflight returned invalid JSON.'
    }
    if (
        $preflight.schema -cne 'captain.business-benchmark-default-preflight.v1' -or
        $preflight.database -cne 'captain_test' -or
        $preflight.status -notin @('resolvable', 'factory_dispatch_required') -or
        ($preflight.production_scope_resolvable -isnot [bool])
    ) {
        throw 'Captain business benchmark default composition preflight is not canonical.'
    }
    if (
        $preflight.production_scope_resolvable -eq $true -and
        -not (Test-ResolvedPreflightBindings -Preflight $preflight -Teams $teams)
    ) {
        throw 'Captain business benchmark preflight scope does not bind the provisioned Claims and Renewal candidates.'
    }

    $processOpenAiKey = [Environment]::GetEnvironmentVariable('OPENAI_API_KEY', 'Process')
    if (
        $preflight.production_scope_resolvable -ne $true -and
        [string]::IsNullOrWhiteSpace($processOpenAiKey)
    ) {
        New-FactoryDispatchCheckpoint `
            -Teams $teams `
            -IssuedAt $issuedAt `
            -RenewalBatchId $renewalBatchId |
            ConvertTo-Json -Compress -Depth 20
        exit 2
    }

    if ($Action -ceq 'RUN' -and [string]::IsNullOrWhiteSpace($processOpenAiKey)) {
        throw 'OPENAI_API_KEY must already exist in the process; demo env files are never read for it.'
    }

    if ($preflight.production_scope_resolvable -ne $true -or $Action -ceq 'BUILD') {
        $factoryArguments = @(
            $factoryRunner,
            '--workspace-root', $repositoryRoot,
            '--python-executable', $python,
            '--hermes-python-executable', $hermesPython,
            '--job-id', [string]$claims.job.job_id,
            '--job-id', [string]$renewal.job.job_id,
            '--hermes-provider', 'openai-api',
            '--hermes-model', $model,
            '--hermes-max-usd', $maximumHermesUsd,
            '--maximum-dispatches', '12'
        )
        if ($Action -ceq 'BUILD') {
            $factoryArguments += '--stop-before-quality-warden'
        }
        $factoryErrorPath = Join-Path `
            $environment['CAPTAIN_BENCHMARK_AUTHORITY_ROOT'] `
            'runtime-state/factory-operator-stderr.log'
        $null = New-Item -ItemType Directory -Force -Path (Split-Path $factoryErrorPath)
        Remove-Item -LiteralPath $factoryErrorPath -Force -ErrorAction SilentlyContinue
        $rawFactory = @(& $python @factoryArguments 2>$factoryErrorPath)
        $factoryExitCode = $LASTEXITCODE
        if ($factoryExitCode -eq 2) {
            try {
                $factoryInterruption = ($rawFactory -join [Environment]::NewLine) |
                    ConvertFrom-Json -Depth 20
            }
            catch {
                throw 'Captain Factory interruption checkpoint returned invalid JSON.'
            }
            if (-not (Test-CodexBuildInterruptedCheckpoint -Checkpoint $factoryInterruption)) {
                throw 'Captain Factory interruption checkpoint is not canonical.'
            }
            Remove-Item -LiteralPath $factoryErrorPath -Force -ErrorAction SilentlyContinue
            $factoryInterruption | ConvertTo-Json -Compress -Depth 20
            exit 2
        }
        if ($factoryExitCode -ne 0) {
            throw 'Captain Factory live operator failed closed; inspect private evidence.'
        }
        Remove-Item -LiteralPath $factoryErrorPath -Force -ErrorAction SilentlyContinue
        try {
            $factoryResult = ($rawFactory -join [Environment]::NewLine) |
                ConvertFrom-Json -Depth 100
        }
        catch {
            throw 'Captain Factory live operator returned invalid JSON.'
        }
        $expectedJobIds = @(
            [string]$claims.job.job_id,
            [string]$renewal.job.job_id
        ) | Sort-Object
        $actualJobIds = @($factoryResult.results | ForEach-Object { [string]$_.job_id }) |
            Sort-Object
        if (
            $factoryResult.schema -cne 'captain.business-demo-factory-operator.v1' -or
            $factoryResult.database -cne 'captain_test' -or
            @($factoryResult.results).Count -ne 2 -or
            ($expectedJobIds -join ',') -cne ($actualJobIds -join ',') -or
            @($factoryResult.results | Where-Object {
                $_.schema -cne 'captain.factory-dispatch-run-result.v1' -or
                $_.status -notin @(
                    'complete',
                    'captain_action_required',
                    'infrastructure_blocked',
                    'dispatch_limit_reached',
                    'stop_point_reached'
                )
            }).Count -ne 0
        ) {
            throw 'Captain Factory live operator result is not canonical.'
        }
        if ($Action -ceq 'BUILD') {
            $invalidStops = @($factoryResult.results | Where-Object {
                $_.status -cne 'stop_point_reached' -or
                $_.next_action.kind -cne 'dispatch_quality_warden' -or
                [string]$_.next_action.job_id -cne [string]$_.job_id
            })
            if ($invalidStops.Count -ne 0) {
                throw 'Captain Factory Build did not stop both jobs before Quality Warden.'
            }
        }
        elseif (@($factoryResult.results | Where-Object {
            $_.status -ceq 'stop_point_reached'
        }).Count -ne 0) {
            throw 'Captain Factory Run stopped before Quality Warden unexpectedly.'
        }

        if ($Action -ceq 'RUN') {
            $rawPreflight = @(& $python $preflightScript)
            if ($LASTEXITCODE -ne 0) {
                throw 'Captain business benchmark post-Factory preflight failed closed.'
            }
            try {
                $preflight = ($rawPreflight -join [Environment]::NewLine) |
                    ConvertFrom-Json -Depth 20
            }
            catch {
                throw 'Captain business benchmark post-Factory preflight returned invalid JSON.'
            }
            if (
                $preflight.schema -cne 'captain.business-benchmark-default-preflight.v1' -or
                $preflight.database -cne 'captain_test' -or
                $preflight.status -notin @('resolvable', 'factory_dispatch_required') -or
                ($preflight.production_scope_resolvable -isnot [bool])
            ) {
                throw 'Captain business benchmark post-Factory preflight is not canonical.'
            }
            if (
                $preflight.production_scope_resolvable -eq $true -and
                -not (Test-ResolvedPreflightBindings -Preflight $preflight -Teams $teams)
            ) {
                throw 'Captain business benchmark post-Factory scope does not bind the provisioned Claims and Renewal candidates.'
            }
            if ($preflight.production_scope_resolvable -ne $true) {
                New-FactoryDispatchCheckpoint `
                    -Teams $teams `
                    -IssuedAt $issuedAt `
                    -RenewalBatchId $renewalBatchId |
                    ConvertTo-Json -Compress -Depth 20
                exit 2
            }
        }
    }

    if ($Action -ceq 'BUILD') {
        New-CandidatesReady `
            -Teams $teams `
            -IssuedAt $issuedAt `
            -RenewalBatchId $renewalBatchId |
            ConvertTo-Json -Compress -Depth 20
        exit 0
    }

    $liveFailure = $null
    try {
        $null = @(& $liveRunner -Profile all -PythonPath $python)
        if ($LASTEXITCODE -ne 0) {
            $liveFailure = 'provider runner returned a non-zero exit code'
        }
    }
    catch {
        $liveFailure = 'provider runner failed'
    }
    if ($null -ne $liveFailure) {
        $review = Get-HumanReviewCheckpoint -Python $python -IssuedAt $issuedAt
        if ($null -ne $review) {
            $review | ConvertTo-Json -Compress -Depth 20
            exit 3
        }
        throw 'Business benchmark provider gate failed closed; inspect private evidence.'
    }

    [ordered]@{
        schema = 'captain.business-benchmark-demo-run.v1'
        status = 'completed'
        database = 'captain_test'
        issued_at = $issuedAt
        maximum_usd_per_team = $maximumUsdPerTeam
    } | ConvertTo-Json -Compress -Depth 10
}
finally {
    Pop-Location
}
