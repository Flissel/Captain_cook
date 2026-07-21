#requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$InputPath,
    [string]$GatewayUrl,
    [string]$RuntimeUrl,
    [string]$MinibookUrl,
    [string]$ArtifactDirectory = 'artifacts/capability-factory',
    [string]$CheckpointDirectory = '.superpowers/sdd/capability-factory-checkpoints',
    [string]$SandboxImage,
    [Guid]$CorrelationId = [Guid]::NewGuid(),
    [ValidateRange(1, 86400)]
    [int]$WallClockBudgetSeconds = 600
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$rootEnv = Join-Path $root '.env'
$testCompose = Join-Path $root 'docker-compose.test.yml'
$captainCompose = Join-Path $root 'docker-compose.yml'
$servicesScript = Join-Path $PSScriptRoot 'live-demo-services.ps1'
$liveTest = Join-Path $root 'tests/live/test_to_be_built_capability_factory_live.py'
$python = Join-Path $root '.venv/Scripts/python.exe'

function Read-LocalEnvironment([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'The gitignored root .env is required for the live gate.'
    }
    $allowed = @(
        'CAPTAIN_GATEWAY_TOKEN', 'CAPTAIN_RUNTIME_TOKEN', 'CAPTAIN_GATEWAY_URL',
        'CAPTAIN_RUNTIME_URL', 'TEST_MARIADB_DSN', 'LEDGER_DSN',
        'MINIBOOK_BACKEND_URL', 'MINIBOOK_API_KEY', 'MINIBOOK_PROJECTION_API_KEY',
        'CAPTAIN_CAPABILITY_SANDBOX_IMAGE', 'MARIADB_PASSWORD',
        'MARIADB_ROOT_PASSWORD', 'MARIADB_TEST_PASSWORD',
        'MARIADB_TEST_ROOT_PASSWORD', 'MARIADB_TEST_PORT', 'GATEWAY_PORT'
    )
    $values = [ordered]@{}
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith('#')) {
            continue
        }
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

function Invoke-ComposeValidation($Values) {
    foreach ($item in $Values.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable([string]$item.Key, [string]$item.Value, 'Process')
    }
    & docker compose --env-file $rootEnv --file $testCompose config --quiet *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Isolated test Compose configuration is invalid.' }
    & docker compose --env-file $rootEnv --file $captainCompose config --quiet *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Captain service Compose configuration is invalid.' }
}

function Assert-IsolatedDatabase($Values) {
    $testDsn = Require-Value $Values 'TEST_MARIADB_DSN'
    $ledgerDsn = Require-Value $Values 'LEDGER_DSN'
    $env:CAPABILITY_FACTORY_TEST_DSN = $testDsn
    $env:CAPABILITY_FACTORY_LEDGER_DSN = $ledgerDsn
    try {
        & $python -c @'
import os
from urllib.parse import urlsplit
for name in ("CAPABILITY_FACTORY_TEST_DSN", "CAPABILITY_FACTORY_LEDGER_DSN"):
    parsed = urlsplit(os.environ[name])
    if parsed.scheme not in {"mariadb", "mysql"}:
        raise SystemExit(2)
    if parsed.path.strip("/") != "captain_test":
        raise SystemExit(3)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit(4)
'@ *> $null
        if ($LASTEXITCODE -ne 0) {
            throw 'Live Gate requires loopback MariaDB database exactly captain_test.'
        }
    } finally {
        Remove-Item Env:CAPABILITY_FACTORY_TEST_DSN -ErrorAction SilentlyContinue
        Remove-Item Env:CAPABILITY_FACTORY_LEDGER_DSN -ErrorAction SilentlyContinue
    }
}

function Assert-SandboxImage([string]$Image) {
    if ($Image -notmatch '^(?:[a-z0-9.-]+(?::[0-9]+)?/)?captain-[a-z0-9._/-]+@sha256:[0-9a-f]{64}$') {
        throw 'A Captain-owned digest-pinned capability sandbox image is required.'
    }
    $repoDigests = & docker image inspect $Image --format '{{json .RepoDigests}}' 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($repoDigests -join ''))) {
        throw 'The digest-pinned Captain capability sandbox image is unavailable locally; pulling is forbidden.'
    }
    $digests = ($repoDigests -join '') | ConvertFrom-Json
    if ($digests -notcontains $Image) {
        throw 'The local Captain capability sandbox image does not match the configured digest.'
    }
}

function Assert-FactoryInput([string]$SelectedInput, [string]$Artifacts, [string]$Checkpoints) {
    $code = @'
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID
from agenten.agent_factory.holdout_store import InMemoryPrivateHoldoutStore
from agenten.agent_factory.input_compiler import FactoryInputCompiler
from agenten.agent_factory.input_document import load_factory_input
from agenten.agent_factory.job_builder import build_factory_job
document = load_factory_input(Path(sys.argv[1]))
compiled = FactoryInputCompiler(holdout_store=InMemoryPrivateHoldoutStore()).compile(document, 1)
job = build_factory_job(
    compiled,
    correlation_id=UUID(sys.argv[2]),
    now=datetime.now(timezone.utc),
    wall_clock_budget_seconds=int(sys.argv[3]),
)
if not job.acceptance_assertion_ids or not job.private_holdout_refs:
    raise SystemExit(5)
'@
    & $python -c $code $SelectedInput ([string]$CorrelationId) ([string]$WallClockBudgetSeconds) *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Selected TO_BE_BUILT input failed strict parser/compiler preflight.' }
    if (-not (Test-Path -LiteralPath (Split-Path -Parent $Artifacts) -PathType Container)) {
        throw 'Artifact parent directory is unavailable.'
    }
    if (-not (Test-Path -LiteralPath (Split-Path -Parent $Checkpoints) -PathType Container)) {
        throw 'Checkpoint parent directory is unavailable.'
    }
}

function Invoke-ProductionRecovery {
    throw 'Production CapabilityCreationPort, CaptainEvidenceIssuerPort, Gateway HTTP, Runtime HTTP, and Minibook projection adapters are not installed; recovery execution is blocked.'
}

function Invoke-ProductionNormalBatches {
    throw 'Normal E2E batches cannot run before the production capability-factory adapter bundle is installed.'
}

Push-Location $root
try {
    # STEP 1: validate Compose and redacted local configuration.
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw 'The project Python 3.11 environment is required.'
    }
    $values = Read-LocalEnvironment $rootEnv
    Invoke-ComposeValidation $values
    $selectedInput = Resolve-SafeWorkspacePath $InputPath 'InputPath'
    if (-not (Test-Path -LiteralPath $selectedInput -PathType Leaf)) {
        throw 'Selected TO_BE_BUILT input file does not exist.'
    }
    $artifactPath = Resolve-SafeWorkspacePath $ArtifactDirectory 'ArtifactDirectory'
    $checkpointPath = Resolve-SafeWorkspacePath $CheckpointDirectory 'CheckpointDirectory'
    $GatewayUrl = if ($GatewayUrl) { $GatewayUrl } else { Require-Value $values 'CAPTAIN_GATEWAY_URL' }
    $RuntimeUrl = if ($RuntimeUrl) { $RuntimeUrl } else { Require-Value $values 'CAPTAIN_RUNTIME_URL' }
    $MinibookUrl = if ($MinibookUrl) { $MinibookUrl } else { Require-Value $values 'MINIBOOK_BACKEND_URL' }
    $SandboxImage = if ($SandboxImage) { $SandboxImage } else { Require-Value $values 'CAPTAIN_CAPABILITY_SANDBOX_IMAGE' }
    foreach ($name in @('CAPTAIN_GATEWAY_TOKEN','CAPTAIN_RUNTIME_TOKEN','MINIBOOK_API_KEY','MINIBOOK_PROJECTION_API_KEY')) {
        $null = Require-Value $values $name
    }
    Assert-SandboxImage $SandboxImage

    # STEP 2: require isolated loopback database exactly captain_test.
    Assert-IsolatedDatabase $values

    # STEP 3: start and health-check only Captain services, Runtime, and Minibook.
    & $servicesScript start
    if ($LASTEXITCODE -ne 0) { throw 'Captain service start/health gate failed.' }

    # STEP 4: strict-preflight the selected TO_BE_BUILT document.
    Assert-FactoryInput $selectedInput $artifactPath $checkpointPath

    # STEP 5: run one intentional expected-failure recovery case.
    Invoke-ProductionRecovery

    # STEP 6: run three distinct normal E2E batches without test-side repair.
    Invoke-ProductionNormalBatches

    # STEP 7: query Gateway terminal, package, execution, and ordered feed.
    $env:CAPABILITY_FACTORY_LIVE_REQUIRED = '1'
    & $python -m pytest -q --no-cov -m live $liveTest
    if ($LASTEXITCODE -ne 0) { throw 'Provider-backed Gateway evidence gate failed.' }

    # STEP 8: rebuild and read the Minibook projection.
    # The live test above performs a zero-cursor rebuild and duplicate replay readback.

    # STEP 9: run final non-mutating health checks.
    & $servicesScript health
    if ($LASTEXITCODE -ne 0) { throw 'Final non-mutating service health gate failed.' }

    # STEP 10: print only redacted IDs, counts, timings, artifact path, and commit SHA.
    $manifestPath = [string]$env:CAPABILITY_FACTORY_LIVE_MANIFEST
    if ([string]::IsNullOrWhiteSpace($manifestPath)) {
        throw 'The production run did not provide a redacted evidence manifest.'
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $safe = [ordered]@{
        status = $manifest.summary.terminal_state
        correlation_id = $manifest.summary.correlation_id
        factory_job_id = $manifest.summary.factory_job_id
        creation_job_id = $manifest.summary.creation_job_id
        recovery_id = $manifest.summary.recovery_id
        e2e_batch_ids = @($manifest.summary.e2e_batch_ids)
        execution_command_id = $manifest.summary.execution_command_id
        execution_result_id = $manifest.summary.execution_result_id
        projection_event_ids = @($manifest.summary.projection_event_ids)
        evidence_count = @($manifest.summary.release_evidence_sha256).Count
        artifact_path = [IO.Path]::GetRelativePath($root, $manifestPath)
        database = 'captain_test'
        commit_sha = (& git rev-parse HEAD).Trim()
    }
    $safe | ConvertTo-Json -Depth 5
} finally {
    Remove-Item Env:CAPABILITY_FACTORY_LIVE_REQUIRED -ErrorAction SilentlyContinue
    Pop-Location
}
