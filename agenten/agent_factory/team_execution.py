"""Captain-governed execution of sealed generated AutoGen teams."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from collections.abc import AsyncGenerator, Mapping, Sequence
from typing import Any, Callable, Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.base import TaskResult, TerminatedException, TerminationCondition
from autogen_agentchat.conditions import MaxMessageTermination
from autogen_agentchat.messages import (
    BaseAgentEvent,
    BaseChatMessage,
    HandoffMessage,
    StopMessage,
    ToolCallExecutionEvent,
)
from autogen_agentchat.teams import RoundRobinGroupChat, SelectorGroupChat, Swarm
from autogen_core import CancellationToken
from autogen_core.model_context import BufferedChatCompletionContext
from autogen_core.models import (
    ChatCompletionClient,
    CreateResult,
    LLMMessage,
    ModelCapabilities,
    ModelInfo,
    RequestUsage,
)
from autogen_core.tools import BaseTool, FunctionTool, Tool, ToolSchema

from agenten.agent_factory.candidate_evaluation import (
    FactoryAutoGenTeamManifestV1,
    FactoryCandidateEvaluator,
    FactoryCandidateEvaluationResult,
    ResolvedFactoryCandidate,
)
from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryLease, FactoryRole
from agenten.agent_factory.evidence_store import FactoryEvidenceStore
from agenten.agent_factory.execution_budget import (
    FactoryBudgetPort,
    FactoryBudgetReservationV1,
    FactoryUsageReceiptV1,
)
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_factory.leases import validate_factory_lease
from agenten.agent_factory.n8n_tools import OpaqueN8nToolReference
from agenten.agent_factory.hermes_cli import (
    CaptainHermesReplayRetryAuthorizationPort,
    FactorySkillReplayHermesRetryableFailureError,
    FactorySkillReplayRecord,
    FactorySkillReplayStore,
    ReleasedFactorySkillCatalog,
)
from agenten.agent_factory.outcome_contracts import AssertionOutcome, ExecutionOutcomeV1
from agenten.agent_factory.orchestration import FactoryDispatch
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import (
    FactorySkillInvocationV1,
    FactorySkillStep,
    TeamExecutionEvidenceV1,
)
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityGrant,
    CapabilityGrantRevocation,
    CapabilityProfile,
    IntegrationIntent,
    RuntimeOperation,
    RuntimeStatus,
)
from agenten.agent_runtime.capabilities import validate_grant
from agenten.targets.n8n import N8nExecutionEvidence


_HOST_SESSION_RESOURCE_LOCKS: dict[tuple[str, int], asyncio.Lock] = {}


class FactoryPricingQuoteV1(BaseModel):
    """Versioned provider pricing evidence used for host-computed receipts."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    quote_id: str = Field(min_length=1, max_length=128)
    job_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    execution_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    effective_at: datetime
    max_cost_per_call: Decimal
    input_cost_per_million: Decimal
    output_cost_per_million: Decimal
    minimum_cost_usd: Decimal
    evidence_ref: ArtifactRef

    @field_validator(
        "input_cost_per_million",
        "output_cost_per_million",
        "minimum_cost_usd",
        "max_cost_per_call",
        mode="before",
    )
    @classmethod
    def require_decimal_price(cls, value: object) -> Decimal:
        if isinstance(value, (bool, float)) or not isinstance(value, (str, Decimal)):
            raise ValueError("pricing values must be decimal strings")
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("pricing values must be finite") from exc
        if not parsed.is_finite() or parsed < 0:
            raise ValueError("pricing values must be finite and non-negative")
        return parsed

    @field_validator("effective_at")
    @classmethod
    def require_utc_effective_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("pricing effective_at must be UTC")
        return value

    @model_validator(mode="after")
    def require_bounded_quote(self) -> "FactoryPricingQuoteV1":
        if self.max_cost_per_call <= 0 or self.minimum_cost_usd > self.max_cost_per_call:
            raise ValueError("pricing quote must fit its positive per-call maximum")
        return self

    def cost(self, usage: RequestUsage) -> Decimal:
        calculated = (
            Decimal(usage.prompt_tokens) * self.input_cost_per_million
            + Decimal(usage.completion_tokens) * self.output_cost_per_million
        ) / Decimal("1000000")
        return max(calculated, self.minimum_cost_usd).quantize(
            Decimal("0.01"), rounding=ROUND_CEILING
        )


class FactoryPricingAuthorityPort(Protocol):
    """Captain-authoritative resolver for a job- and policy-bound price quote."""

    def resolve(
        self,
        *,
        job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
        provider: str,
        model: str,
        now: datetime,
    ) -> FactoryPricingQuoteV1: ...


class FactoryModelClientBindingV1(BaseModel):
    """Immutable pre-effect identity of one host-owned budgeted model client."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, strict=True)
    model: str = Field(min_length=1, max_length=128)


class FactoryPaidEffectAuthorityPort(Protocol):
    """Re-authorize the released execute-team skill before each paid effect."""

    def authorize(
        self,
        *,
        job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
        now: datetime,
    ) -> ReleasedHermesSkill: ...


class CaptainReleasedSkillAuthority:
    """Validate Captain's catalog release and its on-disk immutable skill digest."""

    def __init__(
        self,
        *,
        catalog: ReleasedFactorySkillCatalog,
        skill_root: Path,
    ) -> None:
        self._catalog = catalog
        self._skill_root = skill_root

    def authorize(
        self,
        *,
        job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
        now: datetime,
    ) -> ReleasedHermesSkill:
        validate_factory_lease(
            invocation.lease,
            job=job,
            role=FactoryRole.REAL_CASE_TESTER,
            attempt=invocation.attempt,
            now=now,
        )
        if (
            invocation.step is not FactorySkillStep.EXECUTE_TEAM
            or invocation.job_id != job.job_id
            or invocation.correlation_id != job.correlation_id
            or invocation.subject_version != job.subject_version
            or invocation.acceptance_assertion_ids != job.acceptance_assertion_ids
        ):
            raise ValueError("paid model effect is not bound to this execute-team job")
        released = self._catalog.released_for(job, FactorySkillStep.EXECUTE_TEAM)
        if released != invocation.released_skill:
            raise ValueError("execute-team invocation does not match Captain's catalog")
        if (
            released.skill_id != "captain-factory-execute-team"
            or released.capability != "factory_workflow"
            or released.released_at > now
            or released.content_ref.uri
            != f"artifact://released-skills/{released.skill_id}/v{released.version}"
            or released.content_ref.media_type != "application/json"
        ):
            raise ValueError("Captain execute-team skill release is not authorized")
        directory = (self._skill_root.resolve() / released.skill_id).resolve()
        try:
            directory.relative_to(self._skill_root.resolve())
        except ValueError as exc:
            raise ValueError("execute-team skill directory is outside its root") from exc
        if not directory.is_dir() or not (directory / "SKILL.md").is_file():
            raise ValueError("released execute-team skill directory is missing")
        if _skill_directory_digest(directory) != released.content_sha256:
            raise ValueError("released execute-team skill digest does not match Captain")
        return released


class BudgetedChatCompletionClient(ChatCompletionClient):
    """Host-owned model wrapper with one Captain reservation per provider call."""

    def __init__(
        self,
        *,
        job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
        attempt: int,
        delegate: ChatCompletionClient,
        budget: FactoryBudgetPort,
        evidence_store: FactoryEvidenceStore,
        provider: str,
        model: str,
        max_cost_per_call: Decimal,
        paid_effect_authority: FactoryPaidEffectAuthorityPort,
        pricing_authority: FactoryPricingAuthorityPort,
        clock: Callable[[], datetime],
    ) -> None:
        if max_cost_per_call <= 0:
            raise ValueError("model call maximum cost must be positive")
        if (
            invocation.job_id != job.job_id
            or invocation.correlation_id != job.correlation_id
            or invocation.subject_version != job.subject_version
            or invocation.attempt != attempt
        ):
            raise ValueError("budgeted model invocation does not match its current job")
        if model not in job.execution_policy.allowed_models:
            raise ValueError("budgeted model is not allowed by the execution policy")
        self._job = job
        self._binding = FactoryModelClientBindingV1(
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            subject_version=job.subject_version,
            attempt=attempt,
            model=model,
        )
        self._invocation = invocation
        self._attempt = attempt
        self._delegate = delegate
        self._budget = budget
        self._evidence_store = evidence_store
        self._provider = provider
        self._model = model
        self._max_cost_per_call = max_cost_per_call
        self._paid_effect_authority = paid_effect_authority
        self._pricing_authority = pricing_authority
        self._clock = clock
        self._usage_receipts: list[FactoryUsageReceiptV1] = []
        self._provider_dispatched = False
        self._provider_dispatch_count = 0
        self._provider_effects_with_unknown_usage: set[UUID] = set()

    @property
    def usage_receipts(self) -> tuple[FactoryUsageReceiptV1, ...]:
        return tuple(self._usage_receipts)

    @property
    def model(self) -> str:
        return self._model

    @property
    def binding(self) -> FactoryModelClientBindingV1:
        return self._binding

    @property
    def provider_dispatched(self) -> bool:
        return self._provider_dispatched

    @property
    def any_provider_effect_started(self) -> bool:
        """Whether this invocation dispatched at least one paid provider effect."""

        return self._provider_dispatched

    @property
    def provider_effect_dispatched_with_unknown_usage(self) -> bool:
        """Whether a dispatched provider effect still lacks authoritative usage."""

        return bool(self._provider_effects_with_unknown_usage)

    @property
    def provider_dispatch_count(self) -> int:
        return self._provider_dispatch_count

    @property
    def unresolved_reservation_ids(self) -> frozenset[UUID]:
        return frozenset(self._provider_effects_with_unknown_usage)

    async def create(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Tool | ToolSchema] = [],
        tool_choice: Tool | Literal["auto", "required", "none"] = "auto",
        json_output: bool | type[BaseModel] | None = None,
        extra_create_args: Mapping[str, Any] = {},
        cancellation_token: CancellationToken | None = None,
    ) -> CreateResult:
        reservation, pricing_quote = self._reserve()
        scoped_create_args = dict(extra_create_args)
        if tools:
            scoped_create_args["parallel_tool_calls"] = False
        else:
            scoped_create_args.pop("parallel_tool_calls", None)
        try:
            self._provider_dispatched = True
            self._provider_dispatch_count += 1
            self._provider_effects_with_unknown_usage.add(reservation.reservation_id)
            result = await self._delegate.create(
                messages,
                tools=tools,
                tool_choice=tool_choice,
                json_output=json_output,
                extra_create_args=scoped_create_args,
                cancellation_token=cancellation_token,
            )
        except asyncio.CancelledError:
            # Once delegated, cancellation does not prove the provider avoided cost.
            # Leave the reservation active for authoritative reconciliation.
            raise
        await self._finalize(reservation, result.usage, pricing_quote)
        return result

    async def create_stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Tool | ToolSchema] = [],
        tool_choice: Tool | Literal["auto", "required", "none"] = "auto",
        json_output: bool | type[BaseModel] | None = None,
        extra_create_args: Mapping[str, Any] = {},
        cancellation_token: CancellationToken | None = None,
    ) -> AsyncGenerator[str | CreateResult, None]:
        reservation, pricing_quote = self._reserve()
        scoped_create_args = dict(extra_create_args)
        if tools:
            scoped_create_args["parallel_tool_calls"] = False
        else:
            scoped_create_args.pop("parallel_tool_calls", None)
        finalized = False
        try:
            self._provider_dispatched = True
            self._provider_dispatch_count += 1
            self._provider_effects_with_unknown_usage.add(reservation.reservation_id)
            async for item in self._delegate.create_stream(
                messages,
                tools=tools,
                tool_choice=tool_choice,
                json_output=json_output,
                extra_create_args=scoped_create_args,
                cancellation_token=cancellation_token,
            ):
                if isinstance(item, CreateResult):
                    await self._finalize(reservation, item.usage, pricing_quote)
                    finalized = True
                yield item
            if not finalized:
                raise ValueError("provider stream ended without final usage")
        except asyncio.CancelledError:
            # A started stream may already have incurred provider cost.
            raise

    def _reserve(self) -> tuple[FactoryBudgetReservationV1, FactoryPricingQuoteV1]:
        now = self._clock()
        self._paid_effect_authority.authorize(
            job=self._job,
            invocation=self._invocation,
            now=now,
        )
        pricing_quote = self._pricing_authority.resolve(
            job=self._job,
            invocation=self._invocation,
            provider=self._provider,
            model=self._model,
            now=now,
        )
        if (
            pricing_quote.job_id != self._job.job_id
            or pricing_quote.subject_version != self._job.subject_version
            or pricing_quote.execution_policy_sha256
            != _execution_policy_digest(self._job)
            or pricing_quote.provider != self._provider
            or pricing_quote.model != self._model
            or pricing_quote.effective_at > now
            or pricing_quote.max_cost_per_call != self._max_cost_per_call
        ):
            raise ValueError("Captain pricing quote does not match this paid model effect")
        reservation = self._budget.reserve(
            self._job,
            attempt=self._attempt,
            requested_usd=self._max_cost_per_call,
            now=now,
        )
        return reservation, pricing_quote

    async def _finalize(
        self,
        reservation: FactoryBudgetReservationV1,
        usage: RequestUsage,
        pricing_quote: FactoryPricingQuoteV1,
    ) -> None:
        ended_at = self._clock()
        cost = pricing_quote.cost(usage)
        evidence_ref = await self._evidence_store.persist(
            self._job,
            json.dumps(
                {
                    "schema": "captain.factory-provider-usage.v1",
                    "provider": self._provider,
                    "model": self._model,
                    "input_units": usage.prompt_tokens,
                    "output_units": usage.completion_tokens,
                    "cost_usd": str(cost),
                    "reservation_id": str(reservation.reservation_id),
                    "job_id": str(self._job.job_id),
                    "correlation_id": str(self._job.correlation_id),
                    "attempt": self._attempt,
                    "execution_policy_sha256": pricing_quote.execution_policy_sha256,
                    "pricing_quote_id": pricing_quote.quote_id,
                    "pricing_version": pricing_quote.version,
                    "pricing_effective_at": pricing_quote.effective_at.isoformat(),
                    "pricing_evidence_sha256": pricing_quote.evidence_ref.sha256,
                    "pricing_evidence_uri": pricing_quote.evidence_ref.uri,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        receipt = FactoryUsageReceiptV1(
            schema_name="captain.factory-usage-receipt.v1",
            receipt_id=uuid5(
                NAMESPACE_URL,
                f"factory-provider-usage|{reservation.reservation_id}",
            ),
            reservation_id=reservation.reservation_id,
            job_id=self._job.job_id,
            correlation_id=self._job.correlation_id,
            attempt=self._attempt,
            provider=self._provider,
            model=self._model,
            input_units=usage.prompt_tokens,
            output_units=usage.completion_tokens,
            cost_usd=cost,
            started_at=reservation.reserved_at,
            ended_at=ended_at,
            evidence_ref=evidence_ref,
        )
        self._budget.record_usage(self._job, reservation, receipt)
        self._usage_receipts.append(receipt)
        self._provider_effects_with_unknown_usage.discard(reservation.reservation_id)

    async def close(self) -> None:
        await self._delegate.close()

    def actual_usage(self) -> RequestUsage:
        return self._delegate.actual_usage()

    def total_usage(self) -> RequestUsage:
        return self._delegate.total_usage()

    def count_tokens(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Tool | ToolSchema] = [],
    ) -> int:
        return self._delegate.count_tokens(messages, tools=tools)

    def remaining_tokens(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Tool | ToolSchema] = [],
    ) -> int:
        return self._delegate.remaining_tokens(messages, tools=tools)

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._delegate.capabilities

    @property
    def model_info(self) -> ModelInfo:
        return self._delegate.model_info


class FactoryHandoffEvidenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    from_agent: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    to_agent: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    evidence_ref: ArtifactRef


class ResolvedFactoryHoldoutCase(BaseModel):
    """Private holdout body returned only to the host runner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: PrivateHoldoutRef
    body: bytes = Field(min_length=1)

    @model_validator(mode="after")
    def require_body_digest(self) -> "ResolvedFactoryHoldoutCase":
        if hashlib.sha256(self.body).hexdigest() != self.reference.sha256:
            raise ValueError("private holdout body does not match its Captain digest")
        return self


class FactoryHoldoutAssertionDecisionV1(BaseModel):
    """One typed, redacted Captain-side holdout decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assertion_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    passed: StrictBool
    provenance_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")


class FactoryHoldoutEvaluationReceiptV1(BaseModel):
    """Redacted evaluator receipt; never contains the holdout body or agent prose."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_name: Literal["captain.factory-holdout-evaluation-receipt.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    holdout_ref: PrivateHoldoutRef
    candidate_ref: ArtifactRef
    assertion_ids: tuple[str, ...] = Field(min_length=1)
    decisions: tuple[FactoryHoldoutAssertionDecisionV1, ...] = Field(min_length=1)
    evaluator_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    evaluator_version: str = Field(min_length=1, max_length=64)
    evaluated_at: datetime

    @field_validator("evaluated_at")
    @classmethod
    def require_utc_evaluated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("holdout evaluation time must be UTC")
        return value

    @model_validator(mode="after")
    def require_exact_decisions(self) -> "FactoryHoldoutEvaluationReceiptV1":
        decision_ids = tuple(item.assertion_id for item in self.decisions)
        if decision_ids != self.assertion_ids or len(decision_ids) != len(
            set(decision_ids)
        ):
            raise ValueError("holdout receipt decisions must exactly match assertion IDs")
        return self


class FactoryHoldoutEvaluatorPort(Protocol):
    async def resolve(
        self,
        reference: PrivateHoldoutRef,
    ) -> ResolvedFactoryHoldoutCase: ...

    async def evaluate(
        self,
        reference: PrivateHoldoutRef,
        result: TaskResult,
        assertion_ids: tuple[str, ...],
    ) -> FactoryHoldoutEvaluationReceiptV1: ...


class FactoryToolExecutionEvidenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    status: Literal["succeeded", "failed"]
    evidence_ref: ArtifactRef


class FactoryN8nExecutionEvidenceV1(BaseModel):
    """Observed n8n effect authorized by its own Captain runtime grant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    approved_tool_ref: OpaqueN8nToolReference
    runtime_command: AgentRuntimeCommand
    capability_grant: CapabilityGrant
    runtime_result: AgentRuntimeResult
    mcp_call_id: str = Field(min_length=1, max_length=128)
    workflow_ref: ArtifactRef
    execution: N8nExecutionEvidence
    evidence_ref: ArtifactRef

    @model_validator(mode="after")
    def require_scoped_execution(self) -> "FactoryN8nExecutionEvidenceV1":
        command = self.runtime_command
        grant = self.capability_grant
        runtime = self.runtime_result
        if (
            self.approved_tool_ref.tool_name != self.tool_name
            or command.subject_id != self.approved_tool_ref.tool_name
            or command.payload.subtask_id != self.approved_tool_ref.tool_name
            or command.payload.prompt_ref.sha256
            != _opaque_n8n_tool_reference_digest(self.approved_tool_ref)
            or grant.profile is not CapabilityProfile.N8N_BUILDER
            or "mcp.n8n" not in grant.capabilities
            or grant.mcp_servers != ("n8n-mcp",)
            or command.event_id != grant.command_id
            or command.payload.capability_profile is not CapabilityProfile.N8N_BUILDER
            or command.payload.integration_intent is not IntegrationIntent.N8N
            or command.payload.batch_id != grant.batch_id
            or command.payload.subtask_id != grant.subtask_id
            or command.payload.workspace_ref != grant.workspace_ref
            or runtime.grant_id != grant.grant_id
            or runtime.command_id != command.event_id
            or runtime.correlation_id != command.correlation_id
            or runtime.subject_id != command.subject_id
            or runtime.subject_version != command.subject_version
            or runtime.operation is not command.payload.operation
            or self.workflow_ref.sha256 != self.execution.artifact_digest
            or not self.execution.execution_id.strip()
            or not self.execution.workflow_id.strip()
            or not self.mcp_call_id.strip()
            or self.execution.correlation_id != str(command.correlation_id)
        ):
            raise ValueError("n8n execution evidence is not scoped or digest-matched")
        return self


class FactoryN8nToolAuthorizationV1(BaseModel):
    """The exact Captain command/grant claim checked before one n8n call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    approved_tool_ref: OpaqueN8nToolReference
    runtime_command: AgentRuntimeCommand
    capability_grant: CapabilityGrant

    @model_validator(mode="after")
    def require_captain_work_node_binding(self) -> "FactoryN8nToolAuthorizationV1":
        if (
            self.approved_tool_ref.tool_name != self.tool_name
            or self.runtime_command.subject_id != self.approved_tool_ref.tool_name
            or self.runtime_command.payload.subtask_id
            != self.approved_tool_ref.tool_name
            or self.runtime_command.payload.prompt_ref.sha256
            != _opaque_n8n_tool_reference_digest(self.approved_tool_ref)
        ):
            raise ValueError(
                "n8n authorization does not match the Captain-approved work node"
            )
        return self


def _opaque_n8n_tool_reference_digest(
    reference: OpaqueN8nToolReference,
) -> str:
    encoded = json.dumps(
        reference.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class FactoryN8nToolAdapterPort(Protocol):
    """Host-owned n8n tool plus a fresh authorization claim for every call."""

    def tool(self, name: str) -> BaseTool[BaseModel, Any]: ...

    def authorization(self, name: str) -> FactoryN8nToolAuthorizationV1: ...

    def observed_evidence(self) -> tuple[FactoryN8nExecutionEvidenceV1, ...]: ...


class FactoryN8nGrantStatePort(Protocol):
    async def get_grant(self, command_id: UUID) -> CapabilityGrant | None: ...

    async def get_grant_revocation(
        self,
        command_id: UUID,
    ) -> CapabilityGrantRevocation | None: ...


class CaptainN8nGrantAuthority:
    """Resolve the canonical grant and revocation state before accepting n8n evidence."""

    def __init__(self, state: FactoryN8nGrantStatePort) -> None:
        self._state = state

    async def authorize(
        self,
        evidence: FactoryN8nExecutionEvidenceV1,
        *,
        now: datetime,
    ) -> CapabilityGrant:
        claim = FactoryN8nToolAuthorizationV1(
            tool_name=evidence.tool_name,
            approved_tool_ref=evidence.approved_tool_ref,
            runtime_command=evidence.runtime_command,
            capability_grant=evidence.capability_grant,
        )
        return await self.authorize_command(claim, now=now)

    async def authorize_command(
        self,
        claim: FactoryN8nToolAuthorizationV1,
        *,
        now: datetime,
    ) -> CapabilityGrant:
        stored = await self._state.get_grant(claim.runtime_command.event_id)
        if stored is None or stored != claim.capability_grant:
            raise ValueError("n8n grant is unknown or not canonical")
        revocation = await self._state.get_grant_revocation(
            claim.runtime_command.event_id
        )
        return validate_grant(
            stored,
            claim.runtime_command,
            now,
            revocation,
        )


class FactoryN8nGrantAuthorityPort(Protocol):
    async def authorize_command(
        self,
        claim: FactoryN8nToolAuthorizationV1,
        *,
        now: datetime,
    ) -> CapabilityGrant: ...

    async def authorize(
        self,
        evidence: FactoryN8nExecutionEvidenceV1,
        *,
        now: datetime,
    ) -> CapabilityGrant: ...


class CaptainAuthorizedN8nTool(BaseTool[BaseModel, Any]):
    """Revalidate Captain authority immediately before the external tool effect."""

    def __init__(
        self,
        *,
        name: str,
        approved_tool_ref: OpaqueN8nToolReference,
        adapter: FactoryN8nToolAdapterPort,
        authority: FactoryN8nGrantAuthorityPort,
        clock: Callable[[], datetime],
    ) -> None:
        delegate = adapter.tool(name)
        super().__init__(
            delegate.args_type(),
            delegate.return_type(),
            delegate.name,
            delegate.description,
        )
        self._name_from_manifest = name
        self._approved_tool_ref_from_manifest = approved_tool_ref
        self._adapter = adapter
        self._delegate = delegate
        self._authority = authority
        self._clock = clock

    async def run(
        self,
        args: BaseModel,
        cancellation_token: CancellationToken,
    ) -> Any:
        raw_claim = self._adapter.authorization(self._name_from_manifest)
        claim = FactoryN8nToolAuthorizationV1.model_validate(
            raw_claim.model_dump(mode="python")
        )
        if (
            claim.tool_name != self._name_from_manifest
            or claim.approved_tool_ref != self._approved_tool_ref_from_manifest
        ):
            raise ValueError("n8n authorization claim belongs to a different tool")
        await self._authority.authorize_command(claim, now=self._clock())
        return await self._delegate.run(args, cancellation_token)


class FactoryTeamRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["succeeded", "failed", "unresolved"]
    runtime_result: AgentRuntimeResult
    execution_outcome: ExecutionOutcomeV1
    usage_receipts: tuple[FactoryUsageReceiptV1, ...]
    handoff_evidence_refs: tuple[ArtifactRef, ...] = ()
    tool_evidence_refs: tuple[ArtifactRef, ...] = ()
    workflow_evidence_refs: tuple[ArtifactRef, ...] = ()
    handoffs: tuple[FactoryHandoffEvidenceV1, ...] = ()
    tool_executions: tuple[FactoryToolExecutionEvidenceV1, ...] = ()
    n8n_executions: tuple[FactoryN8nExecutionEvidenceV1, ...] = ()
    conversation_pattern: Literal[
        "swarm",
        "selector_group_chat",
        "round_robin_group_chat",
        "single_agent",
    ]
    message_count: int = Field(ge=0, strict=True)
    handoff_count: int = Field(ge=0, strict=True)
    tool_call_count: int = Field(ge=0, strict=True)
    termination_reason: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def require_receipt_for_success(self) -> "FactoryTeamRunResult":
        if self.status == "succeeded" and not self.usage_receipts:
            raise ValueError("successful live run requires a usage receipt")
        if self.handoffs and tuple(item.evidence_ref for item in self.handoffs) != self.handoff_evidence_refs:
            raise ValueError("typed handoff evidence refs do not match")
        typed_tool_refs = tuple(item.evidence_ref for item in self.tool_executions)
        if typed_tool_refs and not set(typed_tool_refs).issubset(self.tool_evidence_refs):
            raise ValueError("typed tool evidence refs do not match")
        if self.n8n_executions and not {
            item.evidence_ref for item in self.n8n_executions
        }.issubset(self.workflow_evidence_refs):
            raise ValueError("n8n execution evidence refs do not match")
        if len({item.mcp_call_id for item in self.n8n_executions}) != len(
            self.n8n_executions
        ) or len({item.execution.execution_id for item in self.n8n_executions}) != len(
            self.n8n_executions
        ):
            raise ValueError("n8n MCP call and execution IDs must be unique")
        return self


class _FactoryActivityCeilingTermination(TerminationCondition):
    def __init__(
        self,
        *,
        max_handoffs: int,
        max_tool_calls: int,
        handoff_tool_names: Sequence[str] = (),
    ) -> None:
        self._max_handoffs = max_handoffs
        self._max_tool_calls = max_tool_calls
        self._handoff_tool_names = frozenset(handoff_tool_names)
        self._handoffs = 0
        self._tool_calls = 0
        self._terminated = False

    @property
    def terminated(self) -> bool:
        return self._terminated

    async def __call__(
        self,
        messages: Sequence[BaseAgentEvent | BaseChatMessage],
    ) -> StopMessage | None:
        self._handoffs += sum(isinstance(item, HandoffMessage) for item in messages)
        self._tool_calls += sum(
            sum(
                execution.name not in self._handoff_tool_names
                for execution in item.content
            )
            for item in messages
            if isinstance(item, ToolCallExecutionEvent)
        )
        reason = None
        if self._handoffs > self._max_handoffs and self._max_handoffs > 0:
            reason = "max_handoffs"
        if self._tool_calls > self._max_tool_calls and self._max_tool_calls > 0:
            reason = "max_tool_calls"
        if reason is None:
            return None
        self._terminated = True
        return StopMessage(source="factory_host", content=reason)

    async def reset(self) -> None:
        self._handoffs = 0
        self._tool_calls = 0
        self._terminated = False


class _FactoryTaskCompletedTermination(TerminationCondition):
    _TERMINAL_MARKERS = (
        "TERMINATE",
        "captain.business-benchmark-terminal.v1",
    )

    def __init__(self, *, require_handoff: bool) -> None:
        self._require_handoff = require_handoff
        self._handoff_targets: set[str] = set()
        self._terminated = False

    @property
    def terminated(self) -> bool:
        return self._terminated

    async def __call__(
        self,
        messages: Sequence[BaseAgentEvent | BaseChatMessage],
    ) -> StopMessage | None:
        if self._terminated:
            raise TerminatedException("Termination condition has already been reached")
        self._handoff_targets.update(
            item.target for item in messages if isinstance(item, HandoffMessage)
        )
        for message in messages:
            if isinstance(message, HandoffMessage):
                continue
            if self._require_handoff and message.source not in self._handoff_targets:
                continue
            content = message.to_text()
            if any(marker in content for marker in self._TERMINAL_MARKERS):
                self._terminated = True
                return StopMessage(source="factory_host", content="task_completed")
        return None

    async def reset(self) -> None:
        self._handoff_targets.clear()
        self._terminated = False


class _FactoryToolCallCounter:
    def __init__(self, maximum: int) -> None:
        self._maximum = maximum
        self.count = 0

    def claim(self) -> None:
        if self.count >= self._maximum:
            raise RuntimeError("factory global tool-call ceiling reached")
        self.count += 1


class _FactoryCappedTool(BaseTool[BaseModel, Any]):
    def __init__(self, delegate: BaseTool[BaseModel, Any], counter: _FactoryToolCallCounter) -> None:
        super().__init__(
            delegate.args_type(),
            delegate.return_type(),
            delegate.name,
            delegate.description,
        )
        self._delegate = delegate
        self._counter = counter

    async def run(
        self,
        args: BaseModel,
        cancellation_token: CancellationToken,
    ) -> Any:
        self._counter.claim()
        return await self._delegate.run(args, cancellation_token)


class CandidatePreflightPort(Protocol):
    def validate(
        self,
        candidate: ResolvedFactoryCandidate,
        max_seconds: float,
    ) -> FactoryCandidateEvaluationResult: ...


class FactoryTeamRunner(Protocol):
    async def run(
        self,
        *,
        job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
        candidate: ResolvedFactoryCandidate,
        case_ref: PrivateHoldoutRef,
        lease: FactoryLease,
        allowed_models: tuple[str, ...],
        max_seconds: float,
    ) -> FactoryTeamRunResult: ...


class HostAutoGenSessionIdentityV1(BaseModel):
    """Complete Captain identity bound to one provider-backed AutoGen session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["captain.host-autogen-session-identity.v1"] = (
        "captain.host-autogen-session-identity.v1"
    )
    job_id: UUID
    correlation_id: UUID
    subject_id: str = Field(min_length=1, max_length=128)
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, strict=True)
    invocation_id: UUID
    request_id: UUID
    runtime_session_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"
    )
    case_id: str = Field(min_length=1, max_length=128)
    case_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    variant: Literal["candidate", "single_agent_baseline"]
    effect_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    claim_id: UUID
    fence: int = Field(ge=1, strict=True)
    model: str = Field(min_length=1, max_length=128)
    execution_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def for_factory_execution(
        cls,
        *,
        job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
        case_ref: PrivateHoldoutRef,
        subject_id: str,
        variant: Literal["candidate", "single_agent_baseline"],
        request_id: UUID,
        runtime_session_id: str,
        effect_id: str,
        claim_id: UUID,
        fence: int,
        model: str,
    ) -> "HostAutoGenSessionIdentityV1":
        return cls(
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            subject_id=subject_id,
            subject_version=job.subject_version,
            attempt=invocation.attempt,
            invocation_id=invocation.invocation_id,
            request_id=request_id,
            runtime_session_id=runtime_session_id,
            case_id=case_ref.holdout_id,
            case_sha256=case_ref.sha256,
            variant=variant,
            effect_id=effect_id,
            claim_id=claim_id,
            fence=fence,
            model=model,
            execution_policy_sha256=_execution_policy_digest(job),
        )


class SealedSingleAgentPolicyV1(BaseModel):
    """Digest-bound baseline policy with no team, routing, grant, or publish authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["captain.sealed-single-agent-policy.v1"] = (
        "captain.sealed-single-agent-policy.v1"
    )
    agent_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    system_prompt_ref: ArtifactRef
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: str = Field(min_length=1, max_length=128)
    allowed_tools: tuple[str, ...] = ()
    max_messages: int = Field(ge=1, le=100, strict=True)
    max_tool_calls: int = Field(ge=0, le=100, strict=True)
    team_manifest_ref: None = None
    routing_authority: Literal[False] = False
    publication_authority: Literal[False] = False
    grant_authority: Literal[False] = False
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("allowed_tools")
    @classmethod
    def require_unique_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("baseline tools must be unique named entries")
        return value

    @model_validator(mode="after")
    def require_digest_binding(self) -> "SealedSingleAgentPolicyV1":
        if self.prompt_sha256 != self.system_prompt_ref.sha256:
            raise ValueError("baseline prompt digest does not match its sealed reference")
        if self.policy_sha256 != _single_agent_policy_digest(self):
            raise ValueError("baseline policy digest does not match its sealed content")
        return self

    @classmethod
    def seal(
        cls,
        *,
        agent_name: str,
        system_prompt_ref: ArtifactRef,
        execution_policy_sha256: str,
        model: str,
        allowed_tools: tuple[str, ...],
        max_messages: int,
        max_tool_calls: int,
    ) -> "SealedSingleAgentPolicyV1":
        values: dict[str, object] = {
            "schema_name": "captain.sealed-single-agent-policy.v1",
            "agent_name": agent_name,
            "system_prompt_ref": system_prompt_ref,
            "prompt_sha256": system_prompt_ref.sha256,
            "execution_policy_sha256": execution_policy_sha256,
            "model": model,
            "allowed_tools": allowed_tools,
            "max_messages": max_messages,
            "max_tool_calls": max_tool_calls,
            "team_manifest_ref": None,
            "routing_authority": False,
            "publication_authority": False,
            "grant_authority": False,
        }
        values["policy_sha256"] = _single_agent_policy_digest(values)
        return cls.model_validate(values)


class HostAutoGenSessionResult(BaseModel):
    """Redacted host observation plus the in-process result for private evaluation."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        arbitrary_types_allowed=True,
    )

    task_result: TaskResult = Field(repr=False, exclude=True)
    runtime_evidence_ref: ArtifactRef
    usage_receipts: tuple[FactoryUsageReceiptV1, ...]
    handoffs: tuple[FactoryHandoffEvidenceV1, ...] = ()
    tool_executions: tuple[FactoryToolExecutionEvidenceV1, ...] = ()
    n8n_executions: tuple[FactoryN8nExecutionEvidenceV1, ...] = ()
    workflow_evidence_refs: tuple[ArtifactRef, ...] = ()
    conversation_pattern: Literal[
        "swarm",
        "selector_group_chat",
        "round_robin_group_chat",
        "single_agent",
    ]
    message_count: int = Field(ge=0, strict=True)
    handoff_count: int = Field(ge=0, strict=True)
    tool_call_count: int = Field(ge=0, strict=True)
    termination_reason: str = Field(min_length=1, max_length=200)
    provider_started: StrictBool
    provider_usage_unresolved: StrictBool
    human_handoff_completed: Literal[False] = False

    @model_validator(mode="after")
    def require_redacted_evidence_consistency(self) -> "HostAutoGenSessionResult":
        if self.handoff_count != len(self.handoffs):
            raise ValueError("session handoff count does not match typed evidence")
        if self.tool_call_count != len(self.tool_executions):
            raise ValueError("session tool count does not match typed evidence")
        if self.n8n_executions and not {
            item.evidence_ref for item in self.n8n_executions
        }.issubset(self.workflow_evidence_refs):
            raise ValueError("session n8n evidence refs do not match")
        return self


class _HostAutoGenSessionInterruption:
    identity: HostAutoGenSessionIdentityV1
    provider_started: bool
    provider_usage_unresolved: bool
    usage_receipts: tuple[FactoryUsageReceiptV1, ...]

    def _capture_session_state(
        self,
        *,
        identity: HostAutoGenSessionIdentityV1,
        model_client: BudgetedChatCompletionClient,
        receipt_offset: int,
        dispatch_offset: int,
        unresolved_before: frozenset[UUID],
    ) -> None:
        self.identity = identity
        self.provider_started = model_client.provider_dispatch_count > dispatch_offset
        self.provider_usage_unresolved = bool(
            model_client.unresolved_reservation_ids - unresolved_before
        )
        self.usage_receipts = model_client.usage_receipts[receipt_offset:]


class HostAutoGenSessionTimeoutError(
    _HostAutoGenSessionInterruption,
    asyncio.TimeoutError,
):
    """Timed-out session retaining the exact paid-effect reconciliation state."""

    def __init__(
        self,
        *,
        identity: HostAutoGenSessionIdentityV1,
        model_client: BudgetedChatCompletionClient,
        receipt_offset: int,
        dispatch_offset: int,
        unresolved_before: frozenset[UUID],
    ) -> None:
        asyncio.TimeoutError.__init__(self, "host AutoGen session timed out")
        self._capture_session_state(
            identity=identity,
            model_client=model_client,
            receipt_offset=receipt_offset,
            dispatch_offset=dispatch_offset,
            unresolved_before=unresolved_before,
        )


class HostAutoGenSessionCancelledError(
    _HostAutoGenSessionInterruption,
    asyncio.CancelledError,
):
    """Cancelled session retaining the exact paid-effect reconciliation state."""

    def __init__(
        self,
        *,
        identity: HostAutoGenSessionIdentityV1,
        model_client: BudgetedChatCompletionClient,
        receipt_offset: int,
        dispatch_offset: int,
        unresolved_before: frozenset[UUID],
    ) -> None:
        asyncio.CancelledError.__init__(self, "host AutoGen session cancelled")
        self._capture_session_state(
            identity=identity,
            model_client=model_client,
            receipt_offset=receipt_offset,
            dispatch_offset=dispatch_offset,
            unresolved_before=unresolved_before,
        )


@dataclass(frozen=True)
class AuthorityFreeBaselineTool:
    """Explicitly non-authoritative callable available to the fair baseline."""

    name: str
    function: Callable[..., Any]
    integration_intent: Literal[IntegrationIntent.NONE] = IntegrationIntent.NONE
    mutates_external_state: Literal[False] = False
    routing_authority: Literal[False] = False
    publication_authority: Literal[False] = False
    grant_authority: Literal[False] = False

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "a").isalnum():
            raise ValueError("authority-free baseline tool name is invalid")
        if not callable(self.function):
            raise ValueError("authority-free baseline tool must be callable")
        if (
            self.integration_intent is not IntegrationIntent.NONE
            or self.mutates_external_state
            or self.routing_authority
            or self.publication_authority
            or self.grant_authority
        ):
            raise ValueError("baseline tool must be authority-free")


class HostAutoGenSessionExecutor:
    """Execute candidate or authority-free baseline sessions under one host boundary."""

    def __init__(
        self,
        *,
        model_client: BudgetedChatCompletionClient | None = None,
        model_client_factory: Callable[
            [HostAutoGenSessionIdentityV1], BudgetedChatCompletionClient
        ]
        | None = None,
        evidence_store: FactoryEvidenceStore,
        holdouts: FactoryHoldoutEvaluatorPort,
        tools: Mapping[str, Callable[..., Any]],
        baseline_tools: Mapping[str, AuthorityFreeBaselineTool] | None = None,
        baseline_n8n_tools: Mapping[str, OpaqueN8nToolReference] | None = None,
        evaluator: FactoryCandidateEvaluator | None = None,
        n8n_adapter: FactoryN8nToolAdapterPort | None = None,
        n8n_authority: FactoryN8nGrantAuthorityPort | None = None,
        clock: Callable[[], datetime],
    ) -> None:
        if (model_client is None) == (model_client_factory is None):
            raise ValueError("provide exactly one fixed model client or session factory")
        self._fixed_model_client = model_client
        self._model_client_factory = model_client_factory
        self._evidence_store = evidence_store
        self._holdouts = holdouts
        self._tools = dict(tools)
        self._baseline_tools = {
            name: tool.function for name, tool in (baseline_tools or {}).items()
        }
        if any(name != tool.name for name, tool in (baseline_tools or {}).items()):
            raise ValueError("baseline tool registry key does not match sealed tool")
        self._baseline_n8n_tools = dict(baseline_n8n_tools or {})
        if any(
            name != reference.tool_name
            for name, reference in self._baseline_n8n_tools.items()
        ):
            raise ValueError("baseline n8n tool registry key does not match opaque ref")
        if set(self._baseline_tools).intersection(self._baseline_n8n_tools):
            raise ValueError("baseline tool cannot have ambiguous authority")
        self._evaluator = evaluator or FactoryCandidateEvaluator()
        self._n8n_adapter = n8n_adapter
        self._n8n_authority = n8n_authority
        self._clock = clock
        lock_resources: set[tuple[str, int]] = set()
        if n8n_adapter is not None:
            lock_resources.add(("n8n_adapter", id(n8n_adapter)))
        if model_client is not None:
            lock_resources.add(("model_client", id(model_client)))
        if not lock_resources:
            lock_resources.add(("executor", id(self)))
        self._session_locks = tuple(
            _HOST_SESSION_RESOURCE_LOCKS.setdefault(resource, asyncio.Lock())
            for resource in sorted(lock_resources)
        )

    async def run_baseline(
        self,
        *,
        job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
        case_ref: PrivateHoldoutRef,
        identity: HostAutoGenSessionIdentityV1,
        policy: SealedSingleAgentPolicyV1,
        workspace: Path,
        allowed_models: tuple[str, ...],
        max_seconds: float,
    ) -> HostAutoGenSessionResult:
        self._validate_identity(
            job=job,
            invocation=invocation,
            case_ref=case_ref,
            identity=identity,
            subject_id="single_agent_baseline",
            variant="single_agent_baseline",
        )
        if self._model_client_factory is None:
            raise ValueError("baseline execution requires a fresh model client factory")
        model_client = self._client_for(identity)
        if (
            policy.execution_policy_sha256 != _execution_policy_digest(job)
            or policy.model != identity.model
            or policy.model != model_client.model
            or policy.model not in allowed_models
        ):
            raise ValueError("baseline policy is not bound to the current execution policy")
        resolved_tools = self._resolve_tools(
            policy.allowed_tools,
            maximum=policy.max_tool_calls,
            n8n_tools=self._baseline_n8n_tools,
            registry=self._baseline_tools,
        )
        agent = AssistantAgent(
            name=policy.agent_name,
            model_client=model_client,
            tools=[resolved_tools[name] for name in policy.allowed_tools],
            handoffs=[],
            model_context=BufferedChatCompletionContext(
                buffer_size=policy.max_messages
            ),
            system_message=_sealed_text(workspace, policy.system_prompt_ref),
            max_tool_iterations=max(1, policy.max_tool_calls),
        )
        task = await self._resolve_task(case_ref)
        cancellation_token = CancellationToken()
        receipt_offset = len(model_client.usage_receipts)
        dispatch_offset = model_client.provider_dispatch_count
        unresolved_before = model_client.unresolved_reservation_ids
        n8n_evidence_offset = (
            len(self._n8n_adapter.observed_evidence())
            if self._n8n_adapter is not None
            else 0
        )
        try:
            result = await asyncio.wait_for(
                agent.run(task=task, cancellation_token=cancellation_token),
                timeout=max_seconds,
            )
        except asyncio.TimeoutError as exc:
            cancellation_token.cancel()
            raise HostAutoGenSessionTimeoutError(
                identity=identity,
                model_client=model_client,
                receipt_offset=receipt_offset,
                dispatch_offset=dispatch_offset,
                unresolved_before=unresolved_before,
            ) from exc
        except asyncio.CancelledError as exc:
            cancellation_token.cancel()
            raise HostAutoGenSessionCancelledError(
                identity=identity,
                model_client=model_client,
                receipt_offset=receipt_offset,
                dispatch_offset=dispatch_offset,
                unresolved_before=unresolved_before,
            ) from exc
        return await self._observe(
            job=job,
            identity=identity,
            model_client=model_client,
            result=result,
            receipt_offset=receipt_offset,
            dispatch_offset=dispatch_offset,
            unresolved_before=unresolved_before,
            conversation_pattern="single_agent",
            max_messages=policy.max_messages,
            max_handoffs=0,
            max_tool_calls=policy.max_tool_calls,
            termination_conditions=("task_completed", "max_messages", "max_tool_calls"),
            handoff_tool_names=(),
            n8n_tools={
                name: self._baseline_n8n_tools[name]
                for name in policy.allowed_tools
                if name in self._baseline_n8n_tools
            },
            n8n_evidence_offset=n8n_evidence_offset,
        )

    async def run_candidate(
        self,
        *,
        job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
        case_ref: PrivateHoldoutRef,
        identity: HostAutoGenSessionIdentityV1,
        candidate: ResolvedFactoryCandidate,
        manifest: FactoryAutoGenTeamManifestV1 | None = None,
        allowed_tools: tuple[str, ...] | None = None,
        allowed_models: tuple[str, ...],
        max_seconds: float,
    ) -> HostAutoGenSessionResult:
        expected_manifest = manifest
        for lock in self._session_locks:
            await lock.acquire()
        try:
            with self._evaluator.verified_team_workspace(candidate) as (
                workspace,
                verified_manifest,
            ):
                return await self._run_candidate_session(
                    job=job,
                    invocation=invocation,
                    case_ref=case_ref,
                    identity=identity,
                    workspace=workspace,
                    manifest=verified_manifest,
                    expected_manifest=expected_manifest,
                    candidate=candidate,
                    allowed_tools=allowed_tools,
                    allowed_models=allowed_models,
                    max_seconds=max_seconds,
                )
        finally:
            for lock in reversed(self._session_locks):
                lock.release()

    async def _run_candidate_session(
        self,
        *,
        job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
        case_ref: PrivateHoldoutRef,
        identity: HostAutoGenSessionIdentityV1,
        workspace: Path,
        manifest: FactoryAutoGenTeamManifestV1,
        expected_manifest: FactoryAutoGenTeamManifestV1 | None,
        candidate: ResolvedFactoryCandidate,
        allowed_tools: tuple[str, ...] | None,
        allowed_models: tuple[str, ...],
        max_seconds: float,
    ) -> HostAutoGenSessionResult:
        if expected_manifest is not None and manifest != expected_manifest:
            raise ValueError("verified candidate manifest changed after runtime preflight")
        self._validate_identity(
            job=job,
            invocation=invocation,
            case_ref=case_ref,
            identity=identity,
            subject_id=candidate.candidate.candidate_id,
            variant="candidate",
        )
        model_client = self._client_for(identity)
        if model_client.model != identity.model or model_client.model not in allowed_models:
            raise ValueError("host model client is not allowed by the factory job")
        declared_tools = tuple(
            dict.fromkeys(tool for agent in manifest.agents for tool in agent.tools)
        )
        if allowed_tools is None:
            required_tools = declared_tools
        else:
            allowed_set = set(allowed_tools)
            if (
                len(allowed_tools) != len(allowed_set)
                or allowed_set - set(declared_tools)
            ):
                raise ValueError(
                    "allowed tools must be a subset of the sealed candidate manifest"
                )
            required_tools = tuple(
                name for name in declared_tools if name in allowed_set
            )
        required_tool_names = set(required_tools)
        n8n_tools = {
            tool.name: tool.opaque_reference()
            for tool in candidate.candidate.n8n_tools
            if tool.name in required_tool_names
        }
        resolved_tools = self._resolve_tools(
            required_tools,
            maximum=manifest.max_tool_calls,
            n8n_tools=n8n_tools,
            registry=self._tools,
        )
        participants = [
            AssistantAgent(
                name=agent.name,
                model_client=model_client,
                tools=[
                    resolved_tools[name]
                    for name in agent.tools
                    if name in required_tool_names
                ],
                handoffs=list(agent.handoffs),
                model_context=(
                    BufferedChatCompletionContext(buffer_size=manifest.max_messages)
                    if manifest.memory_policy == "buffered"
                    else None
                ),
                system_message=_sealed_text(workspace, agent.system_prompt_ref),
                max_tool_iterations=max(1, manifest.max_tool_calls),
            )
            for agent in manifest.agents
        ]
        handoff_tool_names = tuple(
            f"transfer_to_{target}"
            for agent in manifest.agents
            for target in agent.handoffs
        )
        termination: TerminationCondition = MaxMessageTermination(
            manifest.max_messages,
            include_agent_event=True,
        )
        termination = termination | _FactoryActivityCeilingTermination(
            max_handoffs=manifest.max_handoffs,
            max_tool_calls=manifest.max_tool_calls,
            handoff_tool_names=handoff_tool_names,
        )
        if "task_completed" in manifest.termination_conditions:
            termination = (
                _FactoryTaskCompletedTermination(
                    require_handoff=any(agent.handoffs for agent in manifest.agents)
                )
                | termination
            )
        team: Swarm | SelectorGroupChat | RoundRobinGroupChat | None = None
        if manifest.conversation_pattern == "swarm":
            team = Swarm(
                participants,
                termination_condition=termination,
                max_turns=manifest.max_messages,
            )
        elif manifest.conversation_pattern == "selector_group_chat":
            team = SelectorGroupChat(
                participants,
                model_client=model_client,
                termination_condition=termination,
                max_turns=manifest.max_messages,
            )
        elif manifest.conversation_pattern == "round_robin_group_chat":
            team = RoundRobinGroupChat(
                participants,
                termination_condition=termination,
                max_turns=manifest.max_messages,
            )
        task = await self._resolve_task(case_ref)
        cancellation_token = CancellationToken()
        receipt_offset = len(model_client.usage_receipts)
        dispatch_offset = model_client.provider_dispatch_count
        unresolved_before = model_client.unresolved_reservation_ids
        n8n_evidence_offset = (
            len(self._n8n_adapter.observed_evidence())
            if self._n8n_adapter is not None
            else 0
        )
        try:
            if manifest.conversation_pattern == "single_agent":
                if len(participants) != 1:
                    raise ValueError("single-agent topology requires exactly one participant")
                result = await asyncio.wait_for(
                    participants[0].run(
                        task=task,
                        cancellation_token=cancellation_token,
                    ),
                    timeout=max_seconds,
                )
            else:
                assert team is not None
                result = await asyncio.wait_for(
                    team.run(task=task, cancellation_token=cancellation_token),
                    timeout=max_seconds,
                )
        except asyncio.TimeoutError as exc:
            cancellation_token.cancel()
            raise HostAutoGenSessionTimeoutError(
                identity=identity,
                model_client=model_client,
                receipt_offset=receipt_offset,
                dispatch_offset=dispatch_offset,
                unresolved_before=unresolved_before,
            ) from exc
        except asyncio.CancelledError as exc:
            cancellation_token.cancel()
            raise HostAutoGenSessionCancelledError(
                identity=identity,
                model_client=model_client,
                receipt_offset=receipt_offset,
                dispatch_offset=dispatch_offset,
                unresolved_before=unresolved_before,
            ) from exc
        return await self._observe(
            job=job,
            identity=identity,
            model_client=model_client,
            result=result,
            receipt_offset=receipt_offset,
            dispatch_offset=dispatch_offset,
            unresolved_before=unresolved_before,
            conversation_pattern=manifest.conversation_pattern,
            max_messages=manifest.max_messages,
            max_handoffs=manifest.max_handoffs,
            max_tool_calls=manifest.max_tool_calls,
            termination_conditions=manifest.termination_conditions,
            handoff_tool_names=handoff_tool_names,
            n8n_tools=n8n_tools,
            n8n_evidence_offset=n8n_evidence_offset,
        )

    async def _resolve_task(self, case_ref: PrivateHoldoutRef) -> str:
        private_case = await self._holdouts.resolve(case_ref)
        if private_case.reference != case_ref:
            raise ValueError("private holdout resolver returned a different reference")
        try:
            return private_case.body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("private holdout body must be UTF-8") from exc

    def _resolve_tools(
        self,
        required_tools: tuple[str, ...],
        *,
        maximum: int,
        n8n_tools: Mapping[str, OpaqueN8nToolReference],
        registry: Mapping[str, Callable[..., Any]],
    ) -> dict[str, BaseTool[BaseModel, Any]]:
        required_n8n_tools = set(required_tools).intersection(n8n_tools)
        if required_n8n_tools and (
            self._n8n_adapter is None or self._n8n_authority is None
        ):
            raise ValueError(
                "n8n tools require trusted n8n adapter and grant authority"
            )
        resolved: dict[str, Callable[..., Any] | BaseTool[BaseModel, Any]] = dict(
            registry
        )
        if self._n8n_adapter is not None:
            assert self._n8n_authority is not None or not required_n8n_tools
            for name in required_n8n_tools:
                raw_claim = self._n8n_adapter.authorization(name)
                claim = FactoryN8nToolAuthorizationV1.model_validate(
                    raw_claim.model_dump(mode="python")
                )
                if (
                    claim.tool_name != name
                    or claim.approved_tool_ref != n8n_tools[name]
                ):
                    raise ValueError(
                        "n8n authorization claim belongs to a different tool"
                    )
            resolved.update(
                {
                    name: CaptainAuthorizedN8nTool(
                        name=name,
                        approved_tool_ref=n8n_tools[name],
                        adapter=self._n8n_adapter,
                        authority=self._n8n_authority,
                        clock=self._clock,
                    )
                    for name in required_n8n_tools
                }
            )
        unknown_tools = set(required_tools) - set(resolved)
        if unknown_tools:
            raise ValueError(f"host tool is not registered: {sorted(unknown_tools)[0]}")
        counter = _FactoryToolCallCounter(maximum)
        capped: dict[str, BaseTool[BaseModel, Any]] = {}
        for name in required_tools:
            raw_tool = resolved[name]
            delegate = (
                raw_tool
                if isinstance(raw_tool, BaseTool)
                else FunctionTool(
                    raw_tool,
                    description=(raw_tool.__doc__ or f"Host tool {name}"),
                    name=name,
                )
            )
            capped[name] = _FactoryCappedTool(delegate, counter)
        return capped

    async def _observe(
        self,
        *,
        job: AgentFactoryJobV3,
        identity: HostAutoGenSessionIdentityV1,
        model_client: BudgetedChatCompletionClient,
        result: TaskResult,
        receipt_offset: int,
        dispatch_offset: int,
        unresolved_before: frozenset[UUID],
        conversation_pattern: Literal[
            "swarm", "selector_group_chat", "round_robin_group_chat", "single_agent"
        ],
        max_messages: int,
        max_handoffs: int,
        max_tool_calls: int,
        termination_conditions: tuple[str, ...],
        handoff_tool_names: tuple[str, ...],
        n8n_tools: Mapping[str, OpaqueN8nToolReference],
        n8n_evidence_offset: int,
    ) -> HostAutoGenSessionResult:
        handoff_messages = tuple(
            message for message in result.messages if isinstance(message, HandoffMessage)
        )
        tool_events = tuple(
            message
            for message in result.messages
            if isinstance(message, ToolCallExecutionEvent)
        )
        handoff_tool_name_set = frozenset(handoff_tool_names)
        tool_event_executions = tuple(
            (event, execution)
            for event in tool_events
            for execution in event.content
            if execution.name not in handoff_tool_name_set
        )
        tool_call_count = len(tool_event_executions)
        if len(result.messages) > max_messages:
            raise ValueError("AutoGen team exceeded the message ceiling")
        if len(handoff_messages) > max_handoffs:
            raise ValueError("AutoGen team exceeded the handoff ceiling")
        if tool_call_count > max_tool_calls:
            raise ValueError("AutoGen team exceeded the tool-call ceiling")
        termination_reason = _session_termination_reason(
            result,
            termination_conditions=termination_conditions,
            max_messages=max_messages,
        )
        usage_receipts = model_client.usage_receipts[receipt_offset:]
        if any(
            receipt.job_id != identity.job_id
            or receipt.correlation_id != identity.correlation_id
            or receipt.attempt != identity.attempt
            or receipt.model != identity.model
            for receipt in usage_receipts
        ):
            raise ValueError("usage receipt does not match session identity")
        observation_ref = await self._evidence_store.persist(
            job,
            json.dumps(
                {
                    "schema": "captain.factory-autogen-observation.v2",
                    "identity": identity.model_dump(mode="json"),
                    "conversation_pattern": conversation_pattern,
                    "message_count": len(result.messages),
                    "handoff_count": len(handoff_messages),
                    "tool_call_count": tool_call_count,
                    "termination_reason": termination_reason,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        handoffs: list[FactoryHandoffEvidenceV1] = []
        for ordinal, message in enumerate(handoff_messages, start=1):
            handoff_ref = await self._evidence_store.persist(
                job,
                json.dumps(
                    {
                        "schema": "captain.factory-autogen-handoff.v1",
                        "invocation_id": str(identity.invocation_id),
                        "runtime_session_id": identity.runtime_session_id,
                        "ordinal": ordinal,
                        "from_agent": message.source,
                        "to_agent": message.target,
                        "observation_ref": observation_ref.model_dump(mode="json"),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
            handoffs.append(
                FactoryHandoffEvidenceV1(
                    from_agent=message.source,
                    to_agent=message.target,
                    evidence_ref=handoff_ref,
                )
            )
        tool_executions: list[FactoryToolExecutionEvidenceV1] = []
        for ordinal, (event, execution) in enumerate(tool_event_executions, start=1):
            status: Literal["succeeded", "failed"] = (
                "failed" if execution.is_error else "succeeded"
            )
            tool_ref = await self._evidence_store.persist(
                job,
                json.dumps(
                    {
                        "schema": "captain.factory-autogen-tool-execution.v1",
                        "invocation_id": str(identity.invocation_id),
                        "runtime_session_id": identity.runtime_session_id,
                        "ordinal": ordinal,
                        "agent_name": event.source,
                        "tool_name": execution.name,
                        "status": status,
                        "observation_ref": observation_ref.model_dump(mode="json"),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
            tool_executions.append(
                FactoryToolExecutionEvidenceV1(
                    agent_name=event.source,
                    tool_name=execution.name,
                    status=status,
                    evidence_ref=tool_ref,
                )
            )
        all_n8n_evidence = (
            self._n8n_adapter.observed_evidence()
            if self._n8n_adapter is not None
            else ()
        )
        n8n_executions = all_n8n_evidence[n8n_evidence_offset:]
        if n8n_executions and self._n8n_authority is None:
            raise ValueError("n8n evidence requires trusted grant authority")
        for n8n_execution in n8n_executions:
            assert self._n8n_authority is not None
            if (
                n8n_execution.tool_name not in n8n_tools
                or n8n_execution.approved_tool_ref
                != n8n_tools.get(n8n_execution.tool_name)
                or n8n_execution.runtime_command.correlation_id
                != identity.correlation_id
                or n8n_execution.runtime_command.subject_version
                != identity.subject_version
                or n8n_execution.runtime_result.correlation_id
                != identity.correlation_id
                or n8n_execution.runtime_result.subject_version
                != identity.subject_version
                or n8n_execution.execution.correlation_id
                != str(identity.correlation_id)
            ):
                raise ValueError("n8n evidence does not match session identity")
            await self._n8n_authority.authorize(n8n_execution, now=self._clock())
        observed_n8n_calls = tuple(
            item for item in tool_executions if item.tool_name in n8n_tools
        )
        if len(n8n_executions) != len(observed_n8n_calls) or any(
            observed.tool_name != evidence.tool_name
            for observed, evidence in zip(observed_n8n_calls, n8n_executions)
        ):
            raise ValueError("n8n tool call is missing host-owned execution evidence")
        n8n_refs = _unique_refs(
            tuple(
                reference
                for item in n8n_executions
                for reference in (item.workflow_ref, item.evidence_ref)
            )
        )
        return HostAutoGenSessionResult(
            task_result=result,
            runtime_evidence_ref=observation_ref,
            usage_receipts=usage_receipts,
            handoffs=tuple(handoffs),
            tool_executions=tuple(tool_executions),
            n8n_executions=n8n_executions,
            workflow_evidence_refs=n8n_refs,
            conversation_pattern=conversation_pattern,
            message_count=len(result.messages),
            handoff_count=len(handoffs),
            tool_call_count=tool_call_count,
            termination_reason=termination_reason,
            provider_started=(
                model_client.provider_dispatch_count > dispatch_offset
            ),
            provider_usage_unresolved=bool(
                model_client.unresolved_reservation_ids - unresolved_before
            ),
            human_handoff_completed=False,
        )

    def _client_for(
        self, identity: HostAutoGenSessionIdentityV1
    ) -> BudgetedChatCompletionClient:
        if self._model_client_factory is not None:
            client = self._model_client_factory(identity)
        else:
            assert self._fixed_model_client is not None
            client = self._fixed_model_client
        binding = client.binding
        if (
            binding.job_id != identity.job_id
            or binding.correlation_id != identity.correlation_id
            or binding.subject_version != identity.subject_version
            or binding.attempt != identity.attempt
            or binding.model != identity.model
        ):
            raise ValueError("model client does not match session identity")
        return client

    @staticmethod
    def _validate_identity(
        *,
        job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
        case_ref: PrivateHoldoutRef,
        identity: HostAutoGenSessionIdentityV1,
        subject_id: str,
        variant: Literal["candidate", "single_agent_baseline"],
    ) -> None:
        expected = (
            (invocation.job_id, job.job_id),
            (invocation.correlation_id, job.correlation_id),
            (invocation.subject_version, job.subject_version),
            (identity.job_id, job.job_id),
            (identity.correlation_id, job.correlation_id),
            (identity.subject_id, subject_id),
            (identity.subject_version, job.subject_version),
            (identity.attempt, invocation.attempt),
            (identity.invocation_id, invocation.invocation_id),
            (identity.case_id, case_ref.holdout_id),
            (identity.case_sha256, case_ref.sha256),
            (identity.variant, variant),
            (identity.execution_policy_sha256, _execution_policy_digest(job)),
        )
        if any(observed != required for observed, required in expected):
            raise ValueError("AutoGen session identity does not match its execution")


class HostAutoGenTeamRunner:
    """Instantiate AutoGen 0.7.5 teams from sealed data under host instrumentation."""

    def __init__(
        self,
        *,
        model_client: BudgetedChatCompletionClient,
        evaluator: FactoryCandidateEvaluator | None = None,
        evidence_store: FactoryEvidenceStore,
        holdouts: FactoryHoldoutEvaluatorPort,
        tools: Mapping[str, Callable[..., Any]],
        n8n_adapter: FactoryN8nToolAdapterPort | None = None,
        n8n_authority: FactoryN8nGrantAuthorityPort | None = None,
        session_executor: HostAutoGenSessionExecutor | None = None,
        allowed_tools_for: Callable[
            [PrivateHoldoutRef, ResolvedFactoryCandidate],
            tuple[str, ...] | None,
        ]
        | None = None,
        clock: Callable[[], datetime],
    ) -> None:
        self._model_client = model_client
        self._evaluator = evaluator or FactoryCandidateEvaluator()
        self._evidence_store = evidence_store
        self._holdouts = holdouts
        self._tools = dict(tools)
        self._n8n_adapter = n8n_adapter
        self._n8n_authority = n8n_authority
        self._allowed_tools_for = allowed_tools_for
        self._clock = clock
        self._session_executor = session_executor or HostAutoGenSessionExecutor(
            model_client=model_client,
            evidence_store=evidence_store,
            holdouts=holdouts,
            tools=tools,
            evaluator=self._evaluator,
            n8n_adapter=n8n_adapter,
            n8n_authority=n8n_authority,
            clock=clock,
        )

    @property
    def paid_effect_started(self) -> bool:
        return self._model_client.provider_dispatched

    @property
    def provider_effect_dispatched_with_unknown_usage(self) -> bool:
        return self._model_client.provider_effect_dispatched_with_unknown_usage

    @property
    def any_provider_effect_started(self) -> bool:
        model_client = getattr(self, "_model_client", None)
        return bool(
            model_client is not None and model_client.any_provider_effect_started
        )

    @property
    def provider_usage_receipts(self) -> tuple[FactoryUsageReceiptV1, ...]:
        model_client = getattr(self, "_model_client", None)
        if model_client is None:
            return ()
        return model_client.usage_receipts

    async def run(
        self,
        *,
        job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
        candidate: ResolvedFactoryCandidate,
        case_ref: PrivateHoldoutRef,
        lease: FactoryLease,
        allowed_models: tuple[str, ...],
        max_seconds: float,
    ) -> FactoryTeamRunResult:
        if self._model_client.model not in allowed_models:
            raise ValueError("host model client is not allowed by the factory job")
        request_id = uuid5(
            invocation.invocation_id,
            f"factory-autogen-request:{case_ref.holdout_id}:{case_ref.sha256}",
        )
        runtime_session_id = (
            f"autogen-team-{invocation.attempt}-{request_id.hex}"
        )
        identity_payload = "|".join(
            (
                str(job.job_id),
                str(job.correlation_id),
                candidate.candidate.candidate_id,
                str(job.subject_version),
                str(invocation.attempt),
                str(invocation.invocation_id),
                str(request_id),
                case_ref.holdout_id,
                case_ref.sha256,
                self._model_client.model,
            )
        )
        identity = HostAutoGenSessionIdentityV1.for_factory_execution(
            job=job,
            invocation=invocation,
            case_ref=case_ref,
            subject_id=candidate.candidate.candidate_id,
            variant="candidate",
            request_id=request_id,
            runtime_session_id=runtime_session_id,
            effect_id=hashlib.sha256(identity_payload.encode("utf-8")).hexdigest(),
            claim_id=uuid5(request_id, "factory-autogen-claim"),
            fence=1,
            model=self._model_client.model,
        )
        session = await self._session_executor.run_candidate(
            job=job,
            invocation=invocation,
            case_ref=case_ref,
            identity=identity,
            candidate=candidate,
            allowed_tools=(
                self._allowed_tools_for(case_ref, candidate)
                if self._allowed_tools_for is not None
                else None
            ),
            allowed_models=allowed_models,
            max_seconds=max_seconds,
        )
        result = session.task_result
        raw_holdout_receipt = await self._holdouts.evaluate(
            case_ref,
            result,
            invocation.acceptance_assertion_ids,
        )
        holdout_receipt = FactoryHoldoutEvaluationReceiptV1.model_validate(
            raw_holdout_receipt.model_dump(mode="python", by_alias=True)
            if isinstance(raw_holdout_receipt, BaseModel)
            else raw_holdout_receipt
        )
        if (
            holdout_receipt.holdout_ref != case_ref
            or holdout_receipt.candidate_ref
            != candidate.candidate.source_archive_ref
            or holdout_receipt.assertion_ids
            != invocation.acceptance_assertion_ids
        ):
            raise ValueError("holdout evaluator receipt does not match this run")
        decision_ref = await self._evidence_store.persist(
            job,
            holdout_receipt.model_dump_json(
                by_alias=True,
                exclude_none=True,
            ).encode("utf-8"),
        )
        normalized_results = {
            item.assertion_id: item.passed
            for item in holdout_receipt.decisions
        }
        resolved_status: Literal["succeeded", "unresolved"] = (
            "succeeded" if all(normalized_results.values()) else "unresolved"
        )
        observation_ref = session.runtime_evidence_ref
        handoffs = session.handoffs
        tool_executions = session.tool_executions
        n8n_executions = session.n8n_executions
        n8n_refs = session.workflow_evidence_refs
        command_id = uuid5(
            NAMESPACE_URL,
            f"factory-autogen-command|{invocation.invocation_id}",
        )
        result_id = uuid5(command_id, "result")
        assertions = tuple(
            AssertionOutcome(
                assertion_id=assertion_id,
                status="passed" if normalized_results[assertion_id] else "failed",
                integration_intent=IntegrationIntent.NONE,
                evidence_refs=(observation_ref, decision_ref),
            )
            for assertion_id in invocation.acceptance_assertion_ids
        )
        succeeded = resolved_status == "succeeded"
        runtime = AgentRuntimeResult(
            schema_name="captain.agent-runtime-result.v1",
            event_id=result_id,
            command_id=command_id,
            correlation_id=job.correlation_id,
            occurred_at=self._clock(),
            producer="agent-runtime",
            subject_id=candidate.candidate.candidate_id,
            subject_version=job.subject_version,
            grant_id=lease.lease_id,
            operation=RuntimeOperation.CODEX_RUN,
            status=(RuntimeStatus.SUCCEEDED if succeeded else RuntimeStatus.FAILED),
            session_id=runtime_session_id,
            artifact_refs=(observation_ref,),
            evidence_refs=(observation_ref, decision_ref, *n8n_refs),
            error=None if succeeded else "Captain holdout assertions unresolved",
        )
        outcome = ExecutionOutcomeV1(
            schema_name="captain.execution-outcome.v1",
            capability_id=job.required_capability,
            capability_version=1,
            team_version=1,
            correlation_id=job.correlation_id,
            command_id=command_id,
            result_id=result_id,
            output_ref=observation_ref,
            assertion_outcomes=assertions,
            tool_versions=tuple(
                sorted({f"{item.tool_name}@1" for item in tool_executions})
            ),
            evidence_refs=(observation_ref, decision_ref, *n8n_refs),
            status="succeeded" if succeeded else "failed",
        )
        return FactoryTeamRunResult(
            status=resolved_status,
            runtime_result=runtime,
            execution_outcome=outcome,
            usage_receipts=session.usage_receipts,
            handoff_evidence_refs=tuple(item.evidence_ref for item in handoffs),
            tool_evidence_refs=tuple(
                item.evidence_ref for item in tool_executions
            ),
            handoffs=handoffs,
            tool_executions=tool_executions,
            n8n_executions=n8n_executions,
            workflow_evidence_refs=n8n_refs,
            conversation_pattern=session.conversation_pattern,
            message_count=session.message_count,
            handoff_count=session.handoff_count,
            tool_call_count=session.tool_call_count,
            termination_reason=session.termination_reason,
        )


class TeamExecutionService:
    """Claim once, then delegate per-call reservations to the host model client."""

    def __init__(
        self,
        *,
        job: AgentFactoryJobV3,
        preflight: CandidatePreflightPort,
        runner: FactoryTeamRunner,
        evidence_store: FactoryEvidenceStore,
        replay_store: FactorySkillReplayStore | None = None,
        replay_retry_authority: (
            CaptainHermesReplayRetryAuthorizationPort | None
        ) = None,
        clock: Callable[[], datetime],
    ) -> None:
        self._job = job
        self._preflight = preflight
        self._runner = runner
        self._evidence_store = evidence_store
        self._replay_store = replay_store
        self._replay_retry_authority = replay_retry_authority
        self._clock = clock

    async def execute(
        self,
        invocation: FactorySkillInvocationV1,
        candidate: ResolvedFactoryCandidate,
        case_ref: PrivateHoldoutRef,
    ) -> TeamExecutionEvidenceV1:
        now = self._active_time(invocation, case_ref)
        invocation = _holdout_scoped_invocation(invocation, case_ref)
        remaining = min(
            (self._job.deadline_at - now).total_seconds(),
            (invocation.lease.expires_at - now).total_seconds(),
        )
        preflight = self._preflight.validate(candidate, remaining)
        preflight_ref = await self._evidence_store.persist(
            self._job,
            preflight.model_dump_json(exclude_none=True).encode("utf-8"),
        )
        if preflight.status != "succeeded":
            return self._failed_evidence(
                invocation,
                candidate,
                case_ref=case_ref,
                preflight_ref=preflight_ref,
            )
        if not self._job.execution_policy.live_execution:
            raise ValueError("offline factory policy forbids paid team execution")
        if self._replay_store is None:
            raise ValueError("paid team execution requires an atomic replay store")
        try:
            replay = await self._replay_store.claim(invocation)
        except FactorySkillReplayHermesRetryableFailureError as exc:
            if self._replay_retry_authority is None:
                raise
            authorization = self._replay_retry_authority.active(
                exc.record,
                requested_invocation=invocation,
                now=now,
            )
            replay = await self._replay_store.retry_failed_hermes(
                exc.record,
                requested_invocation=invocation,
                authorization=authorization,
            )
        if not replay.acquired:
            if not isinstance(replay.record.artifact, TeamExecutionEvidenceV1):
                raise ValueError("team execution replay is missing completed evidence")
            return replay.record.artifact
        pending = replay.record
        try:
            run = await asyncio.wait_for(
                self._runner.run(
                    job=self._job,
                    invocation=invocation,
                    candidate=candidate,
                    case_ref=case_ref,
                    lease=invocation.lease,
                    allowed_models=self._job.execution_policy.allowed_models,
                    max_seconds=remaining,
                ),
                timeout=remaining,
            )
        except asyncio.CancelledError:
            if (
                isinstance(self._runner, HostAutoGenTeamRunner)
                and self._runner.any_provider_effect_started
            ):
                return await asyncio.shield(
                    self._record_provider_cost_unresolved(
                        pending,
                        invocation,
                        candidate,
                        case_ref=case_ref,
                        preflight_ref=preflight_ref,
                        error_type="CancelledError",
                        usage_receipts=self._runner.provider_usage_receipts,
                    )
                )
            await asyncio.shield(self._replay_store.abandon(pending))
            raise
        except Exception as exc:
            if (
                isinstance(self._runner, HostAutoGenTeamRunner)
                and not self._runner.any_provider_effect_started
            ):
                await asyncio.shield(self._replay_store.abandon(pending))
                raise
            return await self._record_provider_cost_unresolved(
                pending,
                invocation,
                candidate,
                case_ref=case_ref,
                preflight_ref=preflight_ref,
                error_type=type(exc).__name__,
                usage_receipts=(
                    self._runner.provider_usage_receipts
                    if isinstance(self._runner, HostAutoGenTeamRunner)
                    else ()
                ),
            )
        try:
            self._require_run_bindings(
                invocation,
                candidate,
                run,
                topology=preflight.team_execution_manifest,
            )
            evidence = self._run_evidence(
                invocation,
                candidate,
                case_ref=case_ref,
                preflight_ref=preflight_ref,
                run=run,
            )
        except Exception:
            await self._replay_store.fail(
                pending, failure_kind="evidence_binding_failed"
            )
            raise
        await self._complete_replay(pending, evidence)
        return evidence

    async def _complete_replay(
        self,
        pending: FactorySkillReplayRecord,
        evidence: TeamExecutionEvidenceV1,
    ) -> None:
        assert self._replay_store is not None
        await self._replay_store.complete(
            pending,
            artifact=evidence,
            transcript_ref=evidence.artifact_ref,
        )

    async def _record_provider_cost_unresolved(
        self,
        pending: FactorySkillReplayRecord,
        invocation: FactorySkillInvocationV1,
        candidate: ResolvedFactoryCandidate,
        *,
        case_ref: PrivateHoldoutRef,
        preflight_ref: ArtifactRef,
        error_type: str,
        usage_receipts: tuple[FactoryUsageReceiptV1, ...] = (),
    ) -> TeamExecutionEvidenceV1:
        failure_ref = await self._evidence_store.persist(
            self._job,
            json.dumps(
                {
                    "schema": "hermes.factory-provider-failure.v1",
                    "status": "unresolved",
                    "reason": "provider_cost_unresolved",
                    "error_type": error_type,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        unresolved = self._unresolved_evidence(
            invocation,
            candidate,
            case_ref=case_ref,
            preflight_ref=preflight_ref,
            failure_ref=failure_ref,
            usage_receipts=usage_receipts,
        )
        await self._complete_replay(pending, unresolved)
        return unresolved

    def _active_time(
        self,
        invocation: FactorySkillInvocationV1,
        case_ref: PrivateHoldoutRef,
    ) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
            raise ValueError("team execution clock must be UTC")
        validate_factory_lease(
            invocation.lease,
            job=self._job,
            role=FactoryRole.REAL_CASE_TESTER,
            attempt=invocation.attempt,
            now=now,
        )
        if (
            invocation.step is not FactorySkillStep.EXECUTE_TEAM
            or invocation.job_id != self._job.job_id
            or invocation.correlation_id != self._job.correlation_id
            or invocation.subject_version != self._job.subject_version
            or invocation.acceptance_assertion_ids
            != self._job.acceptance_assertion_ids
            or not self._job.occurred_at <= now < self._job.deadline_at
        ):
            raise ValueError("team execution requires the matching active JobV3 lease")
        if case_ref not in self._job.private_holdout_refs:
            raise ValueError("team execution case is not authorized by the factory job")
        return now

    def _failed_evidence(
        self,
        invocation: FactorySkillInvocationV1,
        candidate: ResolvedFactoryCandidate,
        *,
        case_ref: PrivateHoldoutRef,
        preflight_ref: ArtifactRef,
    ) -> TeamExecutionEvidenceV1:
        command_id = uuid5(
            NAMESPACE_URL,
            f"factory-team-preflight|{invocation.invocation_id}",
        )
        result_id = uuid5(
            NAMESPACE_URL,
            f"factory-team-preflight-result|{invocation.invocation_id}",
        )
        assertions = tuple(
            AssertionOutcome(
                assertion_id=assertion_id,
                status="failed",
                integration_intent=IntegrationIntent.NONE,
                evidence_refs=(preflight_ref,),
            )
            for assertion_id in invocation.acceptance_assertion_ids
        )
        outcome = ExecutionOutcomeV1(
            schema_name="captain.execution-outcome.v1",
            capability_id=self._job.required_capability,
            capability_version=1,
            team_version=1,
            correlation_id=invocation.correlation_id,
            command_id=command_id,
            result_id=result_id,
            output_ref=preflight_ref,
            assertion_outcomes=assertions,
            evidence_refs=(preflight_ref,),
            status="failed",
        )
        return TeamExecutionEvidenceV1(
            schema_name="hermes.factory-team-execution-evidence.v1",
            invocation=invocation,
            invocation_id=invocation.invocation_id,
            job_id=invocation.job_id,
            correlation_id=invocation.correlation_id,
            subject_version=invocation.subject_version,
            attempt=invocation.attempt,
            occurred_at=self._clock(),
            producer="hermes",
            artifact_ref=preflight_ref,
            evidence_refs=(preflight_ref,),
            acceptance_assertion_ids=invocation.acceptance_assertion_ids,
            run_number=self._run_number(case_ref),
            candidate_ref=candidate.candidate.source_archive_ref,
            holdout_ref=case_ref,
            execution_outcome=outcome,
            termination_reason="preflight_failed",
            status="failed",
        )

    def _require_run_bindings(
        self,
        invocation: FactorySkillInvocationV1,
        candidate: ResolvedFactoryCandidate,
        run: FactoryTeamRunResult,
        *,
        topology: FactoryAutoGenTeamManifestV1 | None,
    ) -> None:
        runtime = run.runtime_result
        outcome = run.execution_outcome
        assertion_ids = tuple(item.assertion_id for item in outcome.assertion_outcomes)
        known_evidence = {
            *runtime.artifact_refs,
            *runtime.evidence_refs,
            *outcome.evidence_refs,
        }
        if (
            runtime.correlation_id != invocation.correlation_id
            or runtime.subject_id != candidate.candidate.candidate_id
            or runtime.subject_version != invocation.subject_version
            or outcome.correlation_id != invocation.correlation_id
            or outcome.command_id != runtime.command_id
            or outcome.result_id != runtime.event_id
            or outcome.capability_id != self._job.required_capability
            or assertion_ids != invocation.acceptance_assertion_ids
            or not set(run.handoff_evidence_refs).issubset(known_evidence)
            or not set(run.tool_evidence_refs).issubset(known_evidence)
            or not set(run.workflow_evidence_refs).issubset(known_evidence)
        ):
            raise ValueError("team run evidence does not match the Captain invocation")
        if run.status == "succeeded" and (
            runtime.status.value != "succeeded"
            or outcome.status != "succeeded"
            or any(item.status != "passed" for item in outcome.assertion_outcomes)
        ):
            raise ValueError("successful team run requires passed runtime evidence")
        if topology is not None:
            agents = {agent.name: agent for agent in topology.agents}
            if (
                run.conversation_pattern != topology.conversation_pattern
                or run.message_count > topology.max_messages
                or run.handoff_count != len(run.handoffs)
                or run.handoff_count > topology.max_handoffs
                or run.tool_call_count != len(run.tool_executions)
                or run.tool_call_count > topology.max_tool_calls
            ):
                raise ValueError("team counters do not match the sealed manifest")
            if run.termination_reason not in topology.termination_conditions:
                raise ValueError("team termination is not declared by the sealed manifest")
            for handoff in run.handoffs:
                source = agents.get(handoff.from_agent)
                if source is None or handoff.to_agent not in source.handoffs:
                    raise ValueError("team handoff is not allowed by the sealed manifest")
            for tool in run.tool_executions:
                agent = agents.get(tool.agent_name)
                if agent is None or tool.tool_name not in agent.tools:
                    raise ValueError("team tool call is not allowed by the sealed manifest")
        uses_n8n = any(
            assertion.integration_intent is IntegrationIntent.N8N
            for assertion in outcome.assertion_outcomes
        )
        if uses_n8n and not run.n8n_executions:
            raise ValueError("n8n execution evidence is required for n8n activity")
        candidate_tools = {tool.name for tool in candidate.candidate.n8n_tools}
        for n8n in run.n8n_executions:
            observed_at = n8n.runtime_result.occurred_at
            if (
                n8n.tool_name not in candidate_tools
                or n8n.runtime_result.correlation_id != invocation.correlation_id
                or n8n.execution.correlation_id != str(invocation.correlation_id)
                or not n8n.capability_grant.issued_at
                <= observed_at
                < n8n.capability_grant.expires_at
                or n8n.evidence_ref not in known_evidence
                or n8n.workflow_ref not in known_evidence
            ):
                raise ValueError("n8n execution evidence does not match the Captain run")

    def _unresolved_evidence(
        self,
        invocation: FactorySkillInvocationV1,
        candidate: ResolvedFactoryCandidate,
        *,
        case_ref: PrivateHoldoutRef,
        preflight_ref: ArtifactRef,
        failure_ref: ArtifactRef,
        usage_receipts: tuple[FactoryUsageReceiptV1, ...] = (),
    ) -> TeamExecutionEvidenceV1:
        command_id = uuid5(
            NAMESPACE_URL,
            f"factory-team-provider|{invocation.invocation_id}",
        )
        result_id = uuid5(
            NAMESPACE_URL,
            f"factory-team-provider-result|{invocation.invocation_id}",
        )
        usage_refs = tuple(receipt.evidence_ref for receipt in usage_receipts)
        evidence_refs = _unique_refs((preflight_ref, failure_ref, *usage_refs))
        assertions = tuple(
            AssertionOutcome(
                assertion_id=assertion_id,
                status="failed",
                integration_intent=IntegrationIntent.NONE,
                evidence_refs=(failure_ref,),
            )
            for assertion_id in invocation.acceptance_assertion_ids
        )
        outcome = ExecutionOutcomeV1(
            schema_name="captain.execution-outcome.v1",
            capability_id=self._job.required_capability,
            capability_version=1,
            team_version=1,
            correlation_id=invocation.correlation_id,
            command_id=command_id,
            result_id=result_id,
            output_ref=failure_ref,
            assertion_outcomes=assertions,
            evidence_refs=evidence_refs,
            status="failed",
        )
        return TeamExecutionEvidenceV1(
            schema_name="hermes.factory-team-execution-evidence.v1",
            invocation=invocation,
            invocation_id=invocation.invocation_id,
            job_id=invocation.job_id,
            correlation_id=invocation.correlation_id,
            subject_version=invocation.subject_version,
            attempt=invocation.attempt,
            occurred_at=self._clock(),
            producer="hermes",
            artifact_ref=failure_ref,
            evidence_refs=evidence_refs,
            acceptance_assertion_ids=invocation.acceptance_assertion_ids,
            run_number=self._run_number(case_ref),
            candidate_ref=candidate.candidate.source_archive_ref,
            holdout_ref=case_ref,
            execution_outcome=outcome,
            usage_receipt_refs=usage_refs,
            termination_reason="provider_cost_unresolved",
            status="unresolved",
        )

    def _run_evidence(
        self,
        invocation: FactorySkillInvocationV1,
        candidate: ResolvedFactoryCandidate,
        *,
        case_ref: PrivateHoldoutRef,
        preflight_ref: ArtifactRef,
        run: FactoryTeamRunResult,
    ) -> TeamExecutionEvidenceV1:
        outcome = run.execution_outcome
        artifact_ref = outcome.output_ref
        if artifact_ref is None:
            if not run.runtime_result.artifact_refs:
                raise ValueError("team run is missing a public output artifact")
            artifact_ref = run.runtime_result.artifact_refs[0]
        usage_refs = tuple(receipt.evidence_ref for receipt in run.usage_receipts)
        evidence_refs = _unique_refs(
            (
                preflight_ref,
                *run.runtime_result.artifact_refs,
                *run.runtime_result.evidence_refs,
                *outcome.evidence_refs,
                *usage_refs,
                *run.handoff_evidence_refs,
                *run.tool_evidence_refs,
                *run.workflow_evidence_refs,
            )
        )
        return TeamExecutionEvidenceV1(
            schema_name="hermes.factory-team-execution-evidence.v1",
            invocation=invocation,
            invocation_id=invocation.invocation_id,
            job_id=invocation.job_id,
            correlation_id=invocation.correlation_id,
            subject_version=invocation.subject_version,
            attempt=invocation.attempt,
            occurred_at=self._clock(),
            producer="hermes",
            artifact_ref=artifact_ref,
            evidence_refs=evidence_refs,
            acceptance_assertion_ids=invocation.acceptance_assertion_ids,
            run_number=self._run_number(case_ref),
            candidate_ref=candidate.candidate.source_archive_ref,
            holdout_ref=case_ref,
            execution_outcome=outcome,
            usage_receipt_refs=usage_refs,
            handoff_evidence_refs=run.handoff_evidence_refs,
            tool_evidence_refs=run.tool_evidence_refs,
            workflow_evidence_refs=run.workflow_evidence_refs,
            termination_reason=run.termination_reason,
            status=run.status,
        )

    def _run_number(self, case_ref: PrivateHoldoutRef) -> int:
        """Use the Captain-authorized holdout order as stable run identity."""

        try:
            return self._job.private_holdout_refs.index(case_ref) + 1
        except ValueError as exc:
            raise ValueError(
                "team execution case is not authorized by the factory job"
            ) from exc


class TeamExecutionCandidateAdapter:
    """Wire CandidateEvaluationFactory real cases to the governed service."""

    def __init__(
        self,
        *,
        service_for: Callable[
            [AgentFactoryJobV3, FactorySkillInvocationV1],
            TeamExecutionService,
        ],
        invocation_for: Callable[[FactoryDispatch], FactorySkillInvocationV1],
        holdout_for: Callable[[AgentFactoryJobV3], PrivateHoldoutRef] | None = None,
    ) -> None:
        self._service_for = service_for
        self._invocation_for = invocation_for
        self._holdout_for = holdout_for or self._sole_holdout

    async def execute(
        self,
        request: FactoryDispatch,
        candidate: ResolvedFactoryCandidate,
    ) -> TeamExecutionEvidenceV1:
        if not isinstance(request.job, AgentFactoryJobV3):
            raise ValueError("team execution requires AgentFactoryJobV3")
        invocation = self.invocation_for(request)
        if request.lease is None or invocation.lease != request.lease:
            raise ValueError("team invocation must preserve the dispatch lease")
        return await self._service_for(request.job, invocation).execute(
            invocation,
            candidate,
            self._holdout_for(request.job),
        )

    def invocation_for(
        self,
        request: FactoryDispatch,
    ) -> FactorySkillInvocationV1:
        if not isinstance(request.job, AgentFactoryJobV3):
            raise ValueError("team execution requires AgentFactoryJobV3")
        invocation = self._invocation_for(request)
        return _holdout_scoped_invocation(
            invocation,
            self._holdout_for(request.job),
        )

    @staticmethod
    def _sole_holdout(job: AgentFactoryJobV3) -> PrivateHoldoutRef:
        if len(job.private_holdout_refs) != 1:
            raise ValueError("multiple private holdouts require an explicit selector")
        return job.private_holdout_refs[0]


@dataclass(frozen=True)
class FactoryLiveTeamExecutionPorts:
    """All trusted ports required by the explicit live composition root."""

    model_client_for: Callable[
        [AgentFactoryJobV3, FactorySkillInvocationV1],
        ChatCompletionClient,
    ]
    budget: FactoryBudgetPort
    pricing_authority: FactoryPricingAuthorityPort
    replay_store: FactorySkillReplayStore
    holdouts: FactoryHoldoutEvaluatorPort
    n8n_adapter: FactoryN8nToolAdapterPort
    n8n_authority: FactoryN8nGrantAuthorityPort
    released_skill_catalog: ReleasedFactorySkillCatalog
    skill_root: Path
    tools: Mapping[str, Callable[..., Any]]
    provider: str
    model: str
    max_cost_per_call: Decimal
    clock: Callable[[], datetime]
    allowed_tools_for: Callable[
        [PrivateHoldoutRef, ResolvedFactoryCandidate],
        tuple[str, ...] | None,
    ] | None = None


def compose_live_team_execution(
    *,
    job: AgentFactoryJobV3,
    evidence_store: FactoryEvidenceStore,
    ports: FactoryLiveTeamExecutionPorts,
    holdout_selector: (
        Callable[[AgentFactoryJobV3], PrivateHoldoutRef] | None
    ) = None,
    replay_retry_authority: (
        CaptainHermesReplayRetryAuthorizationPort | None
    ) = None,
) -> TeamExecutionCandidateAdapter:
    """Compose only the host AutoGen runner; generated runners are never accepted."""

    required_ports = (
        ports.model_client_for,
        ports.budget,
        ports.pricing_authority,
        ports.replay_store,
        ports.holdouts,
        ports.n8n_adapter,
        ports.n8n_authority,
        ports.released_skill_catalog,
        ports.skill_root,
        ports.clock,
    )
    if any(port is None for port in required_ports):
        raise ValueError("live team execution requires every authoritative port")
    if (
        not job.execution_policy.live_execution
        or not ports.provider.strip()
        or ports.model not in job.execution_policy.allowed_models
        or ports.max_cost_per_call <= 0
    ):
        raise ValueError("live team execution configuration is not Captain-authorized")
    skill_authority = CaptainReleasedSkillAuthority(
        catalog=ports.released_skill_catalog,
        skill_root=ports.skill_root,
    )

    def holdout_for(current_job: AgentFactoryJobV3) -> PrivateHoldoutRef:
        if holdout_selector is None:
            if len(current_job.private_holdout_refs) != 1:
                raise ValueError(
                    "live execution requires an explicit Captain holdout selector"
                )
            selected = current_job.private_holdout_refs[0]
        else:
            selected = holdout_selector(current_job)
        if selected not in current_job.private_holdout_refs:
            raise ValueError(
                "live execution holdout selector returned an unauthorized scope"
            )
        return selected

    def invocation_for(request: FactoryDispatch) -> FactorySkillInvocationV1:
        if request.job != job or request.lease is None:
            raise ValueError("live team composition received a different job or lease")
        now = ports.clock()
        validate_factory_lease(
            request.lease,
            job=job,
            role=FactoryRole.REAL_CASE_TESTER,
            attempt=request.action.attempt,
            now=now,
        )
        released = ports.released_skill_catalog.released_for(
            job,
            FactorySkillStep.EXECUTE_TEAM,
        )
        holdout = holdout_for(job)
        binding = json.dumps(
            {
                "job_id": str(job.job_id),
                "correlation_id": str(job.correlation_id),
                "subject_version": job.subject_version,
                "attempt": request.action.attempt,
                "step": FactorySkillStep.EXECUTE_TEAM.value,
                "holdout_id": holdout.holdout_id,
                "holdout_sha256": holdout.sha256,
                "released_skill_id": released.skill_id,
                "released_skill_version": released.version,
                "released_skill_sha256": released.content_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        idempotency_key = hashlib.sha256(binding.encode("utf-8")).hexdigest()
        invocation = FactorySkillInvocationV1(
            schema_name="captain.factory-skill-invocation.v1",
            invocation_id=uuid5(
                NAMESPACE_URL,
                f"captain.factory-team-live:{idempotency_key}",
            ),
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            subject_version=job.subject_version,
            attempt=request.action.attempt,
            step=FactorySkillStep.EXECUTE_TEAM,
            released_skill=released,
            input_ref=job.input_ref,
            input_sha256=job.input_ref.sha256,
            lease=request.lease,
            idempotency_key=idempotency_key,
            acceptance_assertion_ids=job.acceptance_assertion_ids,
            execution_scope_ref=holdout,
        )
        skill_authority.authorize(job=job, invocation=invocation, now=now)
        return invocation

    def service_for(
        current_job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
    ) -> TeamExecutionService:
        delegate = ports.model_client_for(current_job, invocation)
        model_client = BudgetedChatCompletionClient(
            job=current_job,
            invocation=invocation,
            attempt=invocation.attempt,
            delegate=delegate,
            budget=ports.budget,
            evidence_store=evidence_store,
            provider=ports.provider,
            model=ports.model,
            max_cost_per_call=ports.max_cost_per_call,
            paid_effect_authority=skill_authority,
            pricing_authority=ports.pricing_authority,
            clock=ports.clock,
        )
        runner = HostAutoGenTeamRunner(
            model_client=model_client,
            evaluator=FactoryCandidateEvaluator(),
            evidence_store=evidence_store,
            holdouts=ports.holdouts,
            tools=ports.tools,
            n8n_adapter=ports.n8n_adapter,
            n8n_authority=ports.n8n_authority,
            allowed_tools_for=ports.allowed_tools_for,
            clock=ports.clock,
        )
        return TeamExecutionService(
            job=current_job,
            preflight=FactoryCandidateEvaluator(),
            runner=runner,
            evidence_store=evidence_store,
            replay_store=ports.replay_store,
            replay_retry_authority=replay_retry_authority,
            clock=ports.clock,
        )

    return TeamExecutionCandidateAdapter(
        service_for=service_for,
        invocation_for=invocation_for,
        holdout_for=holdout_for,
    )


def _unique_refs(references: tuple[ArtifactRef, ...]) -> tuple[ArtifactRef, ...]:
    observed: dict[tuple[str, str, str], ArtifactRef] = {}
    for reference in references:
        key = (reference.uri, reference.sha256, reference.media_type)
        observed.setdefault(key, reference)
    return tuple(observed.values())


def _execution_policy_digest(job: AgentFactoryJobV3) -> str:
    encoded = json.dumps(
        job.execution_policy.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _single_agent_policy_digest(
    policy: SealedSingleAgentPolicyV1 | Mapping[str, object],
) -> str:
    if isinstance(policy, BaseModel):
        payload = policy.model_dump(mode="json", exclude={"policy_sha256"})
    else:
        payload = dict(policy)
        payload.pop("policy_sha256", None)
        prompt_ref = payload.get("system_prompt_ref")
        if isinstance(prompt_ref, BaseModel):
            payload["system_prompt_ref"] = prompt_ref.model_dump(mode="json")
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _session_termination_reason(
    result: TaskResult,
    *,
    termination_conditions: tuple[str, ...],
    max_messages: int,
) -> str:
    stop_reason = result.stop_reason or ""
    if "max_handoffs" in stop_reason:
        return "max_handoffs"
    if "max_tool_calls" in stop_reason:
        return "max_tool_calls"
    if len(result.messages) >= max_messages or "Maximum number" in stop_reason:
        return "max_messages"
    if "task_completed" in termination_conditions:
        return "task_completed"
    raise ValueError("AutoGen stopped without a declared termination condition")


def _holdout_scoped_invocation(
    invocation: FactorySkillInvocationV1,
    case_ref: PrivateHoldoutRef,
) -> FactorySkillInvocationV1:
    if invocation.execution_scope_ref is not None:
        if invocation.execution_scope_ref != case_ref:
            raise ValueError("team invocation is scoped to a different holdout")
        return invocation
    binding = "|".join(
        (
            invocation.idempotency_key,
            case_ref.holdout_id,
            case_ref.uri,
            case_ref.sha256,
        )
    )
    scoped_key = hashlib.sha256(binding.encode("utf-8")).hexdigest()
    return invocation.model_copy(
        update={
            "invocation_id": uuid5(
                NAMESPACE_URL,
                f"captain.factory-team-holdout:{scoped_key}",
            ),
            "idempotency_key": scoped_key,
            "execution_scope_ref": case_ref,
        }
    )


def _skill_directory_digest(directory: Path) -> str:
    entries = sorted(
        directory.rglob("*"),
        key=lambda item: item.relative_to(directory).as_posix(),
    )
    if any(item.is_symlink() for item in entries):
        raise ValueError("released execute-team skill cannot contain symlinks")
    manifest = [
        {
            "path": item.relative_to(directory).as_posix(),
            "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
            "size": item.stat().st_size,
        }
        for item in entries
        if item.is_file()
    ]
    encoded = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sealed_text(workspace: Path, reference: ArtifactRef) -> str:
    matches = tuple(
        path
        for path in workspace.rglob("*")
        if path.is_file()
        and hashlib.sha256(path.read_bytes()).hexdigest() == reference.sha256
    )
    if not matches:
        raise ValueError("sealed system prompt artifact is missing")
    try:
        return matches[0].read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("sealed system prompt must be UTF-8") from exc
