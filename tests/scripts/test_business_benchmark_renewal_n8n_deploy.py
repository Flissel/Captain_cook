from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "deploy-business-benchmark-renewal-n8n.ps1"
WORKFLOW = (
    ROOT
    / "examples"
    / "business_benchmark_candidates"
    / "customer_renewal_orchestration_team"
    / "workflows"
    / "renewal_context_read.json"
)


def _run_validate(path: Path) -> subprocess.CompletedProcess[str]:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is unavailable")
    return subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(SCRIPT),
            "-Action",
            "Validate",
            "-WorkflowPath",
            str(path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _pwsh_literal(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _run_harness(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is unavailable")
    harness = tmp_path / "harness.ps1"
    harness.write_text(source, encoding="utf-8")
    return subprocess.run(
        [pwsh, "-NoProfile", "-File", str(harness)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_validate_accepts_canonical_workflow_and_reports_exact_digest() -> None:
    result = _run_validate(WORKFLOW)

    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence == {
        "schema": "captain.business-benchmark-renewal-n8n-validation.v1",
        "status": "validated",
        "workflow_name": "Captain Renewal Context Read v1",
        "canonical_sha256": hashlib.sha256(WORKFLOW.read_bytes()).hexdigest(),
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("active", True),
        ("contract.effect", "write"),
        ("contract.intent", "generic"),
        ("contract.idempotency", "optional"),
        ("contract.mutation_operations", ["update"]),
    ],
)
def test_validate_rejects_non_read_only_contract(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    payload = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    target = payload
    parts = field.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value
    path = tmp_path / "workflow.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = _run_validate(path)

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "credential" not in combined.casefold()
    assert "token" not in combined.casefold()


def test_validate_rejects_unknown_top_level_publish_material(tmp_path: Path) -> None:
    payload = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    payload["credentials"] = {"secret": "must-not-be-published"}
    path = tmp_path / "workflow.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = _run_validate(path)

    assert result.returncode != 0
    assert "must-not-be-published" not in result.stdout + result.stderr


def test_deploy_source_is_captain_only_fail_closed_and_idempotent() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "http://127.0.0.1:5679" in source
    assert "15678" not in source
    assert 'ValidateSet("Validate", "Deploy")' in source
    assert "CAPTAIN_N8N_API_KEY" in source
    assert "CAPTAIN_N8N_MCP_TOKEN" in source
    assert ".env.captain-n8n" in source
    assert "X-N8N-API-KEY" in source
    assert 'Authorization = "Bearer $McpToken"' in source
    assert "-Verb POST" in source
    assert "-Verb PUT" in source
    assert "-Verb DELETE" not in source
    assert "-Verb PATCH" not in source
    assert "allowedPublishFields" in source
    assert '@("name", "nodes", "connections", "settings")' in source
    assert "published_sha256" in source
    assert "canonical_sha256" in source
    assert "workflow_id" in source
    assert "ownership" in source.casefold()
    assert "tools/call" in source
    assert '"execute_workflow"' in source
    assert '"get_execution"' in source
    assert ".captain-cook" in source
    assert "renewal-context-n8n-ownership.v1.json" in source
    assert "renewal-context-n8n-deployments" in source
    assert "renewal-context-n8n-activations" in source
    assert "renewal-context-n8n-smoke-receipts" in source
    assert "Write-ImmutableJson" in source
    assert "Move-Item" not in source


def test_script_never_serializes_or_echoes_secret_values() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    forbidden = (
        "Write-Host $ApiKey",
        "Write-Output $ApiKey",
        "Write-Host $McpToken",
        "Write-Output $McpToken",
        "api_key = $ApiKey",
        "mcp_token = $McpToken",
    )
    assert all(value not in source for value in forbidden)


def test_create_first_readback_failure_then_retry_reuses_exact_owned_workflow(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    harness = f"""
$ErrorActionPreference = 'Stop'
$env:CAPTAIN_N8N_API_KEY = 'dummy-api-value-for-test'
$env:CAPTAIN_N8N_MCP_TOKEN = 'dummy-mcp-value-for-test'
$env:CAPTAIN_N8N_PORT = '5679'
$global:remote = $null
$global:posts = 0
$global:puts = 0
$global:executes = 0
$global:gets = 0
$global:postFields = @()
$global:activations = 0
$global:detailReads = 0
$global:activationJson = $false
function Invoke-WebRequest {{
    param($Uri, $Method = 'GET', $Headers, $Body, $ContentType, [switch]$UseBasicParsing, $TimeoutSec, $ErrorAction)
    $target = [string]$Uri
    $verb = [string]$Method
    if ($target -eq 'http://127.0.0.1:5679/healthz') {{
        return [pscustomobject]@{{ StatusCode = 200; Content = '{{}}' }}
    }}
    if ($target.StartsWith('http://127.0.0.1:5679/api/v1/workflows?') -and $verb -eq 'GET') {{
        $data = if ($null -eq $global:remote) {{ @() }} else {{ @($global:remote) }}
        return [pscustomobject]@{{ StatusCode = 200; Content = (@{{ data = $data; nextCursor = $null }} | ConvertTo-Json -Depth 40 -Compress) }}
    }}
    if ($target -eq 'http://127.0.0.1:5679/api/v1/workflows' -and $verb -eq 'POST') {{
        $global:posts++
        $payload = $Body | ConvertFrom-Json -Depth 40
        $global:postFields = @($payload.PSObject.Properties | ForEach-Object {{ $_.Name }} | Sort-Object)
        $payload | Add-Member -NotePropertyName id -NotePropertyValue 'renewal-owned-1'
        $payload | Add-Member -NotePropertyName active -NotePropertyValue $false
        $payload.settings | Add-Member -NotePropertyName saveExecutionProgress -NotePropertyValue $false
        $global:remote = $payload
        return [pscustomobject]@{{ StatusCode = 200; Content = ($payload | ConvertTo-Json -Depth 40 -Compress) }}
    }}
    if ($target -eq 'http://127.0.0.1:5679/api/v1/workflows/renewal-owned-1' -and $verb -eq 'GET') {{
        $global:detailReads++
        if ($global:detailReads -eq 1) {{ throw 'simulated readback transport loss after create' }}
        return [pscustomobject]@{{ StatusCode = 200; Content = ($global:remote | ConvertTo-Json -Depth 40 -Compress) }}
    }}
    if ($target -eq 'http://127.0.0.1:5679/api/v1/workflows/renewal-owned-1' -and $verb -eq 'PUT') {{
        $global:puts++
        throw 'unexpected update'
    }}
    if ($target -eq 'http://127.0.0.1:5679/api/v1/workflows/renewal-owned-1/activate' -and $verb -eq 'POST') {{
        $activationBody = $Body | ConvertFrom-Json -Depth 4
        if ($ContentType -ne 'application/json' -or @($activationBody.PSObject.Properties).Count -ne 0) {{ throw 'activation requires canonical empty JSON' }}
        $global:activationJson = $true
        $global:activations++
        $global:remote.active = $true
        return [pscustomobject]@{{ StatusCode = 200; Content = (@{{ id = 'renewal-owned-1'; active = $true }} | ConvertTo-Json -Compress) }}
    }}
    if ($target -eq 'http://127.0.0.1:5679/mcp-server/http' -and $verb -eq 'POST') {{
        $request = $Body | ConvertFrom-Json -Depth 40
        if ($request.params.name -eq 'execute_workflow') {{
            $global:executes++
            $value = @{{ workflowId = 'renewal-owned-1'; executionId = 'execution-2'; status = 'running' }}
        }} elseif ($request.params.name -eq 'get_execution') {{
            $global:gets++
            $value = @{{
                workflowId = 'renewal-owned-1'
                executionId = 'execution-2'
                status = 'success'
                output = @{{
                    operation = 'read_renewal_context'
                    idempotency_key = 'captain-renewal-smoke-{hashlib.sha256(WORKFLOW.read_bytes()).hexdigest()[:32]}'
                    status = 'read'
                    facts = @('renewal_window.synthetic-90d', 'engagement_band.synthetic-medium', 'commercial_evidence_state.synthetic-complete', 'consent_state.synthetic-consented')
                }}
            }}
        }} else {{ throw 'unexpected MCP tool' }}
        $response = @{{ jsonrpc = '2.0'; id = $request.id; result = @{{ structuredContent = $value }} }}
        return [pscustomobject]@{{ StatusCode = 200; Content = ($response | ConvertTo-Json -Depth 40 -Compress) }}
    }}
    throw "unexpected request $verb $target"
}}
$firstFailed = $false
$firstError = $null
try {{
    $null = & {_pwsh_literal(SCRIPT)} -Action Deploy -EvidenceDirectory {_pwsh_literal(evidence)}
}} catch {{
    $firstFailed = $true
    $firstError = $_.Exception.Message
}}
$ownershipAfterFailure = @(Get-ChildItem -LiteralPath {_pwsh_literal(evidence)} -Filter 'renewal-context-n8n-ownership.v1.json' -File -ErrorAction SilentlyContinue).Count
$deploymentAfterFailure = @(Get-ChildItem -LiteralPath (Join-Path {_pwsh_literal(evidence)} 'renewal-context-n8n-deployments') -Filter '*.json' -File -ErrorAction SilentlyContinue).Count
$secondFailed = $false
$secondError = $null
try {{
    $null = & {_pwsh_literal(SCRIPT)} -Action Deploy -EvidenceDirectory {_pwsh_literal(evidence)}
}} catch {{
    $secondFailed = $true
    $secondError = $_.Exception.Message
}}
@{{
    first_failed = $firstFailed
    first_error = $firstError
    second_failed = $secondFailed
    second_error = $secondError
    posts = $global:posts
    puts = $global:puts
    executes = $global:executes
    gets = $global:gets
    post_fields = @($global:postFields)
    activations = $global:activations
    activation_json = $global:activationJson
    ownership_after_failure = $ownershipAfterFailure
    deployment_after_failure = $deploymentAfterFailure
    ownership_receipts = @(Get-ChildItem -LiteralPath {_pwsh_literal(evidence)} -Filter 'renewal-context-n8n-ownership.v1.json' -File -ErrorAction SilentlyContinue).Count
    deployment_receipts = @(Get-ChildItem -LiteralPath (Join-Path {_pwsh_literal(evidence)} 'renewal-context-n8n-deployments') -Filter '*.json' -File -ErrorAction SilentlyContinue).Count
    activation_receipts = @(Get-ChildItem -LiteralPath (Join-Path {_pwsh_literal(evidence)} 'renewal-context-n8n-activations') -Filter '*.json' -File -ErrorAction SilentlyContinue).Count
    smoke_receipts = @(Get-ChildItem -LiteralPath (Join-Path {_pwsh_literal(evidence)} 'renewal-context-n8n-smoke-receipts') -Filter '*.json' -File -ErrorAction SilentlyContinue).Count
}} | ConvertTo-Json -Compress
"""

    result = _run_harness(tmp_path, harness)

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["second_failed"] is False, json.dumps(summary, indent=2)
    assert summary == {
        "first_failed": True,
        "first_error": "Captain n8n REST request failed closed.",
        "second_failed": False,
        "second_error": None,
        "posts": 1,
        "puts": 0,
        "executes": 1,
        "gets": 1,
        "post_fields": ["connections", "name", "nodes", "settings"],
        "activations": 1,
        "activation_json": True,
        "ownership_after_failure": 1,
        "deployment_after_failure": 0,
        "ownership_receipts": 1,
        "deployment_receipts": 1,
        "activation_receipts": 1,
        "smoke_receipts": 1,
    }


def test_existing_same_name_without_ownership_fails_before_mutation(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    canonical = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    remote = {
        key: canonical[key] for key in ("name", "nodes", "connections", "settings")
    }
    remote["id"] = "foreign-workflow-1"
    remote_json = json.dumps(remote).replace("'", "''")
    harness = f"""
$ErrorActionPreference = 'Stop'
$env:CAPTAIN_N8N_API_KEY = 'dummy-api-value-for-test'
$env:CAPTAIN_N8N_MCP_TOKEN = 'dummy-mcp-value-for-test'
$env:CAPTAIN_N8N_PORT = '5679'
$global:mutations = 0
$remote = '{remote_json}' | ConvertFrom-Json -Depth 40
function Invoke-WebRequest {{
    param($Uri, $Method = 'GET', $Headers, $Body, $ContentType, [switch]$UseBasicParsing, $TimeoutSec, $ErrorAction)
    $target = [string]$Uri
    $verb = [string]$Method
    if ($target -eq 'http://127.0.0.1:5679/healthz') {{ return [pscustomobject]@{{ StatusCode = 200; Content = '{{}}' }} }}
    if ($target.StartsWith('http://127.0.0.1:5679/api/v1/workflows?') -and $verb -eq 'GET') {{
        return [pscustomobject]@{{ StatusCode = 200; Content = (@{{ data = @($remote); nextCursor = $null }} | ConvertTo-Json -Depth 40 -Compress) }}
    }}
    $global:mutations++
    throw 'mutation must not occur'
}}
$failed = $false
try {{
    $null = & {_pwsh_literal(SCRIPT)} -Action Deploy -EvidenceDirectory {_pwsh_literal(evidence)}
}} catch {{ $failed = $true }}
@{{ failed = $failed; mutations = $global:mutations; evidence_exists = (Test-Path -LiteralPath {_pwsh_literal(evidence)}) }} | ConvertTo-Json -Compress
"""

    result = _run_harness(tmp_path, harness)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "failed": True,
        "mutations": 0,
        "evidence_exists": False,
    }


def test_activation_unknown_outcome_is_recovered_without_second_post(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    canonical_sha = hashlib.sha256(WORKFLOW.read_bytes()).hexdigest()
    harness = f"""
$ErrorActionPreference = 'Stop'
$env:CAPTAIN_N8N_API_KEY = 'dummy-api-value-for-test'
$env:CAPTAIN_N8N_MCP_TOKEN = 'dummy-mcp-value-for-test'
$env:CAPTAIN_N8N_PORT = '5679'
$global:remote = $null
$global:posts = 0
$global:puts = 0
$global:activations = 0
$global:executes = 0
$global:gets = 0
function Invoke-WebRequest {{
    param($Uri, $Method = 'GET', $Headers, $Body, $ContentType, [switch]$UseBasicParsing, $TimeoutSec, $ErrorAction)
    $target = [string]$Uri
    $verb = [string]$Method
    if ($target -eq 'http://127.0.0.1:5679/healthz') {{ return [pscustomobject]@{{ StatusCode = 200; Content = '{{}}' }} }}
    if ($target.StartsWith('http://127.0.0.1:5679/api/v1/workflows?') -and $verb -eq 'GET') {{
        $data = if ($null -eq $global:remote) {{ @() }} else {{ @($global:remote) }}
        return [pscustomobject]@{{ StatusCode = 200; Content = (@{{ data = $data; nextCursor = $null }} | ConvertTo-Json -Depth 40 -Compress) }}
    }}
    if ($target -eq 'http://127.0.0.1:5679/api/v1/workflows' -and $verb -eq 'POST') {{
        $global:posts++
        $global:remote = $Body | ConvertFrom-Json -Depth 40
        $global:remote | Add-Member -NotePropertyName id -NotePropertyValue 'renewal-owned-activation'
        $global:remote | Add-Member -NotePropertyName active -NotePropertyValue $false
        $global:remote.settings | Add-Member -NotePropertyName providerDefault -NotePropertyValue $false
        return [pscustomobject]@{{ StatusCode = 200; Content = ($global:remote | ConvertTo-Json -Depth 40 -Compress) }}
    }}
    if ($target -eq 'http://127.0.0.1:5679/api/v1/workflows/renewal-owned-activation' -and $verb -eq 'GET') {{
        return [pscustomobject]@{{ StatusCode = 200; Content = ($global:remote | ConvertTo-Json -Depth 40 -Compress) }}
    }}
    if ($target -eq 'http://127.0.0.1:5679/api/v1/workflows/renewal-owned-activation' -and $verb -eq 'PUT') {{
        $global:puts++
        throw 'unexpected update'
    }}
    if ($target -eq 'http://127.0.0.1:5679/api/v1/workflows/renewal-owned-activation/activate' -and $verb -eq 'POST') {{
        $global:activations++
        if ($global:activations -gt 1) {{ throw 'duplicate activation forbidden' }}
        $global:remote.active = $true
        throw 'simulated lost activation response'
    }}
    if ($target -eq 'http://127.0.0.1:5679/mcp-server/http' -and $verb -eq 'POST') {{
        $request = $Body | ConvertFrom-Json -Depth 40
        if ($request.params.name -eq 'execute_workflow') {{
            $global:executes++
            $value = @{{ workflowId = 'renewal-owned-activation'; executionId = 'activation-execution-1'; status = 'running' }}
        }} elseif ($request.params.name -eq 'get_execution') {{
            $global:gets++
            $value = @{{
                workflowId = 'renewal-owned-activation'
                executionId = 'activation-execution-1'
                status = 'success'
                output = @{{
                    operation = 'read_renewal_context'
                    idempotency_key = 'captain-renewal-smoke-{canonical_sha[:32]}'
                    status = 'read'
                    facts = @('renewal_window.synthetic-90d', 'engagement_band.synthetic-medium', 'commercial_evidence_state.synthetic-complete', 'consent_state.synthetic-consented')
                }}
            }}
        }} else {{ throw 'unexpected MCP tool' }}
        $response = @{{ jsonrpc = '2.0'; id = $request.id; result = @{{ structuredContent = $value }} }}
        return [pscustomobject]@{{ StatusCode = 200; Content = ($response | ConvertTo-Json -Depth 40 -Compress) }}
    }}
    throw "unexpected request $verb $target"
}}
$firstFailed = $false
try {{
    $null = & {_pwsh_literal(SCRIPT)} -Action Deploy -EvidenceDirectory {_pwsh_literal(evidence)}
}} catch {{ $firstFailed = $true }}
$activationAfterFailure = @(Get-ChildItem -LiteralPath (Join-Path {_pwsh_literal(evidence)} 'renewal-context-n8n-activations') -Filter '*.json' -File -ErrorAction SilentlyContinue).Count
$secondFailed = $false
try {{
    $null = & {_pwsh_literal(SCRIPT)} -Action Deploy -EvidenceDirectory {_pwsh_literal(evidence)}
}} catch {{ $secondFailed = $true }}
@{{
    first_failed = $firstFailed
    second_failed = $secondFailed
    posts = $global:posts
    puts = $global:puts
    activations = $global:activations
    executes = $global:executes
    gets = $global:gets
    activation_after_failure = $activationAfterFailure
    activation_receipts = @(Get-ChildItem -LiteralPath (Join-Path {_pwsh_literal(evidence)} 'renewal-context-n8n-activations') -Filter '*.json' -File -ErrorAction SilentlyContinue).Count
    smoke_receipts = @(Get-ChildItem -LiteralPath (Join-Path {_pwsh_literal(evidence)} 'renewal-context-n8n-smoke-receipts') -Filter '*.json' -File -ErrorAction SilentlyContinue).Count
}} | ConvertTo-Json -Compress
"""

    result = _run_harness(tmp_path, harness)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "first_failed": True,
        "second_failed": False,
        "posts": 1,
        "puts": 0,
        "activations": 1,
        "executes": 1,
        "gets": 1,
        "activation_after_failure": 0,
        "activation_receipts": 1,
        "smoke_receipts": 1,
    }
