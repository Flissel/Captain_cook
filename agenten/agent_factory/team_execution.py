"""Captain-governed execution of sealed generated AutoGen teams."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from collections.abc import AsyncGenerator, Mapping, Sequence
from typing import Any, Callable, Literal, Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.base import TaskResult
from autogen_agentchat.conditions import MaxMessageTermination, TextMentionTermination
from autogen_agentchat.messages import HandoffMessage, ToolCallExecutionEvent
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
from autogen_core.tools import Tool, ToolSchema

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
from agenten.agent_factory.hermes_cli import (
    FactorySkillReplayRecord,
    FactorySkillReplayStore,
)
from agenten.agent_factory.outcome_contracts import AssertionOutcome, ExecutionOutcomeV1
from agenten.agent_factory.orchestration import FactoryDispatch
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
    CapabilityProfile,
    IntegrationIntent,
    RuntimeOperation,
    RuntimeStatus,
)
from agenten.targets.n8n import N8nExecutionEvidence


class FactoryPricingQuoteV1(BaseModel):
    """Versioned provider pricing evidence used for host-computed receipts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    quote_id: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    effective_at: datetime
    input_cost_per_million: Decimal
    output_cost_per_million: Decimal
    minimum_cost_usd: Decimal
    evidence_ref: ArtifactRef

    @field_validator(
        "input_cost_per_million",
        "output_cost_per_million",
        "minimum_cost_usd",
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

    def cost(self, usage: RequestUsage) -> Decimal:
        calculated = (
            Decimal(usage.prompt_tokens) * self.input_cost_per_million
            + Decimal(usage.completion_tokens) * self.output_cost_per_million
        ) / Decimal("1000000")
        return max(calculated, self.minimum_cost_usd).quantize(
            Decimal("0.01"), rounding=ROUND_CEILING
        )


class BudgetedChatCompletionClient(ChatCompletionClient):
    """Host-owned model wrapper with one Captain reservation per provider call."""

    def __init__(
        self,
        *,
        job: AgentFactoryJobV3,
        attempt: int,
        delegate: ChatCompletionClient,
        budget: FactoryBudgetPort,
        evidence_store: FactoryEvidenceStore,
        provider: str,
        model: str,
        max_cost_per_call: Decimal,
        pricing_quote: FactoryPricingQuoteV1,
        clock: Callable[[], datetime],
    ) -> None:
        if max_cost_per_call <= 0:
            raise ValueError("model call maximum cost must be positive")
        if model not in job.execution_policy.allowed_models:
            raise ValueError("budgeted model is not allowed by the execution policy")
        if (
            pricing_quote.provider != provider
            or pricing_quote.model != model
            or pricing_quote.effective_at > clock()
        ):
            raise ValueError("provider pricing quote does not match this model call")
        self._job = job
        self._attempt = attempt
        self._delegate = delegate
        self._budget = budget
        self._evidence_store = evidence_store
        self._provider = provider
        self._model = model
        self._max_cost_per_call = max_cost_per_call
        self._pricing_quote = pricing_quote
        self._clock = clock
        self._usage_receipts: list[FactoryUsageReceiptV1] = []

    @property
    def usage_receipts(self) -> tuple[FactoryUsageReceiptV1, ...]:
        return tuple(self._usage_receipts)

    @property
    def model(self) -> str:
        return self._model

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
        reservation = self._reserve()
        try:
            result = await self._delegate.create(
                messages,
                tools=tools,
                tool_choice=tool_choice,
                json_output=json_output,
                extra_create_args=extra_create_args,
                cancellation_token=cancellation_token,
            )
        except asyncio.CancelledError:
            self._release(reservation, "cancelled")
            raise
        await self._finalize(reservation, result.usage)
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
        reservation = self._reserve()
        finalized = False
        try:
            async for item in self._delegate.create_stream(
                messages,
                tools=tools,
                tool_choice=tool_choice,
                json_output=json_output,
                extra_create_args=extra_create_args,
                cancellation_token=cancellation_token,
            ):
                if isinstance(item, CreateResult):
                    await self._finalize(reservation, item.usage)
                    finalized = True
                yield item
            if not finalized:
                raise ValueError("provider stream ended without final usage")
        except asyncio.CancelledError:
            if not finalized:
                self._release(reservation, "cancelled")
            raise

    def _reserve(self) -> FactoryBudgetReservationV1:
        return self._budget.reserve(
            self._job,
            attempt=self._attempt,
            requested_usd=self._max_cost_per_call,
            now=self._clock(),
        )

    async def _finalize(
        self,
        reservation: FactoryBudgetReservationV1,
        usage: RequestUsage,
    ) -> None:
        ended_at = self._clock()
        cost = self._pricing_quote.cost(usage)
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
                    "pricing_quote_id": self._pricing_quote.quote_id,
                    "pricing_version": self._pricing_quote.version,
                    "pricing_effective_at": self._pricing_quote.effective_at.isoformat(),
                    "pricing_evidence_sha256": self._pricing_quote.evidence_ref.sha256,
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

    def _release(
        self,
        reservation: FactoryBudgetReservationV1,
        reason: Literal["provider_failed", "cancelled", "unused"],
    ) -> None:
        self._budget.release(
            self._job,
            reservation,
            now=self._clock(),
            reason=reason,
        )

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
    ) -> Mapping[str, bool]: ...


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
            grant.profile is not CapabilityProfile.N8N_BUILDER
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


class FactoryN8nToolAdapterPort(Protocol):
    """Host-owned n8n call surface with Captain-bound execution evidence."""

    def tool(self, name: str) -> Callable[..., Any]: ...

    def observed_evidence(self) -> tuple[FactoryN8nExecutionEvidenceV1, ...]: ...


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
        clock: Callable[[], datetime],
    ) -> None:
        self._model_client = model_client
        self._evaluator = evaluator or FactoryCandidateEvaluator()
        self._evidence_store = evidence_store
        self._holdouts = holdouts
        self._tools = dict(tools)
        self._n8n_adapter = n8n_adapter
        self._clock = clock

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
        receipt_offset = len(self._model_client.usage_receipts)
        with self._evaluator.verified_team_workspace(candidate) as (
            workspace,
            manifest,
        ):
            n8n_tool_names = {tool.name for tool in candidate.candidate.n8n_tools}
            required_n8n_tools = {
                tool
                for agent in manifest.agents
                for tool in agent.tools
                if tool in n8n_tool_names
            }
            if required_n8n_tools and self._n8n_adapter is None:
                raise ValueError("candidate n8n tools require a trusted n8n adapter")
            resolved_tools = dict(self._tools)
            if self._n8n_adapter is not None:
                resolved_tools.update(
                    {
                        name: self._n8n_adapter.tool(name)
                        for name in required_n8n_tools
                    }
                )
            unknown_tools = {
                tool
                for agent in manifest.agents
                for tool in agent.tools
                if tool not in resolved_tools
            }
            if unknown_tools:
                raise ValueError(
                    f"host tool is not registered: {sorted(unknown_tools)[0]}"
                )
            participants = [
                AssistantAgent(
                    name=agent.name,
                    model_client=self._model_client,
                    tools=[resolved_tools[name] for name in agent.tools],
                    handoffs=list(agent.handoffs),
                    model_context=(
                        BufferedChatCompletionContext(
                            buffer_size=manifest.max_messages
                        )
                        if manifest.memory_policy == "buffered"
                        else None
                    ),
                    system_message=_sealed_text(workspace, agent.system_prompt_ref),
                    max_tool_iterations=manifest.max_tool_calls,
                )
                for agent in manifest.agents
            ]
            termination = TextMentionTermination("TERMINATE") | MaxMessageTermination(
                manifest.max_messages,
                include_agent_event=True,
            )
            if manifest.conversation_pattern == "swarm":
                team = Swarm(
                    participants,
                    termination_condition=termination,
                    max_turns=manifest.max_messages,
                )
            elif manifest.conversation_pattern == "selector_group_chat":
                team = SelectorGroupChat(
                    participants,
                    model_client=self._model_client,
                    termination_condition=termination,
                    max_turns=manifest.max_messages,
                )
            elif manifest.conversation_pattern == "round_robin_group_chat":
                team = RoundRobinGroupChat(
                    participants,
                    termination_condition=termination,
                    max_turns=manifest.max_messages,
                )
            private_case = await self._holdouts.resolve(case_ref)
            if private_case.reference != case_ref:
                raise ValueError("private holdout resolver returned a different reference")
            try:
                task = private_case.body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("private holdout body must be UTF-8") from exc
            cancellation_token = CancellationToken()
            try:
                if manifest.conversation_pattern == "single_agent":
                    if len(participants) != 1:
                        raise ValueError(
                            "single-agent topology requires exactly one participant"
                        )
                    result = await asyncio.wait_for(
                        participants[0].run(
                            task=task,
                            cancellation_token=cancellation_token,
                        ),
                        timeout=max_seconds,
                    )
                else:
                    result = await asyncio.wait_for(
                        team.run(
                            task=task,
                            cancellation_token=cancellation_token,
                        ),
                        timeout=max_seconds,
                    )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                cancellation_token.cancel()
                raise
            handoff_messages = tuple(
                message for message in result.messages if isinstance(message, HandoffMessage)
            )
            tool_events = tuple(
                message
                for message in result.messages
                if isinstance(message, ToolCallExecutionEvent)
            )
            tool_call_count = sum(len(event.content) for event in tool_events)
            if len(result.messages) > manifest.max_messages:
                raise ValueError("AutoGen team exceeded the message ceiling")
            if len(handoff_messages) > manifest.max_handoffs:
                raise ValueError("AutoGen team exceeded the handoff ceiling")
            if tool_call_count > manifest.max_tool_calls:
                raise ValueError("AutoGen team exceeded the tool-call ceiling")
            assertion_results = await self._holdouts.evaluate(
                case_ref,
                result,
                invocation.acceptance_assertion_ids,
            )
            if set(assertion_results) != set(invocation.acceptance_assertion_ids):
                resolved_status: Literal["succeeded", "unresolved"] = "unresolved"
                normalized_results = {
                    assertion_id: False
                    for assertion_id in invocation.acceptance_assertion_ids
                }
            else:
                normalized_results = dict(assertion_results)
                resolved_status = (
                    "succeeded" if all(normalized_results.values()) else "unresolved"
                )
            observation_ref = await self._evidence_store.persist(
                job,
                json.dumps(
                    {
                        "schema": "captain.factory-autogen-observation.v1",
                        "conversation_pattern": manifest.conversation_pattern,
                        "message_count": len(result.messages),
                        "handoff_count": len(handoff_messages),
                        "tool_call_count": tool_call_count,
                        "stop_reason": result.stop_reason,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
            handoffs = tuple(
                FactoryHandoffEvidenceV1(
                    from_agent=message.source,
                    to_agent=message.target,
                    evidence_ref=observation_ref,
                )
                for message in handoff_messages
            )
            tool_executions = tuple(
                FactoryToolExecutionEvidenceV1(
                    agent_name=event.source,
                    tool_name=execution.name,
                    status="failed" if execution.is_error else "succeeded",
                    evidence_ref=observation_ref,
                )
                for event in tool_events
                for execution in event.content
            )
            n8n_executions = (
                self._n8n_adapter.observed_evidence()
                if self._n8n_adapter is not None
                else ()
            )
            observed_n8n_calls = tuple(
                item for item in tool_executions if item.tool_name in n8n_tool_names
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
                    evidence_refs=(observation_ref,),
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
                session_id=f"autogen-team-{invocation.attempt}",
                artifact_refs=(observation_ref,),
                evidence_refs=(observation_ref, *n8n_refs),
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
                evidence_refs=(observation_ref, *n8n_refs),
                status="succeeded" if succeeded else "failed",
            )
            receipts = self._model_client.usage_receipts[receipt_offset:]
            return FactoryTeamRunResult(
                status=resolved_status,
                runtime_result=runtime,
                execution_outcome=outcome,
                usage_receipts=receipts,
                handoff_evidence_refs=tuple(item.evidence_ref for item in handoffs),
                tool_evidence_refs=tuple(
                    item.evidence_ref for item in tool_executions
                ),
                handoffs=handoffs,
                tool_executions=tool_executions,
                n8n_executions=n8n_executions,
                workflow_evidence_refs=n8n_refs,
                conversation_pattern=manifest.conversation_pattern,
                message_count=len(result.messages),
                handoff_count=len(handoffs),
                tool_call_count=tool_call_count,
                termination_reason=(
                    "max_messages"
                    if len(result.messages) >= manifest.max_messages
                    else "task_completed"
                ),
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
        clock: Callable[[], datetime],
    ) -> None:
        self._job = job
        self._preflight = preflight
        self._runner = runner
        self._evidence_store = evidence_store
        self._replay_store = replay_store
        self._clock = clock

    async def execute(
        self,
        invocation: FactorySkillInvocationV1,
        candidate: ResolvedFactoryCandidate,
        case_ref: PrivateHoldoutRef,
    ) -> TeamExecutionEvidenceV1:
        now = self._active_time(invocation, case_ref)
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
                preflight_ref=preflight_ref,
            )
        if not self._job.execution_policy.live_execution:
            raise ValueError("offline factory policy forbids paid team execution")
        if self._replay_store is None:
            raise ValueError("paid team execution requires an atomic replay store")
        replay = await self._replay_store.claim(invocation)
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
            await self._replay_store.fail(pending, failure_kind="cancelled")
            raise
        except Exception as exc:
            failure_ref = await self._evidence_store.persist(
                self._job,
                json.dumps(
                    {
                        "schema": "hermes.factory-provider-failure.v1",
                        "status": "unresolved",
                        "reason": "provider_cost_unresolved",
                        "error_type": type(exc).__name__,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
            unresolved = self._unresolved_evidence(
                invocation,
                candidate,
                preflight_ref=preflight_ref,
                failure_ref=failure_ref,
            )
            await self._complete_replay(pending, unresolved)
            return unresolved
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

    def _active_time(
        self,
        invocation: FactorySkillInvocationV1,
        case_ref: PrivateHoldoutRef,
    ) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
            raise ValueError("team execution clock must be UTC")
        if (
            invocation.step is not FactorySkillStep.EXECUTE_TEAM
            or invocation.job_id != self._job.job_id
            or invocation.correlation_id != self._job.correlation_id
            or invocation.subject_version != self._job.subject_version
            or invocation.acceptance_assertion_ids
            != self._job.acceptance_assertion_ids
            or invocation.lease.role is not FactoryRole.REAL_CASE_TESTER
            or invocation.lease.job_id != self._job.job_id
            or invocation.lease.correlation_id != self._job.correlation_id
            or invocation.lease.attempt != invocation.attempt
            or not invocation.lease.issued_at <= now < invocation.lease.expires_at
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
            run_number=invocation.attempt,
            candidate_ref=candidate.candidate.source_archive_ref,
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
        preflight_ref: ArtifactRef,
        failure_ref: ArtifactRef,
    ) -> TeamExecutionEvidenceV1:
        command_id = uuid5(
            NAMESPACE_URL,
            f"factory-team-provider|{invocation.invocation_id}",
        )
        result_id = uuid5(
            NAMESPACE_URL,
            f"factory-team-provider-result|{invocation.invocation_id}",
        )
        evidence_refs = (preflight_ref, failure_ref)
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
            run_number=invocation.attempt,
            candidate_ref=candidate.candidate.source_archive_ref,
            execution_outcome=outcome,
            termination_reason="provider_cost_unresolved",
            status="unresolved",
        )

    def _run_evidence(
        self,
        invocation: FactorySkillInvocationV1,
        candidate: ResolvedFactoryCandidate,
        *,
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
            run_number=invocation.attempt,
            candidate_ref=candidate.candidate.source_archive_ref,
            execution_outcome=outcome,
            usage_receipt_refs=usage_refs,
            handoff_evidence_refs=run.handoff_evidence_refs,
            tool_evidence_refs=run.tool_evidence_refs,
            workflow_evidence_refs=run.workflow_evidence_refs,
            termination_reason=run.termination_reason,
            status=run.status,
        )


class TeamExecutionCandidateAdapter:
    """Wire CandidateEvaluationFactory real cases to the governed service."""

    def __init__(
        self,
        *,
        service_for: Callable[[AgentFactoryJobV3], TeamExecutionService],
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
        invocation = self._invocation_for(request)
        if request.lease is None or invocation.lease != request.lease:
            raise ValueError("team invocation must preserve the dispatch lease")
        return await self._service_for(request.job).execute(
            invocation,
            candidate,
            self._holdout_for(request.job),
        )

    @staticmethod
    def _sole_holdout(job: AgentFactoryJobV3) -> PrivateHoldoutRef:
        if len(job.private_holdout_refs) != 1:
            raise ValueError("multiple private holdouts require an explicit selector")
        return job.private_holdout_refs[0]


def _unique_refs(references: tuple[ArtifactRef, ...]) -> tuple[ArtifactRef, ...]:
    observed: dict[tuple[str, str, str], ArtifactRef] = {}
    for reference in references:
        key = (reference.uri, reference.sha256, reference.media_type)
        observed.setdefault(key, reference)
    return tuple(observed.values())


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
