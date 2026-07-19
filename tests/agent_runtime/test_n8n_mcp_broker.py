from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
import httpx
from fastapi.testclient import TestClient

from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    CapabilityGrant,
    CapabilityGrantRevocation,
)
from agenten.agent_runtime.n8n_mcp_broker import (
    McpLeaseDenied,
    McpLeaseIssuer,
    McpLeaseRevocationAuthorizer,
    create_mcp_broker_app,
)


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def command() -> AgentRuntimeCommand:
    return AgentRuntimeCommand.model_validate(
        {
            "schema": "captain.agent-runtime-command.v1",
            "event_id": "cda0c364-5d06-4d2a-a23a-1518294e2b9b",
            "correlation_id": "cda0c364-5d06-4d2a-a23a-1518294e2b9c",
            "occurred_at": NOW,
            "producer": "captain-swarm",
            "subject_id": "task-1",
            "subject_version": 1,
            "payload": {
                "operation": "codex.run",
                "project_id": "project-1",
                "batch_id": "batch-1",
                "subtask_id": "task-1",
                "workspace_ref": "workspace://project-1/task-1",
                "prompt_ref": {
                    "uri": "artifact://prompts/task-1",
                    "sha256": "a" * 64,
                    "media_type": "text/markdown",
                },
                "integration_intent": "n8n",
                "capability_profile": "n8n-builder",
                "limits": {"wall_seconds": 60, "max_iterations": 1},
            },
        }
    )


def grant(value: AgentRuntimeCommand) -> CapabilityGrant:
    return CapabilityGrant.model_validate(
        {
            "schema": "captain.capability-grant.v1",
            "grant_id": "grant-1",
            "command_id": str(value.event_id),
            "batch_id": "batch-1",
            "batch_version": 1,
            "subtask_id": "task-1",
            "workspace_ref": "workspace://project-1/task-1",
            "profile": "n8n-builder",
            "capabilities": [
                "codex.cancel",
                "codex.heartbeat",
                "codex.resume",
                "codex.run",
                "codex.status",
                "mcp.n8n",
                "tests.run",
                "workspace.write",
            ],
            "mcp_servers": ["n8n-mcp"],
            "issued_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        }
    )


class Reader:
    def __init__(self) -> None:
        self.revocation: CapabilityGrantRevocation | None = None
        self.calls: list[UUID] = []

    async def get_grant_revocation(
        self, command_id: UUID
    ) -> CapabilityGrantRevocation | None:
        self.calls.append(command_id)
        return self.revocation


def test_signed_mcp_lease_is_bound_to_exact_grant_command_and_endpoint() -> None:
    value = command()
    token = McpLeaseIssuer("broker-signing-secret").issue(
        grant(value), value, "http://127.0.0.1:5680", NOW
    )

    claim = McpLeaseIssuer("broker-signing-secret").verify(token, NOW)

    assert claim.grant_id == "grant-1"
    assert claim.command_id == value.event_id
    assert claim.endpoint_identity == "http://127.0.0.1:5680"
    assert "broker-signing-secret" not in token


def test_tampered_or_expired_mcp_lease_is_rejected() -> None:
    value = command()
    issuer = McpLeaseIssuer("broker-signing-secret")
    token = issuer.issue(grant(value), value, "http://127.0.0.1:5680", NOW)

    with pytest.raises(McpLeaseDenied, match="signature"):
        issuer.verify(f"{token}x", NOW)
    with pytest.raises(McpLeaseDenied, match="expired"):
        issuer.verify(token, NOW + timedelta(minutes=6))


@pytest.mark.asyncio
async def test_revocation_authorizer_denies_a_persisted_matching_revocation() -> None:
    value = command()
    issuer = McpLeaseIssuer("broker-signing-secret")
    token = issuer.issue(grant(value), value, "http://127.0.0.1:5680", NOW)
    reader = Reader()
    reader.revocation = CapabilityGrantRevocation(
        schema_name="captain.capability-grant-revocation.v1",
        revocation_id=uuid4(),
        grant_id="grant-1",
        command_id=value.event_id,
        revoked_at=NOW,
        reason="captain_cancelled",
    )

    with pytest.raises(McpLeaseDenied, match="revoked"):
        await McpLeaseRevocationAuthorizer(issuer, reader).authorize(token, NOW)

    assert reader.calls == [value.event_id]


def test_broker_forwards_only_authorized_request_with_internal_n8n_token() -> None:
    value = command()
    issuer = McpLeaseIssuer("broker-signing-secret")
    token = issuer.issue(grant(value), value, "http://127.0.0.1:5680", NOW)
    reader = Reader()
    seen: dict[str, str] = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["authorization"]
        seen["payload"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": {}})

    app = create_mcp_broker_app(
        authorizer=McpLeaseRevocationAuthorizer(issuer, reader),
        expected_endpoint_identity="http://127.0.0.1:5680",
        upstream_url="http://n8n.internal/mcp-server/http",
        upstream_token="internal-n8n-token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        clock=lambda: NOW,
    )
    with TestClient(app) as broker:
        response = broker.post(
            "/mcp-server/http",
            headers={"Authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

    assert response.status_code == 200
    assert seen == {
        "authorization": "Bearer internal-n8n-token",
        "payload": '{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
    }
    assert "internal-n8n-token" not in response.text


@pytest.mark.asyncio
async def test_broker_authorizer_rejects_revoked_lease_before_upstream_request() -> None:
    value = command()
    issuer = McpLeaseIssuer("broker-signing-secret")
    token = issuer.issue(grant(value), value, "http://127.0.0.1:5680", NOW)
    reader = Reader()
    reader.revocation = CapabilityGrantRevocation(
        schema_name="captain.capability-grant-revocation.v1",
        revocation_id=uuid4(),
        grant_id="grant-1",
        command_id=value.event_id,
        revoked_at=NOW,
        reason="captain_cancelled",
    )
    calls = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        del request
        calls += 1
        return httpx.Response(200)

    app = create_mcp_broker_app(
        authorizer=McpLeaseRevocationAuthorizer(issuer, reader),
        expected_endpoint_identity="http://127.0.0.1:5680",
        upstream_url="http://n8n.internal/mcp-server/http",
        upstream_token="internal-n8n-token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        clock=lambda: NOW,
    )
    with TestClient(app) as broker:
        response = broker.post(
            "/mcp-server/http",
            headers={"Authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

    assert response.status_code == 403
    assert calls == 0
