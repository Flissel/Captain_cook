from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import pytest

import agenten.agent_runtime.runtime_codex as runtime_codex

from agenten.agent_runtime.runtime_codex import (
    PowerShellRuntimeCodexRunner,
    RuntimeCodexExecution,
    RuntimeCodexInvocation,
    RuntimeCodexProcessResult,
    RuntimeCodexUsageV1,
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
    RuntimeProviderUsageReceiptV1,
    RuntimeUsagePricingSnapshotV1,
)
from agenten.agent_runtime.production_bootstrap import (
    RuntimeAdapterBinding,
    RuntimeAdapterContext,
)
from agenten.agent_runtime import confined_files
from agenten.agent_factory.orchestration import FactoryDispatchError
from agenten.execution.codex_supervisor import (
    CodexJsonlInvalidObjectError,
    CodexOutputObserverError,
)


NOW = datetime(2026, 8, 8, 8, 0, tzinfo=timezone.utc)
PRICING = RuntimeUsagePricingSnapshotV1(
    schema_name="captain.runtime-usage-pricing-snapshot.v1",
    snapshot_id="openai-test-2026-08-09",
    provider="openai",
    model="gpt-5.6-terra",
    input_cost_per_million_usd=Decimal("1.25"),
    cached_input_cost_per_million_usd=Decimal("0.125"),
    output_cost_per_million_usd=Decimal("10.00"),
    effective_at=NOW,
    expires_at=NOW + timedelta(days=2),
)


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


def _cost_bound(command: AgentRuntimeCommand) -> AgentRuntimeCommand:
    return command.model_copy(
        update={
            "payload": command.payload.model_copy(
                update={
                    "maximum_cost_usd": Decimal("0.75"),
                    "budget_reservation_id": uuid4(),
                    "cost_authority_ref": (
                        "gateway://capability-resume-authorizations/" f"{uuid4()}"
                    ),
                    "cost_job_id": uuid4(),
                    "cost_run_id": uuid4(),
                    "cost_input_id": "input-one",
                    "cost_capability_id": "claims-capability",
                    "cost_capability_version": 1,
                }
            )
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
        self.include_usage = False

    async def run(self, invocation, observer):
        self.invocations.append(invocation)
        first = {"type": "thread.started", "thread_id": "runtime-thread"}
        second = self.private_event or (
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100_000,
                    "cached_input_tokens": 0,
                    "output_tokens": 12_500,
                },
            }
            if self.include_usage
            else {"type": "turn.completed"}
        )
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
        usage = (
            RuntimeCodexUsageV1(
                schema_name="captain.runtime-codex-usage.v1",
                request_id=invocation.request_id,
                command_id=invocation.command_id,
                command_identity_sha256=invocation.command_identity_sha256,
                session_id="runtime-thread",
                model=invocation.model,
                input_units=100_000,
                cached_input_units=0,
                output_units=12_500,
                pricing_snapshot_id=invocation.pricing_snapshot_id,
                pricing_snapshot_sha256=invocation.pricing_snapshot_sha256,
                started_at=NOW,
                ended_at=NOW + timedelta(seconds=1),
            )
            if self.include_usage
            else None
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
            usage=usage,
        )

    async def cancel(self, session_id: str) -> str:
        assert self.invocations
        assert session_id == self.invocations[-1].session_id
        self.cancel_calls += 1
        self.exit_code = 130
        self.release.set()
        return "verified_cancelled"


def _codex_adapter(
    tmp_path: Path,
    runner: StreamingRunner,
    *,
    observer=None,
    environ: dict[str, str] | None = None,
    pricing_snapshots: tuple[RuntimeUsagePricingSnapshotV1, ...] = (),
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
            pricing_snapshots=pricing_snapshots,
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
async def test_resume_carries_cost_authority_and_persists_digest_bound_usage_receipt(
    tmp_path: Path,
) -> None:
    runner = StreamingRunner(exit_code=124)
    adapter, _, artifacts = _codex_adapter(
        tmp_path, runner, pricing_snapshots=(PRICING,)
    )
    prompt_ref = artifacts.put(b"cost-bound resume", "text/markdown")
    original = _command(prompt_ref)
    await adapter.start(original, _grant(original))
    runner.exit_code = 0
    runner.include_usage = True
    resumed = _cost_bound(
        _command(
            prompt_ref,
            operation="codex.resume",
            correlation_id=original.correlation_id,
            causation_id=original.event_id,
        )
    )

    result = await adapter.resume(resumed, _grant(resumed))

    invocation = runner.invocations[-1]
    assert invocation.request_id == resumed.event_id
    assert invocation.maximum_cost_usd == "0.75"
    assert invocation.cost_authority_ref == resumed.payload.cost_authority_ref
    assert invocation.hard_ceiling_enforced is False
    assert result.status is RuntimeStatus.POLICY_FAILED
    assert result.error == "codex.resume provider hard ceiling is unavailable"
    assert result.cost_evidence is not None
    receipt = RuntimeProviderUsageReceiptV1.model_validate_json(
        artifacts.read_bytes(result.cost_evidence.evidence_ref)
    )
    assert receipt.request_id == resumed.event_id
    assert receipt.command_id == original.event_id
    assert receipt.result_id == result.event_id
    assert receipt.actual_cost_usd == Decimal("0.250000")
    assert receipt.pricing_snapshot_sha256 == PRICING.snapshot_sha256


@pytest.mark.asyncio
async def test_exact_proxy_bound_resume_is_not_downgraded_by_production_adapter(
    tmp_path: Path,
) -> None:
    runner = StreamingRunner(exit_code=124)
    adapter, _, artifacts = _codex_adapter(
        tmp_path, runner, pricing_snapshots=(PRICING,)
    )
    prompt_ref = artifacts.put(b"proxy-bound resume", "text/markdown")
    original = _command(prompt_ref)
    await adapter.start(original, _grant(original))
    runner.exit_code = 0
    runner.include_usage = True
    resumed = _cost_bound(_command(
        prompt_ref, operation="codex.resume",
        correlation_id=original.correlation_id, causation_id=original.event_id,
    ))
    resumed = resumed.model_copy(update={"payload": resumed.payload.model_copy(update={
        "provider_proxy_url": "http://127.0.0.1:18091/v1",
        "provider_policy_sha256": "4" * 64,
        "provider_price_card_sha256": "5" * 64,
        "provider_context_sha256": "6" * 64,
        "provider_session_id": "runtime-thread",
        "provider_result_id": uuid5(resumed.event_id, "captain.runtime-result"),
    })})

    result = await adapter.resume(resumed, _grant(resumed))

    invocation = runner.invocations[-1]
    assert invocation.hard_ceiling_enforced is True
    assert invocation.provider_proxy_url == "http://127.0.0.1:18091/v1"
    assert invocation.provider_context_sha256 == "6" * 64
    assert result.status is RuntimeStatus.SUCCEEDED
    assert result.error is None
    assert result.cost_evidence is not None


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
async def test_verified_operator_cancel_is_cancelled_despite_non_130_child_exit(
    tmp_path: Path,
) -> None:
    class Non130CancelRunner(StreamingRunner):
        async def cancel(self, session_id: str) -> str:
            assert session_id == self.invocations[-1].session_id
            self.cancel_calls += 1
            self.exit_code = 17
            self.release.set()
            return "verified_cancelled"

    runner = Non130CancelRunner()
    runner.block = True
    adapter, execution, artifacts = _codex_adapter(tmp_path, runner)
    prompt_ref = artifacts.put(b"verified operator cancel", "text/markdown")
    original = _command(prompt_ref)
    active = asyncio.create_task(adapter.start(original, _grant(original)))
    await runner.started.wait()
    cancel = _command(
        prompt_ref,
        operation="codex.cancel",
        correlation_id=original.correlation_id,
        causation_id=original.event_id,
    )

    cancelled = await adapter.cancel(cancel, _grant(cancel))
    terminal = await active
    evidence = execution.terminal_evidence(original.event_id)

    assert cancelled.status is RuntimeStatus.CANCELLED
    assert terminal.status is RuntimeStatus.CANCELLED
    assert evidence is not None
    assert evidence.exit_code == 130
    assert evidence.process_cleanup_status == "verified_cancelled"
    assert runner.cancel_calls == 1


@pytest.mark.asyncio
async def test_verified_cancel_wins_when_child_result_arrives_before_cancel_returns(
    tmp_path: Path,
) -> None:
    class ResultBeforeCancelOutcomeRunner(StreamingRunner):
        def __init__(self) -> None:
            super().__init__()
            self.result_ready = asyncio.Event()

        async def run(self, invocation, observer):
            result = await super().run(invocation, observer)
            self.result_ready.set()
            return result

        async def cancel(self, session_id: str) -> str:
            assert session_id == self.invocations[-1].session_id
            self.cancel_calls += 1
            self.exit_code = 17
            self.release.set()
            await self.result_ready.wait()
            return "verified_cancelled"

    runner = ResultBeforeCancelOutcomeRunner()
    runner.block = True
    adapter, execution, artifacts = _codex_adapter(tmp_path, runner)
    prompt_ref = artifacts.put(b"result before verified cancel", "text/markdown")
    original = _command(prompt_ref)
    active = asyncio.create_task(adapter.start(original, _grant(original)))
    await runner.started.wait()
    cancel = _command(
        prompt_ref,
        operation="codex.cancel",
        correlation_id=original.correlation_id,
        causation_id=original.event_id,
    )

    cancelled, terminal = await asyncio.gather(
        adapter.cancel(cancel, _grant(cancel)),
        active,
    )
    evidence = execution.terminal_evidence(original.event_id)

    assert cancelled.status is RuntimeStatus.CANCELLED
    assert terminal.status is RuntimeStatus.CANCELLED
    assert evidence is not None
    assert evidence.exit_code == 130
    assert evidence.process_cleanup_status == "verified_cancelled"
    assert runner.cancel_calls == 1


@pytest.mark.asyncio
async def test_cancelled_attempt_does_not_cancel_successful_resume_or_status(
    tmp_path: Path,
) -> None:
    runner = StreamingRunner()
    runner.block = True
    adapter, execution, artifacts = _codex_adapter(tmp_path, runner)
    prompt_ref = artifacts.put(b"cancel then resume", "text/markdown")
    original = _command(prompt_ref)
    active = asyncio.create_task(adapter.start(original, _grant(original)))
    await runner.started.wait()
    cancel = _command(
        prompt_ref,
        operation="codex.cancel",
        correlation_id=original.correlation_id,
        causation_id=original.event_id,
    )
    cancelled = await adapter.cancel(cancel, _grant(cancel))
    assert (await active).status is RuntimeStatus.CANCELLED
    prior = execution.terminal_evidence(original.event_id)
    assert prior is not None
    prior_json = prior.model_dump_json(by_alias=True)

    runner.exit_code = 0
    runner.started = asyncio.Event()
    runner.release = asyncio.Event()
    resumed = _command(
        prompt_ref,
        operation="codex.resume",
        correlation_id=original.correlation_id,
        causation_id=original.event_id,
    )
    resume_task = asyncio.create_task(adapter.resume(resumed, _grant(resumed)))
    await runner.started.wait()
    status = _command(
        prompt_ref,
        operation="codex.status",
        correlation_id=original.correlation_id,
        causation_id=original.event_id,
    )

    assert (await adapter.status(status, _grant(status))).status is RuntimeStatus.RUNNING
    runner.release.set()
    assert (await resume_task).status is RuntimeStatus.SUCCEEDED
    assert (await adapter.status(status, _grant(status))).status is RuntimeStatus.SUCCEEDED
    assert cancelled.status is RuntimeStatus.CANCELLED
    assert prior.model_dump_json(by_alias=True) == prior_json


@pytest.mark.asyncio
async def test_prelaunch_failure_records_no_active_process_not_required_cleanup(
    tmp_path: Path,
) -> None:
    class MissingExecutableRunner(StreamingRunner):
        async def run(self, invocation: object, observer: object) -> object:
            del invocation, observer
            raise FileNotFoundError("private missing executable detail")

        async def cancel(self, session_id: str) -> str:
            del session_id
            self.cancel_calls += 1
            return "no_active_process"

    runner = MissingExecutableRunner()
    adapter, execution, artifacts = _codex_adapter(tmp_path, runner)
    prompt_ref = artifacts.put(b"prelaunch failure", "text/markdown")
    command = _command(prompt_ref)

    result = await adapter.start(command, _grant(command))
    evidence = execution.terminal_evidence(command.event_id)

    assert result.status is RuntimeStatus.FAILED
    assert evidence is not None
    assert evidence.process_cleanup_status == "not_required"
    assert evidence.failure_kind == "output_read_failed"
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
            error.bind_terminal_evidence(
                process_cleanup_status="verified_cancelled",
                journal_path=None,
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
async def test_current_runner_accepts_real_shaped_jsonl_usage_without_retaining_raw_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage_type = getattr(runtime_codex, "RuntimeCodexUsageV1", None)
    assert usage_type is not None, "typed runtime Codex usage is missing"
    process_runner = PowerShellRuntimeCodexRunner(
        repository_root=tmp_path,
        executable="codex",
        environ={},
        evidence_root=tmp_path / "runtime-codex",
    )
    authorized_commands: list[tuple[str, ...]] = []

    class JsonlRunner:
        async def run(self, authorized):
            authorized_commands.append(authorized.command)
            await observer(
                {"type": "thread.started", "thread_id": "usage-session"}
            )
            await observer(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 100_000,
                        "cached_input_tokens": 20_000,
                        "output_tokens": 12_500,
                    },
                }
            )
            return SimpleNamespace(
                exit_code=0,
                terminal_status="succeeded",
                process_cleanup_status="not_required",
            )

    observer = None

    def make_runner(_invocation, observed):
        nonlocal observer
        observer = observed
        return JsonlRunner()

    monkeypatch.setattr(process_runner, "_make_runner", make_runner)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    invocation = RuntimeCodexInvocation(
        command_id=uuid4(),
        request_id=uuid4(),
        correlation_id=uuid4(),
        subject_id="runtime-task",
        prompt_sha256="1" * 64,
        command_identity_sha256="2" * 64,
        session_id="usage-session",
        workspace_ref="workspace://runtime/task",
        workspace_binding_sha256="3" * 64,
        workspace=workspace,
        prompt="not persisted",
        timeout_seconds=30,
        model="gpt-5.6-terra",
        pricing_snapshot_id="openai-test-2026-08-09",
        pricing_snapshot_sha256="4" * 64,
    )

    async def ignore(_event: dict[str, object]) -> None:
        return None

    result = await process_runner.run(invocation, ignore)

    assert authorized_commands == [
        ("codex", "exec", "--json", "--model", "gpt-5.6-terra", "not persisted")
    ]
    assert result.usage == usage_type(
        schema_name="captain.runtime-codex-usage.v1",
        request_id=invocation.request_id,
        command_id=invocation.command_id,
        command_identity_sha256=invocation.command_identity_sha256,
        session_id="usage-session",
        model="gpt-5.6-terra",
        input_units=100_000,
        cached_input_units=20_000,
        output_units=12_500,
        pricing_snapshot_id="openai-test-2026-08-09",
        pricing_snapshot_sha256="4" * 64,
        started_at=result.usage.started_at,
        ended_at=result.usage.ended_at,
    )


@pytest.mark.asyncio
async def test_proxy_bound_runtime_child_has_only_proxy_credential_and_immutable_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_runner = PowerShellRuntimeCodexRunner(
        repository_root=tmp_path,
        executable="codex",
        environ={
            "PATH": "safe-path",
            "OPENAI_API_KEY": "must-not-cross",
            "CAPTAIN_GATEWAY_TOKEN": "must-not-cross",
            "CAPTAIN_RUNTIME_TOKEN": "must-not-cross",
            "CAPTAIN_PROVIDER_RECEIPT_GATEWAY_TOKEN": "must-not-cross",
            "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN": "proxy-only",
        },
        evidence_root=tmp_path / "runtime-codex",
    )
    captured = None

    class FakeCodex:
        async def run(self, authorized):
            nonlocal captured
            captured = authorized
            return SimpleNamespace(
                exit_code=0,
                terminal_status="succeeded",
                process_cleanup_status="not_required",
            )

    monkeypatch.setattr(process_runner, "_make_runner", lambda *_args: FakeCodex())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    invocation = RuntimeCodexInvocation(
        command_id=uuid4(), correlation_id=uuid4(), subject_id="runtime-task",
        prompt_sha256="1" * 64, command_identity_sha256="2" * 64,
        session_id="proxy-session", workspace_ref="workspace://runtime/task",
        workspace_binding_sha256="3" * 64, workspace=workspace, prompt="build",
        timeout_seconds=30, resume=True, hard_ceiling_enforced=True,
        provider_proxy_url="http://127.0.0.1:18091/v1",
        provider_policy_sha256="4" * 64,
        provider_price_card_sha256="5" * 64,
        provider_context_sha256="6" * 64,
    )

    await process_runner.run(invocation, lambda _event: asyncio.sleep(0))

    assert captured is not None
    child = captured.child_environment()
    assert child["CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN"] == "proxy-only"
    assert "OPENAI_API_KEY" not in child
    assert not any(name in child for name in (
        "CAPTAIN_GATEWAY_TOKEN", "CAPTAIN_RUNTIME_TOKEN",
        "CAPTAIN_PROVIDER_RECEIPT_GATEWAY_TOKEN",
    ))


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


def test_runtime_codex_rejects_reparsed_state_root_before_process_launch(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    evidence_root = tmp_path / "runtime-codex"
    evidence_root.mkdir()
    state_root = evidence_root / "state"
    if os.name == "nt":
        linked = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(state_root), str(outside)],
            capture_output=True,
            check=False,
            text=True,
        )
        if linked.returncode != 0:
            pytest.skip("directory junctions are unavailable on this host")
    else:
        try:
            state_root.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(ValueError, match="symlink|reparse|confined"):
        PowerShellRuntimeCodexRunner(
            repository_root=tmp_path,
            executable="codex",
            environ={},
            evidence_root=evidence_root,
        )

    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_runtime_codex_state_write_rejects_final_handle_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "scripts" / "codex-session.ps1"
    script.parent.mkdir()
    script.write_text("# test launcher", encoding="utf-8")
    pwsh = tmp_path / "pwsh.exe"
    codex = tmp_path / "codex.exe"
    pwsh.write_bytes(b"test")
    codex.write_bytes(b"test")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    process_runner = PowerShellRuntimeCodexRunner(
        repository_root=tmp_path,
        executable="codex",
        environ={"CODEX_HOME": str(codex_home)},
        evidence_root=tmp_path / "runtime-codex",
    )
    monkeypatch.setattr(
        "agenten.agent_runtime.runtime_codex.shutil.which",
        lambda executable: str(pwsh if executable == "pwsh" else codex),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    invocation = RuntimeCodexInvocation(
        command_id=uuid4(),
        correlation_id=uuid4(),
        subject_id="operator",
        prompt_sha256="1" * 64,
        command_identity_sha256="2" * 64,
        session_id="final-handle",
        workspace_ref="workspace://runtime/task",
        workspace_binding_sha256="3" * 64,
        workspace=workspace,
        prompt="not persisted",
        timeout_seconds=30,
    )

    async def observe(_event: dict[str, object]) -> None:
        return None

    runner = process_runner._make_runner(invocation, observe)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(
        confined_files,
        "_final_path_for_open_file",
        lambda _stream, *, requested_path: outside / requested_path.name,
    )
    state = {
        "session_id": "final-handle",
        "pid": 42,
        "started_at_utc": "2026-08-08T12:00:00Z",
        "start_time_utc_ticks": 123,
        "executable": str(codex),
    }

    with pytest.raises(CodexOutputObserverError):
        await runner._accept_process_state(json.dumps(state).encode("utf-8"))

    assert list(outside.iterdir()) == []
    assert not list((tmp_path / "runtime-codex").rglob("*.json"))


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
