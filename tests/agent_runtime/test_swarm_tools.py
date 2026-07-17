from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    ArtifactRef,
    RuntimeStatus,
)
from agenten.agent_runtime.tools import (
    AuthoritativeRuntimeState,
    RuntimeToolContext,
    RuntimeToolset,
    RuntimeToolUnavailable,
    available_tools,
)


NOW = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)


class FakeClock:
    def now(self) -> datetime:
        return NOW


class FakeService:
    def __init__(self) -> None:
        self.commands: list[AgentRuntimeCommand] = []

    async def execute(self, command: AgentRuntimeCommand) -> AgentRuntimeResult:
        self.commands.append(command)
        return AgentRuntimeResult(
            schema_name="captain.agent-runtime-result.v1",
            event_id=UUID(int=20 + len(self.commands)),
            command_id=command.event_id,
            correlation_id=command.correlation_id,
            occurred_at=NOW,
            producer="agent-runtime",
            subject_id=command.subject_id,
            subject_version=command.subject_version,
            grant_id="grant-tool-test",
            operation=command.payload.operation,
            status=RuntimeStatus.SUCCEEDED,
        )


def context(state: AuthoritativeRuntimeState) -> RuntimeToolContext:
    return RuntimeToolContext(
        state=state,
        project_id="project-1",
        correlation_id=UUID(int=2),
        causation_id=UUID(int=1),
        subject_id="subtask-1",
        subject_version=3,
        batch_id="batch-1",
        subtask_id="subtask-1",
        workspace_ref="workspace://authorized/project-1/subtask-1",
        prompt_ref=ArtifactRef(
            uri="artifact://runtime/prompt-1",
            sha256="a" * 64,
            media_type="text/markdown",
        ),
        integration_intent="none",
        wall_seconds=900,
        max_iterations=3,
    )


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("project_received", {"hermes.plan"}),
        ("agent_design_requested", {"hermes.design_agent"}),
        ("subtask_ready", {"codex.run", "codex.status"}),
        ("redo", {"codex.resume", "codex.status"}),
        ("passed", set()),
    ],
)
def test_tools_are_derived_from_authoritative_state(
    state: str,
    expected: set[str],
) -> None:
    assert available_tools(AuthoritativeRuntimeState(state)) == frozenset(expected)


@pytest.mark.asyncio
async def test_tool_wrapper_builds_command_without_model_supplied_capabilities() -> None:
    service = FakeService()
    tools = RuntimeToolset(service=service, clock=FakeClock())

    result = await tools.invoke("codex.run", context(AuthoritativeRuntimeState.SUBTASK_READY))

    command = service.commands[0]
    assert result.command_id == command.event_id
    assert command.payload.capability_profile.value == "code-builder"
    assert command.payload.integration_intent.value == "none"
    assert "capabilities" not in command.model_dump(mode="json", by_alias=True)["payload"]


@pytest.mark.asyncio
async def test_n8n_intent_selects_n8n_profile_only_for_codex_build() -> None:
    service = FakeService()
    tools = RuntimeToolset(service=service, clock=FakeClock())
    value = context(AuthoritativeRuntimeState.SUBTASK_READY).model_copy(
        update={"integration_intent": "n8n"}
    )

    await tools.invoke("codex.run", value)

    assert service.commands[0].payload.capability_profile.value == "n8n-builder"


@pytest.mark.asyncio
async def test_unavailable_tool_is_rejected_before_service_call() -> None:
    service = FakeService()
    tools = RuntimeToolset(service=service, clock=FakeClock())

    with pytest.raises(RuntimeToolUnavailable, match="not available"):
        await tools.invoke("codex.run", context(AuthoritativeRuntimeState.PROJECT_RECEIVED))

    assert service.commands == []


def test_tool_command_id_is_stable_for_crash_replay() -> None:
    service = FakeService()
    tools = RuntimeToolset(service=service, clock=FakeClock())
    value = context(AuthoritativeRuntimeState.REDO)

    first = tools.command_for("codex.resume", value)
    replay = tools.command_for("codex.resume", value)

    assert first == replay
    assert first.event_id == replay.event_id


def test_context_forbids_raw_capability_input() -> None:
    payload = context(AuthoritativeRuntimeState.SUBTASK_READY).model_dump(mode="json")
    payload["capabilities"] = ["mcp.n8n"]

    with pytest.raises(ValueError):
        RuntimeToolContext.model_validate(payload)
