#requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$InputPath,
    [switch]$UseManagedGateway,
    [string]$GatewayUrl = 'http://127.0.0.1:18090',
    [string]$RuntimeUrl,
    [string]$MinibookUrl,
    [string]$ArtifactDirectory = 'artifacts/capability-factory',
    [string]$CheckpointDirectory = '.superpowers/sdd/capability-factory-checkpoints',
    [string]$SandboxImage,
    [Guid]$CorrelationId = [Guid]::NewGuid(),
    [ValidateRange(1, 2147483647)]
    [int]$SubjectVersion = 1,
    [ValidateRange(1, 86400)]
    [int]$WallClockBudgetSeconds = 600
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$rootEnv = Join-Path $root '.env'
$python = Join-Path $root '.venv/Scripts/python.exe'
$liveTest = Join-Path $root 'tests/live/test_to_be_built_capability_factory_live.py'
$gatewayProcess = $null

function Read-LocalEnvironment([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'The gitignored root .env is required for the live gate.'
    }
    $allowed = @(
        'CAPTAIN_GATEWAY_TOKEN', 'WORKER_GATEWAY_TOKEN', 'CAPTAIN_RUNTIME_TOKEN',
        'CAPTAIN_RUNTIME_URL', 'TEST_MARIADB_DSN', 'MINIBOOK_BACKEND_URL',
        'MINIBOOK_API_KEY', 'MINIBOOK_PROJECTION_API_KEY',
        'CAPTAIN_CAPABILITY_SANDBOX_IMAGE',
        'CAPABILITY_FACTORY_ENTRYPOINT_ADAPTER_MANIFEST', 'CAPABILITY_FACTORY_ENTRYPOINT_ADAPTER_SHA256',
        'FACTORY_LIVE_RUNTIME_ADAPTER_MANIFEST', 'FACTORY_LIVE_RUNTIME_ADAPTER_SHA256',
        'CAPTAIN_FACTORY_JOB_ID', 'OPENAI_API_KEY', 'OPENAI_MODEL', 'LLM_PROVIDER', 'CONTEXT7_API_KEY',
        'CAPTAIN_FACTORY_SKILL_ROOT', 'CAPTAIN_FACTORY_WORKSPACE_REF',
        'CAPTAIN_FACTORY_PROVIDER', 'CAPTAIN_FACTORY_HERMES_PROVIDER', 'CAPTAIN_FACTORY_HERMES_MODEL', 'CAPTAIN_FACTORY_HERMES_MAX_COST_PER_CALL_USD', 'CAPTAIN_FACTORY_MODEL',
        'CAPTAIN_FACTORY_MAX_COST_USD', 'CAPTAIN_FACTORY_MAX_COST_PER_CALL_USD',
        'CAPTAIN_FACTORY_RUNTIME_SECONDS', 'CAPTAIN_RUNTIME_EVIDENCE_DIAGNOSTICS',
        'HERMES_EXECUTABLE', 'CODEX_EXECUTABLE', 'CAPTAIN_RUNTIME_ARTIFACT_ROOT',
        'CAPTAIN_N8N_URL', 'CAPTAIN_N8N_API_KEY', 'CAPTAIN_N8N_MCP_TOKEN',
        'CAPTAIN_N8N_MCP_BROKER_URL', 'CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET',
        'CAPTAIN_N8N_BATCH_ID', 'CAPTAIN_N8N_PROJECT_ID',
        'CAPTAIN_N8N_WORKSPACE_REF', 'CAPTAIN_FACTORY_N8N_WORKFLOW_ID'
    )
    $values = [ordered]@{}
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith('#')) { continue }
        if ($line -notmatch '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            throw 'The root .env contains an invalid line.'
        }
        $name = $Matches[1]
        if ($allowed -notcontains $name) { continue }
        if ($values.Contains($name)) { throw "Duplicate local environment alias: $name" }
        $values[$name] = $Matches[2]
    }
    return $values
}

function Require-Value($Values, [string]$Name) {
    if (-not $Values.Contains($Name) -or [string]::IsNullOrWhiteSpace([string]$Values[$Name])) {
        throw "Required live alias is missing: $Name"
    }
    return [string]$Values[$Name]
}

function Resolve-SafeWorkspacePath([string]$Value, [string]$Label) {
    $candidate = if ([IO.Path]::IsPathRooted($Value)) {
        [IO.Path]::GetFullPath($Value)
    } else {
        [IO.Path]::GetFullPath((Join-Path $root $Value))
    }
    $relative = [IO.Path]::GetRelativePath($root, $candidate)
    if ($relative -eq '..' -or $relative.StartsWith("..$([IO.Path]::DirectorySeparatorChar)")) {
        throw "$Label must remain inside the Captain workspace."
    }
    return $candidate
}

function Invoke-PythonFactory([string[]]$Arguments) {
    $output = & $python -m agenten.agent_factory.capability_factory_entrypoint @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw 'Python capability-factory entrypoint failed closed.'
    }
    return ($output -join "`n")
}

Push-Location $root
try {
    # STEP 1: validate local inputs, redacted aliases, URLs, and the pinned image.
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw 'The project Python 3.11 environment is required.'
    }
    # A caller may opt into the narrowly-redacted local diagnostics even when
    # the checked-in demo environment keeps that switch disabled.
    $diagnosticOverride = [Environment]::GetEnvironmentVariable('CAPTAIN_RUNTIME_EVIDENCE_DIAGNOSTICS', 'Process')
    $values = Read-LocalEnvironment $rootEnv
    foreach ($item in $values.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable([string]$item.Key, [string]$item.Value, 'Process')
    }
    if ($diagnosticOverride -eq '1') {
        [Environment]::SetEnvironmentVariable('CAPTAIN_RUNTIME_EVIDENCE_DIAGNOSTICS', '1', 'Process')
    }
    $selectedInput = Resolve-SafeWorkspacePath $InputPath 'InputPath'
    if (-not (Test-Path -LiteralPath $selectedInput -PathType Leaf)) {
        throw 'Selected TO_BE_BUILT input file does not exist.'
    }
    $artifactPath = Resolve-SafeWorkspacePath $ArtifactDirectory 'ArtifactDirectory'
    $checkpointPath = Resolve-SafeWorkspacePath $CheckpointDirectory 'CheckpointDirectory'
    $RuntimeUrl = if ($RuntimeUrl) { $RuntimeUrl } else { Require-Value $values 'CAPTAIN_RUNTIME_URL' }
    $MinibookUrl = if ($MinibookUrl) { $MinibookUrl } else { Require-Value $values 'MINIBOOK_BACKEND_URL' }
    $SandboxImage = if ($SandboxImage) { $SandboxImage } else { Require-Value $values 'CAPTAIN_CAPABILITY_SANDBOX_IMAGE' }
    foreach ($name in @(
        'CAPTAIN_GATEWAY_TOKEN', 'WORKER_GATEWAY_TOKEN', 'CAPTAIN_RUNTIME_TOKEN',
        'MINIBOOK_API_KEY', 'MINIBOOK_PROJECTION_API_KEY',
        'CAPABILITY_FACTORY_ENTRYPOINT_ADAPTER_MANIFEST', 'CAPABILITY_FACTORY_ENTRYPOINT_ADAPTER_SHA256',
        'FACTORY_LIVE_RUNTIME_ADAPTER_MANIFEST', 'FACTORY_LIVE_RUNTIME_ADAPTER_SHA256',
        'OPENAI_API_KEY',
        'CAPTAIN_RUNTIME_ARTIFACT_ROOT'
    )) { $null = Require-Value $values $name }
    if ($SandboxImage -notmatch '^(?:[a-z0-9.-]+(?::[0-9]+)?/)?captain-[a-z0-9._/-]+@sha256:[0-9a-f]{64}$') {
        throw 'A Captain-owned digest-pinned capability sandbox image is required.'
    }
    $commonArguments = @(
        '--input', $selectedInput,
        '--artifact-dir', $artifactPath,
        '--checkpoint-dir', $checkpointPath,
        '--gateway-url', $GatewayUrl,
        '--runtime-url', $RuntimeUrl,
        '--minibook-url', $MinibookUrl,
        '--sandbox-image', $SandboxImage,
        '--correlation-id', $CorrelationId.ToString(),
        '--subject-version', $SubjectVersion.ToString(),
        '--wall-clock-budget-seconds', $WallClockBudgetSeconds.ToString()
    )

    # STEP 2: fail closed before starting services when production adapters are absent.
    $null = Invoke-PythonFactory ($commonArguments + '--preflight-only')
    & docker image inspect $SandboxImage *> $null
    if ($LASTEXITCODE -ne 0) {
        throw 'The pinned capability sandbox image is unavailable locally; pulling is forbidden.'
    }

    # STEP 3: attest the exact isolated MariaDB identity without exposing its DSN.
    $env:LEDGER_DSN = Require-Value $values 'TEST_MARIADB_DSN'
    & $python -c @'
import os
from urllib.parse import urlsplit
from blockchain.mariadb_storage import MariaDBStorage
dsn = os.environ["LEDGER_DSN"]
parsed = urlsplit(dsn)
if parsed.scheme not in {"mysql", "mariadb"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.path.strip("/") != "captain_test":
    raise SystemExit(2)
with MariaDBStorage(dsn).transaction() as connection:
    with connection.cursor() as cursor:
        cursor.execute("SELECT DATABASE() AS database_name")
        row = cursor.fetchone()
if row is None or row["database_name"] != "captain_test":
    raise SystemExit(3)
'@ *> $null
    if ($LASTEXITCODE -ne 0) { throw 'MariaDB did not attest database exactly captain_test.' }

    # STEP 4: start a dedicated Gateway or attest the managed demo Gateway.
    $gatewayUri = [Uri]$GatewayUrl
    if ($gatewayUri.Scheme -ne 'http' -or $gatewayUri.Host -notin @('127.0.0.1', 'localhost')) {
        throw 'The dedicated Gateway URL must be loopback HTTP.'
    }
    $health = $null
    $portOccupied = Test-NetConnection -ComputerName $gatewayUri.Host -Port $gatewayUri.Port -InformationLevel Quiet
    if ($portOccupied) {
        if (-not $UseManagedGateway) {
            throw 'The dedicated Gateway port is already occupied; adoption is forbidden.'
        }
        try {
            $headers = @{ Authorization = "Bearer $($values['CAPTAIN_GATEWAY_TOKEN'])" }
            $authority = Invoke-WebRequest -Uri "$GatewayUrl/batches?status=READY" -Headers $headers -TimeoutSec 3
            $health = Invoke-RestMethod -Uri "$GatewayUrl/healthz" -TimeoutSec 3
        } catch {
            throw 'Managed Gateway failed authenticated authority attestation.'
        }
        if ($authority.StatusCode -ne 200 -or $health.status -ne 'ok') {
            throw 'Managed Gateway failed authenticated authority attestation.'
        }
    } else {
        if ($UseManagedGateway) { throw 'The managed Gateway is not listening on its configured endpoint.' }
        $env:GATEWAY_PORT = $gatewayUri.Port.ToString()
        $gatewayProcess = Start-Process -FilePath $python -ArgumentList @('-m', 'gateway.app') -PassThru -WindowStyle Hidden
        foreach ($attempt in 1..60) {
            try {
                $health = Invoke-RestMethod -Uri "$GatewayUrl/healthz" -TimeoutSec 2
                if ($health.status -eq 'ok') { break }
            } catch {}
            if ($gatewayProcess.HasExited) { throw 'Dedicated captain_test Gateway exited during startup.' }
            Start-Sleep -Milliseconds 500
        }
    }
    if ($null -eq $health -or $health.status -ne 'ok') { throw 'Dedicated captain_test Gateway did not become healthy.' }

    # STEP 5: health-check every dependent service before the first factory mutation.
    foreach ($target in @("$RuntimeUrl/health", "$MinibookUrl/health")) {
        $response = Invoke-WebRequest -Uri $target -TimeoutSec 3
        if ($response.StatusCode -lt 200 -or $response.StatusCode -ge 300) {
            throw "Required factory service is unhealthy: $target"
        }
    }

    # Execute creation/reuse, publication when needed, and one capability run.
    $run = $null
    foreach ($attempt in 1..80) {
        $runJson = Invoke-PythonFactory $commonArguments
        $run = $runJson | ConvertFrom-Json
        if ($run.status -ne 'ready_to_use') { throw 'Capability factory did not reach ready_to_use.' }
        if ($run.summary.execution_state -eq 'completed') { break }
        if ($run.summary.execution_state -ne 'retry_pending') {
            throw 'Capability runtime did not reach a replayable terminal state.'
        }
        Start-Sleep -Seconds 15
    }
    if ($null -eq $run -or $run.summary.execution_state -ne 'completed') {
        throw 'Capability runtime did not complete within the bounded replay window.'
    }

    # STEP 6: bind the content-addressed manifest into the explicit provider-backed live gate.
    $manifestPath = Join-Path $root ([string]$run.manifest)
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw 'Capability factory did not produce its evidence manifest.'
    }
    $env:CAPABILITY_FACTORY_LIVE_MANIFEST = $manifestPath
    $env:CAPABILITY_FACTORY_LIVE_REQUIRED = '1'
    $env:CAPTAIN_GATEWAY_URL = $GatewayUrl
    & $python -m pytest -q --no-cov -m live $liveTest
    if ($LASTEXITCODE -ne 0) { throw 'Provider-backed capability evidence gate failed.' }

    # STEP 7: require the live gate to rebuild and duplicate-replay the projection.
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if (@($manifest.summary.projection_event_ids).Count -lt 1) {
        throw 'No correlated projection event was recorded.'
    }

    # STEP 8: retain only stable IDs, digests, targets, and measured timings.
    if (-not $run.digests.manifest_sha256 -or -not $run.timings.duration_seconds) {
        throw 'CLI result omitted required digest or timing evidence.'
    }

    # STEP 9: perform the final non-mutating check against the dedicated Gateway.
    $finalHealth = Invoke-RestMethod -Uri "$GatewayUrl/healthz" -TimeoutSec 3
    if ($finalHealth.status -ne 'ok') { throw 'Final dedicated Gateway health check failed.' }

    # STEP 10: print the already-redacted Python result plus commit and DB identity.
    [ordered]@{
        status = $run.status
        summary = $run.summary
        manifest = $run.manifest
        targets = $run.targets
        timings = $run.timings
        digests = $run.digests
        database = 'captain_test'
        commit_sha = (& git rev-parse HEAD).Trim()
    } | ConvertTo-Json -Depth 8
} finally {
    Remove-Item Env:CAPABILITY_FACTORY_LIVE_REQUIRED -ErrorAction SilentlyContinue
    Remove-Item Env:CAPABILITY_FACTORY_LIVE_MANIFEST -ErrorAction SilentlyContinue
    if ($null -ne $gatewayProcess -and -not $gatewayProcess.HasExited) {
        Stop-Process -Id $gatewayProcess.Id -Force
        $gatewayProcess.WaitForExit()
    }
    Pop-Location
}
