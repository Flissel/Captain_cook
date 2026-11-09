"""Captain write tools on the MCP server: opt-in, fixed, annotated."""

from __future__ import annotations

import json
from typing import Any, get_args

import httpx
import pytest
from starlette.testclient import TestClient

from agenten.captain_mcp.server import (
    BatchCancelReason,
    CaptainGatewayReader,
    CaptainGatewayWriter,
    CaptainMcpConfigurationError,
    GrantRevokeReason,
    McpSettings,
    RetireReason,
    create_mcp_app,
)
from tests.captain_mcp.test_captain_mcp_server import (
    GATEWAY_TOKEN,
    INIT,
    MCP_TOKEN,
    environment,
    reader,
    rpc,
)

CAPTAIN_TOKEN = "captain-mcp-captain-token"
WRITE_TOOLS = {
    "register_agent",
    "retire_agent",
    "grant_agent_capability",
    "revoke_agent_capability",
    "assign_batch",
    "cancel_batch",
    "approve_batch",
}
READ_TOOLS = {"list_batches", "get_batch", "get_agent", "get_agent_outcomes"}


def captain_gateway(seen: list[httpx.Request], *, status: int = 201, body: Any = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        payload = body if body is not None else {"echo": json.loads(request.content or b"{}")}
        return httpx.Response(status, json=payload, request=request)

    return httpx.MockTransport(handler)


def writer(seen: list[httpx.Request], **options: Any) -> CaptainGatewayWriter:
    client = httpx.AsyncClient(transport=captain_gateway(seen, **options))
    return CaptainGatewayWriter("https://gateway.test/", CAPTAIN_TOKEN, client)


# --- gateway writes -------------------------------------------------------


@pytest.mark.asyncio
async def test_writes_use_the_captain_token_and_report_replays() -> None:
    seen: list[httpx.Request] = []
    created = await writer(seen).assign_batch("batch/1", "openclaw")
    replayed = await writer(seen, status=200).assign_batch("batch-1", "openclaw")

    assert created["replayed"] is False and replayed["replayed"] is True
    assert created["record"] == {"echo": {"agent_id": "openclaw"}}
    assert seen[0].url.raw_path == b"/batches/batch%2F1/assign"
    assert all(request.headers["authorization"] == f"Bearer {CAPTAIN_TOKEN}" for request in seen)


@pytest.mark.asyncio
async def test_every_write_hits_its_dedicated_route() -> None:
    seen: list[httpx.Request] = []
    w = writer(seen)
    await w.register_agent("openclaw", "1", "captain", "a" * 64)
    await w.retire_agent("openclaw", "captain_retired")
    await w.grant_agent_capability("openclaw", "grant-1", ["python"], "2026-12-01T00:00:00+00:00")
    await w.revoke_agent_capability("openclaw", "grant-1", "captain_revoked")
    await w.assign_batch("batch-1", "openclaw")
    await w.cancel_batch("batch-1", "unroutable")
    await w.approve_batch("batch-1")

    assert [(request.method, request.url.path) for request in seen] == [
        ("POST", "/agents"),
        ("POST", "/agents/openclaw/retire"),
        ("POST", "/agents/openclaw/grants"),
        ("POST", "/agents/openclaw/grants/grant-1/revoke"),
        ("POST", "/batches/batch-1/assign"),
        ("POST", "/batches/batch-1/cancel"),
        ("POST", "/batches/batch-1/approve"),
    ]
    assert json.loads(seen[2].content) == {
        "grant_id": "grant-1",
        "capability_tags": ["python"],
        "expires_at": "2026-12-01T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_short_gateway_reasons_surface_but_bodies_do_not() -> None:
    with pytest.raises(RuntimeError, match=r"status 409: agent is retired$"):
        await writer([], status=409, body={"detail": "agent is retired"}).assign_batch("b", "openclaw")
    with pytest.raises(RuntimeError) as structured:
        await writer([], status=422, body={"detail": [{"input": "secret-ledger-content"}]}).approve_batch("b")

    assert "secret-ledger-content" not in str(structured.value)
    assert str(structured.value).endswith("status 422")


@pytest.mark.asyncio
async def test_unreachable_gateway_is_reported_without_details() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom with internals", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(broken))
    with pytest.raises(RuntimeError, match="could not reach the gateway") as excinfo:
        await CaptainGatewayWriter("https://gateway.test", CAPTAIN_TOKEN, client).approve_batch("b")
    assert "internals" not in str(excinfo.value)


# --- settings -------------------------------------------------------------


def test_writes_stay_off_without_a_captain_token() -> None:
    assert McpSettings.from_env(environment()).captain_token is None


def test_captain_token_must_differ_from_the_other_tokens() -> None:
    for clash in (GATEWAY_TOKEN, MCP_TOKEN):
        with pytest.raises(CaptainMcpConfigurationError):
            McpSettings.from_env(environment(CAPTAIN_MCP_CAPTAIN_TOKEN=clash))
    assert (
        McpSettings.from_env(environment(CAPTAIN_MCP_CAPTAIN_TOKEN=CAPTAIN_TOKEN)).captain_token
        == CAPTAIN_TOKEN
    )


def test_reason_vocabularies_match_the_gateway() -> None:
    from gateway.agent_lifecycle import RetireReason as GatewayRetire
    from gateway.agent_lifecycle import RevokeReason as GatewayRevoke
    from gateway.contracts import CancelReason as GatewayCancel

    assert set(get_args(RetireReason)) == set(get_args(GatewayRetire))
    assert set(get_args(GrantRevokeReason)) == set(get_args(GatewayRevoke))
    assert set(get_args(BatchCancelReason)) == set(get_args(GatewayCancel))


# --- MCP over HTTP --------------------------------------------------------


def _tools(app: Any) -> dict[str, dict[str, Any]]:
    with TestClient(app, base_url="http://127.0.0.1:18121") as client:
        assert rpc(client, "initialize", INIT).status_code == 200
        listed = rpc(client, "tools/list", {})
    return {tool["name"]: tool for tool in listed.json()["result"]["tools"]}


def test_read_only_server_exposes_no_write_tools() -> None:
    tools = _tools(create_mcp_app(reader([]), inbound_token=MCP_TOKEN))

    assert set(tools) == READ_TOOLS
    assert all(tool["annotations"]["readOnlyHint"] is True for tool in tools.values())


def test_writer_adds_exactly_the_fixed_write_tools_with_annotations() -> None:
    tools = _tools(create_mcp_app(reader([]), inbound_token=MCP_TOKEN, writer=writer([])))

    assert set(tools) == READ_TOOLS | WRITE_TOOLS
    for name in WRITE_TOOLS:
        assert tools[name]["annotations"]["readOnlyHint"] is False
    assert tools["retire_agent"]["annotations"]["destructiveHint"] is True
    assert tools["cancel_batch"]["annotations"]["destructiveHint"] is True
    assert tools["assign_batch"]["annotations"]["destructiveHint"] is False
    reasons = tools["cancel_batch"]["inputSchema"]["properties"]["reason"]
    assert "unroutable" in json.dumps(reasons)


def test_write_tool_call_reaches_the_gateway_as_captain() -> None:
    seen: list[httpx.Request] = []
    app = create_mcp_app(reader([]), inbound_token=MCP_TOKEN, writer=writer(seen))
    with TestClient(app, base_url="http://127.0.0.1:18121") as client:
        rpc(client, "initialize", INIT)
        called = rpc(
            client,
            "tools/call",
            {"name": "assign_batch", "arguments": {"batch_id": "batch-1", "agent_id": "openclaw"}},
        )

    assert called.json()["result"]["isError"] is False
    assert seen[0].url.path == "/batches/batch-1/assign"
    assert seen[0].headers["authorization"] == f"Bearer {CAPTAIN_TOKEN}"


def test_agent_reads_use_the_agent_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[] if request.url.path.endswith("/outcomes") else {}, request=request)

    agent_reader = CaptainGatewayReader(
        "https://gateway.test", GATEWAY_TOKEN, httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    app = create_mcp_app(agent_reader, inbound_token=MCP_TOKEN)
    with TestClient(app, base_url="http://127.0.0.1:18121") as client:
        rpc(client, "initialize", INIT)
        rpc(client, "tools/call", {"name": "get_agent", "arguments": {"agent_id": "openclaw"}})
        rpc(client, "tools/call", {"name": "get_agent_outcomes", "arguments": {"agent_id": "openclaw"}})

    assert [request.url.path for request in seen] == ["/agents/openclaw", "/agents/openclaw/outcomes"]
    assert all(request.headers["authorization"] == f"Bearer {GATEWAY_TOKEN}" for request in seen)
