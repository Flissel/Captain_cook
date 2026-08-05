"""Read-only n8n credential metadata discovery for Captain integration setup."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone

import httpx
from pydantic import ValidationError

from agenten.agent_factory.contracts import FactoryLease, FactoryRole
from agenten.agent_factory.integration_setup import (
    IntegrationCredentialRequirementV1,
    N8nCredentialMetadataV1,
)
from agenten.agent_runtime.contracts import IntegrationIntent
from agenten.agent_runtime.n8n_endpoint import (
    N8nEndpoint,
    N8nEndpointConfigurationError,
    _validate_builder_endpoint,
)


_MAX_RESPONSE_BYTES = 512 * 1024


class N8nCredentialDiscoveryError(RuntimeError):
    """n8n metadata discovery failed without retaining provider diagnostics."""


class CaptainN8nCredentialMetadataClient:
    """Call only n8n's secret-free ``list_credentials`` MCP operation."""

    def __init__(self, *, http: httpx.AsyncClient) -> None:
        self._http = http

    async def discover(
        self,
        *,
        lease: FactoryLease,
        endpoint: N8nEndpoint,
        requirement: IntegrationCredentialRequirementV1,
        now: datetime,
        timeout_seconds: float,
    ) -> tuple[N8nCredentialMetadataV1, ...]:
        _require_active_n8n_lease(lease, now)
        try:
            _validate_builder_endpoint(endpoint)
        except N8nEndpointConfigurationError:
            raise N8nCredentialDiscoveryError(
                "Captain n8n credential metadata endpoint is not authorized"
            ) from None
        token = endpoint.mcp_token.strip()
        if not token:
            raise N8nCredentialDiscoveryError(
                "Captain n8n credential metadata access is unavailable"
            )
        if timeout_seconds <= 0:
            raise ValueError("credential discovery timeout must be positive")

        call_id = _call_id(lease, requirement)
        arguments: dict[str, object] = {
            "limit": 200,
            "type": requirement.credential_type,
        }
        if requirement.project_id is not None:
            arguments["projectId"] = requirement.project_id
        payload = {
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {
                "name": "list_credentials",
                "arguments": arguments,
            },
        }
        try:
            async with asyncio.timeout(timeout_seconds):
                async with self._http.stream(
                    "POST",
                    f"{endpoint.api_base_url.rstrip('/')}/mcp-server/http",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=httpx.Timeout(timeout_seconds),
                ) as response:
                    if response.status_code < 200 or response.status_code >= 300:
                        raise N8nCredentialDiscoveryError(
                            "Captain n8n credential metadata request failed"
                        )
                    value = await _structured_result(response, call_id)
        except (TimeoutError, httpx.TimeoutException, httpx.RequestError):
            raise N8nCredentialDiscoveryError(
                "Captain n8n credential metadata request failed"
            ) from None
        data = value.get("data")
        if not isinstance(data, list):
            raise N8nCredentialDiscoveryError(
                "Captain n8n credential metadata response is invalid"
            )
        try:
            result = tuple(
                metadata
                for item in data
                if (
                    metadata := _metadata(item)
                ).credential_type == requirement.credential_type
                and (
                    requirement.project_id is None
                    or metadata.project_id == requirement.project_id
                )
            )
        except (TypeError, ValidationError, ValueError):
            raise N8nCredentialDiscoveryError(
                "Captain n8n credential metadata response is invalid"
            ) from None
        ids = tuple(item.credential_id for item in result)
        if len(ids) != len(set(ids)):
            raise N8nCredentialDiscoveryError(
                "Captain n8n credential metadata response is invalid"
            )
        return tuple(sorted(result, key=lambda item: item.credential_id))


def _require_active_n8n_lease(lease: FactoryLease, now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    normalized_now = now.astimezone(timezone.utc)
    if (
        lease.role is not FactoryRole.TOOL_INTEGRATOR
        or lease.integration_intent is not IntegrationIntent.N8N
        or "mcp.n8n" not in lease.capabilities
        or not (lease.issued_at <= normalized_now < lease.expires_at)
    ):
        raise PermissionError("credential discovery requires an active Captain n8n lease")


def _call_id(
    lease: FactoryLease,
    requirement: IntegrationCredentialRequirementV1,
) -> str:
    digest = hashlib.sha256(
        "|".join(
            (
                lease.lease_id,
                str(lease.job_id),
                str(lease.correlation_id),
                requirement.integration_key,
                requirement.credential_alias,
                requirement.credential_type,
                requirement.project_id or "",
            )
        ).encode("utf-8")
    ).hexdigest()
    return f"captain-credential-list-{digest[:48]}"


async def _structured_result(
    response: httpx.Response,
    call_id: str,
) -> dict[str, object]:
    content_type = response.headers.get("content-type", "").lower()
    if "text/event-stream" in content_type:
        return await _streamed_sse_result(response, call_id)
    content = await response.aread()
    if not content or len(content) > _MAX_RESPONSE_BYTES:
        raise N8nCredentialDiscoveryError(
            "Captain n8n credential metadata response is invalid"
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise N8nCredentialDiscoveryError(
            "Captain n8n credential metadata response is invalid"
        ) from None
    envelopes: list[object] = []
    try:
        envelopes.append(json.loads(text))
    except json.JSONDecodeError:
        envelopes.extend(_sse_values(text))
    envelope_matches = tuple(
        item
        for item in envelopes
        if isinstance(item, dict) and item.get("id") == call_id
    )
    if len(envelope_matches) != 1:
        raise N8nCredentialDiscoveryError(
            "Captain n8n credential metadata response is invalid"
        )
    return _structured_envelope(envelope_matches[0], call_id)


def _structured_envelope(
    envelope: dict[str, object],
    call_id: str,
) -> dict[str, object]:
    if envelope.get("id") != call_id:
        raise N8nCredentialDiscoveryError(
            "Captain n8n credential metadata response is invalid"
        )
    if envelope.get("jsonrpc") != "2.0" or "error" in envelope:
        raise N8nCredentialDiscoveryError(
            "Captain n8n credential metadata response is invalid"
        )
    result = envelope.get("result")
    if (
        not isinstance(result, dict)
        or result.get("isError") is True
        or result.get("is_error") is True
    ):
        raise N8nCredentialDiscoveryError(
            "Captain n8n credential metadata response is invalid"
        )
    structured = result.get(
        "structuredContent", result.get("structured_content")
    )
    for _ in range(2):
        if not isinstance(structured, str):
            break
        try:
            structured = json.loads(structured)
        except json.JSONDecodeError:
            break
    if not isinstance(structured, dict):
        raise N8nCredentialDiscoveryError(
            "Captain n8n credential metadata response is invalid"
        )
    return structured


async def _streamed_sse_result(
    response: httpx.Response,
    call_id: str,
) -> dict[str, object]:
    data_lines: list[str] = []
    consumed = 0
    async for line in response.aiter_lines():
        consumed += len(line.encode("utf-8")) + 1
        if consumed > _MAX_RESPONSE_BYTES:
            raise N8nCredentialDiscoveryError(
                "Captain n8n credential metadata response is invalid"
            )
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
        if line or not data_lines:
            continue
        try:
            envelope = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            data_lines = []
            continue
        data_lines = []
        if isinstance(envelope, dict) and envelope.get("id") == call_id:
            return _structured_envelope(envelope, call_id)
    if data_lines:
        try:
            envelope = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            envelope = None
        if isinstance(envelope, dict) and envelope.get("id") == call_id:
            return _structured_envelope(envelope, call_id)
    raise N8nCredentialDiscoveryError(
        "Captain n8n credential metadata response is invalid"
    )


def _sse_values(text: str) -> tuple[object, ...]:
    values: list[object] = []
    data_lines: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if not line:
            if data_lines:
                try:
                    values.append(json.loads("\n".join(data_lines)))
                except json.JSONDecodeError:
                    pass
                data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        try:
            values.append(json.loads("\n".join(data_lines)))
        except json.JSONDecodeError:
            pass
    if not values:
        raise N8nCredentialDiscoveryError(
            "Captain n8n credential metadata response is invalid"
        )
    return tuple(values)


def _metadata(value: object) -> N8nCredentialMetadataV1:
    if not isinstance(value, dict):
        raise ValueError("credential metadata item must be an object")
    home_project = value.get("homeProject")
    if home_project is not None and not isinstance(home_project, dict):
        raise ValueError("credential home project must be an object")
    project_id = None if home_project is None else home_project.get("id")
    project_name = None if home_project is None else home_project.get("name")
    return N8nCredentialMetadataV1(
        credential_id=value.get("id"),
        credential_name=value.get("name"),
        credential_type=value.get("type"),
        project_id=project_id,
        project_name=project_name,
    )
