"""Typed n8n tool catalog; workflow IDs never enter an agent tool call."""

from __future__ import annotations

from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agenten.agent_factory.contracts import FactoryLease, FactoryRole
from agenten.agent_factory.integration_setup import (
    IntegrationSetupPlanV1,
    IntegrationSetupStatus,
)
from agenten.agent_runtime.contracts import IntegrationIntent
from agenten.agent_factory.skill_evaluation import ToolGapMarker, ToolImplementationOption
from agenten.targets.n8n import N8nDeployment, N8nExecutionEvidence, ValidationCase


class TypedN8nTool(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = Field(min_length=1)
    input_schema_ref: str = Field(pattern=r"^artifact://")
    output_schema_ref: str = Field(pattern=r"^artifact://")
    integration_key: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9-]{0,63}$",
        exclude=True,
    )

    def opaque_reference(self) -> "OpaqueN8nToolReference":
        """Return the only serializable reference for this approved typed tool."""

        return OpaqueN8nToolReference(
            schema_name="captain.n8n-mcp-tool-reference.v1",
            tool_name=self.name,
            input_schema_ref=self.input_schema_ref,
            output_schema_ref=self.output_schema_ref,
        )


class OpaqueN8nToolReference(BaseModel):
    """A typed n8n MCP capability without endpoint or workflow implementation data."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_name: Literal["captain.n8n-mcp-tool-reference.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    input_schema_ref: str = Field(pattern=r"^artifact://")
    output_schema_ref: str = Field(pattern=r"^artifact://")
    server_name: Literal["n8n-mcp"] = "n8n-mcp"


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

    def resolve_tool_gap_option(
        self,
        *,
        lease: FactoryLease,
        marker: ToolGapMarker,
        option: ToolImplementationOption,
    ) -> OpaqueN8nToolReference:
        """Resolve one Captain-approved tool gap option without exposing n8n internals."""

        _require_n8n_tool_lease(lease)
        if marker.least_privilege_capability != "mcp.n8n":
            raise PermissionError("tool gap does not request the approved n8n capability")
        if option not in marker.implementation_options:
            raise PermissionError("tool gap option is not part of the recorded marker")
        try:
            tool = self._tools[option.option_id]
        except KeyError as exc:
            raise PermissionError("tool gap option is not a registered typed n8n tool") from exc
        if (
            tool.input_schema_ref != marker.input_contract_ref.uri
            or tool.output_schema_ref != marker.output_contract_ref.uri
        ):
            raise PermissionError("typed n8n tool schemas do not match the tool gap contract")
        return tool.opaque_reference()

    async def invoke(
        self,
        *,
        lease: FactoryLease,
        call: TypedN8nCall,
        mcp: N8nMcpPort,
        integration_setup_plan: IntegrationSetupPlanV1 | None = None,
    ) -> dict[str, object]:
        _require_n8n_tool_lease(lease)
        try:
            tool = self._tools[call.tool_name]
        except KeyError as exc:
            raise PermissionError("n8n tool is not registered in Captain's typed catalog") from exc
        if tool.integration_key is not None:
            matching_connections = (
                ()
                if integration_setup_plan is None
                else tuple(
                    connection
                    for connection in integration_setup_plan.connections
                    if connection.requirement.integration_key == tool.integration_key
                )
            )
            if not matching_connections or any(
                connection.status is not IntegrationSetupStatus.READY
                for connection in matching_connections
            ):
                raise PermissionError("integration connection is not ready")
        if isinstance(mcp, N8nDeploymentToolAdapter):
            return await mcp.call_with_context(call)
        return await mcp.call_typed_tool(tool, call.payload)


def resolve_tool_gap_n8n_option(
    *,
    lease: FactoryLease,
    marker: ToolGapMarker,
    option: ToolImplementationOption,
    catalog: TypedN8nCatalog,
) -> OpaqueN8nToolReference:
    """Resolve a TODO_TOOL option through the Captain-owned typed catalog."""

    return catalog.resolve_tool_gap_option(lease=lease, marker=marker, option=option)


def _require_n8n_tool_lease(lease: FactoryLease) -> None:
    if lease.role is not FactoryRole.TOOL_INTEGRATOR:
        raise PermissionError("typed n8n tools require a Captain-issued n8n lease")
    if lease.integration_intent is not IntegrationIntent.N8N or "mcp.n8n" not in lease.capabilities:
        raise PermissionError("typed n8n tools require a Captain-issued n8n lease")
