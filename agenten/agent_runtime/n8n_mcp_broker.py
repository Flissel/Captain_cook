"""Lease-scoped credentials for Captain's isolated n8n MCP broker."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agenten.agent_runtime.capabilities import CapabilityDenied, validate_grant
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    CapabilityGrant,
    CapabilityGrantRevocation,
    CapabilityProfile,
)


class McpLeaseDenied(RuntimeError):
    """The caller cannot use the Captain n8n MCP broker."""


class McpLeaseClaim(BaseModel):
    """Minimal, signed broker claim; it deliberately contains no n8n credential."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_name: str = Field(
        alias="schema",
        serialization_alias="schema",
        pattern=r"^captain\.n8n-mcp-lease\.v1$",
    )
    command_id: UUID
    grant_id: str = Field(min_length=1)
    endpoint_identity: str = Field(min_length=1)
    issued_at: datetime
    expires_at: datetime

    @field_validator("issued_at", "expires_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("lease timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)


class CapabilityGrantRevocationReader(Protocol):
    async def get_grant_revocation(
        self, command_id: UUID
    ) -> CapabilityGrantRevocation | None: ...


class McpLeaseIssuer:
    """Issue and verify deterministic HMAC-protected MCP broker leases."""

    def __init__(self, signing_secret: str) -> None:
        if len(signing_secret) < 16:
            raise ValueError("MCP broker signing secret must contain at least 16 characters")
        self._key = signing_secret.encode("utf-8")

    def issue(
        self,
        grant: CapabilityGrant,
        command: AgentRuntimeCommand,
        endpoint_identity: str,
        now: datetime,
    ) -> str:
        if not endpoint_identity:
            raise ValueError("endpoint_identity must not be empty")
        try:
            validate_grant(grant, command, now)
        except CapabilityDenied as exc:
            raise McpLeaseDenied(str(exc)) from exc
        if grant.profile is not CapabilityProfile.N8N_BUILDER:
            raise McpLeaseDenied("MCP broker requires an n8n-builder capability grant")
        if grant.mcp_servers != ("n8n-mcp",):
            raise McpLeaseDenied("MCP broker requires exactly the n8n-mcp server")
        claim = McpLeaseClaim(
            schema_name="captain.n8n-mcp-lease.v1",
            command_id=command.event_id,
            grant_id=grant.grant_id,
            endpoint_identity=endpoint_identity,
            issued_at=now,
            expires_at=grant.expires_at,
        )
        payload = _canonical_claim(claim)
        signature = hmac.new(self._key, payload, hashlib.sha256).digest()
        return f"{_b64url(payload)}.{_b64url(signature)}"

    def verify(self, token: str, now: datetime) -> McpLeaseClaim:
        try:
            encoded_payload, encoded_signature = token.split(".", maxsplit=1)
            payload = _unb64url(encoded_payload)
            supplied_signature = _unb64url(encoded_signature)
        except (ValueError, UnicodeEncodeError):
            raise McpLeaseDenied("MCP lease token is malformed") from None
        expected_signature = hmac.new(self._key, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise McpLeaseDenied("MCP lease token signature is invalid")
        try:
            claim = McpLeaseClaim.model_validate_json(payload)
        except (ValidationError, ValueError):
            raise McpLeaseDenied("MCP lease token payload is invalid") from None
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if now.astimezone(timezone.utc) >= claim.expires_at:
            raise McpLeaseDenied("MCP lease token is expired")
        return claim


class McpLeaseRevocationAuthorizer:
    """Authorize each broker call against Captain's durable revocation record."""

    def __init__(
        self,
        issuer: McpLeaseIssuer,
        revocations: CapabilityGrantRevocationReader,
    ) -> None:
        self._issuer = issuer
        self._revocations = revocations

    async def authorize(self, token: str, now: datetime) -> McpLeaseClaim:
        claim = self._issuer.verify(token, now)
        revocation = await self._revocations.get_grant_revocation(claim.command_id)
        if revocation is not None:
            if (
                revocation.command_id != claim.command_id
                or revocation.grant_id != claim.grant_id
            ):
                raise McpLeaseDenied("MCP lease revocation does not match its claim")
            raise McpLeaseDenied("MCP lease has been revoked by Captain")
        return claim


class McpLeaseAuthorizer(Protocol):
    async def authorize(self, token: str, now: datetime) -> McpLeaseClaim: ...


def create_mcp_broker_app(
    *,
    authorizer: McpLeaseAuthorizer,
    expected_endpoint_identity: str,
    upstream_url: str,
    upstream_token: str,
    client: httpx.AsyncClient,
    clock: Callable[[], datetime],
) -> FastAPI:
    """Create the narrow, Captain-only reverse proxy for n8n MCP HTTP calls."""

    if not expected_endpoint_identity:
        raise ValueError("expected_endpoint_identity must not be empty")
    if not upstream_url.startswith("http"):
        raise ValueError("upstream_url must be an HTTP URL")
    if not upstream_token:
        raise ValueError("upstream_token must not be empty")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/mcp-server/http")
    async def proxy_mcp(request: Request) -> Response:
        try:
            token = _bearer_token(request.headers.get("authorization"))
            claim = await authorizer.authorize(token, clock())
        except McpLeaseDenied:
            raise HTTPException(status_code=403, detail="Captain MCP lease denied") from None
        if claim.endpoint_identity != expected_endpoint_identity:
            raise HTTPException(status_code=403, detail="Captain MCP lease denied")
        try:
            upstream = await client.post(
                upstream_url,
                content=await request.body(),
                headers={
                    "Authorization": f"Bearer {upstream_token}",
                    "Content-Type": request.headers.get(
                        "content-type", "application/json"
                    ),
                    "Accept": request.headers.get("accept", "application/json"),
                },
            )
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="Captain n8n MCP unavailable") from None
        safe_headers = {
            name: value
            for name, value in upstream.headers.items()
            if name.lower() in {"content-type", "cache-control"}
        }
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=safe_headers,
        )

    return app


def _canonical_claim(claim: McpLeaseClaim) -> bytes:
    return json.dumps(
        claim.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64url(value: str) -> bytes:
    if not value or any(character.isspace() for character in value):
        raise ValueError("invalid base64url")
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}".encode("ascii"))


def _bearer_token(value: str | None) -> str:
    if value is None:
        raise McpLeaseDenied("MCP lease token is missing")
    scheme, separator, token = value.partition(" ")
    if scheme.lower() != "bearer" or not separator or not token:
        raise McpLeaseDenied("MCP lease token is malformed")
    return token
