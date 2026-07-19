"""Process entrypoint for the isolated Captain n8n MCP lease broker."""

from __future__ import annotations

import os
from collections.abc import Mapping

import httpx
import uvicorn
from fastapi import FastAPI

from agenten.agent_runtime.gateway_client import GatewayRuntimeClient
from agenten.agent_runtime.n8n_mcp_broker import (
    McpLeaseIssuer,
    McpLeaseRevocationAuthorizer,
    create_mcp_broker_app,
)


def create_app_from_environment(environment: Mapping[str, str]) -> FastAPI:
    """Build a broker that knows only its upstream and Captain read authority."""

    broker_url = _required(environment, "CAPTAIN_N8N_MCP_BROKER_URL")
    upstream_url = _required(environment, "CAPTAIN_MCP_BROKER_UPSTREAM_URL")
    upstream_token = _required(environment, "CAPTAIN_N8N_MCP_TOKEN")
    signing_secret = _required(environment, "CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET")
    gateway_url = _required(environment, "CAPTAIN_GATEWAY_URL")
    gateway_token = _required(environment, "CAPTAIN_GATEWAY_TOKEN")
    client = httpx.AsyncClient(timeout=30)
    gateway = GatewayRuntimeClient(gateway_url, gateway_token, client)
    app = create_mcp_broker_app(
        authorizer=McpLeaseRevocationAuthorizer(
            McpLeaseIssuer(signing_secret), gateway
        ),
        expected_endpoint_identity=broker_url.rstrip("/"),
        upstream_url=upstream_url,
        upstream_token=upstream_token,
        client=client,
        clock=_utc_now,
    )

    @app.on_event("shutdown")
    async def close_http_client() -> None:
        await client.aclose()

    return app


def main() -> None:
    app = create_app_from_environment(os.environ)
    port = int(os.environ.get("CAPTAIN_N8N_MCP_BROKER_PORT", "5680"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be configured for the Captain MCP broker")
    return value


def _utc_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


if __name__ == "__main__":
    main()
