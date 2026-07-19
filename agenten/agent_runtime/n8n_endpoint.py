"""Fail-closed n8n endpoint selection for Captain runtime work."""

from __future__ import annotations

import ipaddress
from datetime import datetime
from typing import Literal, Mapping
from urllib.parse import SplitResult, urlsplit

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from agenten.agent_runtime.capabilities import CapabilityDenied, validate_grant
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    CapabilityGrant,
    CapabilityGrantRevocation,
    CapabilityProfile,
)
from agenten.agent_runtime.n8n_mcp_broker import McpLeaseIssuer


class N8nEndpointConfigurationError(ValueError):
    """The selected n8n endpoint is incomplete or crosses ownership boundaries."""


class N8nEndpoint(BaseModel):
    """Immutable n8n connection data with a non-serializable credential."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["external", "captain-builder"]
    api_base_url: str = Field(min_length=1)
    webhook_base_url: str = Field(min_length=1)
    api_key: str = Field(min_length=1, exclude=True, repr=False)
    mcp_token: str = Field(default="", exclude=True, repr=False)
    mcp_broker_url: str = Field(default="", exclude=True)


class HermesN8nReference(BaseModel):
    """Serializable endpoint identity with private child-process credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint_identity: str = Field(min_length=1)
    server_name: Literal["n8n-mcp"] = "n8n-mcp"
    _child_environment: dict[str, str] = PrivateAttr(default_factory=dict)

    def child_process_environment(self) -> dict[str, str]:
        """Return a fresh credential mapping for one authorized child process."""

        return dict(self._child_environment)


def resolve_n8n_endpoint(environment: Mapping[str, str]) -> N8nEndpoint:
    """Select the explicitly configured n8n endpoint without mode fallback."""

    mode = environment.get("N8N_MODE", "")
    if mode not in {"external", "captain-builder"}:
        raise N8nEndpointConfigurationError(f"unsupported N8N_MODE: {mode!r}")

    if mode == "external":
        base_url = _required_value(environment, "N8N_URL")
        api_key = _required_value(environment, "N8N_MCP_TOKEN")
        normalized_url = _normalize_external_url(base_url)
        captain_url = environment.get("CAPTAIN_N8N_URL", "").strip()
        if captain_url and _endpoint_identity(normalized_url) == _endpoint_identity(
            captain_url
        ):
            raise N8nEndpointConfigurationError(
                "external N8N_URL must not equal CAPTAIN_N8N_URL"
            )
    else:
        base_url = _required_value(environment, "CAPTAIN_N8N_URL")
        api_key = _required_value(
            environment,
            "CAPTAIN_N8N_API_KEY",
            label="Captain n8n API key",
        )
        mcp_token = environment.get("CAPTAIN_N8N_MCP_TOKEN", "").strip()
        mcp_broker_url = environment.get("CAPTAIN_N8N_MCP_BROKER_URL", "").strip()
        normalized_url = _normalize_builder_url(base_url)
    if mode == "external":
        mcp_token = api_key
        mcp_broker_url = ""

    return N8nEndpoint(
        mode=mode,
        api_base_url=normalized_url,
        webhook_base_url=normalized_url,
        api_key=api_key,
        mcp_token=mcp_token,
        mcp_broker_url=(
            _normalize_builder_url(mcp_broker_url)
            if mcp_broker_url
            else ""
        ),
    )


def build_hermes_n8n_reference(
    grant: CapabilityGrant,
    command: AgentRuntimeCommand,
    endpoint: N8nEndpoint,
    now: datetime,
    revocation: CapabilityGrantRevocation | None = None,
    broker_issuer: McpLeaseIssuer | None = None,
) -> HermesN8nReference:
    """Build Hermes child configuration from an active exact n8n lease."""

    validate_grant(grant, command, now, revocation)
    if grant.profile is not CapabilityProfile.N8N_BUILDER:
        raise CapabilityDenied("Hermes n8n configuration requires an n8n-builder grant")
    if grant.mcp_servers != ("n8n-mcp",):
        raise CapabilityDenied(
            "Hermes n8n configuration requires exactly the n8n-mcp server"
        )
    _validate_builder_endpoint(endpoint)
    if endpoint.mcp_broker_url:
        if broker_issuer is None:
            raise N8nEndpointConfigurationError(
                "Captain n8n MCP broker requires a lease-token issuer"
            )
        token = broker_issuer.issue(grant, command, endpoint.mcp_broker_url, now)
        reference = HermesN8nReference(endpoint_identity=endpoint.mcp_broker_url)
        reference._child_environment.update(
            {"N8N_URL": endpoint.mcp_broker_url, "N8N_MCP_TOKEN": token}
        )
        return reference
    if not endpoint.mcp_token:
        raise N8nEndpointConfigurationError(
            "Captain n8n MCP token must not be empty for Hermes MCP access"
        )

    reference = HermesN8nReference(endpoint_identity=endpoint.api_base_url)
    reference._child_environment.update(
        {
            "N8N_URL": endpoint.api_base_url,
            "N8N_MCP_TOKEN": endpoint.mcp_token,
        }
    )
    return reference


def _required_value(
    environment: Mapping[str, str],
    name: str,
    *,
    label: str | None = None,
) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise N8nEndpointConfigurationError(f"{label or name} must not be empty")
    return value


def _validate_builder_endpoint(endpoint: N8nEndpoint) -> None:
    if endpoint.mode != "captain-builder":
        raise N8nEndpointConfigurationError(
            "Hermes n8n configuration requires a captain-builder endpoint"
        )
    api_base_url = _normalize_builder_url(endpoint.api_base_url)
    webhook_base_url = _normalize_builder_url(endpoint.webhook_base_url)
    if _endpoint_identity(api_base_url) != _endpoint_identity(webhook_base_url):
        raise N8nEndpointConfigurationError(
            "captain-builder API and webhook endpoints must have one identity"
        )


def _normalize_builder_url(value: str) -> str:
    parsed = _parse_url(value)
    try:
        port = parsed.port
    except ValueError:
        raise N8nEndpointConfigurationError(
            "CAPTAIN_N8N_URL must be a loopback HTTP URL with an explicit port"
        ) from None
    if port == 15678:
        raise N8nEndpointConfigurationError(
            "captain-builder must not target the VibeMind n8n port"
        )
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname is None
        or port is None
        or not _is_loopback_host(parsed.hostname)
    ):
        raise N8nEndpointConfigurationError(
            "CAPTAIN_N8N_URL must be a loopback HTTP URL with an explicit port"
        )
    return value.rstrip("/")


def _normalize_external_url(value: str) -> str:
    parsed = _parse_url(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise N8nEndpointConfigurationError(
            "N8N_URL must be an HTTP URL without userinfo"
        )
    return value.rstrip("/")


def _parse_url(value: str) -> SplitResult:
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise N8nEndpointConfigurationError(
            "selected n8n URL must be a valid HTTP URL without userinfo"
        ) from None
    if parsed.query or parsed.fragment:
        raise N8nEndpointConfigurationError(
            "selected n8n URL must not contain a query or fragment"
        )
    if _has_dot_segment(parsed.path):
        raise N8nEndpointConfigurationError(
            "selected n8n URL must not contain RFC dot segments"
        )
    return parsed


def _endpoint_identity(value: str) -> tuple[str, str, int | None, str] | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or _has_dot_segment(parsed.path)
    ):
        return None
    if port is None:
        port = 80 if parsed.scheme == "http" else 443
    host = (
        "loopback"
        if _is_loopback_host(parsed.hostname)
        else parsed.hostname.lower()
    )
    return (
        parsed.scheme.lower(),
        host,
        port,
        parsed.path.rstrip("/"),
    )


def _is_loopback_host(host: str) -> bool:
    normalized_host = host.lower().removesuffix(".")
    if normalized_host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _has_dot_segment(path: str) -> bool:
    return any(segment in {".", ".."} for segment in path.split("/"))
