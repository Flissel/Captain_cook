"""Grant-bound Captain n8n tool for the renewal business benchmark.

The adapter is deliberately scoped to one host AutoGen session.  It accepts
only the read-only ``renewal_context_read`` contract, derives the provider
idempotency key from the complete Captain/session/fence binding, and publishes
evidence only after the provider has echoed and satisfied that binding.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from autogen_core import CancellationToken
from autogen_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_factory.business_benchmark_production_ports import (
    factory_execution_policy_sha256,
)
from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.evidence_store import FactoryEvidenceStore
from agenten.agent_factory.n8n_tools import OpaqueN8nToolReference
from agenten.agent_factory.skill_workflow_contracts import FactorySkillInvocationV1
from agenten.agent_factory.team_execution import (
    FactoryN8nExecutionEvidenceV1,
    FactoryN8nToolAuthorizationV1,
    HostAutoGenSessionIdentityV1,
)
from agenten.agent_runtime.contracts import (
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityProfile,
    IntegrationIntent,
    RuntimeStatus,
)
from agenten.agent_runtime.n8n_endpoint import N8nEndpoint
from agenten.targets.n8n import N8nExecutionEvidence


_TOOL_NAME = "renewal_context_read"
_EFFECT = "read_only"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class RenewalCommercialSnapshotV1(_FrozenModel):
    renewal_window: str
    engagement_band: str
    commercial_evidence_state: str
    consent_state: str


class RenewalContextReadInputV1(_FrozenModel):
    operation: Literal["read_renewal_context"]
    idempotency_key: str = Field(min_length=16, max_length=128)
    evidence_partition: Literal["ordinary", "boundary"]
    synthetic_subject_id: str = Field(pattern=r"^subject-[a-z0-9]+$")
    commercial_snapshot: RenewalCommercialSnapshotV1


class RenewalContextReadOutputV1(_FrozenModel):
    operation: Literal["read_renewal_context"]
    idempotency_key: str = Field(min_length=16, max_length=128)
    status: Literal["read"]
    facts: tuple[str, ...] = Field(
        min_length=1,
        json_schema_extra={"uniqueItems": True},
    )

    @field_validator("facts")
    @classmethod
    def require_unique_facts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("renewal facts must be unique")
        return value


class RenewalContextN8nProviderRequestV1(_FrozenModel):
    schema_name: Literal["captain.renewal-context-n8n-request.v1"] = Field(
        default="captain.renewal-context-n8n-request.v1",
        alias="schema",
        serialization_alias="schema",
    )
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    invocation_id: UUID
    request_id: UUID
    runtime_session_id: str = Field(min_length=1, max_length=200)
    case_id: str = Field(min_length=1, max_length=128)
    case_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_ref: str = Field(pattern=r"^workspace://")
    tool: OpaqueN8nToolReference
    effect: Literal["read_only"]
    effect_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    claim_id: UUID
    fence: int = Field(ge=1, strict=True)
    runtime_command_id: UUID
    grant_id: str = Field(min_length=1, max_length=128)
    requested_idempotency_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_payload: RenewalContextReadInputV1

    @model_validator(mode="after")
    def require_bound_idempotency(self) -> "RenewalContextN8nProviderRequestV1":
        if self.tool.tool_name != _TOOL_NAME:
            raise ValueError("renewal request tool binding is invalid")
        expected = _bound_idempotency_key(
            job_id=self.job_id,
            correlation_id=self.correlation_id,
            subject_version=self.subject_version,
            attempt=self.attempt,
            invocation_id=self.invocation_id,
            request_id=self.request_id,
            runtime_session_id=self.runtime_session_id,
            case_id=self.case_id,
            case_sha256=self.case_sha256,
            workspace_ref=self.workspace_ref,
            tool=self.tool,
            effect_id=self.effect_id,
            claim_id=self.claim_id,
            fence=self.fence,
            runtime_command_id=self.runtime_command_id,
            grant_id=self.grant_id,
            requested_idempotency_sha256=self.requested_idempotency_sha256,
            input_payload=self.input_payload,
        )
        if self.input_payload.idempotency_key != expected:
            raise ValueError("renewal request idempotency binding is invalid")
        return self


class RenewalContextN8nProviderResponseV1(_FrozenModel):
    schema_name: Literal["captain.renewal-context-n8n-provider-response.v1"] = Field(
        default="captain.renewal-context-n8n-provider-response.v1",
        alias="schema",
        serialization_alias="schema",
    )
    request: RenewalContextN8nProviderRequestV1
    effect: Literal["read_only"]
    mcp_call_id: str = Field(min_length=1, max_length=128)
    workflow_ref: ArtifactRef
    execution: N8nExecutionEvidence
    runtime_result: AgentRuntimeResult
    output: RenewalContextReadOutputV1


class RenewalContextN8nReceiptV1(_FrozenModel):
    schema_name: Literal["captain.renewal-context-n8n-receipt.v1"] = Field(
        default="captain.renewal-context-n8n-receipt.v1",
        alias="schema",
        serialization_alias="schema",
    )
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    invocation_id: UUID
    request_id: UUID
    runtime_session_id: str = Field(min_length=1, max_length=200)
    case_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_ref: str = Field(pattern=r"^workspace://")
    tool: OpaqueN8nToolReference
    effect: Literal["read_only"]
    effect_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    claim_id: UUID
    fence: int = Field(ge=1, strict=True)
    runtime_command_id: UUID
    grant_id: str = Field(min_length=1, max_length=128)
    mcp_call_id: str = Field(min_length=1, max_length=128)
    workflow_ref: ArtifactRef
    execution: N8nExecutionEvidence
    runtime_result: AgentRuntimeResult
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("renewal receipt timestamp must be UTC")
        return value

class RenewalContextTransientError(RuntimeError):
    """A retryable transport failure; its provider details must not escape."""


class RenewalContextN8nTransportPort(Protocol):
    async def execute(
        self,
        *,
        endpoint: N8nEndpoint,
        request: RenewalContextN8nProviderRequestV1,
        timeout_seconds: float,
    ) -> RenewalContextN8nProviderResponseV1: ...


class _RenewalContextReadTool(
    BaseTool[RenewalContextReadInputV1, dict[str, object]]
):
    def __init__(self, adapter: "CaptainRenewalContextN8nAdapter") -> None:
        super().__init__(
            RenewalContextReadInputV1,
            dict[str, object],
            _TOOL_NAME,
            "Read a Captain-scoped synthetic renewal context without mutation.",
        )
        self._adapter = adapter

    async def run(
        self,
        args: RenewalContextReadInputV1,
        cancellation_token: CancellationToken,
    ) -> dict[str, object]:
        output = await self._adapter._execute(args, cancellation_token)
        return output.model_dump(mode="json")


class CaptainRenewalContextN8nAdapter:
    """One-session implementation of ``FactoryN8nToolAdapterPort``.

    Provider transport is injected so the host can select MCP or an equivalent
    Captain-controlled typed executor without exposing credentials to agents.
    """

    def __init__(
        self,
        *,
        job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
        identity: HostAutoGenSessionIdentityV1,
        authorization: FactoryN8nToolAuthorizationV1,
        endpoint: N8nEndpoint,
        allowed_endpoint_urls: frozenset[str],
        workflow_ref: ArtifactRef,
        transport: RenewalContextN8nTransportPort,
        evidence_store: FactoryEvidenceStore,
        clock: Callable[[], datetime],
        timeout_seconds: float = 10.0,
        max_attempts: int = 2,
    ) -> None:
        if timeout_seconds <= 0 or max_attempts < 1 or max_attempts > 3:
            raise ValueError("renewal n8n retry configuration is invalid")
        _require_captain_endpoint(endpoint, allowed_endpoint_urls)
        _require_session_binding(job, invocation, identity, authorization)
        if workflow_ref.media_type != "application/json":
            raise ValueError("renewal n8n workflow must be a JSON artifact")
        self._job = job
        self._invocation = invocation
        self._identity = identity
        self._authorization = authorization
        self._endpoint = endpoint
        self._workflow_ref = workflow_ref
        self._transport = transport
        self._evidence_store = evidence_store
        self._clock = clock
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._evidence: list[FactoryN8nExecutionEvidenceV1] = []
        self._completed: dict[str, RenewalContextReadOutputV1] = {}
        self._lock = asyncio.Lock()
        self._tool = _RenewalContextReadTool(self)

    def tool(self, name: str) -> BaseTool[BaseModel, Any]:
        if name != _TOOL_NAME:
            raise KeyError("n8n tool is not available in this benchmark adapter")
        return self._tool  # type: ignore[return-value]

    def authorization(self, name: str) -> FactoryN8nToolAuthorizationV1:
        if name != _TOOL_NAME:
            raise KeyError("n8n authorization is not available for this tool")
        return self._authorization

    def observed_evidence(self) -> tuple[FactoryN8nExecutionEvidenceV1, ...]:
        return tuple(self._evidence)

    async def _execute(
        self,
        args: RenewalContextReadInputV1,
        cancellation_token: CancellationToken,
    ) -> RenewalContextReadOutputV1:
        if cancellation_token.is_cancelled():
            raise asyncio.CancelledError
        request = self._request_for(args)
        idempotency_key = request.input_payload.idempotency_key
        async with self._lock:
            cached = self._completed.get(idempotency_key)
            if cached is not None:
                return cached
            response = await self._call_provider(request, cancellation_token)
            self._validate_response(request, response)
            receipt = RenewalContextN8nReceiptV1(
                request_sha256=hashlib.sha256(_canonical_json(request)).hexdigest(),
                response_sha256=hashlib.sha256(_canonical_json(response)).hexdigest(),
                job_id=request.job_id,
                correlation_id=request.correlation_id,
                subject_version=request.subject_version,
                attempt=request.attempt,
                invocation_id=request.invocation_id,
                request_id=request.request_id,
                runtime_session_id=request.runtime_session_id,
                case_sha256=request.case_sha256,
                workspace_ref=request.workspace_ref,
                tool=request.tool,
                effect=request.effect,
                effect_id=request.effect_id,
                claim_id=request.claim_id,
                fence=request.fence,
                runtime_command_id=request.runtime_command_id,
                grant_id=request.grant_id,
                mcp_call_id=response.mcp_call_id,
                workflow_ref=response.workflow_ref,
                execution=response.execution,
                runtime_result=response.runtime_result,
                recorded_at=self._utc_now(),
            )
            evidence_ref = await self._evidence_store.persist(
                self._job,
                _canonical_json(receipt),
            )
            evidence = FactoryN8nExecutionEvidenceV1(
                tool_name=_TOOL_NAME,
                approved_tool_ref=self._authorization.approved_tool_ref,
                runtime_command=self._authorization.runtime_command,
                capability_grant=self._authorization.capability_grant,
                runtime_result=response.runtime_result,
                mcp_call_id=response.mcp_call_id,
                workflow_ref=response.workflow_ref,
                execution=response.execution,
                evidence_ref=evidence_ref,
            )
            self._evidence.append(evidence)
            self._completed[idempotency_key] = response.output
            return response.output

    def _request_for(
        self, args: RenewalContextReadInputV1
    ) -> RenewalContextN8nProviderRequestV1:
        claim = self._authorization
        requested_digest = hashlib.sha256(args.idempotency_key.encode("utf-8")).hexdigest()
        payload = args.model_copy(update={"idempotency_key": "0" * 64})
        values: dict[str, object] = {
            "job_id": self._identity.job_id,
            "correlation_id": self._identity.correlation_id,
            "subject_version": self._identity.subject_version,
            "attempt": self._identity.attempt,
            "invocation_id": self._identity.invocation_id,
            "request_id": self._identity.request_id,
            "runtime_session_id": self._identity.runtime_session_id,
            "case_id": self._identity.case_id,
            "case_sha256": self._identity.case_sha256,
            "workspace_ref": claim.runtime_command.payload.workspace_ref,
            "tool": claim.approved_tool_ref,
            "effect": _EFFECT,
            "effect_id": self._identity.effect_id,
            "claim_id": self._identity.claim_id,
            "fence": self._identity.fence,
            "runtime_command_id": claim.runtime_command.event_id,
            "grant_id": claim.capability_grant.grant_id,
            "requested_idempotency_sha256": requested_digest,
            "input_payload": payload,
        }
        bound_key = _bound_idempotency_key(**values)  # type: ignore[arg-type]
        values["input_payload"] = payload.model_copy(
            update={"idempotency_key": bound_key}
        )
        return RenewalContextN8nProviderRequestV1.model_validate(values)

    async def _call_provider(
        self,
        request: RenewalContextN8nProviderRequestV1,
        cancellation_token: CancellationToken,
    ) -> RenewalContextN8nProviderResponseV1:
        for attempt in range(self._max_attempts):
            if cancellation_token.is_cancelled():
                raise asyncio.CancelledError
            try:
                raw = await asyncio.wait_for(
                    self._transport.execute(
                        endpoint=self._endpoint,
                        request=request,
                        timeout_seconds=self._timeout_seconds,
                    ),
                    timeout=self._timeout_seconds,
                )
                return RenewalContextN8nProviderResponseV1.model_validate(raw)
            except asyncio.CancelledError:
                raise
            except (TimeoutError, RenewalContextTransientError):
                if attempt + 1 == self._max_attempts:
                    break
            except Exception:
                raise RuntimeError("Captain n8n renewal read failed closed") from None
        raise RuntimeError("Captain n8n renewal read failed closed") from None

    def _validate_response(
        self,
        request: RenewalContextN8nProviderRequestV1,
        response: RenewalContextN8nProviderResponseV1,
    ) -> None:
        claim = self._authorization
        expected_facts = tuple(
            sorted(
                f"{key}.{value}"
                for key, value in request.input_payload.commercial_snapshot.model_dump(
                    mode="json"
                ).items()
            )
        )
        if (
            response.request != request
            or response.effect != _EFFECT
            or response.workflow_ref != self._workflow_ref
            or response.execution.artifact_digest != self._workflow_ref.sha256
            or response.execution.correlation_id != str(request.correlation_id)
            or response.runtime_result.command_id != claim.runtime_command.event_id
            or response.runtime_result.correlation_id != request.correlation_id
            or response.runtime_result.subject_id != _TOOL_NAME
            or response.runtime_result.subject_version != request.subject_version
            or response.runtime_result.grant_id != claim.capability_grant.grant_id
            or response.runtime_result.operation is not claim.runtime_command.payload.operation
            or response.runtime_result.status is not RuntimeStatus.SUCCEEDED
            or response.output.idempotency_key
            != request.input_payload.idempotency_key
            or frozenset(response.output.facts) != frozenset(expected_facts)
        ):
            raise ValueError("renewal n8n provider binding does not match request")

    def _utc_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("renewal n8n clock must be UTC")
        return value


def _require_captain_endpoint(
    endpoint: N8nEndpoint,
    allowed_endpoint_urls: frozenset[str],
) -> None:
    allowed = frozenset(item.rstrip("/") for item in allowed_endpoint_urls if item.strip())
    api_url = endpoint.api_base_url.rstrip("/")
    webhook_url = endpoint.webhook_base_url.rstrip("/")
    parsed = urlsplit(api_url)
    broker = urlsplit(endpoint.mcp_broker_url) if endpoint.mcp_broker_url else None
    if (
        endpoint.mode != "captain-builder"
        or not allowed
        or api_url not in allowed
        or webhook_url != api_url
        or parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.port != 5679
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (
            broker is not None
            and (
                broker.scheme != "http"
                or broker.hostname not in {"localhost", "127.0.0.1", "::1"}
                or broker.port != 5680
                or broker.username is not None
                or broker.password is not None
                or broker.path not in {"", "/"}
                or broker.query
                or broker.fragment
            )
        )
    ):
        raise ValueError(
            "renewal benchmark requires the allowlisted Captain-owned n8n endpoint; VibeMind is forbidden"
        )


def _require_session_binding(
    job: AgentFactoryJobV3,
    invocation: FactorySkillInvocationV1,
    identity: HostAutoGenSessionIdentityV1,
    claim: FactoryN8nToolAuthorizationV1,
) -> None:
    command = claim.runtime_command
    grant = claim.capability_grant
    if (
        invocation.job_id != job.job_id
        or invocation.correlation_id != job.correlation_id
        or invocation.subject_version != job.subject_version
        or invocation.attempt != identity.attempt
        or identity.job_id != job.job_id
        or identity.correlation_id != job.correlation_id
        or identity.subject_version != job.subject_version
        or identity.invocation_id != invocation.invocation_id
        or invocation.execution_scope_ref is None
        or invocation.execution_scope_ref not in job.private_holdout_refs
        or identity.execution_policy_sha256 != factory_execution_policy_sha256(job)
        or claim.tool_name != _TOOL_NAME
        or claim.approved_tool_ref.tool_name != _TOOL_NAME
        or command.correlation_id != job.correlation_id
        or command.subject_version != job.subject_version
        or command.payload.workspace_ref != grant.workspace_ref
        or command.payload.integration_intent is not IntegrationIntent.N8N
        or command.payload.capability_profile is not CapabilityProfile.N8N_BUILDER
        or command.event_id != grant.command_id
        or grant.profile is not CapabilityProfile.N8N_BUILDER
        or grant.mcp_servers != ("n8n-mcp",)
        or "mcp.n8n" not in grant.capabilities
    ):
        raise ValueError("renewal n8n job, invocation, workspace, or grant binding is invalid")


def _bound_idempotency_key(
    *,
    job_id: UUID,
    correlation_id: UUID,
    subject_version: int,
    attempt: int,
    invocation_id: UUID,
    request_id: UUID,
    runtime_session_id: str,
    case_id: str,
    case_sha256: str,
    workspace_ref: str,
    tool: OpaqueN8nToolReference,
    effect_id: str,
    claim_id: UUID,
    fence: int,
    runtime_command_id: UUID,
    grant_id: str,
    requested_idempotency_sha256: str,
    input_payload: RenewalContextReadInputV1,
    **_: object,
) -> str:
    payload = input_payload.model_dump(mode="json")
    payload["idempotency_key"] = requested_idempotency_sha256
    binding = {
        "schema": "captain.renewal-context-n8n-idempotency.v1",
        "job_id": str(job_id),
        "correlation_id": str(correlation_id),
        "subject_version": subject_version,
        "attempt": attempt,
        "invocation_id": str(invocation_id),
        "request_id": str(request_id),
        "runtime_session_id": runtime_session_id,
        "case_id": case_id,
        "case_sha256": case_sha256,
        "workspace_ref": workspace_ref,
        "tool": tool.model_dump(mode="json", by_alias=True),
        "effect": _EFFECT,
        "effect_id": effect_id,
        "claim_id": str(claim_id),
        "fence": fence,
        "runtime_command_id": str(runtime_command_id),
        "grant_id": grant_id,
        "input_payload": payload,
    }
    return hashlib.sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _canonical_json(model: BaseModel) -> bytes:
    return json.dumps(
        model.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


__all__ = [
    "CaptainRenewalContextN8nAdapter",
    "RenewalCommercialSnapshotV1",
    "RenewalContextN8nProviderRequestV1",
    "RenewalContextN8nProviderResponseV1",
    "RenewalContextN8nReceiptV1",
    "RenewalContextN8nTransportPort",
    "RenewalContextReadInputV1",
    "RenewalContextReadOutputV1",
    "RenewalContextTransientError",
]
