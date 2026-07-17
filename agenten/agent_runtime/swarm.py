"""Deterministic guards around reasoning-model runtime action selection."""

from __future__ import annotations

import asyncio
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agenten.agent_runtime.contracts import AgentRuntimeResult
from agenten.agent_runtime.tools import (
    AuthoritativeRuntimeState,
    RuntimeToolContext,
    available_tools,
)


class SwarmSelectionError(RuntimeError):
    """A reasoning selector chose outside the deterministic action set."""


class RuntimeTaskProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    plan_version: int = Field(ge=1, strict=True)
    lane: Literal["hermes", "captain", "codex"]
    depends_on: tuple[str, ...] = ()
    context: RuntimeToolContext

    @model_validator(mode="after")
    def validate_task_binding(self) -> "RuntimeTaskProjection":
        if self.task_id in self.depends_on:
            raise ValueError("task cannot depend on itself")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("task dependencies must not contain duplicates")
        if self.context.subject_id != self.task_id:
            raise ValueError("task_id must match context subject_id")
        return self


class SwarmAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    tool_name: str
    result: AgentRuntimeResult


class SwarmDispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tasks: tuple[RuntimeTaskProjection, ...]
    authoritative_plan_version: int = Field(ge=1, strict=True)


class SwarmDispatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    actions: tuple[SwarmAction, ...]


class ReasoningSelector(Protocol):
    async def select(self, task_id: str, tools: tuple[str, ...]) -> str: ...


class RuntimeToolInvoker(Protocol):
    async def invoke(
        self,
        name: str,
        value: RuntimeToolContext,
    ) -> AgentRuntimeResult: ...


def ready_tasks(
    tasks: tuple[RuntimeTaskProjection, ...],
    *,
    authoritative_plan_version: int,
    max_parallel: int = 3,
) -> tuple[RuntimeTaskProjection, ...]:
    """Return a bounded deterministic set; never infer readiness in the model."""

    if max_parallel < 1:
        raise ValueError("max_parallel must be positive")
    by_id = {task.task_id: task for task in tasks}
    if len(by_id) != len(tasks):
        raise ValueError("runtime task projections must have unique task IDs")
    for task in tasks:
        missing = set(task.depends_on) - set(by_id)
        if missing:
            raise ValueError(f"runtime task has unknown dependencies: {sorted(missing)}")

    passed = {
        task.task_id
        for task in tasks
        if task.plan_version == authoritative_plan_version
        and task.context.state is AuthoritativeRuntimeState.PASSED
    }
    candidates = sorted(
        (
            task
            for task in tasks
            if task.plan_version == authoritative_plan_version
            and task.context.state is not AuthoritativeRuntimeState.PASSED
            and set(task.depends_on).issubset(passed)
            and available_tools(task.context.state)
        ),
        key=lambda task: task.task_id,
    )

    selected: list[RuntimeTaskProjection] = []
    occupied_lanes: set[str] = set()
    writer_workspaces: set[str] = set()
    for task in candidates:
        if task.lane in occupied_lanes:
            continue
        workspace = task.context.workspace_ref
        if task.lane == "codex" and workspace is not None:
            if workspace in writer_workspaces:
                continue
            writer_workspaces.add(workspace)
        selected.append(task)
        occupied_lanes.add(task.lane)
        if len(selected) == max_parallel:
            break
    return tuple(selected)


class SwarmOrchestrator:
    def __init__(
        self,
        *,
        tools: RuntimeToolInvoker,
        selector: ReasoningSelector,
        max_parallel: int = 3,
    ) -> None:
        if max_parallel < 1:
            raise ValueError("max_parallel must be positive")
        self._tools = tools
        self._selector = selector
        self._max_parallel = max_parallel

    async def run_once(
        self,
        tasks: tuple[RuntimeTaskProjection, ...],
        *,
        authoritative_plan_version: int,
    ) -> tuple[SwarmAction, ...]:
        selected = ready_tasks(
            tasks,
            authoritative_plan_version=authoritative_plan_version,
            max_parallel=self._max_parallel,
        )
        choices: list[tuple[RuntimeTaskProjection, str]] = []
        for task in selected:
            permitted = tuple(sorted(available_tools(task.context.state)))
            chosen = await self._selector.select(task.task_id, permitted)
            if chosen not in permitted:
                raise SwarmSelectionError(
                    f"selector chose unavailable tool {chosen} for {task.task_id}"
                )
            choices.append((task, chosen))

        results = await asyncio.gather(
            *(self._tools.invoke(tool, task.context) for task, tool in choices)
        )
        return tuple(
            SwarmAction(task_id=task.task_id, tool_name=tool, result=result)
            for (task, tool), result in zip(choices, results, strict=True)
        )


try:
    from autogen_core import MessageContext, RoutedAgent, message_handler
except ImportError:  # pragma: no cover - deployment dependency is pinned
    AgentRuntimeSwarmRoutedAgent = None
else:

    class AgentRuntimeSwarmRoutedAgent(RoutedAgent):
        """AutoGen transport adapter; orchestration stays in SwarmOrchestrator."""

        def __init__(self, orchestrator: SwarmOrchestrator) -> None:
            super().__init__("Captain agent runtime swarm adapter")
            self._orchestrator = orchestrator

        @message_handler
        async def dispatch(
            self,
            message: SwarmDispatchRequest,
            ctx: MessageContext,
        ) -> SwarmDispatchResponse:
            del ctx
            actions = await self._orchestrator.run_once(
                message.tasks,
                authoritative_plan_version=message.authoritative_plan_version,
            )
            return SwarmDispatchResponse(actions=actions)
