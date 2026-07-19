from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Mapping, Sequence

import pytest

from pydantic import ValidationError

from agenten.execution.codex_policy import (
    AuthorizedCodexRun,
    CodexPolicyViolation,
    FrozenEnvironment,
)
from agenten.execution.codex_events import CodexParseWarning, CodexProcessEvent
from agenten.execution.codex_supervisor import (
    CodexRunRequest,
    CodexRunResult,
    CodexSupervisor,
)
from agenten.delivery.codex_runs import ActiveCodexSessionRecoveryRequired, CodexOutcome
from agenten.execution.process import PackageExecutionStatus


class RecordingPolicy:
    def __init__(self, authorized: AuthorizedCodexRun) -> None:
        self.authorized = authorized
        self.requests: list[CodexRunRequest] = []

    def authorize(self, request: CodexRunRequest) -> AuthorizedCodexRun:
        self.requests.append(request)
        return self.authorized


class DenyingPolicy:
    def __init__(self) -> None:
        self.requests: list[CodexRunRequest] = []

    def authorize(self, request: CodexRunRequest) -> AuthorizedCodexRun:
        self.requests.append(request)
        raise CodexPolicyViolation("request denied before side effects")


class OutsideWorkspacePolicy:
    def authorize(self, request: CodexRunRequest) -> AuthorizedCodexRun:
        raise CodexPolicyViolation("workspace is outside the approved root")


class AllowingPolicy:
    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self.environment = FrozenEnvironment(environment or {})

    def authorize(self, request: CodexRunRequest) -> AuthorizedCodexRun:
        return AuthorizedCodexRun(
            workspace=request.workspace.resolve(),
            command=request.command,
            environment=self.environment,
        )


def _request(workspace: Path) -> CodexRunRequest:
    return CodexRunRequest(
        run_id="run-1",
        trace_id="trace-1",
        batch_id="batch-1",
        worker_id="worker-1",
        session_id="session-1",
        claim_token="claim-secret",
        iteration=1,
        command=("codex", "exec", "--json", "build"),
        workspace=workspace,
    )


class RecordingRunner:
    def __init__(self, exit_code: int, artifacts: tuple[str, ...]) -> None:
        self.exit_code = exit_code
        self.artifacts = artifacts
        self.command: tuple[str, ...] | None = None
        self.cwd: Path | None = None
        self.env: Mapping[str, str] | None = None

    async def run(self, authorized: AuthorizedCodexRun) -> CodexRunResult:
        self.command = authorized.command
        self.cwd = authorized.workspace
        self.env = authorized.child_environment()
        return CodexRunResult(
            exit_code=self.exit_code,
            artifact_references=self.artifacts,
            jsonl_lines=(),
        )


class StreamingRunner:
    def __init__(self, result: CodexRunResult) -> None:
        self.result = result
        self.authorized: AuthorizedCodexRun | None = None

    async def run(self, authorized: AuthorizedCodexRun) -> CodexRunResult:
        self.authorized = authorized
        return self.result


class BlockingRunner:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, authorized: AuthorizedCodexRun) -> CodexRunResult:
        del authorized
        self.started.set()
        await self.release.wait()
        return CodexRunResult(
            exit_code=0,
            artifact_references=("artifact://build/1",),
            jsonl_lines=(),
        )


class InMemoryRunRepository:
    def __init__(self) -> None:
        self.started: set[str] = set()
        self.outcomes: dict[str, CodexOutcome] = {}

    async def start(self, request: CodexRunRequest) -> object:
        self.started.add(request.session_id)
        return object()

    async def append(self, event: object) -> None:
        del event

    async def finish(self, session_id: str, outcome: CodexOutcome) -> None:
        existing = self.outcomes.get(session_id)
        if existing is not None and existing != outcome:
            raise ValueError("terminal outcome conflicts")
        self.outcomes[session_id] = outcome


class RecoveryRequiredRepository:
    async def start(self, request: CodexRunRequest) -> object:
        del request
        raise ActiveCodexSessionRecoveryRequired("session is already active")


class StartMonitor:
    def __init__(self, runner: BlockingRunner) -> None:
        self._runner = runner

    async def wait(self) -> None:
        await self._runner.started.wait()


class PersistingCanceller:
    def __init__(self, runner: BlockingRunner, repository: InMemoryRunRepository) -> None:
        self._runner = runner
        self._repository = repository
        self.calls = 0

    async def cancel(self) -> None:
        self.calls += 1
        await self._repository.finish(
            "session-1",
            CodexOutcome(
                classification="cancelled",
                cancellation_reason="captain_revoked",
            ),
        )
        self._runner.release.set()


class RecordingGateway:
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []

    async def record_codex_session(self, batch_id: str, claim_token: str, *, iteration: int, session_id: str) -> None:
        self.events.append(("codex_session", batch_id, claim_token, iteration, session_id))

    async def record_codex_process(self, batch_id: str, claim_token: str, *, iteration: int, process_id: str, state: str, command_digest: str) -> None:
        self.events.append(("codex_process", batch_id, claim_token, iteration, process_id, state, command_digest))

    async def record_codex_event(
        self,
        batch_id: str,
        claim_token: str,
        *,
        iteration: int,
        session_id: str,
        event: CodexProcessEvent | CodexParseWarning,
    ) -> None:
        self.events.append(("codex_event", batch_id, claim_token, iteration, session_id, event))


class FailingRunner:
    def __init__(self) -> None:
        self.env: Mapping[str, str] | None = None

    async def run(self, authorized: AuthorizedCodexRun) -> CodexRunResult:
        self.env = authorized.child_environment()
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
    policy = RecordingPolicy(
        AuthorizedCodexRun(
            workspace=workspace.resolve(),
            command=("codex", "exec", "--json", "sanitized-task"),
            environment=FrozenEnvironment({"PATH": "safe-path"}),
        )
    )
    supervisor = CodexSupervisor(
        runner=runner,
        gateway=gateway,
        policy=policy,
    )

    request = CodexRunRequest(
        run_id="run-1", trace_id="trace-1", batch_id="batch-1", worker_id="worker-1",
        session_id="session-1", claim_token="claim-secret", iteration=1,
        command=("codex", "exec", "build"), workspace=workspace,
    )
    result = await supervisor.run(request)

    assert result.status is PackageExecutionStatus.SUCCEEDED
    assert result.artifact_refs == ("artifact://build/1",)
    assert policy.requests == [request]
    assert runner.command == ("codex", "exec", "--json", "sanitized-task")
    assert runner.cwd == workspace.resolve()
    assert runner.env == {"PATH": "safe-path"}
    assert [event[0] for event in gateway.events] == ["codex_session", "codex_process", "codex_process"]
    assert [event[5] for event in gateway.events[1:]] == ["started", "exited"]
    assert all(event[2] == "claim-secret" and event[3] == 1 for event in gateway.events)
    assert all("safe-path" not in repr(event) and "claim-secret" not in repr(event[4:]) for event in gateway.events)
    assert len(gateway.events[1][6]) == 64


@pytest.mark.asyncio
async def test_captain_revocation_cancels_the_active_session_without_overwriting_evidence(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = BlockingRunner()
    repository = InMemoryRunRepository()
    canceller = PersistingCanceller(runner, repository)
    supervisor = CodexSupervisor(
        runner=runner,
        gateway=RecordingGateway(),
        policy=AllowingPolicy(),
        repository=repository,
        cancellation_monitor=StartMonitor(runner),
        canceller=canceller,
    )

    result = await asyncio.wait_for(
        supervisor.run(_request(workspace)), timeout=2
    )

    assert result.status is PackageExecutionStatus.FAILED
    assert result.error == "Codex capability grant was revoked by Captain"
    assert canceller.calls == 1
    assert repository.outcomes == {
        "session-1": CodexOutcome(
            classification="cancelled",
            cancellation_reason="captain_revoked",
        )
    }


@pytest.mark.asyncio
async def test_active_gateway_session_never_starts_a_second_codex_runner(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = RecordingRunner(0, ("artifact://build/1",))
    result = await CodexSupervisor(
        runner=runner,
        gateway=RecordingGateway(),
        policy=AllowingPolicy(),
        repository=RecoveryRequiredRepository(),
    ).run(_request(workspace))

    assert result.status is PackageExecutionStatus.EVIDENCE_UNRESOLVED
    assert result.error == "active Codex session requires recovery before retry"
    assert runner.command is None


def test_revocation_monitor_and_canceller_must_be_configured_together(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="must be paired"):
        CodexSupervisor(
            runner=RecordingRunner(0, ("artifact://build/1",)),
            gateway=RecordingGateway(),
            policy=AllowingPolicy(),
            cancellation_monitor=StartMonitor(BlockingRunner()),
        )

    with pytest.raises(ValueError, match="must be paired"):
        CodexSupervisor(
            runner=RecordingRunner(0, ("artifact://build/1",)),
            gateway=RecordingGateway(),
            policy=AllowingPolicy(),
            canceller=PersistingCanceller(BlockingRunner(), InMemoryRunRepository()),
        )


@pytest.mark.asyncio
async def test_policy_denial_precedes_runner_and_gateway_writes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = RecordingRunner(0, ("artifact://build/1",))
    gateway = RecordingGateway()
    policy = DenyingPolicy()
    supervisor = CodexSupervisor(
        runner=runner,
        gateway=gateway,
        policy=policy,
    )
    request = CodexRunRequest(
        run_id="run-1",
        trace_id="trace-1",
        batch_id="batch-1",
        worker_id="worker-1",
        session_id="session-1",
        claim_token="claim-secret",
        iteration=1,
        command=("codex", "exec", "--json", "build"),
        workspace=workspace,
    )

    with pytest.raises(CodexPolicyViolation, match="denied before side effects"):
        await supervisor.run(request)

    assert policy.requests == [request]
    assert runner.command is None
    assert gateway.events == []


def test_supervisor_rejects_legacy_configuration_without_an_injected_policy(
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError, match="policy"):
        CodexSupervisor(
            runner=RecordingRunner(0, ()),
            gateway=RecordingGateway(),
        )


@pytest.mark.asyncio
async def test_authorized_jsonl_lifecycle_is_persisted_before_exit_evidence(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authorized = AuthorizedCodexRun(
        workspace=workspace.resolve(),
        command=("codex", "exec", "--json", "sanitized-task"),
        environment=FrozenEnvironment({"PATH": "safe-path"}),
    )
    policy = RecordingPolicy(authorized)
    runner = StreamingRunner(
        CodexRunResult(
            exit_code=0,
            artifact_references=("artifact://build/1",),
            jsonl_lines=(
                '{"type":"thread.started","thread_id":"thread-1"}',
                '{"type":"turn.completed","usage":{"input_tokens":1,"cached_input_tokens":0,"output_tokens":2}}',
            ),
        )
    )
    gateway = RecordingGateway()
    supervisor = CodexSupervisor(runner=runner, gateway=gateway, policy=policy)

    result = await supervisor.run(_request(workspace))

    assert result.status is PackageExecutionStatus.SUCCEEDED
    assert runner.authorized is authorized
    assert [event[0] for event in gateway.events] == [
        "codex_session",
        "codex_process",
        "codex_event",
        "codex_event",
        "codex_process",
    ]
    assert isinstance(gateway.events[2][-1], CodexProcessEvent)
    assert gateway.events[2][-1].session_id == "thread-1"
    assert gateway.events[-1][5] == "exited"


@pytest.mark.asyncio
async def test_jsonl_warning_is_persisted_and_prevents_success_on_zero_exit(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authorized = AuthorizedCodexRun(
        workspace=workspace.resolve(),
        command=("codex", "exec", "--json", "sanitized-task"),
        environment=FrozenEnvironment({"PATH": "safe-path"}),
    )
    gateway = RecordingGateway()
    supervisor = CodexSupervisor(
        runner=StreamingRunner(
            CodexRunResult(
                exit_code=0,
                artifact_references=("artifact://build/1",),
                jsonl_lines=("{malformed",),
            )
        ),
        gateway=gateway,
        policy=RecordingPolicy(authorized),
    )

    result = await supervisor.run(_request(workspace))

    assert result.status is PackageExecutionStatus.FAILED
    assert result.artifact_refs == ()
    assert result.error == "codex JSONL evidence is incomplete"
    assert isinstance(gateway.events[2][-1], CodexParseWarning)
    assert gateway.events[-1][5] == "exited"


@pytest.mark.asyncio
async def test_failed_jsonl_lifecycle_prevents_success_without_persisting_message_text(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authorized = AuthorizedCodexRun(
        workspace=workspace.resolve(),
        command=("codex", "exec", "--json", "sanitized-task"),
        environment=FrozenEnvironment({"PATH": "safe-path"}),
    )
    gateway = RecordingGateway()
    supervisor = CodexSupervisor(
        runner=StreamingRunner(
            CodexRunResult(
                exit_code=0,
                artifact_references=("artifact://build/1",),
                jsonl_lines=(
                    '{"type":"error","message":"untrusted-secret-text"}',
                ),
            )
        ),
        gateway=gateway,
        policy=RecordingPolicy(authorized),
    )

    result = await supervisor.run(_request(workspace))

    assert result.status is PackageExecutionStatus.FAILED
    assert result.error == "codex JSONL evidence is incomplete"
    event = gateway.events[2][-1]
    assert isinstance(event, CodexProcessEvent)
    assert event.lifecycle == "failed"
    assert "untrusted-secret-text" not in event.model_dump_json()


def test_run_result_is_frozen_and_strict() -> None:
    result = CodexRunResult(
        exit_code=0,
        artifact_references=("artifact://build/1",),
        jsonl_lines=(),
    )

    with pytest.raises(ValidationError):
        result.exit_code = 1
    with pytest.raises(ValidationError):
        CodexRunResult.model_validate(
            {
                "exit_code": "0",
                "artifact_references": (),
                "jsonl_lines": (),
            }
        )


@pytest.mark.asyncio
async def test_nonzero_exit_is_typed_and_does_not_expose_environment_values(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    supervisor = CodexSupervisor(
        runner=RecordingRunner(17, ()), gateway=RecordingGateway(),
        policy=AllowingPolicy(),
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
        policy=AllowingPolicy({"PATH": "safe-path"}),
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
        policy=AllowingPolicy(),
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
        policy=AllowingPolicy(),
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
        policy=AllowingPolicy(),
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
async def test_rejecting_policy_blocks_gateway_writes_before_any_runner_call(tmp_path: Path) -> None:
    gateway = RecordingGateway()
    supervisor = CodexSupervisor(
        runner=RecordingRunner(0, ("artifact://build/1",)), gateway=gateway,
        policy=OutsideWorkspacePolicy(),
    )

    with pytest.raises(CodexPolicyViolation, match="outside the approved root"):
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
