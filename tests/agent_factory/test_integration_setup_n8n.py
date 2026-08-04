from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from agenten.agent_factory.contracts import FactoryRole
from agenten.agent_factory.integration_setup import (
    IntegrationCredentialRequirementV1,
)
from agenten.agent_factory.integration_setup_n8n import (
    CaptainN8nCredentialMetadataClient,
    N8nCredentialDiscoveryError,
)
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_runtime.contracts import IntegrationIntent
from agenten.agent_runtime.n8n_endpoint import N8nEndpoint
from tests.agent_factory.test_state_machine import job


NOW = datetime(2026, 8, 4, 10, tzinfo=timezone.utc)
MCP_TOKEN = "private-mcp-token-never-serialized"


def requirement() -> IntegrationCredentialRequirementV1:
    return IntegrationCredentialRequirementV1(
        integration_key="crm",
        credential_alias="CRM_API_KEY",
        credential_type="httpBearerAuth",
        required=True,
        setup_method="n8n_ui",
        setup_label="Bearer Auth",
        project_id="captain-production",
    )


def endpoint() -> N8nEndpoint:
    return N8nEndpoint(
        mode="captain-builder",
        api_base_url="http://localhost:5679",
        webhook_base_url="http://localhost:5679",
        api_key="private-api-key-never-used",
        mcp_token=MCP_TOKEN,
    )


def lease(role: FactoryRole = FactoryRole.TOOL_INTEGRATOR):
    return issue_factory_lease(
        job=job(),
        role=role,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=NOW,
        integration_intent=(
            IntegrationIntent.N8N
            if role is FactoryRole.TOOL_INTEGRATOR
            else IntegrationIntent.NONE
        ),
    )


@pytest.mark.asyncio
async def test_discovers_only_sanitized_exact_credential_metadata() -> None:
    async def handler(raw: httpx.Request) -> httpx.Response:
        assert raw.url == httpx.URL("http://localhost:5679/mcp-server/http")
        assert raw.headers["authorization"] == f"Bearer {MCP_TOKEN}"
        body = json.loads(raw.content)
        assert body["method"] == "tools/call"
        assert body["params"] == {
            "name": "list_credentials",
            "arguments": {
                "limit": 200,
                "projectId": "captain-production",
                "type": "httpBearerAuth",
            },
        }
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "isError": False,
                    "structuredContent": {
                        "count": 2,
                        "data": [
                            {
                                "id": "cred-prod",
                                "name": "CRM production",
                                "type": "httpBearerAuth",
                                "homeProject": {
                                    "id": "captain-production",
                                    "name": "Captain production",
                                    "type": "team",
                                },
                                "isGlobal": False,
                                "isManaged": False,
                                "scopes": ["credential:read"],
                                "token": "unexpected-upstream-field-must-be-dropped",
                            },
                            {
                                "id": "wrong-type",
                                "name": "Wrong type",
                                "type": "httpHeaderAuth",
                                "homeProject": {
                                    "id": "captain-production",
                                    "name": "Captain production",
                                    "type": "team",
                                },
                                "isGlobal": False,
                                "isManaged": False,
                                "scopes": ["credential:read"],
                            },
                        ],
                    },
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await CaptainN8nCredentialMetadataClient(http=http).discover(
            lease=lease(),
            endpoint=endpoint(),
            requirement=requirement(),
            now=NOW,
            timeout_seconds=1,
        )

    assert len(result) == 1
    assert result[0].credential_id == "cred-prod"
    assert result[0].credential_type == "httpBearerAuth"
    serialized = result[0].model_dump_json()
    assert "token" not in serialized.lower()
    assert MCP_TOKEN not in serialized


@pytest.mark.asyncio
async def test_discovery_requires_active_captain_n8n_tool_integrator_lease() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("unexpected dispatch"))
    ) as http:
        with pytest.raises(PermissionError, match="n8n lease"):
            await CaptainN8nCredentialMetadataClient(http=http).discover(
                lease=lease(FactoryRole.AGENT_ARCHITECT),
                endpoint=endpoint(),
                requirement=requirement(),
                now=NOW,
                timeout_seconds=1,
            )


@pytest.mark.asyncio
async def test_provider_failure_is_sanitized() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                500,
                text="Authorization: Bearer provider-secret-must-not-escape",
            )
        )
    ) as http:
        with pytest.raises(N8nCredentialDiscoveryError) as caught:
            await CaptainN8nCredentialMetadataClient(http=http).discover(
                lease=lease(),
                endpoint=endpoint(),
                requirement=requirement(),
                now=NOW,
                timeout_seconds=1,
            )

    assert "provider-secret" not in str(caught.value)
    assert MCP_TOKEN not in str(caught.value)
