"""Typed n8n tool catalog; workflow IDs never enter an agent tool call."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from agenten.agent_factory.contracts import FactoryLease, FactoryRole
from agenten.agent_runtime.contracts import IntegrationIntent


class TypedN8nTool(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = Field(min_length=1)
    input_schema_ref: str = Field(pattern=r"^artifact://")
    output_schema_ref: str = Field(pattern=r"^artifact://")


class TypedN8nCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    payload: dict[str, object]


class N8nMcpPort(Protocol):
    async def call_typed_tool(self, tool: TypedN8nTool, payload: dict[str, object]) -> dict[str, object]:
        """Invoke the implementation bound to the registered typed tool."""


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
        return await mcp.call_typed_tool(tool, call.payload)
