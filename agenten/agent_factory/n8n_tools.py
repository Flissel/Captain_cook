"""Typed n8n tool catalog; workflow IDs never enter an agent tool call."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agenten.agent_factory.contracts import FactoryLease, FactoryRole
from agenten.agent_runtime.contracts import IntegrationIntent
from agenten.targets.n8n import N8nDeployment, N8nExecutionEvidence, ValidationCase


class TypedN8nTool(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = Field(min_length=1)
    input_schema_ref: str = Field(pattern=r"^artifact://")
    output_schema_ref: str = Field(pattern=r"^artifact://")


class TypedN8nCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    correlation_id: UUID
    payload: dict[str, object]


class N8nMcpPort(Protocol):
    async def call_typed_tool(self, tool: TypedN8nTool, payload: dict[str, object]) -> dict[str, object]:
        """Invoke the implementation bound to the registered typed tool."""


class N8nExecutionPort(Protocol):
    async def execute(
        self, deployment: N8nDeployment, case: ValidationCase
    ) -> N8nExecutionEvidence:
        """Execute a previously deployed n8n workflow with durable evidence."""


class N8nDeploymentToolAdapter(N8nMcpPort):
    """Expose named tools while keeping deployment workflow IDs out of calls."""

    def __init__(
        self,
        *,
        target: N8nExecutionPort,
        deployments: dict[str, N8nDeployment],
    ) -> None:
        self._target = target
        self._deployments = dict(deployments)

    async def call_typed_tool(
        self, tool: TypedN8nTool, payload: dict[str, object]
    ) -> dict[str, object]:
        raise RuntimeError("use call_with_context for deployment-backed n8n tools")

    async def call_with_context(self, call: TypedN8nCall) -> dict[str, object]:
        try:
            deployment = self._deployments[call.tool_name]
        except KeyError as exc:
            raise PermissionError("n8n tool has no Captain-approved deployment") from exc
        evidence = await self._target.execute(
            deployment,
            ValidationCase(
                case_id=call.case_id,
                correlation_id=str(call.correlation_id),
                input_payload=call.payload,
            ),
        )
        return evidence.model_dump(mode="json")


class TypedN8nCatalog:
    def __init__(self, tools: tuple[TypedN8nTool, ...]) -> None:
        names = [tool.name for tool in tools]
        if len(names) != len(set(names)):
            raise ValueError("typed n8n tool names must be unique")
        self._tools = {tool.name: tool for tool in tools}

    async def invoke(
        self, *, lease: FactoryLease, call: TypedN8nCall, mcp: N8nMcpPort
    ) -> dict[str, object]:
        if lease.role is not FactoryRole.TOOL_INTEGRATOR:
            raise PermissionError("typed n8n tools require the tool integrator lease")
        if lease.integration_intent is not IntegrationIntent.N8N or "mcp.n8n" not in lease.capabilities:
            raise PermissionError("typed n8n tools require a Captain-issued n8n lease")
        try:
            tool = self._tools[call.tool_name]
        except KeyError as exc:
            raise PermissionError("n8n tool is not registered in Captain's typed catalog") from exc
        if isinstance(mcp, N8nDeploymentToolAdapter):
            return await mcp.call_with_context(call)
        return await mcp.call_typed_tool(tool, call.payload)
