"""Gateway-owned client for the isolated capability benchmark runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
from decimal import Decimal
from typing import Protocol
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from pydantic import SecretStr

from agenten.agent_runtime.contracts import ArtifactRef
from gateway.capability_generic_benchmark import (
    GenericBenchmarkExecutionEvidence,
    GenericBenchmarkExecutionRequest,
    GenericToolCallEvidence,
    generic_execution_request_digest,
)
from gateway.capability_live_pipeline import CapabilityLiveAuthorityUnavailable


class CapabilityBenchmarkProcessTransport(Protocol):
    async def post(self, endpoint: str, body: bytes) -> bytes: ...


class _RejectBenchmarkRedirects(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str,
        headers: object, newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


class GatewayHttpBenchmarkProcessTransport:
    """Concrete Gateway HTTP transport; all I/O occurs beyond serialization."""

    __slots__ = (
        "_timeout_seconds", "_bearer_token_value", "_configuration_digest",
        "_sealed",
    )

    def __init__(self, *, bearer_token: SecretStr,
                 timeout_seconds: float = 30.0) -> None:
        if (isinstance(timeout_seconds, bool) or timeout_seconds <= 0
                or timeout_seconds > 300):
            raise ValueError("benchmark runtime timeout is invalid")
        token = bearer_token.get_secret_value()
        if len(token) < 16:
            raise ValueError("benchmark runtime bearer token is invalid")
        object.__setattr__(self, "_timeout_seconds", float(timeout_seconds))
        object.__setattr__(self, "_bearer_token_value", str(token))
        object.__setattr__(self, "_configuration_digest", self._current_digest())
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("benchmark HTTP transport binding is immutable")
        object.__setattr__(self, name, value)

    def configuration_digest(self) -> str:
        if self._configuration_digest != self._current_digest():
            raise ValueError("benchmark HTTP transport configuration changed")
        return self._configuration_digest

    def _current_digest(self) -> str:
        token_digest = hashlib.sha256(
            self._bearer_token_value.encode()
        ).hexdigest()
        return hashlib.sha256(json.dumps({
            "transport": type(self).__name__,
            "timeout_seconds": self._timeout_seconds,
            "bearer_token_digest": token_digest,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    async def post(self, endpoint: str, body: bytes) -> bytes:
        self.configuration_digest()
        return await asyncio.to_thread(self._post_sync, endpoint, body)

    def _post_sync(self, endpoint: str, body: bytes) -> bytes:
        request = Request(
            endpoint, data=body, method="POST",
            headers={
                "Content-Type": "application/json", "Accept": "application/json",
                "Authorization": self._authorization_header(),
            },
        )
        opener = build_opener(_RejectBenchmarkRedirects())
        with opener.open(request, timeout=self._timeout_seconds) as response:
            value = response.read(4 * 1024 * 1024 + 1)
        if len(value) > 4 * 1024 * 1024:
            raise ValueError("benchmark runtime response is too large")
        return value

    def _authorization_header(self) -> str:
        self.configuration_digest()
        return "Bearer " + self._bearer_token_value

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(timeout_seconds={self._timeout_seconds!r}, "
            "bearer_token=<redacted>)"
        )


class GatewayIsolatedBenchmarkExecutorClient:
    """Send execution-only bytes across the Captain process boundary."""

    __slots__ = (
        "gateway_store", "authority_id", "_endpoint", "_transport", "_sealed",
        "_allowed_hosts", "_configuration_digest",
        "tool_registry",
    )

    def __init__(self, *, gateway_store: object, authority_id: UUID,
                 endpoint: str, transport: CapabilityBenchmarkProcessTransport,
                 allowed_hosts: tuple[str, ...] = (),
                 tool_registry: object | None = None) -> None:
        parsed = urlsplit(endpoint)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise ValueError("benchmark runtime endpoint is invalid")
        host = (parsed.hostname or "").casefold()
        normalized_allowed = tuple(sorted({item.casefold() for item in allowed_hosts}))
        loopback = host in {"localhost", "127.0.0.1", "::1"}
        if not loopback and host not in normalized_allowed:
            raise ValueError("benchmark runtime endpoint host is not allowlisted")
        if not loopback and parsed.scheme != "https":
            raise ValueError("non-loopback benchmark runtime requires HTTPS")
        if not isinstance(authority_id, UUID):
            raise ValueError("benchmark runtime authority is invalid")
        if not callable(getattr(transport, "post", None)):
            raise ValueError("benchmark runtime transport is invalid")
        object.__setattr__(self, "gateway_store", gateway_store)
        object.__setattr__(self, "authority_id", authority_id)
        object.__setattr__(self, "_endpoint", endpoint)
        object.__setattr__(self, "_transport", transport)
        object.__setattr__(self, "_allowed_hosts", normalized_allowed)
        object.__setattr__(self, "tool_registry", tool_registry)
        object.__setattr__(self, "_configuration_digest", self._binding_digest())
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("isolated benchmark client binding is immutable")
        object.__setattr__(self, name, value)

    def uses_production_transport(self) -> bool:
        return (
            type(self._transport) is GatewayHttpBenchmarkProcessTransport
            and self._configuration_digest == self._binding_digest()
        )

    def sealed_configuration_digest(self) -> str:
        if not self.uses_production_transport():
            raise ValueError("isolated benchmark configuration is not sealed")
        return self._configuration_digest

    def _binding_digest(self) -> str:
        transport_digest = (
            self._transport.configuration_digest()
            if type(self._transport) is GatewayHttpBenchmarkProcessTransport
            else "non-production-transport"
        )
        return hashlib.sha256(json.dumps({
            "endpoint": self._endpoint,
            "authority_id": str(self.authority_id),
            "allowed_hosts": self._allowed_hosts,
            "transport_digest": transport_digest,
            "tool_registry_digest": getattr(self.tool_registry, "digest", None),
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(authority_id={self.authority_id!r}, "
            "endpoint=<redacted>, transport=<redacted>)"
        )

    async def execute(
        self, request: GenericBenchmarkExecutionRequest,
    ) -> GenericBenchmarkExecutionEvidence:
        if self._configuration_digest != self._binding_digest():
            raise CapabilityLiveAuthorityUnavailable(
                "isolated benchmark client configuration changed"
            )
        expected_digest = generic_execution_request_digest(
            job_id=request.job_id, capability_id=request.capability_id,
            input_ref=request.input_ref, case_ref=request.case_ref,
            team_kind=request.team_kind, model_id=request.model_id,
            model_policy_digest=request.model_policy_digest,
            execution_case=request.execution_case,
            execution_plan=request.execution_plan,
            request_nonce=request.request_nonce,
        )
        if expected_digest != request.request_digest:
            raise CapabilityLiveAuthorityUnavailable(
                "isolated benchmark request binding is invalid"
            )
        payload = {
            "schema": "captain.capability-benchmark-execution-request.v1",
            "job_id": str(request.job_id),
            "capability_id": request.capability_id,
            "input_ref": request.input_ref.model_dump(mode="json"),
            "case": {
                "case_id": request.case_ref.case_id,
                "category": request.case_ref.category.value,
                "case_digest": request.case_ref.case_digest,
            },
            "team_kind": request.team_kind,
            "model_id": request.model_id,
            "model_policy_digest": request.model_policy_digest,
            "execution_case": {
                "setup": request.execution_case.setup,
                "action": request.execution_case.action,
                "opaque_source_refs": request.execution_case.opaque_source_refs,
                "declared_tool_keys": request.execution_case.declared_tool_keys,
                "tool_authority_sha256": (
                    request.execution_case.tool_authority_sha256
                ),
            },
            "execution_plan": {
                "candidate_ref": request.execution_plan.candidate_ref.model_dump(mode="json"),
                "team_manifest_ref": request.execution_plan.team_manifest_ref.model_dump(mode="json"),
                "baseline_policy_ref": request.execution_plan.baseline_policy_ref.model_dump(mode="json"),
                "model_ref": request.execution_plan.model_ref.model_dump(mode="json"),
                "execution_policy_ref": request.execution_plan.execution_policy_ref.model_dump(mode="json"),
                "team_agent_names": request.execution_plan.team_agent_names,
                "baseline_agent_name": request.execution_plan.baseline_agent_name,
            },
            "request_nonce": str(request.request_nonce),
            "request_digest": request.request_digest,
        }
        body = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()
        try:
            response = await self._transport.post(self._endpoint, body)
        except Exception:
            raise CapabilityLiveAuthorityUnavailable(
                "isolated benchmark runtime is unavailable"
            ) from None
        try:
            return self._parse_response(request, response)
        except CapabilityLiveAuthorityUnavailable:
            raise
        except Exception:
            raise CapabilityLiveAuthorityUnavailable(
                "isolated benchmark response is invalid"
            ) from None

    @staticmethod
    def _parse_response(
        request: GenericBenchmarkExecutionRequest, response: bytes,
    ) -> GenericBenchmarkExecutionEvidence:
        if not isinstance(response, bytes):
            raise ValueError("response must be bytes")
        value = json.loads(response)
        required = {
            "schema", "request_nonce", "request_digest", "agent_count",
            "outcome_ref", "message_event_refs", "handoff_event_refs",
            "tool_calls", "rationale_ref", "cost_usd", "latency_ms",
            "trace_id", "run_id",
        }
        if (not isinstance(value, dict) or set(value) != required
                or value.get("schema") != "captain.capability-benchmark-execution-response.v1"
                or value.get("request_nonce") != str(request.request_nonce)
                or value.get("request_digest") != request.request_digest):
            raise CapabilityLiveAuthorityUnavailable(
                "isolated benchmark response changed request binding"
            )
        tool_calls = tuple(
            GenericToolCallEvidence(
                tool_key=item["tool_key"],
                call_ref=ArtifactRef.model_validate(item["call_ref"]),
                lease_ref=ArtifactRef.model_validate(item["lease_ref"]),
                receipt_ref=ArtifactRef.model_validate(item["receipt_ref"]),
            )
            for item in value["tool_calls"]
        )
        return GenericBenchmarkExecutionEvidence(
            case_ref=request.case_ref, team_kind=request.team_kind,
            agent_count=value["agent_count"], model_id=request.model_id,
            model_policy_digest=request.model_policy_digest,
            input_ref=request.input_ref, capability_id=request.capability_id,
            outcome_ref=ArtifactRef.model_validate(value["outcome_ref"]),
            message_event_refs=tuple(
                ArtifactRef.model_validate(item) for item in value["message_event_refs"]
            ),
            handoff_event_refs=tuple(
                ArtifactRef.model_validate(item) for item in value["handoff_event_refs"]
            ),
            tool_calls=tool_calls,
            rationale_ref=ArtifactRef.model_validate(value["rationale_ref"]),
            cost_usd=Decimal(value["cost_usd"]), latency_ms=value["latency_ms"],
            trace_id=UUID(value["trace_id"]), run_id=UUID(value["run_id"]),
            request_nonce=request.request_nonce,
            request_digest=request.request_digest,
        )


__all__ = [
    "CapabilityBenchmarkProcessTransport",
    "GatewayHttpBenchmarkProcessTransport",
    "GatewayIsolatedBenchmarkExecutorClient",
]
