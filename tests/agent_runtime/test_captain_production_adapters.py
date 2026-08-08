from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agenten.agent_runtime.runtime_codex import (
    PowerShellRuntimeCodexRunner,
    RuntimeCodexExecution,
    RuntimeCodexProcessResult,
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
from agenten.agent_runtime import confined_files
from agenten.agent_factory.orchestration import FactoryDispatchError
from agenten.execution.codex_supervisor import CodexJsonlInvalidObjectError


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
    workspace_ref: str = "workspace://runtime/task",
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
                "workspace_ref": workspace_ref if codex else None,
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
            process_cleanup_status=("verified_cancelled" if self.exit_code == 130 else "not_required"),
            elapsed_ms=1250,
            session_id="runtime-thread",
            event_count=2,
            event_types=("thread.started", "turn.completed"),
            last_event_sha256=hashlib.sha256(
                json.dumps(second, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
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
    (tmp_path / ".captain-cook" / "workspaces" / "runtime" / "task").mkdir(
        parents=True,
        exist_ok=True,
    )
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
async def test_codex_resolves_granted_workspace_under_confined_runtime_root(
    tmp_path: Path,
) -> None:
    runner = StreamingRunner()
    adapter, _, artifacts = _codex_adapter(tmp_path, runner)
    workspace = tmp_path / ".captain-cook" / "workspaces" / "runtime" / "task"
    workspace.mkdir(parents=True, exist_ok=True)
    prompt_ref = artifacts.put(b"workspace-bound task", "text/markdown")
    command = _command(prompt_ref)

    await adapter.start(command, _grant(command))

    assert runner.invocations[0].workspace == workspace.resolve()


@pytest.mark.asyncio
async def test_duplicate_codex_start_is_single_flight_and_reuses_terminal_receipt(
    tmp_path: Path,
) -> None:
    runner = StreamingRunner()
    runner.block = True
    adapter, _, artifacts = _codex_adapter(tmp_path, runner)
    workspace = tmp_path / ".captain-cook" / "workspaces" / "runtime" / "task"
    workspace.mkdir(parents=True, exist_ok=True)
    prompt_ref = artifacts.put(b"single-flight task", "text/markdown")
    command = _command(prompt_ref)
    grant = _grant(command)

    first = asyncio.create_task(adapter.start(command, grant))
    await runner.started.wait()
    second = asyncio.create_task(adapter.start(command, grant))
    await asyncio.sleep(0)

    assert len(runner.invocations) == 1
    runner.release.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert first_result == second_result
    assert len(runner.invocations) == 1


@pytest.mark.asyncio
async def test_duplicate_start_across_bindings_uses_one_durable_process_owner(
    tmp_path: Path,
) -> None:
    first_runner = StreamingRunner()
    first_runner.block = True
    second_runner = StreamingRunner()
    first_adapter, _, artifacts = _codex_adapter(tmp_path, first_runner)
    second_execution = RuntimeCodexExecution(
        runner=second_runner,
        checkpoint_root=tmp_path / "checkpoints",
    )
    second_adapter = CaptainCodexExecutionAdapter(
        execution=second_execution,
        artifacts=artifacts,
        repository_root=tmp_path,
        environ={},
    )
    prompt_ref = artifacts.put(b"cross-binding single-flight", "text/markdown")
    command = _command(prompt_ref)
    grant = _grant(command)

    first = asyncio.create_task(first_adapter.start(command, grant))
    await first_runner.started.wait()
    second = asyncio.create_task(second_adapter.start(command, grant))
    await asyncio.sleep(0.05)

    assert len(first_runner.invocations) + len(second_runner.invocations) == 1
    claim = json.loads(
        (
            tmp_path
            / "checkpoints"
            / "claims"
            / str(command.event_id)
            / "start.json"
        ).read_bytes()
    )
    assert claim["owner_id"]
    assert claim["session_id"].startswith("runtime-")
    assert claim["process_state_ref"].startswith("process-state://")
    assert claim["deadline_at"] > claim["claimed_at"]
    first_runner.release.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert first_result == second_result


@pytest.mark.asyncio
async def test_workspace_ref_prevents_identity_collision_for_same_prompt(
    tmp_path: Path,
) -> None:
    runner = StreamingRunner()
    adapter, _, artifacts = _codex_adapter(tmp_path, runner)
    other = tmp_path / ".captain-cook" / "workspaces" / "runtime" / "other"
    other.mkdir(parents=True)
    prompt_ref = artifacts.put(b"same prompt", "text/markdown")
    correlation_id = uuid4()
    first = _command(prompt_ref, correlation_id=correlation_id)
    second = _command(
        prompt_ref,
        correlation_id=correlation_id,
        workspace_ref="workspace://runtime/other",
    )

    await adapter.start(first, _grant(first))
    await adapter.start(second, _grant(second))

    assert [invocation.workspace for invocation in runner.invocations] == [
        tmp_path / ".captain-cook" / "workspaces" / "runtime" / "task",
        other,
    ]
    assert (
        runner.invocations[0].command_identity_sha256
        != runner.invocations[1].command_identity_sha256
    )


@pytest.mark.asyncio
async def test_hermes_process_receives_only_digest_bound_skill_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = ContentAddressedArtifactAdapter(tmp_path / "artifacts")
    prompt_ref = artifacts.put(b"snapshot-bound plan", "text/markdown")
    command = _command(prompt_ref, operation="hermes.plan")
    skill_name = "captain-agent-factory-loop"
    skill = tmp_path / "skills" / skill_name
    skill.mkdir(parents=True)
    skill_bytes = b"# exact released bytes\n"
    (skill / "SKILL.md").write_bytes(skill_bytes)
    expected = _plan(command, prompt_ref)
    launches: list[dict[str, object]] = []

    class Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return expected.model_dump_json(by_alias=True).encode("utf-8"), b""

    async def create_process(*arguments: object, **options: object) -> Process:
        launches.append({"arguments": arguments, "options": options})
        return Process()

    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    factory = HermesCliFactory(
        settings=HermesCliSettings(
            executable="hermes",
            skill_root=tmp_path / "skills",
            evidence_root=tmp_path / "hermes-evidence",
            working_directory=tmp_path,
        )
    )
    adapter = CaptainHermesPlannerAdapter(
        factory=factory,
        artifacts=artifacts,
        skill_name=skill_name,
        released_skill_sha256=_skill_digest(skill),
        environ={},
    )

    result = await adapter.plan(command, _grant(command))

    assert result == expected
    assert len(launches) == 1
    environment = launches[0]["options"]["env"]
    assert isinstance(environment, dict)
    hermes_home = Path(environment["HERMES_HOME"])
    snapshot = hermes_home / "skills" / skill_name / "SKILL.md"
    assert snapshot.read_bytes() == skill_bytes
    assert _skill_digest(snapshot.parent) == _skill_digest(skill)


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
    assert evidence.process_cleanup_status == "not_required"


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
async def test_active_resume_hides_stale_timeout_terminal_from_status(
    tmp_path: Path,
) -> None:
    runner = StreamingRunner(exit_code=124)
    adapter, _, artifacts = _codex_adapter(tmp_path, runner)
    prompt_ref = artifacts.put(b"active resume", "text/markdown")
    original = _command(prompt_ref)
    await adapter.start(original, _grant(original))
    runner.exit_code = 0
    runner.block = True
    runner.started = asyncio.Event()
    runner.release = asyncio.Event()
    resumed = _command(
        prompt_ref,
        operation="codex.resume",
        correlation_id=original.correlation_id,
        causation_id=original.event_id,
    )
    active = asyncio.create_task(adapter.resume(resumed, _grant(resumed)))
    await runner.started.wait()
    status = _command(
        prompt_ref,
        operation="codex.status",
        correlation_id=original.correlation_id,
        causation_id=original.event_id,
    )

    observed = await adapter.status(status, _grant(status))

    assert observed.status is RuntimeStatus.RUNNING
    runner.release.set()
    assert (await active).status is RuntimeStatus.SUCCEEDED


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


def test_content_addressed_artifacts_reject_non_sha256_namespace(
    tmp_path: Path,
) -> None:
    artifacts = ContentAddressedArtifactAdapter(tmp_path / "sha256")
    reference = artifacts.put(b"namespace-bound", "application/octet-stream")
    forged = reference.model_copy(
        update={"uri": f"artifact://attacker/{reference.sha256}"}
    )

    with pytest.raises(ValueError, match="content-addressed"):
        artifacts.read_bytes(forged)


def test_content_addressed_artifacts_reject_symlinked_store_root(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    store = tmp_path / "sha256"
    try:
        store.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    content = b"must remain confined"

    with pytest.raises(ValueError, match="confined|symlink|reparse"):
        artifacts = ContentAddressedArtifactAdapter(store)
        artifacts.put(content, "application/octet-stream")

    assert not list(outside.rglob(hashlib.sha256(content).hexdigest()))


def test_content_addressed_write_rejects_final_handle_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = ContentAddressedArtifactAdapter(tmp_path / "sha256")
    outside = tmp_path / "outside" / "forged"
    outside.parent.mkdir()
    monkeypatch.setattr(
        confined_files,
        "_final_path_for_open_file",
        lambda _stream, *, requested_path: outside,
    )

    with pytest.raises(ValueError, match="escaped"):
        artifacts.put(b"handle-bound", "application/octet-stream")

    assert not outside.exists()


def test_content_addressed_read_rejects_final_handle_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = ContentAddressedArtifactAdapter(tmp_path / "sha256")
    reference = artifacts.put(b"read-handle-bound", "application/octet-stream")
    outside = tmp_path / "outside-read"
    monkeypatch.setattr(
        confined_files,
        "_final_path_for_open_file",
        lambda _stream, *, requested_path: outside,
    )

    with pytest.raises(ValueError, match="unavailable"):
        artifacts.read_bytes(reference)


def test_content_addressed_target_is_published_only_after_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = ContentAddressedArtifactAdapter(tmp_path / "sha256")
    content = b"atomic publication"
    digest = hashlib.sha256(content).hexdigest()
    target = tmp_path / "sha256" / digest[:2] / digest
    entered = threading.Event()
    release = threading.Event()
    original_fsync = confined_files.os.fsync

    def blocking_fsync(descriptor: int) -> None:
        entered.set()
        assert release.wait(timeout=5)
        original_fsync(descriptor)

    monkeypatch.setattr(confined_files.os, "fsync", blocking_fsync)

    async def publish() -> ArtifactRef:
        return await asyncio.to_thread(
            artifacts.put,
            content,
            "application/octet-stream",
        )

    async def assert_atomic() -> None:
        task = asyncio.create_task(publish())
        assert await asyncio.to_thread(entered.wait, 5)
        assert not target.exists()
        release.set()
        assert await task == _reference(content, media_type="application/octet-stream")
        assert target.read_bytes() == content

    asyncio.run(assert_atomic())


@pytest.mark.asyncio
async def test_concurrent_content_addressed_writers_publish_one_exact_object(
    tmp_path: Path,
) -> None:
    artifacts = ContentAddressedArtifactAdapter(tmp_path / "sha256")
    content = b"concurrent immutable bytes"

    references = await asyncio.gather(
        *(
            asyncio.to_thread(
                artifacts.put,
                content,
                "application/octet-stream",
            )
            for _ in range(8)
        )
    )

    assert references == [references[0]] * 8
    digest = references[0].sha256
    assert (tmp_path / "sha256" / digest[:2] / digest).read_bytes() == content
    assert not list((tmp_path / "sha256").rglob("*.tmp"))


def test_checkpoint_store_rejects_symlinked_root_before_runner_can_start(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-checkpoints"
    outside.mkdir()
    checkpoint_root = tmp_path / "checkpoints"
    try:
        checkpoint_root.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    runner = StreamingRunner()

    with pytest.raises(ValueError, match="symlink|reparse|confined"):
        RuntimeCodexExecution(runner=runner, checkpoint_root=checkpoint_root)

    assert runner.invocations == []
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_checkpoint_index_rejects_competing_terminal_writer(
    tmp_path: Path,
) -> None:
    execution = RuntimeCodexExecution(
        runner=StreamingRunner(),
        checkpoint_root=tmp_path / "checkpoints",
    )
    command_id = uuid4()
    first = _reference(b"first terminal")
    second = _reference(b"competing terminal")

    execution._write_checkpoint_index(command_id, first)

    with pytest.raises(FactoryDispatchError, match="index conflicts"):
        execution._write_checkpoint_index(command_id, second)


@pytest.mark.asyncio
async def test_checkpoint_replay_rejects_command_id_tampering(
    tmp_path: Path,
) -> None:
    runner = StreamingRunner(exit_code=124)
    adapter, execution, artifacts = _codex_adapter(tmp_path, runner)
    prompt_ref = artifacts.put(b"tamper-bound", "text/markdown")
    command = _command(prompt_ref)
    await adapter.start(command, _grant(command))
    checkpoint_root = tmp_path / "checkpoints"
    index_path = checkpoint_root / "by-command" / f"{command.event_id}.json"
    reference = ArtifactRef.model_validate_json(index_path.read_bytes())
    payload_path = checkpoint_root / reference.sha256[:2] / f"{reference.sha256}.json"
    payload = json.loads(payload_path.read_bytes())
    payload["command_id"] = str(uuid4())
    content = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    tampered_path = checkpoint_root / digest[:2] / f"{digest}.json"
    tampered_path.parent.mkdir(parents=True, exist_ok=True)
    tampered_path.write_bytes(content)
    forged = ArtifactRef(
        uri=f"artifact://runtime-codex-checkpoint/{digest}",
        sha256=digest,
        media_type="application/json",
    )
    index_path.write_bytes(
        json.dumps(
            forged.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    restarted = RuntimeCodexExecution(
        runner=StreamingRunner(),
        checkpoint_root=checkpoint_root,
    )

    with pytest.raises(FactoryDispatchError, match="command binding"):
        restarted.terminal_evidence(command.event_id)


@pytest.mark.asyncio
async def test_checkpoint_replay_recomputes_identity_from_persisted_inputs(
    tmp_path: Path,
) -> None:
    runner = StreamingRunner(exit_code=124)
    adapter, _, artifacts = _codex_adapter(tmp_path, runner)
    prompt_ref = artifacts.put(b"identity tamper", "text/markdown")
    command = _command(prompt_ref)
    await adapter.start(command, _grant(command))
    checkpoint_root = tmp_path / "checkpoints"
    index_path = checkpoint_root / "by-command" / f"{command.event_id}.json"
    reference = ArtifactRef.model_validate_json(index_path.read_bytes())
    payload_path = checkpoint_root / reference.sha256[:2] / f"{reference.sha256}.json"
    payload = json.loads(payload_path.read_bytes())
    payload["correlation_id"] = str(uuid4())
    content = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    digest = hashlib.sha256(content).hexdigest()
    tampered_path = checkpoint_root / digest[:2] / f"{digest}.json"
    tampered_path.parent.mkdir(parents=True)
    tampered_path.write_bytes(content)
    forged = ArtifactRef(
        uri=f"artifact://runtime-codex-checkpoint/{digest}",
        sha256=digest,
        media_type="application/json",
    )
    index_path.write_bytes(
        json.dumps(
            forged.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    restarted = RuntimeCodexExecution(
        runner=StreamingRunner(),
        checkpoint_root=checkpoint_root,
    )

    with pytest.raises(FactoryDispatchError, match="identity binding"):
        restarted.terminal_evidence(command.event_id)


@pytest.mark.asyncio
async def test_status_discovers_terminal_checkpoint_after_restart(
    tmp_path: Path,
) -> None:
    runner = StreamingRunner(exit_code=124)
    adapter, _, artifacts = _codex_adapter(tmp_path, runner)
    workspace = tmp_path / ".captain-cook" / "workspaces" / "runtime" / "task"
    workspace.mkdir(parents=True, exist_ok=True)
    prompt_ref = artifacts.put(b"restart recovery", "text/markdown")
    original = _command(prompt_ref)
    await adapter.start(original, _grant(original))
    restarted_execution = RuntimeCodexExecution(
        runner=StreamingRunner(),
        checkpoint_root=tmp_path / "checkpoints",
    )
    restarted = CaptainCodexExecutionAdapter(
        execution=restarted_execution,
        artifacts=artifacts,
        repository_root=tmp_path,
        environ={},
    )
    status = _command(
        prompt_ref,
        operation="codex.status",
        correlation_id=original.correlation_id,
        causation_id=original.event_id,
    )

    result = await restarted.status(status, _grant(status))

    assert result.status is RuntimeStatus.INFRASTRUCTURE_FAILED
    assert result.error == "codex execution timed out (exit 124)"


@pytest.mark.asyncio
async def test_resume_after_restart_uses_original_command_and_session(
    tmp_path: Path,
) -> None:
    first_runner = StreamingRunner(exit_code=124)
    adapter, _, artifacts = _codex_adapter(tmp_path, first_runner)
    prompt_ref = artifacts.put(b"restart resume", "text/markdown")
    original = _command(prompt_ref)
    await adapter.start(original, _grant(original))
    resumed_runner = StreamingRunner(exit_code=0)
    restarted_execution = RuntimeCodexExecution(
        runner=resumed_runner,
        checkpoint_root=tmp_path / "checkpoints",
    )
    restarted = CaptainCodexExecutionAdapter(
        execution=restarted_execution,
        artifacts=artifacts,
        repository_root=tmp_path,
        environ={},
    )
    resume = _command(
        prompt_ref,
        operation="codex.resume",
        correlation_id=original.correlation_id,
        causation_id=original.event_id,
    )

    result = await restarted.resume(resume, _grant(resume))

    assert result.status is RuntimeStatus.SUCCEEDED
    assert resumed_runner.invocations[0].resume is True
    assert resumed_runner.invocations[0].command_id == original.event_id
    assert resumed_runner.invocations[0].session_id == "runtime-thread"


@pytest.mark.asyncio
async def test_observer_failure_persists_typed_resumable_terminal_evidence(
    tmp_path: Path,
) -> None:
    runner = StreamingRunner()

    async def broken_observer(_event: dict[str, object]) -> None:
        raise RuntimeError("observer body must not be persisted")

    adapter, execution, artifacts = _codex_adapter(
        tmp_path,
        runner,
        observer=broken_observer,
    )
    prompt_ref = artifacts.put(b"observer failure", "text/markdown")
    command = _command(prompt_ref)

    result = await adapter.start(command, _grant(command))
    evidence = execution.terminal_evidence(command.event_id)

    assert result.status is RuntimeStatus.FAILED
    assert evidence is not None
    assert evidence.failure_kind == "observer_failed"
    assert evidence.resumable_checkpoint is not None
    persisted = b"".join(
        path.read_bytes() for path in (tmp_path / "checkpoints").rglob("*.json")
    )
    assert b"observer body must not be persisted" not in persisted


@pytest.mark.asyncio
async def test_parser_failure_from_current_runner_becomes_typed_terminal_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / ".captain-cook" / "workspaces" / "runtime" / "task"
    workspace.mkdir(parents=True)
    process_runner = PowerShellRuntimeCodexRunner(
        repository_root=tmp_path,
        executable="codex",
        environ={},
        evidence_root=tmp_path / "private-process",
    )

    class InvalidJsonRunner:
        async def run(self, _authorized: object) -> object:
            error = CodexJsonlInvalidObjectError(
                "Codex JSONL record is not a valid JSON object"
            )
            journal = tmp_path / "private-process" / "redacted.jsonl"
            journal.parent.mkdir(parents=True)
            journal.write_bytes(b"")
            error.bind_terminal_evidence(
                process_cleanup_status="verified_cancelled",
                journal_path=journal,
                journal_sha256=hashlib.sha256(b"").hexdigest(),
                journal_byte_count=0,
                event_count=0,
                event_types=(),
            )
            raise error

    monkeypatch.setattr(
        process_runner,
        "_make_runner",
        lambda _invocation, _observer: InvalidJsonRunner(),
    )
    artifacts = ContentAddressedArtifactAdapter(tmp_path / "artifacts")
    execution = RuntimeCodexExecution(
        runner=process_runner,
        checkpoint_root=tmp_path / "checkpoints",
    )
    adapter = CaptainCodexExecutionAdapter(
        execution=execution,
        artifacts=artifacts,
        repository_root=tmp_path,
        environ={},
    )
    prompt_ref = artifacts.put(b"parser failure", "text/markdown")
    command = _command(prompt_ref)

    result = await adapter.start(command, _grant(command))
    evidence = execution.terminal_evidence(command.event_id)

    assert result.status is RuntimeStatus.FAILED
    assert evidence is not None
    assert evidence.failure_kind == "invalid_json_object"
    assert evidence.process_cleanup_status == "verified_cancelled"
    assert evidence.resumable_checkpoint is not None


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
    assert isinstance(binding.codex._execution._runner, PowerShellRuntimeCodexRunner)
    with pytest.raises((AttributeError, TypeError)):
        binding.hermes = object()  # type: ignore[misc]


@pytest.mark.asyncio
async def test_factory_bound_hermes_uses_dedicated_confined_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    prompt_ref = await binding.artifacts.write(b"confined Hermes", "text/markdown")
    command = _command(prompt_ref, operation="hermes.plan")
    expected = _plan(command, prompt_ref)
    launches: list[dict[str, object]] = []

    class Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return expected.model_dump_json(by_alias=True).encode(), b""

    async def create_process(*arguments: object, **options: object) -> Process:
        launches.append({"arguments": arguments, "options": options})
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    assert await binding.hermes.plan(command, _grant(command)) == expected
    assert launches[0]["options"]["cwd"] == str(
        tmp_path / ".captain-cook" / "workspaces" / "hermes-runtime"
    )
