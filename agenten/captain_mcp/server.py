"""Read-only Captain MCP server over the sole-writer gateway.

Spike scope (plan 2026-11-07, Phase 4/5): prove that OpenClaw in a container
can call Captain tools. It exposes reads only and holds a registered *agent*
token, never the Captain token, so a compromised planner cannot write.
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
from starlette.types import ASGIApp, Receive, Scope, Send

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
_STATUSES = frozenset(get_args(BatchStatusFilter))
# Hosts the server answers for: local callers and OpenClaw inside Docker.
_ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*", "host.docker.internal:*"]


class CaptainMcpConfigurationError(RuntimeError):
    """The MCP server cannot start safely with the given environment."""


class CaptainGatewayReader:
    """Minimal authenticated reads against the Captain gateway."""

    def __init__(self, base_url: str, token: str, client: httpx.AsyncClient) -> None:
        if not base_url.strip() or not token:
            raise ValueError("gateway base_url and token are required")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = client

    async def list_batches(self, status: str) -> list[dict[str, Any]]:
        if status not in _STATUSES:
            raise ValueError(f"unknown batch status {status!r}")
        payload = await self._get("/batches", operation="list batches", params={"status": status})
        if not isinstance(payload, list):
            raise RuntimeError("list batches returned an invalid response")
        return payload

    async def get_batch(self, batch_id: str) -> dict[str, Any]:
        if not batch_id:
            raise ValueError("batch_id must not be empty")
        payload = await self._get(
            f"/batches/{quote(batch_id, safe='')}", operation="read batch"
        )
        if not isinstance(payload, dict):
            raise RuntimeError("read batch returned an invalid response")
        # A digest is not a secret, but the planner has no use for it.
        payload.pop("claim_token_sha256", None)
        return payload

    async def _get(self, path: str, *, operation: str, params: Mapping[str, str] | None = None) -> Any:
        try:
            response = await self._client.get(
                f"{self._base_url}{path}",
                params=params,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.HTTPError:
            raise RuntimeError(f"{operation} could not reach the gateway") from None
        if response.status_code != 200:
            # Bodies are not echoed: they may carry ledger content.
            raise RuntimeError(f"{operation} failed with gateway status {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise RuntimeError(f"{operation} returned an invalid response") from None


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


def create_mcp_app(reader: CaptainGatewayReader, *, inbound_token: str) -> ASGIApp:
    """Build the Streamable HTTP MCP app (served at `/mcp`)."""

    if not inbound_token:
        raise CaptainMcpConfigurationError("inbound MCP token must not be empty")
    server = FastMCP(
        "captain",
        instructions=(
            "Read-only view of Captain's ledger-backed work batches. "
            "Writes are not available on this server."
        ),
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=_ALLOWED_HOSTS,
            allowed_origins=[],
        ),
    )

    @server.tool()
    async def list_batches(status: BatchStatusFilter) -> list[dict[str, Any]]:
        """List work batches (batch_id, title) whose derived status matches."""

        return await reader.list_batches(status)

    @server.tool()
    async def get_batch(batch_id: str) -> dict[str, Any]:
        """Read one batch's derived lifecycle projection from the ledger."""

        return await reader.get_batch(batch_id)

    return _BearerGuard(server.streamable_http_app(), inbound_token)


@dataclass(frozen=True)
class McpSettings:
    gateway_url: str
    gateway_token: str
    inbound_token: str
    host: str = "127.0.0.1"
    port: int = 8121

    @classmethod
    def from_env(cls, environment: Mapping[str, str]) -> "McpSettings":
        gateway_url = _required(environment, "CAPTAIN_GATEWAY_URL")
        gateway_token = _required(environment, "CAPTAIN_MCP_GATEWAY_TOKEN")
        inbound_token = _required(environment, "CAPTAIN_MCP_TOKEN")
        if secrets.compare_digest(gateway_token, inbound_token):
            raise CaptainMcpConfigurationError(
                "CAPTAIN_MCP_TOKEN must differ from CAPTAIN_MCP_GATEWAY_TOKEN"
            )
        port_raw = environment.get("CAPTAIN_MCP_PORT", "8121").strip()
        try:
            port = int(port_raw)
        except ValueError:
            raise CaptainMcpConfigurationError("CAPTAIN_MCP_PORT must be an integer") from None
        return cls(
            gateway_url=gateway_url,
            gateway_token=gateway_token,
            inbound_token=inbound_token,
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
    app = create_mcp_app(reader, inbound_token=settings.inbound_token)
    logger.info("Captain MCP server listening on %s:%s/mcp", settings.host, settings.port)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
