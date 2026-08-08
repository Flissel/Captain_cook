from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agenten.agent_runtime.runtime_codex import (
    RuntimeCodexExecution,
    RuntimeCodexInvocation,
    RuntimeCodexProcessResult,
    SubprocessRuntimeCodexRunner,
)
from agenten.agent_factory.hermes_cli import HermesCliFactory, HermesCliSettings
from agenten.agent_runtime.captain_production_adapters import (
    CaptainCodexExecutionAdapter,
    CaptainHermesPlannerAdapter,
    ContentAddressedArtifactAdapter,
    create_runtime_adapters,
)
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityGrant,
    HermesPlanResult,
    RuntimeStatus,
)
from agenten.agent_runtime.production_bootstrap import (
    RuntimeAdapterBinding,
    RuntimeAdapterContext,
)
from agenten.agent_factory.orchestration import FactoryDispatchError


NOW = datetime(2026, 8, 8, 8, 0, tzinfo=timezone.utc)


def _reference(content: bytes, *, media_type: str = "text/markdown") -> ArtifactRef:
    digest = hashlib.sha256(content).hexdigest()
    return ArtifactRef(
        uri=f"artifact://sha256/{digest}",
        sha256=digest,
        media_type=media_type,
    )


def _command(
    prompt_ref: ArtifactRef,
    *,
    operation: str = "codex.run",
    event_id: UUID | None = None,
    correlation_id: UUID | None = None,
    causation_id: UUID | None = None,
) -> AgentRuntimeCommand:
    correlation = correlation_id or uuid4()
    command_id = event_id or uuid4()
    codex = operation.startswith("codex.")
    return AgentRuntimeCommand.model_validate(
        {
            "schema": "captain.agent-runtime-command.v1",
            "event_id": str(command_id),
            "correlation_id": str(correlation),
            "causation_id": str(causation_id) if causation_id else None,
            "occurred_at": NOW,
            "producer": "captain",
            "subject_id": "runtime-task",
            "subject_version": 1,
            "payload": {
                "operation": operation,
                "project_id": "runtime-project",
                "batch_id": "runtime-batch" if codex else None,
                "subtask_id": "runtime-task" if codex else None,
                "workspace_ref": "workspace://runtime/task" if codex else None,
                "prompt_ref": prompt_ref.model_dump(mode="json"),
                "integration_intent": "none",
                "capability_profile": "code-builder" if codex else "planner",
                "limits": {"wall_seconds": 30, "max_iterations": 2},
            },
        }
    )


def _grant(command: AgentRuntimeCommand) -> CapabilityGrant:
    return CapabilityGrant.model_validate(
        {
            "schema": "captain.capability-grant.v1",
            "grant_id": "runtime-grant",
            "command_id": str(command.event_id),
            "batch_id": command.payload.batch_id or "planning-batch",
            "batch_version": 1,
            "subtask_id": command.payload.subtask_id or command.subject_id,
            "workspace_ref": command.payload.workspace_ref or "workspace://runtime/task",
            "profile": command.payload.capability_profile.value,
            "capabilities": [command.payload.operation.value],
            "mcp_servers": [],
            "issued_at": NOW,
            "expires_at": NOW + timedelta(minutes=5),
        }
    )


def _plan(command: AgentRuntimeCommand, prompt_ref: ArtifactRef) -> HermesPlanResult:
    return HermesPlanResult.model_validate(
        {
            "schema": "captain.hermes-plan-result.v1",
            "project_id": command.payload.project_id,
            "correlation_id": str(command.correlation_id),
            "subject_version": command.subject_version,
            "plan_ref": prompt_ref.model_dump(mode="json"),
            "decision_log_ref": prompt_ref.model_dump(mode="json"),
            "blueprint_refs": [],
            "assumptions": [],
            "open_questions": [],
            "risks": [],
            "integration_intents": [],
            "minibook": {"project_id": "runtime-project", "post_id": "runtime-plan"},
            "planner_id": "hermes-cli",
            "runtime_provenance": "hermes-cli",
            "started_at": NOW,
            "ended_at": NOW + timedelta(seconds=1),
        }
    )


def _skill_digest(directory: Path) -> str:
    manifest = []
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        content = path.read_bytes()
        manifest.append(
            {
                "path": path.relative_to(directory).as_posix(),
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        )
    encoded = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@pytest.mark.asyncio
async def test_hermes_planning_uses_released_bundle_and_returns_typed_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = ContentAddressedArtifactAdapter(tmp_path / "artifacts")
    prompt = b"Plan this bounded runtime task."
    prompt_ref = artifacts.put(prompt, "text/markdown")
    command = _command(prompt_ref, operation="hermes.plan")
    skill = tmp_path / "skills" / "captain-agent-factory-loop"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# released\n", encoding="utf-8")
    expected = _plan(command, prompt_ref)
    factory = HermesCliFactory(
        settings=HermesCliSettings(skill_root=tmp_path / "skills")
    )
    observed: dict[str, object] = {}

    async def fake_run(prompt_text: str, **options: object) -> bytes:
        observed["prompt"] = prompt_text
        observed.update(options)
        return expected.model_dump_json(by_alias=True).encode("utf-8")

    monkeypatch.setattr(factory, "_run_skill_prompt", fake_run)
    adapter = CaptainHermesPlannerAdapter(
        factory=factory,
        artifacts=artifacts,
        skill_name="captain-agent-factory-loop",
        released_skill_sha256=_skill_digest(skill),
        environ={},
    )

    result = await adapter.plan(command, _grant(command))

    assert result == expected
    assert observed["skill_name"] == "captain-agent-factory-loop"
    assert observed["max_seconds"] == 30


@pytest.mark.asyncio
async def test_hermes_planning_rejects_unreleased_skill_before_process_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = ContentAddressedArtifactAdapter(tmp_path / "artifacts")
    prompt_ref = artifacts.put(b"bounded plan", "text/markdown")
    command = _command(prompt_ref, operation="hermes.plan")
    skill = tmp_path / "skills" / "captain-agent-factory-loop"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# unreleased bytes\n", encoding="utf-8")
    factory = HermesCliFactory(
        settings=HermesCliSettings(skill_root=tmp_path / "skills")
    )

    async def forbidden(*_args: object, **_kwargs: object) -> bytes:
        pytest.fail("unreleased Hermes skill must fail before a provider call")

    monkeypatch.setattr(factory, "_run_skill_prompt", forbidden)
    adapter = CaptainHermesPlannerAdapter(
        factory=factory,
        artifacts=artifacts,
        skill_name="captain-agent-factory-loop",
        released_skill_sha256="a" * 64,
        environ={},
    )

    with pytest.raises(FactoryDispatchError, match="released.*digest"):
        await adapter.plan(command, _grant(command))


class StreamingRunner:
    def __init__(self, *, exit_code: int = 0, private_event: dict[str, object] | None = None) -> None:
        self.exit_code = exit_code
        self.private_event = private_event
        self.invocations = []
        self.cancel_calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False

    async def run(self, invocation, observer):
        self.invocations.append(invocation)
        first = {"type": "thread.started", "thread_id": "runtime-thread"}
        second = self.private_event or {"type": "turn.completed"}
        await observer(first)
        await observer(second)
        self.started.set()
        if self.block:
            await self.release.wait()
        status = (
            "timed_out"
            if self.exit_code == 124
            else "cancelled"
            if self.exit_code == 130
            else "succeeded"
            if self.exit_code == 0
            else "failed"
        )
        return RuntimeCodexProcessResult(
            exit_code=self.exit_code,
            terminal_status=status,
            process_cleanup_status=(
                "verified_cancelled" if self.exit_code in {124, 130} else "not_required"
            ),
            elapsed_ms=1250,
            session_id="runtime-thread",
            jsonl_events=(first, second),
        )

    async def cancel(self, session_id: str) -> None:
        assert self.invocations
        assert session_id == self.invocations[-1].session_id
        self.cancel_calls += 1
        self.exit_code = 130
        self.release.set()


def _codex_adapter(
    tmp_path: Path,
    runner: StreamingRunner,
    *,
    observer=None,
    environ: dict[str, str] | None = None,
) -> tuple[CaptainCodexExecutionAdapter, RuntimeCodexExecution, ContentAddressedArtifactAdapter]:
    artifacts = ContentAddressedArtifactAdapter(tmp_path / "artifacts")
    execution = RuntimeCodexExecution(
        runner=runner,
        checkpoint_root=tmp_path / "checkpoints",
    )
    return (
        CaptainCodexExecutionAdapter(
            execution=execution,
            artifacts=artifacts,
            repository_root=tmp_path,
            observer=observer,
            environ=environ or {},
        ),
        execution,
        artifacts,
    )


@pytest.mark.asyncio
async def test_codex_streams_each_parsed_jsonl_event_before_execution_returns(
    tmp_path: Path,
) -> None:
    runner = StreamingRunner()
    observed: list[dict[str, object]] = []
    adapter, _, artifacts = _codex_adapter(tmp_path, runner, observer=observed.append)
    prompt_ref = artifacts.put(b"implement bounded task", "text/markdown")
    command = _command(prompt_ref)

    result = await adapter.start(command, _grant(command))

    assert result.status is RuntimeStatus.SUCCEEDED
    assert observed == [
        {"type": "thread.started", "thread_id": "runtime-thread"},
        {"type": "turn.completed"},
    ]


@pytest.mark.asyncio
async def test_timeout_preserves_typed_terminal_evidence_and_resumable_checkpoint(
    tmp_path: Path,
) -> None:
    runner = StreamingRunner(exit_code=124)
    adapter, execution, artifacts = _codex_adapter(tmp_path, runner)
    prompt_ref = artifacts.put(b"timeout task", "text/markdown")
    command = _command(prompt_ref)

    result = await adapter.start(command, _grant(command))
    evidence = execution.terminal_evidence(command.event_id)

    assert result.status is RuntimeStatus.INFRASTRUCTURE_FAILED
    assert result.error == "codex execution timed out (exit 124)"
    assert evidence is not None
    assert evidence.exit_code == 124
    assert evidence.elapsed_ms == 1250
    assert evidence.last_event_sha256 == hashlib.sha256(
        b'{"type":"turn.completed"}'
    ).hexdigest()
    assert evidence.resumable_checkpoint is not None
    assert evidence.resumable_checkpoint in result.evidence_refs


@pytest.mark.asyncio
async def test_resume_reuses_original_correlation_session_and_command_identity(
    tmp_path: Path,
) -> None:
    runner = StreamingRunner(exit_code=124)
    adapter, _, artifacts = _codex_adapter(tmp_path, runner)
    prompt_ref = artifacts.put(b"resume task", "text/markdown")
    original = _command(prompt_ref)
    await adapter.start(original, _grant(original))
    runner.exit_code = 0
    resumed = _command(
        prompt_ref,
        operation="codex.resume",
        correlation_id=original.correlation_id,
        causation_id=original.event_id,
    )

    result = await adapter.resume(resumed, _grant(resumed))

    assert result.status is RuntimeStatus.SUCCEEDED
    assert len(runner.invocations) == 2
    first, second = runner.invocations
    assert second.correlation_id == first.correlation_id == original.correlation_id
    assert second.command_id == first.command_id == original.event_id
    assert second.command_identity_sha256 == first.command_identity_sha256
    assert first.session_id.startswith("runtime-")
    assert second.session_id == "runtime-thread"


@pytest.mark.asyncio
async def test_repeated_cancel_reaches_owned_child_process_exactly_once(
    tmp_path: Path,
) -> None:
    runner = StreamingRunner()
    runner.block = True
    adapter, _, artifacts = _codex_adapter(tmp_path, runner)
    prompt_ref = artifacts.put(b"cancel task", "text/markdown")
    original = _command(prompt_ref)
    running = asyncio.create_task(adapter.start(original, _grant(original)))
    await runner.started.wait()
    cancel = _command(
        prompt_ref,
        operation="codex.cancel",
        correlation_id=original.correlation_id,
        causation_id=original.event_id,
    )

    first = await adapter.cancel(cancel, _grant(cancel))
    second = await adapter.cancel(cancel, _grant(cancel))
    terminal = await running

    assert first.status is RuntimeStatus.CANCELLED
    assert second.status is RuntimeStatus.CANCELLED
    assert terminal.status is RuntimeStatus.CANCELLED
    assert runner.cancel_calls == 1


@pytest.mark.asyncio
async def test_subprocess_resume_is_not_reclassified_by_prior_timeout_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Reader:
        def __init__(self, lines: list[bytes]) -> None:
            self.lines = list(lines)

        async def readline(self) -> bytes:
            return self.lines.pop(0) if self.lines else b""

        async def read(self, _size: int) -> bytes:
            return b""

    class Process:
        def __init__(self) -> None:
            self.stdout = Reader([b'{"type":"turn.completed"}\n'])
            self.stderr = Reader([])
            self.returncode: int | None = None

        async def wait(self) -> int:
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            self.returncode = 130

        def kill(self) -> None:
            self.returncode = 130

    async def create_process(*_args: object, **_kwargs: object) -> Process:
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    runner = SubprocessRuntimeCodexRunner(
        executable="codex",
        environ={},
    )
    session_id = "runtime-prior-timeout"
    runner._cancelled.add(session_id)
    observed: list[dict[str, object]] = []
    invocation = RuntimeCodexInvocation(
        command_id=uuid4(),
        correlation_id=uuid4(),
        command_identity_sha256="a" * 64,
        session_id=session_id,
        workspace=tmp_path,
        prompt="resume safely",
        timeout_seconds=5,
        resume=True,
    )

    async def observe(event: dict[str, object]) -> None:
        observed.append(event)

    result = await runner.run(invocation, observe)

    assert result.exit_code == 0
    assert result.terminal_status == "succeeded"
    assert observed == [{"type": "turn.completed"}]


@pytest.mark.asyncio
async def test_stream_failure_cancels_owned_subprocess_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Reader:
        def __init__(self, line: bytes = b"") -> None:
            self.line = line

        async def readline(self) -> bytes:
            line, self.line = self.line, b""
            return line

        async def read(self, _size: int) -> bytes:
            return b""

    class Process:
        def __init__(self) -> None:
            self.stdout = Reader(b"not-json\n")
            self.stderr = Reader()
            self.returncode: int | None = None
            self.terminate_calls = 0
            self.stopped = asyncio.Event()

        async def wait(self) -> int:
            await self.stopped.wait()
            assert self.returncode is not None
            return self.returncode

        def terminate(self) -> None:
            self.terminate_calls += 1
            self.returncode = 130
            self.stopped.set()

        def kill(self) -> None:
            self.returncode = 130
            self.stopped.set()

    process = Process()

    async def create_process(*_args: object, **_kwargs: object) -> Process:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    runner = SubprocessRuntimeCodexRunner(executable="codex", environ={})
    invocation = RuntimeCodexInvocation(
        command_id=uuid4(),
        correlation_id=uuid4(),
        command_identity_sha256="a" * 64,
        session_id="runtime-invalid-json",
        workspace=tmp_path,
        prompt="bounded prompt",
        timeout_seconds=5,
    )

    async def observe(_event: dict[str, object]) -> None:
        pytest.fail("invalid JSON must not reach the observer")

    with pytest.raises(FactoryDispatchError, match="JSONL event is invalid"):
        await runner.run(invocation, observe)

    assert process.terminate_calls == 1


@pytest.mark.asyncio
async def test_content_addressed_artifacts_deduplicate_exact_bytes(tmp_path: Path) -> None:
    artifacts = ContentAddressedArtifactAdapter(tmp_path / "sha256")
    content = b"same immutable artifact"

    first = artifacts.put(content, "application/octet-stream")
    second = artifacts.put(content, "application/octet-stream")
    await artifacts.require(first)

    assert first == second
    assert first.sha256 == hashlib.sha256(content).hexdigest()
    assert list((tmp_path / "sha256").rglob(first.sha256)) == [
        tmp_path / "sha256" / first.sha256[:2] / first.sha256
    ]


@pytest.mark.asyncio
async def test_runtime_evidence_excludes_prompt_provider_body_and_environment_secrets(
    tmp_path: Path,
) -> None:
    secret = "sk-private-runtime-secret-value"
    prompt = b"PRIVATE PROMPT BODY must remain outside evidence"
    provider_body = "PRIVATE PROVIDER RESPONSE BODY"
    runner = StreamingRunner(
        exit_code=124,
        private_event={
            "type": "item.completed",
            "item": {"text": provider_body},
            "echo": prompt.decode("utf-8"),
            "credential": secret,
        },
    )
    adapter, execution, artifacts = _codex_adapter(
        tmp_path,
        runner,
        environ={"OPENAI_API_KEY": secret},
    )
    prompt_ref = artifacts.put(prompt, "text/markdown")
    command = _command(prompt_ref)

    result = await adapter.start(command, _grant(command))
    evidence = execution.terminal_evidence(command.event_id)
    public = result.model_dump_json(by_alias=True) + (
        evidence.model_dump_json(by_alias=True) if evidence is not None else ""
    )
    checkpoint_bytes = b"".join(
        path.read_bytes() for path in (tmp_path / "checkpoints").rglob("*.json")
    )

    assert prompt.decode("utf-8") not in public
    assert provider_body not in public
    assert secret not in public
    assert prompt not in checkpoint_bytes
    assert provider_body.encode("utf-8") not in checkpoint_bytes
    assert secret.encode("utf-8") not in checkpoint_bytes


def test_factory_returns_exact_frozen_runtime_binding(tmp_path: Path) -> None:
    skill = tmp_path / "agenten" / "agent_factory" / "skills" / "captain-agent-factory-loop"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# released\n", encoding="utf-8")
    context = RuntimeAdapterContext(
        repository_root=tmp_path,
        artifact_root=tmp_path / ".captain-cook" / "artifacts" / "sha256",
        environ={
            "CAPTAIN_HERMES_RUNTIME_SKILL_SHA256": _skill_digest(skill),
            "CAPTAIN_CODEX_EXECUTABLE": "codex",
        },
    )

    binding = create_runtime_adapters(context)

    assert isinstance(binding, RuntimeAdapterBinding)
    with pytest.raises((AttributeError, TypeError)):
        binding.hermes = object()  # type: ignore[misc]
