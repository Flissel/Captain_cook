"""Captain MCP server over the sole-writer gateway.

Reads use a registered *agent* token. Captain write tools exist only when
the server is given its own Captain token (`CAPTAIN_MCP_CAPTAIN_TOKEN`);
without it the server stays read-only, as in the OpenClaw spike. The write
surface is a fixed list of dedicated gateway routes, never generic
`/blocks`, and every tool carries MCP safety annotations so the client can
apply its approval policy.

Batch approval is deliberately absent: it is the human review gate, and a
planner that could approve its own batches would make the gate void.
Widening an agent's permissions (`grant_agent_capability`) is marked
destructive so the client asks before it runs.
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_args
from urllib.parse import quote

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.types import ASGIApp, Receive, Scope, Send

from agenten.captain_mcp.plan_tools import (
    CaptainPlanner,
    plan_compiler_settings_from_env,
    register_plan_tools,
)
from agenten.planning.gateway_client import GatewayPlanningClient
from agenten.planning.supplied_plan import PlanCompilerSettings

logger = logging.getLogger(__name__)

BatchStatusFilter = Literal[
    "pending_review",
    "pending",
    "claimed",
    "succeeded",
    "failed",
    "rejected",
    "cancelled",
    "failed_after_max_iterations",
    "aborted_infra",
]
# Mirrors of the gateway vocabularies (kept in sync by tests; this package
# talks to the gateway over HTTP only and does not import it).
BatchCancelReason = Literal["captain_cancelled", "operator_cancelled", "policy_violation", "unroutable"]
RetireReason = Literal["captain_retired", "operator_retired", "policy_violation"]
GrantRevokeReason = Literal["captain_revoked", "operator_revoked", "policy_violation"]

_STATUSES = frozenset(get_args(BatchStatusFilter))
# Hosts the server answers for: local callers and OpenClaw inside Docker.
_ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*", "host.docker.internal:*"]
# Gateway error reasons are short fixed strings; anything longer or
# structured (validation echoes) may carry ledger content and is dropped.
_MAX_REASON_LENGTH = 200

_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
_DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
)


class CaptainMcpConfigurationError(RuntimeError):
    """The MCP server cannot start safely with the given environment."""


class _GatewayHttp:
    def __init__(self, base_url: str, token: str, client: httpx.AsyncClient) -> None:
        if not base_url.strip() or not token:
            raise ValueError("gateway base_url and token are required")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = client

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        ok: frozenset[int],
        params: Mapping[str, str] | None = None,
        json_body: Any = None,
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method,
                f"{self._base_url}{path}",
                params=params,
                json=json_body,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.HTTPError:
            raise RuntimeError(f"{operation} could not reach the gateway") from None
        if response.status_code not in ok:
            raise RuntimeError(_failure(operation, response))
        return response


def _failure(operation: str, response: httpx.Response) -> str:
    message = f"{operation} failed with gateway status {response.status_code}"
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return message
    if isinstance(detail, str) and 0 < len(detail) <= _MAX_REASON_LENGTH:
        return f"{message}: {detail}"
    return message


def _json(response: httpx.Response, operation: str) -> Any:
    try:
        return response.json()
    except ValueError:
        raise RuntimeError(f"{operation} returned an invalid response") from None


def _segment(value: str, name: str) -> str:
    if not value:
        raise ValueError(f"{name} must not be empty")
    return quote(value, safe="")


class CaptainGatewayReader(_GatewayHttp):
    """Minimal authenticated reads against the Captain gateway."""

    async def list_batches(self, status: str) -> list[dict[str, Any]]:
        if status not in _STATUSES:
            raise ValueError(f"unknown batch status {status!r}")
        payload = await self._get("/batches", operation="list batches", params={"status": status})
        if not isinstance(payload, list):
            raise RuntimeError("list batches returned an invalid response")
        return payload

    async def get_batch(self, batch_id: str) -> dict[str, Any]:
        payload = await self._get(f"/batches/{_segment(batch_id, 'batch_id')}", operation="read batch")
        if not isinstance(payload, dict):
            raise RuntimeError("read batch returned an invalid response")
        # A digest is not a secret, but the planner has no use for it.
        payload.pop("claim_token_sha256", None)
        return payload

    async def get_agent(self, agent_id: str) -> dict[str, Any]:
        payload = await self._get(f"/agents/{_segment(agent_id, 'agent_id')}", operation="read agent")
        if not isinstance(payload, dict):
            raise RuntimeError("read agent returned an invalid response")
        return payload

    async def get_agent_outcomes(self, agent_id: str) -> list[dict[str, Any]]:
        payload = await self._get(
            f"/agents/{_segment(agent_id, 'agent_id')}/outcomes", operation="read agent outcomes"
        )
        if not isinstance(payload, list):
            raise RuntimeError("read agent outcomes returned an invalid response")
        return payload

    async def _get(self, path: str, *, operation: str, params: Mapping[str, str] | None = None) -> Any:
        response = await self._request(
            "GET", path, operation=operation, ok=frozenset({200}), params=params
        )
        return _json(response, operation)


class CaptainGatewayWriter(_GatewayHttp):
    """Captain-authority writes, one dedicated gateway route per method.

    Every write is replay-safe at the gateway: 201 records a new block, 200
    returns the identical existing one (`replayed`).
    """

    async def register_agent(
        self, agent_id: str, version: str, owner: str, spec_sha256: str
    ) -> dict[str, Any]:
        return await self._post(
            "/agents",
            operation="register agent",
            body={"agent_id": agent_id, "version": version, "owner": owner, "spec_sha256": spec_sha256},
        )

    async def retire_agent(self, agent_id: str, reason: str) -> dict[str, Any]:
        return await self._post(
            f"/agents/{_segment(agent_id, 'agent_id')}/retire",
            operation="retire agent",
            body={"reason": reason},
        )

    async def grant_agent_capability(
        self, agent_id: str, grant_id: str, capability_tags: list[str], expires_at: str
    ) -> dict[str, Any]:
        return await self._post(
            f"/agents/{_segment(agent_id, 'agent_id')}/grants",
            operation="grant agent capability",
            body={"grant_id": grant_id, "capability_tags": capability_tags, "expires_at": expires_at},
        )

    async def revoke_agent_capability(
        self, agent_id: str, grant_id: str, reason: str
    ) -> dict[str, Any]:
        return await self._post(
            f"/agents/{_segment(agent_id, 'agent_id')}/grants/{_segment(grant_id, 'grant_id')}/revoke",
            operation="revoke agent capability",
            body={"reason": reason},
        )

    async def assign_batch(self, batch_id: str, agent_id: str) -> dict[str, Any]:
        return await self._post(
            f"/batches/{_segment(batch_id, 'batch_id')}/assign",
            operation="assign batch",
            body={"agent_id": agent_id},
        )

    async def cancel_batch(self, batch_id: str, reason: str) -> dict[str, Any]:
        return await self._post(
            f"/batches/{_segment(batch_id, 'batch_id')}/cancel",
            operation="cancel batch",
            body={"reason": reason},
        )

    async def _post(self, path: str, *, operation: str, body: Any) -> dict[str, Any]:
        response = await self._request(
            "POST", path, operation=operation, ok=frozenset({200, 201}), json_body=body
        )
        return {"replayed": response.status_code == 200, "record": _json(response, operation)}


class _BearerGuard:
    """Reject every request that lacks the inbound MCP bearer token."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._expected = f"Bearer {token}".encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            presented = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not secrets.compare_digest(presented, self._expected):
                await _unauthorized(send)
                return
        await self._app(scope, receive, send)


async def _unauthorized(send: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
        }
    )
    await send({"type": "http.response.body", "body": b'{"detail":"unauthorized"}'})


def create_mcp_app(
    reader: CaptainGatewayReader,
    *,
    inbound_token: str,
    writer: CaptainGatewayWriter | None = None,
    planner: CaptainPlanner | None = None,
) -> ASGIApp:
    """Build the Streamable HTTP MCP app (served at `/mcp`)."""

    if not inbound_token:
        raise CaptainMcpConfigurationError("inbound MCP token must not be empty")
    server = FastMCP(
        "captain",
        instructions=(
            "Captain's ledger-backed work batches and agents. "
            + (
                "Writes go through fixed Captain tools; there is no generic block write."
                if writer is not None
                else "Writes are not available on this server."
            )
        ),
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=_ALLOWED_HOSTS,
            allowed_origins=[],
        ),
    )
    _register_read_tools(server, reader)
    if writer is not None:
        _register_write_tools(server, writer)
    if planner is not None:
        register_plan_tools(server, planner)
    return _BearerGuard(server.streamable_http_app(), inbound_token)


def _register_read_tools(server: FastMCP, reader: CaptainGatewayReader) -> None:
    @server.tool(annotations=_READ)
    async def list_batches(status: BatchStatusFilter) -> list[dict[str, Any]]:
        """List work batches (batch_id, title) whose derived status matches."""

        return await reader.list_batches(status)

    @server.tool(annotations=_READ)
    async def get_batch(batch_id: str) -> dict[str, Any]:
        """Read one batch's derived lifecycle projection from the ledger."""

        return await reader.get_batch(batch_id)

    @server.tool(annotations=_READ)
    async def get_agent(agent_id: str) -> dict[str, Any]:
        """Read an agent's ledger lifecycle: version, status and capability grants."""

        return await reader.get_agent(agent_id)

    @server.tool(annotations=_READ)
    async def get_agent_outcomes(agent_id: str) -> list[dict[str, Any]]:
        """List every claim the agent held and how its batch ended under it."""

        return await reader.get_agent_outcomes(agent_id)


def _register_write_tools(server: FastMCP, writer: CaptainGatewayWriter) -> None:
    @server.tool(annotations=_WRITE)
    async def register_agent(
        agent_id: str, version: str, owner: str, spec_sha256: str
    ) -> dict[str, Any]:
        """Register a token-holding agent version (spec_sha256: 64 hex chars)."""

        return await writer.register_agent(agent_id, version, owner, spec_sha256)

    @server.tool(annotations=_DESTRUCTIVE)
    async def retire_agent(agent_id: str, reason: RetireReason) -> dict[str, Any]:
        """Retire an agent for good: its token loses every write route."""

        return await writer.retire_agent(agent_id, reason)

    @server.tool(annotations=_DESTRUCTIVE)
    async def grant_agent_capability(
        agent_id: str, grant_id: str, capability_tags: list[str], expires_at: str
    ) -> dict[str, Any]:
        """Grant capability tags until expires_at (ISO 8601 with timezone).

        A registered agent may claim a batch only while one active grant holds
        every capability tag of that batch.
        """

        return await writer.grant_agent_capability(agent_id, grant_id, capability_tags, expires_at)

    @server.tool(annotations=_DESTRUCTIVE)
    async def revoke_agent_capability(
        agent_id: str, grant_id: str, reason: GrantRevokeReason
    ) -> dict[str, Any]:
        """Revoke one capability grant of an agent."""

        return await writer.revoke_agent_capability(agent_id, grant_id, reason)

    @server.tool(annotations=_WRITE)
    async def assign_batch(batch_id: str, agent_id: str) -> dict[str, Any]:
        """Pin an unclaimed batch to one agent; only it may claim the batch."""

        return await writer.assign_batch(batch_id, agent_id)

    @server.tool(annotations=_DESTRUCTIVE)
    async def cancel_batch(batch_id: str, reason: BatchCancelReason) -> dict[str, Any]:
        """Terminally cancel a batch and revoke its claim.

        Use reason "unroutable" for work no registered agent can serve.
        """

        return await writer.cancel_batch(batch_id, reason)


@dataclass(frozen=True)
class McpSettings:
    gateway_url: str
    gateway_token: str
    inbound_token: str
    captain_token: str | None = None
    plan: PlanCompilerSettings | None = None
    host: str = "127.0.0.1"
    port: int = 18121

    @classmethod
    def from_env(cls, environment: Mapping[str, str]) -> "McpSettings":
        gateway_url = _required(environment, "CAPTAIN_GATEWAY_URL")
        gateway_token = _required(environment, "CAPTAIN_MCP_GATEWAY_TOKEN")
        inbound_token = _required(environment, "CAPTAIN_MCP_TOKEN")
        if secrets.compare_digest(gateway_token, inbound_token):
            raise CaptainMcpConfigurationError(
                "CAPTAIN_MCP_TOKEN must differ from CAPTAIN_MCP_GATEWAY_TOKEN"
            )
        captain_token = environment.get("CAPTAIN_MCP_CAPTAIN_TOKEN", "").strip() or None
        if captain_token is not None and (
            secrets.compare_digest(captain_token, gateway_token)
            or secrets.compare_digest(captain_token, inbound_token)
        ):
            raise CaptainMcpConfigurationError(
                "CAPTAIN_MCP_CAPTAIN_TOKEN must differ from the agent and inbound tokens"
            )
        port_raw = environment.get("CAPTAIN_MCP_PORT", "18121").strip()
        try:
            port = int(port_raw)
        except ValueError:
            raise CaptainMcpConfigurationError("CAPTAIN_MCP_PORT must be an integer") from None
        return cls(
            gateway_url=gateway_url,
            gateway_token=gateway_token,
            inbound_token=inbound_token,
            captain_token=captain_token,
            plan=plan_compiler_settings_from_env(environment),
            host=environment.get("CAPTAIN_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=port,
        )


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise CaptainMcpConfigurationError(f"{name} must be configured for the Captain MCP server")
    return value


def main() -> None:
    settings = McpSettings.from_env(os.environ)
    client = httpx.AsyncClient(timeout=30)
    reader = CaptainGatewayReader(settings.gateway_url, settings.gateway_token, client)
    writer = (
        CaptainGatewayWriter(settings.gateway_url, settings.captain_token, client)
        if settings.captain_token is not None
        else None
    )
    planner = (
        CaptainPlanner(
            settings.plan,
            capability_resolver=GatewayPlanningClient(
                settings.gateway_url, settings.gateway_token, client
            ),
            release_client=(
                GatewayPlanningClient(
                    settings.gateway_url, settings.captain_token, client, hold_for_review=True
                )
                if settings.captain_token is not None
                else None
            ),
        )
        if settings.plan is not None
        else None
    )
    app = create_mcp_app(
        reader, inbound_token=settings.inbound_token, writer=writer, planner=planner
    )
    logger.info(
        "Captain MCP server listening on %s:%s/mcp (%s)",
        settings.host,
        settings.port,
        "captain writes enabled" if writer is not None else "read-only",
    )
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
