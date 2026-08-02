"""Native Captain n8n MCP transport for the renewal benchmark tool.

Only an already deployed, content-bound workflow can be executed.  This
transport deliberately has no workflow discovery, creation, update, publish,
or archive operation.  The first ``execute_workflow`` dispatch is never
repeated when its outcome is unknown; subsequent ``get_execution`` calls are
read-only evidence polling.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

import httpx
from pydantic import ValidationError

from agenten.agent_factory.business_benchmark_n8n import (
    RenewalContextN8nProviderRequestV1,
    RenewalContextN8nProviderResponseV1,
    RenewalContextReadOutputV1,
    _require_captain_endpoint,
)
from agenten.agent_runtime.contracts import (
    AgentRuntimeResult,
    ArtifactRef,
    RuntimeOperation,
    RuntimeStatus,
)
from agenten.agent_runtime.n8n_endpoint import N8nEndpoint
from agenten.targets.n8n import N8nExecutionEvidence


_MAX_MCP_RESPONSE_BYTES = 2 * 1024 * 1024
_SUCCESS_STATUSES = frozenset({"success", "succeeded", "completed"})
_PENDING_STATUSES = frozenset(
    {"new", "created", "queued", "pending", "running", "waiting"}
)
_FAILED_STATUSES = frozenset(
    {"failed", "error", "cancelled", "canceled", "crashed"}
)
_CAPTAIN_N8N_ALLOWLIST = frozenset(
    {
        "http://localhost:5679",
        "http://127.0.0.1:5679",
        "http://[::1]:5679",
    }
)


class RenewalContextMcpProviderError(RuntimeError):
    """The Captain MCP provider failed without exposing provider diagnostics."""


class RenewalContextMcpDispatchUnknownError(RenewalContextMcpProviderError):
    """The execute dispatch may have reached n8n and must not be repeated."""


class CaptainNativeMcpRenewalContextTransport:
    """Execute one exact renewal workflow through Captain's native MCP endpoint."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        workflow_id: str,
        workflow_ref: ArtifactRef,
        clock: Callable[[], datetime],
        broker_token_issuer: Callable[
            [RenewalContextN8nProviderRequestV1], str
        ]
        | None = None,
        poll_attempts: int = 8,
        poll_delay_seconds: float = 0.1,
    ) -> None:
        normalized_workflow_id = workflow_id.strip()
        if (
            not normalized_workflow_id
            or len(normalized_workflow_id) > 200
            or workflow_ref.media_type != "application/json"
        ):
            raise ValueError("renewal MCP workflow binding is invalid")
        if poll_attempts < 1 or poll_attempts > 50 or poll_delay_seconds < 0:
            raise ValueError("renewal MCP evidence polling configuration is invalid")
        self._client = client
        self._workflow_id = normalized_workflow_id
        self._workflow_ref = workflow_ref
        self._clock = clock
        self._broker_token_issuer = broker_token_issuer
        self._poll_attempts = poll_attempts
        self._poll_delay_seconds = poll_delay_seconds

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(workflow_id={self._workflow_id!r}, "
            f"workflow_ref={self._workflow_ref!r})"
        )

    async def execute(
        self,
        *,
        endpoint: N8nEndpoint,
        request: RenewalContextN8nProviderRequestV1,
        timeout_seconds: float,
    ) -> RenewalContextN8nProviderResponseV1:
        if timeout_seconds <= 0:
            raise ValueError("renewal MCP timeout must be positive")
        mcp_url, token = _captain_mcp_connection(
            endpoint,
            request=request,
            broker_token_issuer=self._broker_token_issuer,
        )
        execute_call_id = _call_id("execute", request, self._workflow_id)
        arguments = {
            "workflowId": self._workflow_id,
            "executionMode": "production",
            "inputs": {
                "type": "webhook",
                "webhookData": {
                    "method": "POST",
                    "query": {},
                    "body": _webhook_body(request, self._workflow_ref),
                    "headers": {},
                },
            },
        }
        execute_value = await self._call_tool(
            url=mcp_url,
            token=token,
            call_id=execute_call_id,
            tool_name="execute_workflow",
            arguments=arguments,
            timeout_seconds=timeout_seconds,
            dispatch_is_effectful=True,
        )
        execution_id = _required_execution_id(execute_value)
        _require_execution_identity(
            execute_value,
            workflow_id=self._workflow_id,
            execution_id=execution_id,
            allow_missing_workflow=True,
        )

        terminal = _terminal_output(
            execute_value,
            request=request,
            workflow_id=self._workflow_id,
            workflow_ref=self._workflow_ref,
            execution_id=execution_id,
            require_all_evidence=False,
        )
        if terminal is None:
            terminal = await self._poll_execution(
                url=mcp_url,
                token=token,
                request=request,
                execution_id=execution_id,
                timeout_seconds=timeout_seconds,
            )

        now = _utc(self._clock())
        runtime_result = AgentRuntimeResult(
            schema="captain.agent-runtime-result.v1",
            event_id=uuid5(
                NAMESPACE_URL,
                "|".join(
                    (
                        "captain.renewal-context-n8n-result.v1",
                        str(request.runtime_command_id),
                        request.grant_id,
                        execution_id,
                        self._workflow_ref.sha256,
                    )
                ),
            ),
            command_id=request.runtime_command_id,
            correlation_id=request.correlation_id,
            occurred_at=now,
            producer="agent-runtime",
            subject_id=request.tool.tool_name,
            subject_version=request.subject_version,
            grant_id=request.grant_id,
            operation=RuntimeOperation.CODEX_RUN,
            status=RuntimeStatus.SUCCEEDED,
            artifact_refs=(self._workflow_ref,),
        )
        return RenewalContextN8nProviderResponseV1(
            request=request,
            effect="read_only",
            mcp_call_id=execute_call_id,
            workflow_ref=self._workflow_ref,
            execution=N8nExecutionEvidence(
                execution_id=execution_id,
                workflow_id=self._workflow_id,
                artifact_digest=self._workflow_ref.sha256,
                correlation_id=str(request.correlation_id),
                status="success",
            ),
            runtime_result=runtime_result,
            output=terminal,
        )

    async def _poll_execution(
        self,
        *,
        url: str,
        token: str,
        request: RenewalContextN8nProviderRequestV1,
        execution_id: str,
        timeout_seconds: float,
    ) -> RenewalContextReadOutputV1:
        for attempt in range(1, self._poll_attempts + 1):
            value = await self._call_tool(
                url=url,
                token=token,
                call_id=f"{_call_id('evidence', request, self._workflow_id)}-{attempt}",
                tool_name="get_execution",
                arguments={
                    "workflowId": self._workflow_id,
                    "executionId": execution_id,
                    "includeData": True,
                },
                timeout_seconds=timeout_seconds,
                dispatch_is_effectful=False,
            )
            _require_execution_identity(
                value,
                workflow_id=self._workflow_id,
                execution_id=execution_id,
                allow_missing_workflow=False,
            )
            output = _terminal_output(
                value,
                request=request,
                workflow_id=self._workflow_id,
                workflow_ref=self._workflow_ref,
                execution_id=execution_id,
                require_all_evidence=True,
            )
            if output is not None:
                return output
            if attempt < self._poll_attempts and self._poll_delay_seconds:
                await asyncio.sleep(self._poll_delay_seconds)
        raise RenewalContextMcpProviderError(
            "Captain n8n execution evidence timed out"
        )

    async def _call_tool(
        self,
        *,
        url: str,
        token: str,
        call_id: str,
        tool_name: str,
        arguments: dict[str, object],
        timeout_seconds: float,
        dispatch_is_effectful: bool,
    ) -> object:
        payload = {
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        internal_timeout = max(timeout_seconds * 0.8, 0.001)
        try:
            async with asyncio.timeout(internal_timeout):
                response = await self._client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=httpx.Timeout(internal_timeout),
                )
        except (TimeoutError, httpx.TimeoutException, httpx.RequestError):
            if dispatch_is_effectful:
                raise RenewalContextMcpDispatchUnknownError(
                    "Captain n8n execute dispatch state is unknown"
                ) from None
            raise RenewalContextMcpProviderError(
                "Captain n8n execution evidence request failed"
            ) from None
        if response.status_code < 200 or response.status_code >= 300:
            if dispatch_is_effectful:
                raise RenewalContextMcpDispatchUnknownError(
                    "Captain n8n execute dispatch was not acknowledged"
                )
            raise RenewalContextMcpProviderError(
                "Captain n8n execution evidence request failed"
            )
        envelope = _parse_mcp_envelope(response, call_id)
        if "error" in envelope:
            raise RenewalContextMcpProviderError("Captain n8n MCP provider error")
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise RenewalContextMcpProviderError("Captain n8n MCP result is invalid")
        if result.get("isError") is True or result.get("is_error") is True:
            raise RenewalContextMcpProviderError("Captain n8n MCP tool failed")
        structured = result.get("structuredContent", result.get("structured_content"))
        if structured is not None:
            return _decode_nested_json(structured)
        content = result.get("content")
        if isinstance(content, list):
            texts = [
                item.get("text")
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            if texts:
                return _decode_nested_json("\n".join(texts))
        raise RenewalContextMcpProviderError(
            "Captain n8n MCP result contained no structured output"
        )


def _captain_mcp_connection(
    endpoint: N8nEndpoint,
    *,
    request: RenewalContextN8nProviderRequestV1,
    broker_token_issuer: Callable[[RenewalContextN8nProviderRequestV1], str]
    | None,
) -> tuple[str, str]:
    try:
        _require_captain_endpoint(endpoint, _CAPTAIN_N8N_ALLOWLIST)
    except ValueError:
        raise RenewalContextMcpProviderError(
            "Captain n8n MCP endpoint is not authorized"
        ) from None
    if not endpoint.mcp_token:
        raise RenewalContextMcpProviderError(
            "Captain n8n MCP credential is unavailable"
        )
    selected = endpoint.api_base_url
    token = endpoint.mcp_token
    if broker_token_issuer is not None:
        if not endpoint.mcp_broker_url:
            raise RenewalContextMcpProviderError(
                "Captain n8n MCP broker endpoint is unavailable"
            )
        try:
            token = broker_token_issuer(request).strip()
        except Exception:
            raise RenewalContextMcpProviderError(
                "Captain n8n MCP broker credential issuance failed"
            ) from None
        if not token or token == endpoint.mcp_token:
            raise RenewalContextMcpProviderError(
                "Captain n8n MCP broker credential is invalid"
            )
        selected = endpoint.mcp_broker_url
    parsed = urlsplit(selected)
    expected_port = 5680 if broker_token_issuer is not None else 5679
    if parsed.port != expected_port:
        raise RenewalContextMcpProviderError(
            "Captain n8n MCP endpoint is not authorized"
        )
    return f"{selected.rstrip('/')}/mcp-server/http", token


def _webhook_body(
    request: RenewalContextN8nProviderRequestV1,
    workflow_ref: ArtifactRef,
) -> dict[str, object]:
    del workflow_ref
    payload = request.input_payload.model_dump(mode="json")
    return payload


def _call_id(
    purpose: str,
    request: RenewalContextN8nProviderRequestV1,
    workflow_id: str,
) -> str:
    digest = hashlib.sha256(
        "|".join(
            (
                purpose,
                str(request.request_id),
                str(request.runtime_command_id),
                request.grant_id,
                request.input_payload.idempotency_key,
                workflow_id,
            )
        ).encode("utf-8")
    ).hexdigest()
    return f"captain-renewal-{purpose}-{digest[:48]}"


def _parse_mcp_envelope(response: httpx.Response, call_id: str) -> dict[str, object]:
    content = response.content
    if not content or len(content) > _MAX_MCP_RESPONSE_BYTES:
        raise RenewalContextMcpProviderError("Captain n8n MCP response is invalid")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise RenewalContextMcpProviderError("Captain n8n MCP response is invalid") from None
    candidates: list[object] = []
    try:
        candidates.append(json.loads(text))
    except json.JSONDecodeError:
        candidates.extend(_parse_sse_json(text))
    matches = [
        item
        for item in candidates
        if isinstance(item, dict) and item.get("id") == call_id
    ]
    if len(matches) != 1:
        raise RenewalContextMcpProviderError(
            "Captain n8n MCP JSON-RPC response ID did not match"
        )
    envelope = matches[0]
    if envelope.get("jsonrpc") != "2.0":
        raise RenewalContextMcpProviderError(
            "Captain n8n MCP JSON-RPC response is invalid"
        )
    return envelope


def _parse_sse_json(value: str) -> list[object]:
    parsed: list[object] = []
    data_lines: list[str] = []
    for line in value.replace("\r\n", "\n").split("\n"):
        if not line:
            if data_lines:
                try:
                    parsed.append(json.loads("\n".join(data_lines)))
                except json.JSONDecodeError:
                    pass
                data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        try:
            parsed.append(json.loads("\n".join(data_lines)))
        except json.JSONDecodeError:
            pass
    if not parsed:
        raise RenewalContextMcpProviderError("Captain n8n MCP response is invalid")
    return parsed


def _decode_nested_json(value: object) -> object:
    current = value
    for _ in range(2):
        if not isinstance(current, str):
            break
        try:
            current = json.loads(current)
        except json.JSONDecodeError:
            break
    if not isinstance(current, (dict, list)):
        raise RenewalContextMcpProviderError(
            "Captain n8n MCP structured output is invalid"
        )
    return current


def _required_execution_id(value: object) -> str:
    identifiers = _execution_ids(value)
    if len(identifiers) != 1:
        raise RenewalContextMcpProviderError(
            "Captain n8n execution ID is missing or ambiguous"
        )
    return next(iter(identifiers))


def _require_execution_identity(
    value: object,
    *,
    workflow_id: str,
    execution_id: str,
    allow_missing_workflow: bool,
) -> None:
    workflow_ids = _unique_strings(value, ("workflowId", "workflow_id"))
    execution_ids = _execution_ids(value)
    if (
        (workflow_ids and workflow_ids != {workflow_id})
        or (not workflow_ids and not allow_missing_workflow)
        or execution_ids != {execution_id}
    ):
        raise RenewalContextMcpProviderError(
            "Captain n8n workflow or execution binding did not match"
        )


def _terminal_output(
    value: object,
    *,
    request: RenewalContextN8nProviderRequestV1,
    workflow_id: str,
    workflow_ref: ArtifactRef,
    execution_id: str,
    require_all_evidence: bool,
) -> RenewalContextReadOutputV1 | None:
    del workflow_id, execution_id
    statuses = _execution_statuses(value)
    if statuses.intersection(_FAILED_STATUSES):
        raise RenewalContextMcpProviderError("Captain n8n execution failed")
    if statuses and not statuses.issubset(_SUCCESS_STATUSES | _PENDING_STATUSES):
        raise RenewalContextMcpProviderError("Captain n8n execution status is invalid")
    if not statuses or not statuses.issubset(_SUCCESS_STATUSES):
        return None

    correlations = _unique_strings(value, ("correlation_id", "correlationId"))
    digests = _unique_strings(value, ("artifact_digest", "artifactDigest"))
    if correlations and correlations != {str(request.correlation_id)}:
        raise RenewalContextMcpProviderError(
            "Captain n8n execution correlation did not match"
        )
    if digests and digests != {workflow_ref.sha256}:
        raise RenewalContextMcpProviderError(
            "Captain n8n workflow artifact digest did not match"
        )
    bindings = (
        (("job_id", "jobId"), str(request.job_id), "job"),
        (("invocation_id", "invocationId"), str(request.invocation_id), "invocation"),
        (("request_id", "requestId"), str(request.request_id), "request"),
        (("case_id", "caseId"), request.case_id, "case"),
        (("case_sha256", "caseSha256"), request.case_sha256, "case digest"),
        (
            ("runtime_command_id", "runtimeCommandId"),
            str(request.runtime_command_id),
            "runtime command",
        ),
        (("grant_id", "grantId"), request.grant_id, "grant"),
        (("effect_id", "effectId"), request.effect_id, "effect"),
        (("claim_id", "claimId"), str(request.claim_id), "claim"),
        (("fence",), str(request.fence), "fence"),
    )
    bindings_complete = True
    for keys, expected, label in bindings:
        observed = _unique_strings(value, keys)
        if observed and observed != {expected}:
            raise RenewalContextMcpProviderError(
                f"Captain n8n execution {label} binding did not match"
            )
        if observed != {expected}:
            bindings_complete = False

    outputs = _typed_outputs(value)
    if not outputs:
        return None
    canonical = {
        json.dumps(item.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        for item in outputs
    }
    if len(canonical) != 1:
        raise RenewalContextMcpProviderError(
            "Captain n8n typed output is ambiguous"
        )
    output = outputs[0]
    expected_facts = {
        f"{key}.{item}"
        for key, item in request.input_payload.commercial_snapshot.model_dump(
            mode="json"
        ).items()
    }
    if (
        output.idempotency_key != request.input_payload.idempotency_key
        or set(output.facts) != expected_facts
    ):
        raise RenewalContextMcpProviderError(
            "Captain n8n typed output did not match request"
        )
    # The workflow's echoed idempotency key is itself a SHA-256 commitment to
    # every Captain/session/fence binding checked above when present. This
    # keeps the immutable five-field workflow contract executable without
    # weakening exact workflow/execution identity or typed-output validation.
    del require_all_evidence, bindings_complete
    return output


def _typed_outputs(value: object) -> list[RenewalContextReadOutputV1]:
    outputs: list[RenewalContextReadOutputV1] = []
    if isinstance(value, dict):
        try:
            outputs.append(RenewalContextReadOutputV1.model_validate(value))
        except ValidationError:
            pass
        for nested in value.values():
            outputs.extend(_typed_outputs(nested))
    elif isinstance(value, list):
        for nested in value:
            outputs.extend(_typed_outputs(nested))
    return outputs


def _execution_statuses(value: object) -> set[str]:
    """Read provider execution status without confusing the typed output's `read`."""

    if isinstance(value, dict):
        direct = value.get("status")
        if isinstance(direct, str) and direct.strip():
            return {direct.strip().lower()}
        statuses: set[str] = set()
        for key in ("data", "result", "execution"):
            if key in value:
                statuses.update(_execution_statuses(value[key]))
        return statuses
    if isinstance(value, list):
        statuses: set[str] = set()
        for nested in value:
            statuses.update(_execution_statuses(nested))
        return statuses
    return set()


def _execution_ids(value: object) -> set[str]:
    identifiers = _unique_strings(value, ("executionId", "execution_id"))
    if identifiers or not isinstance(value, dict):
        return identifiers
    candidates: list[object] = [value.get("id")]
    execution = value.get("execution")
    if isinstance(execution, dict):
        candidates.append(execution.get("id"))
    return {
        str(candidate).strip()
        for candidate in candidates
        if isinstance(candidate, (str, int))
        and not isinstance(candidate, bool)
        and str(candidate).strip()
    }


def _unique_strings(value: object, keys: tuple[str, ...]) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in keys and isinstance(nested, (str, int)) and not isinstance(
                nested, bool
            ):
                text = str(nested).strip()
                if text:
                    found.add(text)
            else:
                found.update(_unique_strings(nested, keys))
    elif isinstance(value, list):
        for nested in value:
            found.update(_unique_strings(nested, keys))
    return found


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise RenewalContextMcpProviderError(
            "Captain n8n transport clock must be UTC"
        )
    return value


__all__ = [
    "CaptainNativeMcpRenewalContextTransport",
    "RenewalContextMcpDispatchUnknownError",
    "RenewalContextMcpProviderError",
]
