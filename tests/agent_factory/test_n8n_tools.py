from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agenten.agent_factory.contracts import FactoryRole
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.n8n_tools import TypedN8nCall, TypedN8nCatalog, TypedN8nTool
from agenten.agent_runtime.contracts import IntegrationIntent
from tests.agent_factory.test_state_machine import job


NOW = datetime(2026, 7, 19, 10, tzinfo=timezone.utc)


class Mcp:
    async def call_typed_tool(self, tool, payload):
        return {"tool": tool.name, "payload": payload}


@pytest.mark.asyncio
async def test_n8n_call_requires_registered_tool_and_captain_n8n_lease() -> None:
    lease = issue_factory_lease(
        job=job(), role=FactoryRole.TOOL_INTEGRATOR, attempt=1,
        workspace_ref="workspace://factory/support-triage", now=NOW,
        integration_intent=IntegrationIntent.N8N,
    )
    catalog = TypedN8nCatalog((TypedN8nTool(
        name="crm_lookup", description="Look up an approved CRM record",
        input_schema_ref="artifact://schemas/crm-lookup-input",
        output_schema_ref="artifact://schemas/crm-lookup-output",
    ),))

    result = await catalog.invoke(
        lease=lease, call=TypedN8nCall(tool_name="crm_lookup", payload={"email": "a@example.test"}), mcp=Mcp()
    )

    assert result["tool"] == "crm_lookup"
    with pytest.raises(PermissionError, match="not registered"):
        await catalog.invoke(
            lease=lease, call=TypedN8nCall(tool_name="arbitrary_workflow", payload={}), mcp=Mcp()
        )
