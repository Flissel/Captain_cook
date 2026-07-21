from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR = ROOT / "scripts" / "run-capability-live-demo.ps1"
RUNBOOK = ROOT / "docs" / "CAPABILITY_LIVE_DEMO.md"


def _run(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(ORCHESTRATOR), *arguments],
        cwd=ROOT,
        env=env or os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )


def _write_fake_service_runner(path: Path) -> None:
    path.write_text(
        """
param(
    [Parameter(Position=0)][string]$Action,
    [switch]$RecoverDemoCredentials,
    [string]$CredentialSourceEnv
)
Add-Content -LiteralPath (Join-Path $env:FAKE_STATE_DIR 'services.log') -Value $Action
Write-Host "[fake] service action $Action"
""".strip(),
        encoding="utf-8",
    )


def _write_fake_capability_runner(path: Path) -> None:
    path.write_text(
        """
param(
    [Parameter(Mandatory)][string]$InputPath,
    [Parameter(Mandatory)][Guid]$CorrelationId,
    [string]$ArtifactDirectory,
    [string]$CheckpointDirectory,
    [int]$WallClockBudgetSeconds,
    [string]$GatewayUrl
)
$statePath = Join-Path $env:FAKE_STATE_DIR ("$CorrelationId.json")
Add-Content -LiteralPath (Join-Path $env:FAKE_STATE_DIR 'artifact-directories.log') -Value ([IO.Path]::GetFullPath($ArtifactDirectory))
Add-Content -LiteralPath (Join-Path $env:FAKE_STATE_DIR 'runtime-costs.log') -Value $env:CAPTAIN_FACTORY_MAX_COST_USD
Add-Content -LiteralPath (Join-Path $env:FAKE_STATE_DIR 'gateway-urls.log') -Value $GatewayUrl
if (Test-Path -LiteralPath $statePath) {
    $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    $mode = 'reused'
    $creationJobId = $null
    $recoveryId = $null
    $e2e = @()
    $releaseEvidence = @()
} else {
    $state = [ordered]@{
        command_id = [Guid]::NewGuid().ToString()
        result_id = [Guid]::NewGuid().ToString()
        capability_id = "demo-$([IO.Path]::GetFileName((Split-Path $InputPath -Parent)))"
        capability_version = 1
        package_sha256 = ('a' * 64)
        terminal_decision_id = [Guid]::NewGuid().ToString()
        release_authority_job_id = [Guid]::NewGuid().ToString()
    }
    $state | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding utf8
    $mode = 'created'
    $creationJobId = [Guid]::NewGuid().ToString()
    $recoveryId = "controlled-$CorrelationId"
    $e2e = @("$CorrelationId-e2e-1", "$CorrelationId-e2e-2", "$CorrelationId-e2e-3")
    $releaseEvidence = @(('b' * 64), ('c' * 64), ('d' * 64), ('e' * 64))
}
$summary = [ordered]@{
    correlation_id = $CorrelationId.ToString()
    factory_job_id = $state.release_authority_job_id
    invocation_job_id = $state.release_authority_job_id
    release_authority_job_id = $state.release_authority_job_id
    execution_mode = $mode
    execution_state = 'completed'
    retry_expires_at = $null
    creation_job_id = $creationJobId
    terminal_decision_id = $state.terminal_decision_id
    terminal_state = 'ready_to_use'
    capability_id = $state.capability_id
    capability_version = $state.capability_version
    recovery_id = $recoveryId
    e2e_batch_ids = $e2e
    execution_command_id = $state.command_id
    execution_result_id = $state.result_id
    projection_event_ids = @($(if ($env:FAKE_BAD_PROJECTION -eq '1') { [Guid]::NewGuid().ToString() } else { $state.result_id }))
    minibook_projection_verified = $true
    package_sha256 = $state.package_sha256
    release_evidence_sha256 = $releaseEvidence
    unresolved_required_tool_gaps = @()
    unresolved_optional_tool_gaps = @()
}
[ordered]@{
    schema = 'captain.capability-factory-cli-result.v1'
    status = 'ready_to_use'
    summary = $summary
    manifest = "artifacts/fake/$CorrelationId.json"
    timings = @{ duration_seconds = 0.01 }
    digests = @{ input_sha256 = ('f' * 64); manifest_sha256 = ('1' * 64) }
} | ConvertTo-Json -Depth 8
""".strip(),
        encoding="utf-8",
    )


def test_orchestrator_source_is_explicit_fail_closed_and_redacted() -> None:
    source = ORCHESTRATOR.read_text(encoding="utf-8")

    assert "[switch]$LiveProviders" in source
    assert "[switch]$ConfirmProviderCost" in source
    assert "run-capability-factory-live.ps1" in source
    assert "demo_inputs/agent_factory/manifest.json" in source.replace("\\", "/")
    assert "Exactly three distinct demo inputs are required" in source
    assert "controlled recovery evidence" in source
    assert "live-demo-services.ps1" in source
    assert "execution_mode -ne 'reused'" in source
    assert "restart/resume changed execution identity" in source
    assert ".captain-cook/evidence" in source.replace("\\", "/")
    assert "projection_event_ids" in source
    assert "unresolved_required_tool_gaps" in source
    assert "ffmpeg" in source.lower()
    assert "gdigrab" in source
    assert "desktop" not in source.lower()
    assert "OPENAI" not in source.upper()
    assert "Get-ChildItem Env:" not in source
    assert "[int]$WallClockBudgetSeconds = 600" in source
    assert "CAPTAIN_FACTORY_RUNTIME_SECONDS" in source
    assert "Factory runtime budget disagrees with the live orchestrator budget" in source


def test_without_provider_opt_in_only_validates_and_invokes_nothing(tmp_path: Path) -> None:
    fake_service = tmp_path / "fake-services.ps1"
    fake_capability = tmp_path / "fake-capability.ps1"
    _write_fake_service_runner(fake_service)
    _write_fake_capability_runner(fake_capability)
    state = tmp_path / "state"
    state.mkdir()
    environment = os.environ.copy()
    environment["FAKE_STATE_DIR"] = str(state)

    result = _run(
        "-CapabilityRunnerPath", str(fake_capability),
        "-ServiceRunnerPath", str(fake_service),
        env=environment,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "live providers were not requested" in output
    assert list(state.iterdir()) == []


def test_live_run_rejects_runtime_budget_mismatch_before_service_start(
    tmp_path: Path,
) -> None:
    fake_service = tmp_path / "fake-services.ps1"
    fake_capability = tmp_path / "fake-capability.ps1"
    _write_fake_service_runner(fake_service)
    _write_fake_capability_runner(fake_capability)
    state = tmp_path / "state"
    state.mkdir()
    environment = os.environ.copy()
    environment["FAKE_STATE_DIR"] = str(state)
    environment["CAPTAIN_FACTORY_RUNTIME_SECONDS"] = "601"

    result = _run(
        "-LiveProviders",
        "-ConfirmProviderCost",
        "-CapabilityRunnerPath", str(fake_capability),
        "-ServiceRunnerPath", str(fake_service),
        env=environment,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Factory runtime budget disagrees" in output
    assert list(state.iterdir()) == []


def test_live_run_rejects_cost_mismatch_before_service_start(tmp_path: Path) -> None:
    fake_service = tmp_path / "fake-services.ps1"
    fake_capability = tmp_path / "fake-capability.ps1"
    _write_fake_service_runner(fake_service)
    _write_fake_capability_runner(fake_capability)
    state = tmp_path / "state"
    state.mkdir()
    environment = os.environ.copy()
    environment["FAKE_STATE_DIR"] = str(state)
    environment["CAPTAIN_FACTORY_MAX_COST_USD"] = "0.50"
    shared_artifacts = tmp_path / "shared-capability-artifacts"
    environment["CAPTAIN_RUNTIME_ARTIFACT_ROOT"] = str(shared_artifacts)
    environment["MINIBOOK_CREATION_ARTIFACTS"] = str(shared_artifacts)

    result = _run(
        "-LiveProviders",
        "-ConfirmProviderCost",
        "-MaxCostUsdPerInput", "1.00",
        "-CapabilityRunnerPath", str(fake_capability),
        "-ServiceRunnerPath", str(fake_service),
        "-CredentialSourceEnv", str(tmp_path / "missing.env"),
        env=environment,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Factory cost budget disagrees" in output
    assert list(state.iterdir()) == []


def test_fake_live_run_proves_recovery_restart_three_inputs_and_redacts_evidence(
    tmp_path: Path,
) -> None:
    fake_service = tmp_path / "fake-services.ps1"
    fake_capability = tmp_path / "fake-capability.ps1"
    _write_fake_service_runner(fake_service)
    _write_fake_capability_runner(fake_capability)
    state = tmp_path / "state"
    evidence_dir = tmp_path / "evidence"
    state.mkdir()
    environment = os.environ.copy()
    environment["FAKE_STATE_DIR"] = str(state)
    environment["OPENAI_API_KEY"] = "provider-secret-must-not-appear"
    shared_artifacts = tmp_path / "shared-capability-artifacts"
    environment["CAPTAIN_RUNTIME_ARTIFACT_ROOT"] = str(shared_artifacts)
    environment["MINIBOOK_CREATION_ARTIFACTS"] = str(shared_artifacts)
    environment["CAPTAIN_GATEWAY_URL"] = "http://127.0.0.1:19090"

    result = _run(
        "-LiveProviders",
        "-ConfirmProviderCost",
        "-MaxCostUsdPerInput", "1.00",
        "-CapabilityRunnerPath", str(fake_capability),
        "-ServiceRunnerPath", str(fake_service),
        "-EvidenceDirectory", str(evidence_dir),
        "-CredentialSourceEnv", str(tmp_path / "missing.env"),
        env=environment,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "provider-secret-must-not-appear" not in output
    service_actions = (state / "services.log").read_text(encoding="utf-8").splitlines()
    assert service_actions == ["start", "stop", "start"]
    artifact_directories = (
        state / "artifact-directories.log"
    ).read_text(encoding="utf-8").splitlines()
    assert len(artifact_directories) == 4
    assert {Path(value) for value in artifact_directories} == {shared_artifacts}
    assert (state / "runtime-costs.log").read_text(encoding="utf-8").splitlines() == [
        "1.00",
        "1.00",
        "1.00",
        "1.00",
    ]
    assert (state / "gateway-urls.log").read_text(encoding="utf-8").splitlines() == [
        "http://127.0.0.1:19090",
        "http://127.0.0.1:19090",
        "http://127.0.0.1:19090",
        "http://127.0.0.1:19090",
    ]
    evidence_files = list(evidence_dir.glob("capability-live-demo-*.json"))
    assert len(evidence_files) == 1
    evidence_text = evidence_files[0].read_text(encoding="utf-8")
    assert "provider-secret-must-not-appear" not in evidence_text
    evidence = json.loads(evidence_text)
    assert evidence["schema"] == "captain.capability-live-demo-evidence.v1"
    assert evidence["status"] == "ready_to_use"
    assert evidence["controlled_recovery_verified"] is True
    assert evidence["restart_resume_verified"] is True
    assert evidence["gateway_execution_verified"] is True
    assert evidence["minibook_projection_verified"] is True
    assert len(evidence["runs"]) == 3
    assert len({run["correlation_id"] for run in evidence["runs"]}) == 3
    assert len({run["pattern"] for run in evidence["runs"]}) == 3
    assert all(run["terminal_state"] == "ready_to_use" for run in evidence["runs"])
    assert all(run["execution_state"] == "completed" for run in evidence["runs"])
    assert all(run["projection_event_ids"] for run in evidence["runs"])
    assert set(evidence["restart_resume"]) == {
        "capability_id",
        "capability_version",
        "correlation_id",
        "execution_command_id",
        "execution_result_id",
        "projection_event_ids",
    }
    assert not any(key.lower().endswith(("token", "key", "secret")) for key in evidence)


def test_runbook_separates_validation_live_cost_and_named_window_recording() -> None:
    content = RUNBOOK.read_text(encoding="utf-8")

    assert "-LiveProviders" in content
    assert "-ConfirmProviderCost" in content
    assert "-MaxCostUsdPerInput" in content
    assert "-RecordVideo" in content
    assert "-RecordingWindowTitle" in content
    assert "named window" in content.lower()
    assert "captain_test" in content
    assert "three" in content.lower()
    assert "restart" in content.lower()
    assert "VibeMind" in content


def test_live_run_rejects_projection_that_is_not_the_execution_result(tmp_path: Path) -> None:
    fake_service = tmp_path / "fake-services.ps1"
    fake_capability = tmp_path / "fake-capability.ps1"
    _write_fake_service_runner(fake_service)
    _write_fake_capability_runner(fake_capability)
    state = tmp_path / "state"
    evidence_dir = tmp_path / "evidence"
    state.mkdir()
    environment = os.environ.copy()
    environment["FAKE_STATE_DIR"] = str(state)
    environment["FAKE_BAD_PROJECTION"] = "1"

    result = _run(
        "-LiveProviders",
        "-ConfirmProviderCost",
        "-CapabilityRunnerPath", str(fake_capability),
        "-ServiceRunnerPath", str(fake_service),
        "-EvidenceDirectory", str(evidence_dir),
        env=environment,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "projection does not bind the execution result" in output
    assert not list(evidence_dir.glob("capability-live-demo-*.json"))
