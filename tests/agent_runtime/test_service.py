from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from agenten.agent_runtime.capabilities import CapabilityDenied, derive_grant, validate_grant
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityGrant,
    HermesPlanResult,
    RuntimeOperation,
    RuntimeStatus,
)
from agenten.agent_runtime.service import AgentRuntimeService, RuntimeContractViolation
from agenten.validation.contracts import AcceptanceAssertion, AssertionKind, WorkBatch


NOW = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "contracts"
    / "agent_runtime_command.v1.json"
)


def artifact(name: str, digest: str = "a") -> ArtifactRef:
    return ArtifactRef(
        uri=f"artifact://runtime/{name}",
        sha256=digest * 64,
        media_type="text/markdown",
    )


def command_for(operation: str = "codex.run") -> AgentRuntimeCommand:
    value: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    value["event_id"] = str(uuid4())
    value["occurred_at"] = NOW.isoformat().replace("+00:00", "Z")
    value["payload"]["operation"] = operation
    if operation.startswith("hermes."):
        value["payload"]["capability_profile"] = (
            "planner" if operation == "hermes.plan" else "agent-designer"
        )
        value["payload"]["integration_intent"] = "none"
    return AgentRuntimeCommand.model_validate(value)


def batch_for(command: AgentRuntimeCommand) -> WorkBatch:
    return WorkBatch(
        batch_id="batch-1",
        title="Released runtime work",
        goal="Perform the bounded runtime operation.",
        subtask_ids=["subtask-1"],
        target="python",
        capability_tags=[command.payload.capability_profile.value],
        acceptance_criteria=[
            AcceptanceAssertion(
                assertion_id="runtime-result-recorded",
                kind=AssertionKind.STATUS_EQUALS,
                path="status",
                expected="succeeded",
            )
        ],
    )


class FakeClock:
    def now(self) -> datetime:
        return NOW


class FakeCapabilityPolicy:
    def derive(
        self,
        command: AgentRuntimeCommand,
        batch: WorkBatch,
        now: datetime,
    ) -> CapabilityGrant:
        return derive_grant(command, batch, now)

    def validate(
        self,
        grant: CapabilityGrant,
        command: AgentRuntimeCommand,
        now: datetime,
    ) -> CapabilityGrant:
        return validate_grant(grant, command, now)


class FakeState:
    def __init__(self, events: list[str], batch: WorkBatch) -> None:
        self.events = events
        self.batch = batch
        self.commands: dict[UUID, AgentRuntimeCommand] = {}
        self.grants: dict[UUID, CapabilityGrant] = {}
        self.results: dict[UUID, AgentRuntimeResult] = {}

    async def get_result(self, command_id: UUID) -> AgentRuntimeResult | None:
        return self.results.get(command_id)

    async def accept_command(self, command: AgentRuntimeCommand) -> None:
        existing = self.commands.get(command.event_id)
        if existing is not None and existing != command:
            raise RuntimeError("conflicting command replay")
        if existing is None:
            self.events.append("command_accepted")
        self.commands[command.event_id] = command

    async def get_released_batch(self, command: AgentRuntimeCommand) -> WorkBatch:
        assert command.event_id in self.commands
        return self.batch

    async def get_grant(self, command_id: UUID) -> CapabilityGrant | None:
        return self.grants.get(command_id)

    async def record_grant(self, grant: CapabilityGrant) -> CapabilityGrant:
        self.grants[grant.command_id] = grant
        self.events.append("grant_recorded")
        return grant

    async def record_result(self, result: AgentRuntimeResult) -> AgentRuntimeResult:
        assert result.command_id in self.commands
        self.results[result.command_id] = result
        self.events.append("result_recorded")
        return result


class FakeArtifacts:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def require(self, reference: ArtifactRef) -> None:
        assert reference.uri.startswith("artifact://")
        self.events.append("prompt_resolved")


class FakeCodex:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[RuntimeOperation] = []
        self.fail = False
        self.mismatch = False

    async def start(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> AgentRuntimeResult:
        return self._result(command, grant)

    async def resume(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> AgentRuntimeResult:
        return self._result(command, grant)

    async def status(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> AgentRuntimeResult:
        return self._result(command, grant)

    async def cancel(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> AgentRuntimeResult:
        return self._result(command, grant)

    async def heartbeat(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> AgentRuntimeResult:
        return self._result(command, grant)

    def _result(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> AgentRuntimeResult:
        self.events.append("codex_adapter")
        self.calls.append(command.payload.operation)
        assert "command_accepted" in self.events
        assert "grant_recorded" in self.events
        if self.fail:
            raise OSError("sensitive adapter detail")
        return AgentRuntimeResult(
            schema_name="captain.agent-runtime-result.v1",
            event_id=uuid4(),
            command_id=uuid4() if self.mismatch else command.event_id,
            correlation_id=command.correlation_id,
            occurred_at=NOW,
            producer="agent-runtime",
            subject_id=command.subject_id,
            subject_version=command.subject_version,
            grant_id=grant.grant_id,
            operation=command.payload.operation,
            status=RuntimeStatus.SUCCEEDED,
            session_id="codex-session-1",
            artifact_refs=(artifact("build-output"),),
            evidence_refs=(artifact("test-evidence"),),
        )


class FakeHermes:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[RuntimeOperation] = []

    async def plan(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> HermesPlanResult:
        return self._plan(command)

    async def design_agent(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> HermesPlanResult:
        return self._plan(command)

    def _plan(self, command: AgentRuntimeCommand) -> HermesPlanResult:
        self.events.append("hermes_adapter")
        self.calls.append(command.payload.operation)
        return HermesPlanResult(
            schema_name="captain.hermes-plan-result.v1",
            project_id="project-1",
            correlation_id=command.correlation_id,
            subject_version=command.subject_version,
            plan_ref=artifact("hermes-plan", "b"),
            decision_log_ref=artifact("decision-log", "c"),
            blueprint_refs=(artifact("blueprint", "d"),),
            minibook={"project_id": "project-1", "post_id": "post-1"},
            planner_id="hermes-planner-1",
            runtime_provenance="hermes-fixture",
            started_at=NOW,
            ended_at=NOW,
        )


def service_with(
    state: FakeState,
    events: list[str],
    codex: FakeCodex,
    hermes: FakeHermes,
) -> AgentRuntimeService:
    return AgentRuntimeService(
        state=state,
        hermes=hermes,
        codex=codex,
        artifacts=FakeArtifacts(events),
        capabilities=FakeCapabilityPolicy(),
        clock=FakeClock(),
    )


@pytest.mark.asyncio
async def test_command_and_grant_are_persisted_before_external_effect() -> None:
    events: list[str] = []
    command = command_for()
    state = FakeState(events, batch_for(command))
    codex = FakeCodex(events)
    result = await service_with(state, events, codex, FakeHermes(events)).execute(command)

    assert result.status is RuntimeStatus.SUCCEEDED
    assert events == [
        "command_accepted",
        "grant_recorded",
        "prompt_resolved",
        "codex_adapter",
        "result_recorded",
    ]


@pytest.mark.asyncio
async def test_replay_returns_stored_result_without_second_adapter_call() -> None:
    events: list[str] = []
    command = command_for()
    state = FakeState(events, batch_for(command))
    codex = FakeCodex(events)
    first_service = service_with(state, events, codex, FakeHermes(events))
    first = await first_service.execute(command)

    restarted_service = service_with(state, events, codex, FakeHermes(events))
    replay = await restarted_service.execute(command)

    assert replay == first
    assert codex.calls == [RuntimeOperation.CODEX_RUN]
    assert events.count("command_accepted") == 1


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("codex.run", "codex.run"),
        ("codex.resume", "codex.resume"),
        ("codex.status", "codex.status"),
        ("codex.cancel", "codex.cancel"),
        ("codex.heartbeat", "codex.heartbeat"),
    ],
)
@pytest.mark.asyncio
async def test_codex_operations_dispatch_explicitly(operation: str, expected: str) -> None:
    events: list[str] = []
    command = command_for(operation)
    state = FakeState(events, batch_for(command))
    codex = FakeCodex(events)

    await service_with(state, events, codex, FakeHermes(events)).execute(command)

    assert [call.value for call in codex.calls] == [expected]


@pytest.mark.parametrize("operation", ["hermes.plan", "hermes.design_agent"])
@pytest.mark.asyncio
async def test_hermes_results_are_converted_to_runtime_results(operation: str) -> None:
    events: list[str] = []
    command = command_for(operation)
    state = FakeState(events, batch_for(command))
    hermes = FakeHermes(events)

    result = await service_with(state, events, FakeCodex(events), hermes).execute(command)

    assert result.producer == "hermes-runtime"
    assert result.artifact_refs[0].uri.endswith("hermes-plan")
    assert result.evidence_refs[0].uri.endswith("decision-log")
    assert hermes.calls == [command.payload.operation]


@pytest.mark.asyncio
async def test_mismatched_adapter_result_is_not_persisted() -> None:
    events: list[str] = []
    command = command_for()
    state = FakeState(events, batch_for(command))
    codex = FakeCodex(events)
    codex.mismatch = True

    with pytest.raises(RuntimeContractViolation, match="command"):
        await service_with(state, events, codex, FakeHermes(events)).execute(command)

    assert command.event_id not in state.results


@pytest.mark.asyncio
async def test_adapter_exception_becomes_redacted_durable_infrastructure_result() -> None:
    events: list[str] = []
    command = command_for()
    state = FakeState(events, batch_for(command))
    codex = FakeCodex(events)
    codex.fail = True

    result = await service_with(state, events, codex, FakeHermes(events)).execute(command)

    assert result.status is RuntimeStatus.INFRASTRUCTURE_FAILED
    assert result.error == "codex.run adapter failed"
    assert "sensitive" not in result.model_dump_json()
    assert events[-1] == "result_recorded"


@pytest.mark.asyncio
async def test_unreleased_capability_stops_before_artifact_or_adapter() -> None:
    events: list[str] = []
    command = command_for()
    wrong_batch = batch_for(command).model_copy(update={"capability_tags": ["code-builder"]})
    state = FakeState(events, wrong_batch)
    codex = FakeCodex(events)

    with pytest.raises(CapabilityDenied, match="not released"):
        await service_with(state, events, codex, FakeHermes(events)).execute(command)

    assert events == ["command_accepted"]
    assert codex.calls == []
