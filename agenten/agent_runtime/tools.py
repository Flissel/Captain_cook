"""Thin, typed runtime tools exposed to the AutoGen reasoning swarm."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Protocol
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityProfile,
    IntegrationIntent,
    RuntimeOperation,
)


class RuntimeToolUnavailable(RuntimeError):
    """The authoritative state does not expose the requested tool."""


class AuthoritativeRuntimeState(str, Enum):
    PROJECT_RECEIVED = "project_received"
    AGENT_DESIGN_REQUESTED = "agent_design_requested"
    SUBTASK_READY = "subtask_ready"
    REDO = "redo"
    PASSED = "passed"


STATE_TOOLS: dict[AuthoritativeRuntimeState, frozenset[str]] = {
    AuthoritativeRuntimeState.PROJECT_RECEIVED: frozenset({"hermes.plan"}),
    AuthoritativeRuntimeState.AGENT_DESIGN_REQUESTED: frozenset(
        {"hermes.design_agent"}
    ),
    AuthoritativeRuntimeState.SUBTASK_READY: frozenset(
        {"codex.run", "codex.status"}
    ),
    AuthoritativeRuntimeState.REDO: frozenset(
        {"codex.resume", "codex.status"}
    ),
    AuthoritativeRuntimeState.PASSED: frozenset(),
}


class RuntimeToolContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: AuthoritativeRuntimeState
    project_id: str = Field(min_length=1)
    correlation_id: UUID
    causation_id: UUID | None = None
    subject_id: str = Field(min_length=1)
    subject_version: int = Field(ge=1, strict=True)
    batch_id: str | None = Field(default=None, min_length=1)
    subtask_id: str | None = Field(default=None, min_length=1)
    workspace_ref: str | None = Field(default=None, pattern=r"^workspace://")
    prompt_ref: ArtifactRef
    integration_intent: IntegrationIntent = IntegrationIntent.NONE
    wall_seconds: int = Field(ge=1, le=3600, strict=True)
    max_iterations: int = Field(ge=1, le=10, strict=True)

    @model_validator(mode="after")
    def require_codex_bindings(self) -> "RuntimeToolContext":
        if self.state in {
            AuthoritativeRuntimeState.SUBTASK_READY,
            AuthoritativeRuntimeState.REDO,
        } and not all((self.batch_id, self.subtask_id, self.workspace_ref)):
            raise ValueError("Codex states require batch, subtask, and workspace bindings")
        if self.subtask_id is not None and self.subject_id != self.subtask_id:
            raise ValueError("subject_id must match subtask_id")
        return self


class RuntimeServicePort(Protocol):
    async def execute(self, command: AgentRuntimeCommand) -> AgentRuntimeResult: ...


class ToolClock(Protocol):
    def now(self) -> datetime: ...


def available_tools(state: AuthoritativeRuntimeState) -> frozenset[str]:
    return STATE_TOOLS[state]


class RuntimeToolset:
    """Build commands from trusted projections and submit one service request."""

    def __init__(self, *, service: RuntimeServicePort, clock: ToolClock) -> None:
        self._service = service
        self._clock = clock

    async def invoke(
        self,
        tool_name: str,
        context: RuntimeToolContext,
    ) -> AgentRuntimeResult:
        command = self.command_for(tool_name, context)
        return await self._service.execute(command)

    async def hermes_plan(self, context: RuntimeToolContext) -> AgentRuntimeResult:
        return await self.invoke("hermes.plan", context)

    async def hermes_design_agent(
        self,
        context: RuntimeToolContext,
    ) -> AgentRuntimeResult:
        return await self.invoke("hermes.design_agent", context)

    async def codex_run(self, context: RuntimeToolContext) -> AgentRuntimeResult:
        return await self.invoke("codex.run", context)

    async def codex_resume(self, context: RuntimeToolContext) -> AgentRuntimeResult:
        return await self.invoke("codex.resume", context)

    async def codex_status(self, context: RuntimeToolContext) -> AgentRuntimeResult:
        return await self.invoke("codex.status", context)

    def command_for(
        self,
        tool_name: str,
        context: RuntimeToolContext,
    ) -> AgentRuntimeCommand:
        permitted = available_tools(context.state)
        if tool_name not in permitted:
            raise RuntimeToolUnavailable(
                f"{tool_name} is not available in state {context.state.value}"
            )
        try:
            operation = RuntimeOperation(tool_name)
        except ValueError:
            raise RuntimeToolUnavailable(f"unknown runtime tool: {tool_name}") from None

        if operation is RuntimeOperation.HERMES_PLAN:
            profile = CapabilityProfile.PLANNER
        elif operation is RuntimeOperation.HERMES_DESIGN_AGENT:
            profile = CapabilityProfile.AGENT_DESIGNER
        elif context.integration_intent == IntegrationIntent.N8N:
            profile = CapabilityProfile.N8N_BUILDER
        else:
            profile = CapabilityProfile.CODE_BUILDER
        if operation in {
            RuntimeOperation.HERMES_PLAN,
            RuntimeOperation.HERMES_DESIGN_AGENT,
        } and context.integration_intent != IntegrationIntent.NONE:
            raise RuntimeToolUnavailable("Hermes tools cannot receive Codex integration intent")

        event_id = uuid5(
            context.correlation_id,
            "|".join(
                (
                    "agent-runtime-tool",
                    tool_name,
                    context.subject_id,
                    str(context.subject_version),
                )
            ),
        )
        return AgentRuntimeCommand.model_validate(
            {
                "schema": "captain.agent-runtime-command.v1",
                "event_id": event_id,
                "correlation_id": context.correlation_id,
                "causation_id": context.causation_id,
                "occurred_at": self._clock.now(),
                "producer": "captain-swarm",
                "subject_id": context.subject_id,
                "subject_version": context.subject_version,
                "payload": {
                    "operation": operation,
                    "project_id": context.project_id,
                    "batch_id": context.batch_id,
                    "subtask_id": context.subtask_id,
                    "workspace_ref": context.workspace_ref,
                    "prompt_ref": context.prompt_ref,
                    "integration_intent": context.integration_intent,
                    "capability_profile": profile,
                    "limits": {
                        "wall_seconds": context.wall_seconds,
                        "max_iterations": context.max_iterations,
                    },
                },
            }
        )
