from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from autogen_core import AgentId

from agenten.agent_runtime.contracts import AgentRuntimeResult, ArtifactRef, RuntimeStatus
from agenten.agent_runtime.swarm import (
    RuntimeTaskProjection,
    SwarmDispatchRequest,
    SwarmDispatchResponse,
    SwarmOrchestrator,
    SwarmSelectionError,
    ready_tasks,
)
from agenten.agent_runtime.tools import AuthoritativeRuntimeState, RuntimeToolContext
from agenten.runtime.bootstrap import build_runtime_and_bus, register_runtime_swarm


NOW = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)


def context(
    task_id: str,
    state: AuthoritativeRuntimeState,
    *,
    workspace: str | None = None,
) -> RuntimeToolContext:
    return RuntimeToolContext(
        state=state,
        project_id="project-1",
        correlation_id=UUID(int=2),
        subject_id=task_id,
        subject_version=3,
        batch_id=task_id,
        subtask_id=task_id,
        workspace_ref=workspace or f"workspace://authorized/project-1/{task_id}",
        prompt_ref=ArtifactRef(
            uri=f"artifact://runtime/{task_id}",
            sha256="a" * 64,
            media_type="text/markdown",
        ),
        wall_seconds=900,
        max_iterations=3,
    )


def task(
    task_id: str,
    state: AuthoritativeRuntimeState,
    *,
    depends_on: tuple[str, ...] = (),
    lane: str = "codex",
    workspace: str | None = None,
    version: int = 3,
) -> RuntimeTaskProjection:
    return RuntimeTaskProjection(
        task_id=task_id,
        plan_version=version,
        lane=lane,
        depends_on=depends_on,
        context=context(task_id, state, workspace=workspace),
    )


def test_child_is_not_ready_before_parent_passes() -> None:
    parent = task("parent", AuthoritativeRuntimeState.SUBTASK_READY)
    child = task(
        "child",
        AuthoritativeRuntimeState.SUBTASK_READY,
        depends_on=("parent",),
    )

    assert ready_tasks((parent, child), authoritative_plan_version=3) == (parent,)


def test_independent_lanes_overlap_but_shared_workspace_writers_do_not() -> None:
    shared = "workspace://authorized/project-1/shared"
    planning = task(
        "planning",
        AuthoritativeRuntimeState.PROJECT_RECEIVED,
        lane="hermes",
    )
    first = task(
        "build-a",
        AuthoritativeRuntimeState.SUBTASK_READY,
        workspace=shared,
    )
    second = task(
        "build-b",
        AuthoritativeRuntimeState.SUBTASK_READY,
        workspace=shared,
    )

    selected = ready_tasks(
        (planning, first, second),
        authoritative_plan_version=3,
        max_parallel=3,
    )

    assert {item.task_id for item in selected} == {"planning", "build-a"}


def test_stale_plan_version_is_fenced() -> None:
    stale = task("stale", AuthoritativeRuntimeState.SUBTASK_READY, version=2)

    assert ready_tasks((stale,), authoritative_plan_version=3) == ()


class FakeTools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def invoke(self, name: str, value: RuntimeToolContext) -> AgentRuntimeResult:
        self.calls.append((name, value.subject_id))
        return AgentRuntimeResult(
            schema_name="captain.agent-runtime-result.v1",
            event_id=UUID(int=30 + len(self.calls)),
            command_id=UUID(int=40 + len(self.calls)),
            correlation_id=value.correlation_id,
            occurred_at=NOW,
            producer="agent-runtime",
            subject_id=value.subject_id,
            subject_version=value.subject_version,
            grant_id="grant-swarm-test",
            operation=name,
            status=RuntimeStatus.SUCCEEDED,
        )


class FirstSelector:
    async def select(self, task_id: str, tools: tuple[str, ...]) -> str:
        del task_id
        return tools[0]


class InvalidSelector:
    async def select(self, task_id: str, tools: tuple[str, ...]) -> str:
        del task_id, tools
        return "codex.run"


@pytest.mark.asyncio
async def test_reasoning_selector_can_choose_only_guarded_tools() -> None:
    tools = FakeTools()
    orchestrator = SwarmOrchestrator(tools=tools, selector=InvalidSelector())
    planning = task(
        "planning",
        AuthoritativeRuntimeState.PROJECT_RECEIVED,
        lane="hermes",
    )

    with pytest.raises(SwarmSelectionError, match="unavailable tool"):
        await orchestrator.run_once((planning,), authoritative_plan_version=3)

    assert tools.calls == []


@pytest.mark.asyncio
async def test_restart_produces_same_actions_and_no_worker_to_worker_calls() -> None:
    projected = (
        task("build-a", AuthoritativeRuntimeState.SUBTASK_READY),
        task("build-b", AuthoritativeRuntimeState.REDO),
    )
    first_tools = FakeTools()
    first = await SwarmOrchestrator(
        tools=first_tools,
        selector=FirstSelector(),
    ).run_once(projected, authoritative_plan_version=3)

    restarted_tools = FakeTools()
    replay = await SwarmOrchestrator(
        tools=restarted_tools,
        selector=FirstSelector(),
    ).run_once(projected, authoritative_plan_version=3)

    assert [(item.task_id, item.tool_name) for item in first] == [
        (item.task_id, item.tool_name) for item in replay
    ]
    assert first_tools.calls == [("codex.run", "build-a")]
    assert restarted_tools.calls == first_tools.calls


@pytest.mark.asyncio
async def test_bootstrap_registers_real_routed_swarm_adapter() -> None:
    runtime, _ = build_runtime_and_bus()
    tools = FakeTools()
    orchestrator = SwarmOrchestrator(tools=tools, selector=FirstSelector())
    agent_type = await register_runtime_swarm(runtime, orchestrator)
    request = SwarmDispatchRequest(
        tasks=(task("build-a", AuthoritativeRuntimeState.SUBTASK_READY),),
        authoritative_plan_version=3,
    )

    runtime.start()
    try:
        response = await runtime.send_message(
            request,
            AgentId(agent_type, key="project-1"),
        )
        assert isinstance(response, SwarmDispatchResponse)
        assert [(item.task_id, item.tool_name) for item in response.actions] == [
            ("build-a", "codex.run")
        ]
    finally:
        await runtime.close()
