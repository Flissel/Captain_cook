"""Read-only Captain MCP server: the spike's bridge from OpenClaw to the gateway."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from agenten.captain_mcp.server import (
    CaptainGatewayReader,
    CaptainMcpConfigurationError,
    McpSettings,
    create_mcp_app,
)

MCP_TOKEN = "mcp-inbound-token"
GATEWAY_TOKEN = "openclaw-agent-token"


def gateway_handler(seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/batches":
            return httpx.Response(
                200, json=[{"batch_id": "batch-1", "title": "Build"}], request=request
            )
        if request.url.path == "/batches/batch-1":
            return httpx.Response(
                200,
                json={
                    "batch_id": "batch-1",
                    "status": "claimed",
                    "claim_token_sha256": "a" * 64,
                    "claim_iteration": 1,
                },
                request=request,
            )
        return httpx.Response(404, json={"detail": "batch not found"}, request=request)

    return httpx.MockTransport(handler)


def reader(seen: list[httpx.Request]) -> CaptainGatewayReader:
    client = httpx.AsyncClient(transport=gateway_handler(seen))
    return CaptainGatewayReader("https://gateway.test/", GATEWAY_TOKEN, client)


# --- gateway reads --------------------------------------------------------


@pytest.mark.asyncio
async def test_list_batches_reads_with_the_agent_token() -> None:
    seen: list[httpx.Request] = []

    batches = await reader(seen).list_batches("pending")

    assert batches == [{"batch_id": "batch-1", "title": "Build"}]
    assert seen[0].method == "GET"
    assert seen[0].url.params["status"] == "pending"
    assert seen[0].headers["authorization"] == f"Bearer {GATEWAY_TOKEN}"


@pytest.mark.asyncio
async def test_get_batch_drops_the_claim_token_digest() -> None:
    batch = await reader([]).get_batch("batch-1")

    assert batch["status"] == "claimed"
    assert "claim_token_sha256" not in batch


@pytest.mark.asyncio
async def test_gateway_errors_surface_without_response_bodies() -> None:
    with pytest.raises(RuntimeError) as excinfo:
        await reader([]).get_batch("missing")

    assert "404" in str(excinfo.value)
    assert "batch not found" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_unknown_status_is_rejected_before_any_request() -> None:
    seen: list[httpx.Request] = []

    with pytest.raises(ValueError):
        await reader(seen).list_batches("anything")
    assert seen == []


# --- settings -------------------------------------------------------------


def environment(**overrides: str) -> Mapping[str, str]:
    values = {
        "CAPTAIN_GATEWAY_URL": "http://127.0.0.1:8090",
        "CAPTAIN_MCP_GATEWAY_TOKEN": GATEWAY_TOKEN,
        "CAPTAIN_MCP_TOKEN": MCP_TOKEN,
    }
    values.update(overrides)
    return values


def test_settings_load_with_loopback_defaults() -> None:
    settings = McpSettings.from_env(environment())

    assert settings.host == "127.0.0.1"
    assert settings.port == 8121


@pytest.mark.parametrize("missing", ["CAPTAIN_GATEWAY_URL", "CAPTAIN_MCP_GATEWAY_TOKEN", "CAPTAIN_MCP_TOKEN"])
def test_missing_settings_fail_closed(missing: str) -> None:
    values = dict(environment())
    del values[missing]

    with pytest.raises(CaptainMcpConfigurationError) as excinfo:
        McpSettings.from_env(values)
    assert missing in str(excinfo.value)


def test_inbound_and_gateway_tokens_must_differ() -> None:
    with pytest.raises(CaptainMcpConfigurationError):
        McpSettings.from_env(environment(CAPTAIN_MCP_TOKEN=GATEWAY_TOKEN))


# --- MCP over HTTP --------------------------------------------------------


def mcp_client(seen: list[httpx.Request]) -> TestClient:
    return TestClient(create_mcp_app(reader(seen), inbound_token=MCP_TOKEN), base_url="http://127.0.0.1:8121")


def rpc(client: TestClient, method: str, params: dict[str, Any], *, token: str | None = MCP_TOKEN) -> httpx.Response:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(
        "/mcp",
        headers=headers,
        content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}),
    )


INIT = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "test", "version": "0"},
}


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_mcp_endpoint_requires_the_inbound_token(token: str | None) -> None:
    with mcp_client([]) as client:
        response = rpc(client, "initialize", INIT, token=token)

    assert response.status_code == 401


def test_mcp_lists_only_read_tools() -> None:
    with mcp_client([]) as client:
        assert rpc(client, "initialize", INIT).status_code == 200
        listed = rpc(client, "tools/list", {})

    names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert names == {"list_batches", "get_batch"}


def test_mcp_tool_call_reaches_the_gateway() -> None:
    seen: list[httpx.Request] = []
    with mcp_client(seen) as client:
        rpc(client, "initialize", INIT)
        called = rpc(client, "tools/call", {"name": "list_batches", "arguments": {"status": "pending"}})

    result = called.json()["result"]
    assert result["isError"] is False
    assert "batch-1" in json.dumps(result)
    assert seen[0].url.path == "/batches"


def test_container_host_name_is_accepted() -> None:
    """OpenClaw in Docker reaches the host as host.docker.internal."""

    app = create_mcp_app(reader([]), inbound_token=MCP_TOKEN)
    with TestClient(app, base_url="http://host.docker.internal:8121") as client:
        assert rpc(client, "initialize", INIT).status_code == 200
