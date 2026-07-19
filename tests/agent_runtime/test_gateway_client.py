from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    CapabilityGrant,
    CapabilityGrantRevocation,
)
from agenten.agent_runtime.gateway_client import (
    GatewayRuntimeClient,
    GatewayRuntimeConflictError,
    GatewayRuntimeError,
)


FIXTURES = Path(__file__).parents[1] / "fixtures" / "contracts"


def command() -> AgentRuntimeCommand:
    return AgentRuntimeCommand.model_validate_json(
        (FIXTURES / "agent_runtime_command.v1.json").read_text(encoding="utf-8")
    )


def grant() -> CapabilityGrant:
    value = {
        "schema": "captain.capability-grant.v1",
        "grant_id": "grant-gateway-client",
        "command_id": str(command().event_id),
        "batch_id": "batch-1",
        "batch_version": 3,
        "subtask_id": "subtask-1",
        "workspace_ref": "workspace://authorized/project-1/subtask-1",
        "profile": "n8n-builder",
        "capabilities": [
            "codex.resume",
            "codex.run",
            "codex.status",
            "mcp.n8n",
            "tests.run",
            "workspace.write",
        ],
        "mcp_servers": ["n8n-mcp"],
        "issued_at": "2026-07-17T12:00:00Z",
        "expires_at": "2026-07-17T12:15:00Z",
    }
    return CapabilityGrant.model_validate(value)


def result() -> AgentRuntimeResult:
    value = json.loads(
        (FIXTURES / "agent_runtime_result.v1.json").read_text(encoding="utf-8")
    )
    value["grant_id"] = grant().grant_id
    return AgentRuntimeResult.model_validate(value)


def revocation() -> CapabilityGrantRevocation:
    issued_at = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
    return CapabilityGrantRevocation(
        schema_name="captain.capability-grant-revocation.v1",
        revocation_id=uuid4(),
        grant_id=grant().grant_id,
        command_id=command().event_id,
        revoked_at=issued_at + timedelta(minutes=1),
        reason="captain_cancelled",
    )


@pytest.mark.asyncio
async def test_client_writes_exact_aliased_contracts_and_reads_projection() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/commands"):
            return httpx.Response(
                202,
                json={"operation_id": str(command().event_id), "replayed": False},
                request=request,
            )
        if request.url.path.endswith("/grants"):
            return httpx.Response(
                201,
                json={"operation_id": str(command().event_id), "replayed": False},
                request=request,
            )
        if request.url.path.endswith("/results"):
            return httpx.Response(
                201,
                json={"operation_id": str(command().event_id), "replayed": False},
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "operation_id": str(command().event_id),
                "command": command().model_dump(mode="json", by_alias=True),
                "grant": grant().model_dump(mode="json", by_alias=True),
                "result": result().model_dump(mode="json", by_alias=True),
            },
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GatewayRuntimeClient("https://gateway.test/", "captain-secret", http)
        await client.accept_runtime_command(command())
        await client.record_capability_grant(grant())
        await client.record_runtime_result(result())
        operation = await client.get_runtime_operation(command().event_id)

    assert operation.command == command()
    assert operation.grant == grant()
    assert operation.result == result()
    assert [request.url.path for request in requests] == [
        "/v1/runtime/commands",
        "/v1/runtime/grants",
        "/v1/runtime/results",
        f"/v1/runtime/operations/{command().event_id}",
    ]
    for request in requests:
        assert request.headers["authorization"] == "Bearer captain-secret"
    command_body = json.loads(requests[0].content)
    assert command_body["schema"] == "captain.agent-runtime-command.v1"
    assert "schema_name" not in command_body


@pytest.mark.asyncio
async def test_client_maps_conflict_without_leaking_response_or_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            text="captain-secret and remote sensitive detail",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GatewayRuntimeClient("https://gateway.test", "captain-secret", http)
        with pytest.raises(GatewayRuntimeConflictError) as raised:
            await client.accept_runtime_command(command())

    assert "captain-secret" not in str(raised.value)
    assert "sensitive" not in str(raised.value)


@pytest.mark.asyncio
async def test_client_records_and_reads_an_append_only_grant_revocation() -> None:
    value = revocation()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/grant-revocations"):
            return httpx.Response(
                201,
                json={"operation_id": str(command().event_id), "replayed": False},
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "operation_id": str(command().event_id),
                "command": command().model_dump(mode="json", by_alias=True),
                "grant": grant().model_dump(mode="json", by_alias=True),
                "revocation": value.model_dump(mode="json", by_alias=True),
                "result": None,
            },
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GatewayRuntimeClient("https://gateway.test", "captain-secret", http)
        receipt = await client.record_capability_grant_revocation(value)
        observed = await client.get_grant_revocation(command().event_id)

    assert receipt.replayed is False
    assert observed == value
    assert [request.url.path for request in requests] == [
        "/v1/runtime/grant-revocations",
        f"/v1/runtime/operations/{command().event_id}",
    ]
    assert "captain-secret" not in requests[0].content.decode()


@pytest.mark.asyncio
async def test_client_rejects_malformed_success_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"unexpected": True}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GatewayRuntimeClient("https://gateway.test", "captain-secret", http)
        with pytest.raises(GatewayRuntimeError, match="invalid response"):
            await client.accept_runtime_command(command())
