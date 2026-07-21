"""Scoped wrapper for a Captain-authorized n8n MCP adapter."""

from __future__ import annotations

from typing import Any

from autogen_core.tools import BaseTool
from pydantic import BaseModel

from agenten.agent_factory.contracts import FactoryLease, FactoryRole
from agenten.agent_factory.team_execution import (
    FactoryN8nExecutionEvidenceV1,
    FactoryN8nToolAdapterPort,
    FactoryN8nToolAuthorizationV1,
)
from agenten.agent_runtime.contracts import CapabilityProfile, IntegrationIntent


class DisabledCaptainN8nMcpAdapter:
    """Fail closed when the Factory run has no Captain n8n intent."""

    def tool(self, _name: str) -> BaseTool[BaseModel, Any]:
        raise ValueError("Captain n8n MCP is disabled for this Factory run")

    def authorization(self, _name: str) -> FactoryN8nToolAuthorizationV1:
        raise ValueError("Captain n8n MCP is disabled for this Factory run")

    def observed_evidence(self) -> tuple[FactoryN8nExecutionEvidenceV1, ...]:
        return ()


class ScopedCaptainN8nMcpAdapter:
    """Expose an n8n adapter only for an explicit n8n integration intent."""

    def __init__(
        self,
        *,
        lease: FactoryLease,
        delegate: FactoryN8nToolAdapterPort,
    ) -> None:
        if (
            lease.integration_intent is not IntegrationIntent.N8N
            or lease.role is not FactoryRole.TOOL_INTEGRATOR
            or lease.capability_profile is not CapabilityProfile.N8N_BUILDER
            or "mcp.n8n" not in lease.capabilities
        ):
            raise ValueError("Captain n8n MCP requires integration_intent=n8n")
        if delegate is None:
            raise ValueError("Captain n8n MCP delegate is required")
        self._lease = lease
        self._delegate = delegate

    def tool(self, name: str) -> BaseTool[BaseModel, Any]:
        tool = self._delegate.tool(name)
        if not isinstance(tool, BaseTool):
            raise ValueError("Captain n8n MCP returned an untyped tool")
        return tool

    def authorization(self, name: str) -> FactoryN8nToolAuthorizationV1:
        claim = self._delegate.authorization(name)
        if not isinstance(claim, FactoryN8nToolAuthorizationV1):
            raise ValueError("Captain n8n MCP returned an untyped authorization")
        command = claim.runtime_command
        grant = claim.capability_grant
        if (
            claim.tool_name != name
            or command.correlation_id != self._lease.correlation_id
            or command.subject_version != self._lease.subject_version
            or command.payload.workspace_ref != self._lease.workspace_ref
        ):
            raise ValueError("Captain n8n MCP authorization is for a different tool")
        if (
            command.payload.integration_intent is not IntegrationIntent.N8N
            or command.payload.capability_profile is not CapabilityProfile.N8N_BUILDER
            or grant.profile is not CapabilityProfile.N8N_BUILDER
            or "mcp.n8n" not in grant.capabilities
            or grant.mcp_servers != ("n8n-mcp",)
            or grant.command_id != command.event_id
            or grant.batch_id != command.payload.batch_id
            or grant.batch_version != command.subject_version
            or grant.subtask_id != command.payload.subtask_id
            or grant.workspace_ref != command.payload.workspace_ref
        ):
            raise ValueError("Captain n8n authorization lacks the required n8n grant")
        return claim

    def observed_evidence(self) -> tuple[FactoryN8nExecutionEvidenceV1, ...]:
        evidence = self._delegate.observed_evidence()
        if any(not isinstance(item, FactoryN8nExecutionEvidenceV1) for item in evidence):
            raise ValueError("Captain n8n MCP evidence must be typed")
        if any(
            item.runtime_command.correlation_id != self._lease.correlation_id
            or item.runtime_command.subject_version != self._lease.subject_version
            or item.runtime_command.payload.workspace_ref != self._lease.workspace_ref
            for item in evidence
        ):
            raise ValueError("Captain n8n MCP evidence is outside the leased scope")
        call_ids = tuple(item.mcp_call_id for item in evidence)
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("Captain n8n MCP evidence contains duplicate call IDs")
        return evidence
