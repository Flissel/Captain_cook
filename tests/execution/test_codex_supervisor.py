from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

import pytest

from pydantic import ValidationError

from agenten.execution.codex_supervisor import CodexRunRequest, CodexSupervisor
from agenten.execution.process import PackageExecutionStatus


class RecordingRunner:
    def __init__(self, exit_code: int, artifacts: tuple[str, ...]) -> None:
        self.exit_code = exit_code
        self.artifacts = artifacts
        self.command: tuple[str, ...] | None = None
        self.cwd: Path | None = None
        self.env: Mapping[str, str] | None = None

    async def run(self, command: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> tuple[int, tuple[str, ...]]:
        self.command = tuple(command)
        self.cwd = cwd
        self.env = env
        return self.exit_code, self.artifacts


class RecordingGateway:
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []

    async def record_codex_session(self, batch_id: str, claim_token: str, *, iteration: int, session_id: str) -> None:
        self.events.append(("codex_session", batch_id, claim_token, iteration, session_id))

    async def record_codex_process(self, batch_id: str, claim_token: str, *, iteration: int, process_id: str, state: str, command_digest: str) -> None:
        self.events.append(("codex_process", batch_id, claim_token, iteration, process_id, state, command_digest))


class FailingRunner:
    def __init__(self) -> None:
        self.env: Mapping[str, str] | None = None

    async def run(self, command: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> tuple[int, tuple[str, ...]]:
        self.env = env
        raise RuntimeError("runner failed with environment-secret")


class SelectivelyFailingGateway(RecordingGateway):
    def __init__(self, failing_stage: str) -> None:
        super().__init__()
        self.failing_stage = failing_stage

    async def record_codex_session(self, batch_id: str, claim_token: str, *, iteration: int, session_id: str) -> None:
        if self.failing_stage == "session":
            raise RuntimeError("gateway failed with database-secret")
        await super().record_codex_session(
            batch_id, claim_token, iteration=iteration, session_id=session_id
        )

    async def record_codex_process(self, batch_id: str, claim_token: str, *, iteration: int, process_id: str, state: str, command_digest: str) -> None:
        if self.failing_stage == state:
            raise RuntimeError("gateway failed with database-secret")
        await super().record_codex_process(
            batch_id,
            claim_token,
            iteration=iteration,
            process_id=process_id,
            state=state,
            command_digest=command_digest,
        )


@pytest.mark.asyncio
async def test_successful_run_emits_fenced_sanitized_session_and_process_events(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = RecordingRunner(0, ("artifact://build/1",))
    gateway = RecordingGateway()
    supervisor = CodexSupervisor(
        runner=runner,
        gateway=gateway,
        workspace_root=tmp_path,
        environment={"PATH": "safe-path"},
    )

    request = CodexRunRequest(
        run_id="run-1", trace_id="trace-1", batch_id="batch-1", worker_id="worker-1",
        session_id="session-1", claim_token="claim-secret", iteration=1,
        command=("codex", "exec", "build"), workspace=workspace,
    )
    result = await supervisor.run(request)

    assert result.status is PackageExecutionStatus.SUCCEEDED
    assert result.artifact_refs == ("artifact://build/1",)
    assert runner.command == ("codex", "exec", "build")
    assert runner.cwd == workspace.resolve()
    assert runner.env == {"PATH": "safe-path"}
    assert [event[0] for event in gateway.events] == ["codex_session", "codex_process", "codex_process"]
    assert [event[5] for event in gateway.events[1:]] == ["started", "exited"]
    assert all(event[2] == "claim-secret" and event[3] == 1 for event in gateway.events)
    assert all("safe-path" not in repr(event) and "claim-secret" not in repr(event[4:]) for event in gateway.events)
    assert len(gateway.events[1][6]) == 64


@pytest.mark.asyncio
async def test_nonzero_exit_is_typed_and_does_not_expose_environment_values(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    supervisor = CodexSupervisor(
        runner=RecordingRunner(17, ()), gateway=RecordingGateway(),
        workspace_root=tmp_path, environment={"API_TOKEN": "environment-secret"},
    )

    result = await supervisor.run(CodexRunRequest(
        run_id="run-1", trace_id="trace-1", batch_id="batch-1", worker_id="worker-1",
        session_id="session-1", claim_token="claim-secret", iteration=1,
        command=("codex", "exec", "build"), workspace=workspace,
    ))

    assert result.status is PackageExecutionStatus.FAILED
    assert result.error == "codex process exited with code 17"
    assert "environment-secret" not in result.error
    assert "claim-secret" not in result.error


@pytest.mark.asyncio
async def test_runner_failure_is_sanitized_and_only_allowlisted_environment_reaches_runner(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gateway = RecordingGateway()
    runner = FailingRunner()
    supervisor = CodexSupervisor(
        runner=runner, gateway=gateway,
        workspace_root=tmp_path,
        environment={"PATH": "safe-path", "API_TOKEN": "environment-secret"},
    )

    result = await supervisor.run(CodexRunRequest(
        run_id="run-1", trace_id="trace-1", batch_id="batch-1", worker_id="worker-1",
        session_id="session-1", claim_token="claim-secret", iteration=1,
        command=("codex", "exec", "build"), workspace=workspace,
    ))

    assert result.status is PackageExecutionStatus.FAILED
    assert result.error == "codex process could not be started"
    assert "environment-secret" not in result.error
    assert runner.env == {"PATH": "safe-path"}
    assert gateway.events[-1][5] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_stage", ["session", "started"])
async def test_pre_runner_gateway_failure_is_typed_sanitized_and_does_not_run_codex(
    tmp_path: Path, failing_stage: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = RecordingRunner(0, ("artifact://build/1",))
    supervisor = CodexSupervisor(
        runner=runner,
        gateway=SelectivelyFailingGateway(failing_stage),
        workspace_root=tmp_path,
        environment={},
    )

    result = await supervisor.run(CodexRunRequest(
        run_id="run-1", trace_id="trace-1", batch_id="batch-1", worker_id="worker-1",
        session_id="session-1", claim_token="claim-secret", iteration=1,
        command=("codex", "exec", "build"), workspace=workspace,
    ))

    assert result.status is PackageExecutionStatus.FAILED
    assert result.error == "codex execution evidence could not be recorded"
    assert "database-secret" not in result.error
    assert "claim-secret" not in result.error
    assert runner.command is None


@pytest.mark.asyncio
async def test_final_gateway_failure_does_not_report_local_success(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = RecordingRunner(0, ("artifact://build/1",))
    supervisor = CodexSupervisor(
        runner=runner,
        gateway=SelectivelyFailingGateway("exited"),
        workspace_root=tmp_path,
        environment={},
    )

    result = await supervisor.run(CodexRunRequest(
        run_id="run-1", trace_id="trace-1", batch_id="batch-1", worker_id="worker-1",
        session_id="session-1", claim_token="claim-secret", iteration=1,
        command=("codex", "exec", "build"), workspace=workspace,
    ))

    assert runner.command == ("codex", "exec", "build")
    assert result.status is PackageExecutionStatus.FAILED
    assert result.artifact_refs == ()
    assert result.error == "codex execution evidence could not be recorded"
    assert "database-secret" not in result.error


@pytest.mark.asyncio
async def test_cancel_gateway_failure_preserves_sanitized_runner_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    supervisor = CodexSupervisor(
        runner=FailingRunner(),
        gateway=SelectivelyFailingGateway("cancelled"),
        workspace_root=tmp_path,
        environment={},
    )

    result = await supervisor.run(CodexRunRequest(
        run_id="run-1", trace_id="trace-1", batch_id="batch-1", worker_id="worker-1",
        session_id="session-1", claim_token="claim-secret", iteration=1,
        command=("codex", "exec", "build"), workspace=workspace,
    ))

    assert result.status is PackageExecutionStatus.FAILED
    assert result.error == "codex process could not be started"
    assert "database-secret" not in result.error
    assert "environment-secret" not in result.error


@pytest.mark.asyncio
async def test_workspace_outside_root_is_rejected_before_any_gateway_write(tmp_path: Path) -> None:
    gateway = RecordingGateway()
    supervisor = CodexSupervisor(
        runner=RecordingRunner(0, ("artifact://build/1",)), gateway=gateway,
        workspace_root=tmp_path / "approved", environment={},
    )

    with pytest.raises(ValueError, match="outside the approved root"):
        await supervisor.run(CodexRunRequest(
            run_id="run-1", trace_id="trace-1", batch_id="batch-1", worker_id="worker-1",
            session_id="session-1", claim_token="claim-secret", iteration=1,
            command=("codex", "exec"), workspace=tmp_path / "other",
        ))

    assert gateway.events == []


def test_request_is_frozen_and_strict(tmp_path: Path) -> None:
    request = CodexRunRequest(
        run_id="run-1",
        trace_id="trace-1",
        batch_id="batch-1",
        worker_id="worker-1",
        session_id="session-1",
        claim_token="claim-secret",
        iteration=1,
        command=("codex", "exec"),
        workspace=tmp_path,
    )

    with pytest.raises(ValidationError):
        request.iteration = 2
    with pytest.raises(ValidationError):
        CodexRunRequest.model_validate({**request.model_dump(), "iteration": "1"})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", ""),
        ("trace_id", "trace id"),
        ("batch_id", "Batch-1"),
        ("worker_id", "worker/id"),
        ("session_id", "session id"),
        ("claim_token", ""),
        ("iteration", 0),
        ("command", ()),
        ("command", ("codex", "")),
    ],
)
def test_invalid_request_invariants_are_rejected_before_run(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    values: dict[str, object] = {
        "run_id": "run-1",
        "trace_id": "trace-1",
        "batch_id": "batch-1",
        "worker_id": "worker-1",
        "session_id": "session-1",
        "claim_token": "claim-secret",
        "iteration": 1,
        "command": ("codex", "exec"),
        "workspace": tmp_path,
    }
    values[field] = value

    with pytest.raises(ValidationError):
        CodexRunRequest.model_validate(values)
