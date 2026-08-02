"""Provider-backed runtime and durable fence adapters for business benchmarks.

The bridge deliberately keeps private case inputs in process.  Durable state
contains only content digests, Captain identities, redacted terminal output,
and typed provider evidence consumed by :class:`BusinessBenchmarkLiveAdapter`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Awaitable, Callable, Literal, Mapping, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from autogen_agentchat.messages import TextMessage
from pydantic import BaseModel, ConfigDict, Field

from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkRunReceiptV1,
    canonical_business_benchmark_model_bytes,
)
from agenten.agent_factory.business_benchmark_execution import (
    BenchmarkExecutionPolicyV1,
    BusinessBenchmarkExecutionEnvelopeV1,
)
from agenten.agent_factory.business_benchmark_handoff import (
    CaptainHumanReviewPort,
    CaptainHumanReviewRequestV1,
    validate_captain_human_review_receipt,
)
from agenten.agent_factory.business_benchmark_live import (
    BaselineAssistantPolicyV1,
    BenchmarkEvidenceBindingV1,
    BenchmarkTerminalOutputV1,
    BoundBenchmarkHandoffEvidenceV1,
    BoundBenchmarkToolEvidenceV1,
    BoundBenchmarkUsageEvidenceV1,
    BusinessBenchmarkLiveAdapter,
    ProviderBenchmarkExecutionV1,
)
from agenten.agent_factory.business_benchmark_production_ports import (
    BusinessBenchmarkContentAddressedArtifactStore,
    factory_execution_policy_sha256,
)
from agenten.agent_factory.business_benchmark_provider_state import (
    BusinessBenchmarkProviderBindingV1,
    BusinessBenchmarkProviderStateV1,
    BusinessBenchmarkProviderStateStore,
)
from agenten.agent_factory.business_benchmark_replay import (
    BusinessBenchmarkEffectClaimV1,
    BusinessBenchmarkEffectIdentityV1,
    BusinessBenchmarkFenceReceiptV1,
    BusinessBenchmarkPreparedEffectV1,
    BusinessBenchmarkRecoveryObservationV1,
    BusinessBenchmarkRuntimePreparationV1,
)
from agenten.agent_factory.candidate_evaluation import (
    FactoryAutoGenTeamManifestV1,
    ResolvedFactoryCandidate,
)
from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryRole
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_factory.leases import FactoryLeaseDenied, validate_factory_lease
from agenten.agent_factory.skill_workflow_contracts import FactorySkillInvocationV1
from agenten.agent_factory.team_execution import (
    FactoryHandoffEvidenceV1,
    HostAutoGenSessionCancelledError,
    HostAutoGenSessionExecutor,
    HostAutoGenSessionIdentityV1,
    HostAutoGenSessionResult,
    HostAutoGenSessionTimeoutError,
    SealedSingleAgentPolicyV1,
)
from agenten.agent_runtime.contracts import ArtifactRef, IntegrationIntent


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class BusinessBenchmarkPreparationBindingV1(_FrozenModel):
    """Restart-safe envelope projection that excludes case and prompt bodies."""

    schema_name: Literal["captain.business-benchmark-preparation-binding.v1"] = Field(
        default="captain.business-benchmark-preparation-binding.v1",
        alias="schema",
        serialization_alias="schema",
    )
    effect_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_session_id: str = Field(min_length=1, max_length=200)
    request_id: UUID
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    case_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    variant: Literal["candidate", "single_agent_baseline"]
    model_version: str = Field(min_length=1)


@dataclass(frozen=True)
class BusinessBenchmarkTeamRuntimeScopeV1:
    """Exact Captain-owned runtime material for one candidate team attempt."""

    job: AgentFactoryJobV3
    invocation: FactorySkillInvocationV1
    candidate_id: str
    candidate_ref: ArtifactRef
    resolved_candidate: ResolvedFactoryCandidate
    candidate_workspace: Path
    team_manifest: FactoryAutoGenTeamManifestV1
    team_manifest_ref: ArtifactRef
    model: str
    suite_ref: PrivateHoldoutRef
    suite_id: str
    benchmark_policies: Mapping[
        tuple[str, str], BenchmarkExecutionPolicyV1
    ]
    baseline_policy: SealedSingleAgentPolicyV1
    baseline_system_policy_version: str
    allowed_host_tools: tuple[str, ...]
    tool_intents: Mapping[str, IntegrationIntent]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "benchmark_policies",
            MappingProxyType(dict(self.benchmark_policies)),
        )
        object.__setattr__(
            self,
            "tool_intents",
            MappingProxyType(dict(self.tool_intents)),
        )
        if (
            self.invocation.job_id != self.job.job_id
            or self.invocation.correlation_id != self.job.correlation_id
            or self.invocation.subject_version != self.job.subject_version
            or self.invocation.lease.role is not FactoryRole.REAL_CASE_TESTER
            or self.invocation.attempt != self.invocation.lease.attempt
        ):
            raise ValueError("runtime invocation does not match the Captain job")
        if (
            self.candidate_id != self.resolved_candidate.candidate.candidate_id
            or self.candidate_ref
            != self.resolved_candidate.candidate.source_archive_ref
            or self.model not in self.job.execution_policy.allowed_models
            or any(
                policy.model_version != self.model
                or policy.baseline_system_policy_version
                != self.baseline_system_policy_version
                for policy in self.benchmark_policies.values()
            )
            or self.baseline_policy.model != self.model
            or self.baseline_system_policy_version
            != "single-agent-baseline-v1"
            or self.baseline_policy.execution_policy_sha256
            != factory_execution_policy_sha256(self.job)
            or self.team_manifest_ref
            != self.resolved_candidate.candidate.team_manifest.reference
        ):
            raise ValueError("runtime candidate, model, or baseline scope is stale")
        manifest_tools = {
            tool_name
            for agent in self.team_manifest.agents
            for tool_name in agent.tools
        }
        n8n_tools = {
            tool.name for tool in self.resolved_candidate.candidate.n8n_tools
        }
        reserved_host_tools = set(self.resolved_candidate.candidate.host_tools)
        baseline_tools = set(self.baseline_policy.allowed_tools)
        if (
            len(self.allowed_host_tools) != len(set(self.allowed_host_tools))
            or set(self.allowed_host_tools) != manifest_tools
            or set(self.allowed_host_tools) != n8n_tools | reserved_host_tools
            or set(self.tool_intents) != n8n_tools
            or baseline_tools != n8n_tools
            or any(
                not isinstance(intent, IntegrationIntent)
                for intent in self.tool_intents.values()
            )
            or (
                {
                    intent
                    for policy in self.benchmark_policies.values()
                    for intent in policy.allowed_tool_intents
                    if intent is not IntegrationIntent.NONE
                }
                - set(self.tool_intents.values())
            )
        ):
            raise ValueError(
                "runtime host tools require exact manifest and intent bindings"
            )
        if self.invocation.execution_scope_ref != self.suite_ref:
            raise ValueError("runtime invocation is not scoped to the benchmark suite")
        if not self.benchmark_policies or any(
            not case_id
            or len(case_sha256) != 64
            or any(character not in "0123456789abcdef" for character in case_sha256)
            for case_id, case_sha256 in self.benchmark_policies
        ):
            raise ValueError("runtime per-case benchmark policies are invalid")


@dataclass(frozen=True)
class BusinessBenchmarkSessionRequestV1:
    identity: HostAutoGenSessionIdentityV1
    case_ref: PrivateHoldoutRef
    benchmark_case_sha256: str
    redacted_case_task: str = field(repr=False)
    allowed_host_tools: tuple[str, ...]
    maximum_cost_micro_usd: int
    maximum_latency_ms: int
    before_provider_dispatch: Callable[[], Awaitable[None]] | None = field(
        default=None,
        repr=False,
    )


class BusinessBenchmarkSessionFactoryPort(Protocol):
    def create(
        self, request: BusinessBenchmarkSessionRequestV1
    ) -> HostAutoGenSessionExecutor: ...


class BusinessBenchmarkProviderRuntimeBridge:
    """Map one fenced benchmark envelope into a fresh host AutoGen session."""

    def __init__(
        self,
        *,
        scopes: Mapping[UUID, BusinessBenchmarkTeamRuntimeScopeV1],
        session_factory: BusinessBenchmarkSessionFactoryPort,
        artifacts: BusinessBenchmarkContentAddressedArtifactStore,
        provider_state: BusinessBenchmarkProviderStateStore,
        human_review: CaptainHumanReviewPort,
        clock: Callable[[], datetime],
    ) -> None:
        self._scopes = dict(scopes)
        if not self._scopes or any(key != value.job.job_id for key, value in self._scopes.items()):
            raise ValueError("runtime scopes must be keyed by exact Captain job IDs")
        self._session_factory = session_factory
        self._artifacts = artifacts
        self._provider_state = provider_state
        self._human_review = human_review
        self._clock = clock

    async def prepare(
        self, envelope: BusinessBenchmarkExecutionEnvelopeV1
    ) -> BusinessBenchmarkRuntimePreparationV1:
        scope = self._scope_for(envelope)
        identity = self._identity_for(envelope)
        expected_session = f"benchmark-session-{envelope.variant}-{envelope.idempotency_key}"
        if envelope.runtime_session_id != expected_session:
            raise ValueError("runtime session is not stable for the benchmark envelope")
        binding = BusinessBenchmarkPreparationBindingV1(
            schema="captain.business-benchmark-preparation-binding.v1",
            effect_id=identity.effect_id,
            runtime_session_id=envelope.runtime_session_id,
            request_id=envelope.request_id,
            job_id=envelope.job_id,
            correlation_id=envelope.correlation_id,
            subject_version=envelope.subject_version,
            attempt=envelope.attempt,
            case_sha256=envelope.case_sha256,
            variant=envelope.variant,
            model_version=envelope.model_version,
        )
        reference = self._artifacts.put(
            canonical_business_benchmark_model_bytes(binding),
            "application/json",
            namespace="provider-preparation",
        )
        self._artifacts.bind("provider-preparation", identity.effect_id, reference)
        # Scope validation is intentionally completed before the durable prepare write.
        assert scope.job.job_id == envelope.job_id
        return BusinessBenchmarkRuntimePreparationV1(
            schema="captain.business-benchmark-runtime-preparation.v1",
            runtime_session_id=envelope.runtime_session_id,
        )

    def preparation_binding_for(
        self,
        prepared: BusinessBenchmarkPreparedEffectV1,
        claim: BusinessBenchmarkEffectClaimV1,
    ) -> BusinessBenchmarkProviderBindingV1:
        if claim.prepared_effect != prepared:
            raise ValueError("provider preparation does not match its claim")
        reference = self._artifacts.binding(
            "provider-preparation", prepared.identity.effect_id
        )
        if reference is None:
            raise ValueError("durable provider preparation binding is unavailable")
        projection = BusinessBenchmarkPreparationBindingV1.model_validate_json(
            self._artifacts.read_bytes(reference)
        )
        if (
            projection.effect_id != prepared.identity.effect_id
            or projection.runtime_session_id != prepared.runtime_session_id
            or projection.request_id != prepared.identity.request_id
            or projection.job_id != prepared.identity.job_id
            or projection.correlation_id != prepared.identity.correlation_id
            or projection.subject_version != prepared.identity.subject_version
            or projection.attempt != prepared.identity.attempt
            or projection.variant != prepared.identity.variant
        ):
            raise ValueError("durable provider preparation binding is stale")
        return BusinessBenchmarkProviderBindingV1(
            schema="captain.business-benchmark-provider-binding.v1",
            effect_id=projection.effect_id,
            runtime_session_id=projection.runtime_session_id,
            claim_id=claim.claim_id,
            fence=claim.fence,
            job_id=projection.job_id,
            correlation_id=projection.correlation_id,
            attempt=projection.attempt,
            request_id=projection.request_id,
            case_sha256=projection.case_sha256,
            variant=projection.variant,
            model_version=projection.model_version,
        )

    async def execute(
        self,
        envelope: BusinessBenchmarkExecutionEnvelopeV1,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
        *,
        baseline_policy: BaselineAssistantPolicyV1 | None,
        before_provider_dispatch: Callable[[], Awaitable[None]] | None = None,
    ) -> ProviderBenchmarkExecutionV1:
        scope = self._scope_for(envelope)
        if (
            claim.identity != self._identity_for(envelope)
            or claim.prepared_effect.runtime_session_id
            != envelope.runtime_session_id
        ):
            raise ValueError("benchmark envelope does not match the claimed effect")
        binding = self.preparation_binding_for(claim.prepared_effect, claim)
        if (
            binding.request_id != envelope.request_id
            or binding.case_sha256 != envelope.case_sha256
            or binding.variant != envelope.variant
            or binding.model_version != envelope.model_version
        ):
            raise ValueError("provider binding does not match the benchmark envelope")
        self._require_fence(binding, fence_receipt)
        if envelope.variant == "candidate" and baseline_policy is not None:
            raise ValueError("candidate execution cannot receive a baseline policy")
        if envelope.variant == "single_agent_baseline" and baseline_policy is None:
            raise ValueError("baseline execution requires its sealed host policy")

        integration_tools = tuple(
            name
            for name in scope.allowed_host_tools
            if name in scope.tool_intents
            and scope.tool_intents[name] in envelope.allowed_tool_intents
        )
        candidate_allowed_tools = tuple(
            name
            for name in scope.allowed_host_tools
            if name in scope.resolved_candidate.candidate.host_tools
            or name in integration_tools
        )
        baseline_allowed_tools = tuple(
            name
            for name in scope.baseline_policy.allowed_tools
            if name in integration_tools
        )
        execution_allowed_tools = (
            candidate_allowed_tools
            if envelope.variant == "candidate"
            else baseline_allowed_tools
        )
        redacted_case_task = self._redacted_case_task(
            envelope,
            candidate_allowed_tools,
        )
        case_ref = self._case_reference(redacted_case_task)
        identity = HostAutoGenSessionIdentityV1.for_factory_execution(
            job=scope.job,
            invocation=scope.invocation,
            case_ref=case_ref,
            subject_id=(
                scope.candidate_id
                if envelope.variant == "candidate"
                else "single_agent_baseline"
            ),
            variant=envelope.variant,
            request_id=envelope.request_id,
            runtime_session_id=envelope.runtime_session_id,
            effect_id=claim.identity.effect_id,
            claim_id=claim.claim_id,
            fence=claim.fence,
            model=envelope.model_version,
        )
        request = BusinessBenchmarkSessionRequestV1(
            identity=identity,
            case_ref=case_ref,
            benchmark_case_sha256=envelope.case_sha256,
            redacted_case_task=redacted_case_task,
            allowed_host_tools=candidate_allowed_tools,
            maximum_cost_micro_usd=envelope.maximum_cost_micro_usd,
            maximum_latency_ms=envelope.maximum_latency_ms,
            before_provider_dispatch=before_provider_dispatch,
        )
        session = self._session_factory.create(request)
        try:
            if envelope.variant == "candidate":
                observed = await session.run_candidate(
                    job=scope.job,
                    invocation=scope.invocation,
                    case_ref=case_ref,
                    identity=identity,
                    candidate=scope.resolved_candidate,
                    manifest=scope.team_manifest,
                    allowed_tools=execution_allowed_tools,
                    allowed_models=(scope.model,),
                    max_seconds=envelope.maximum_latency_ms / 1_000,
                )
            else:
                assert baseline_policy is not None
                if (
                    baseline_policy.system_policy_version
                    != scope.baseline_system_policy_version
                ):
                    raise ValueError("baseline system policy version is stale")
                sealed = SealedSingleAgentPolicyV1.seal(
                    agent_name=baseline_policy.agent_name,
                    system_prompt_ref=scope.baseline_policy.system_prompt_ref,
                    execution_policy_sha256=scope.baseline_policy.execution_policy_sha256,
                    model=scope.model,
                    allowed_tools=execution_allowed_tools,
                    max_messages=scope.baseline_policy.max_messages,
                    max_tool_calls=scope.baseline_policy.max_tool_calls,
                )
                observed = await session.run_baseline(
                    job=scope.job,
                    invocation=scope.invocation,
                    case_ref=case_ref,
                    identity=identity,
                    policy=sealed,
                    workspace=scope.candidate_workspace,
                    allowed_models=(scope.model,),
                    max_seconds=envelope.maximum_latency_ms / 1_000,
                )
        except HostAutoGenSessionTimeoutError as exc:
            if exc.provider_usage_unresolved:
                raise ValueError("provider usage is unresolved after session timeout") from exc
            return self._interrupted(envelope, claim, exc, status="failed")
        except HostAutoGenSessionCancelledError:
            raise

        return await self._provider_result(
            envelope=envelope,
            claim=claim,
            provider_binding=binding,
            observed=observed,
        )

    async def recover(
        self,
        prepared: BusinessBenchmarkPreparedEffectV1,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
    ) -> BusinessBenchmarkRecoveryObservationV1:
        binding = self.preparation_binding_for(prepared, claim)
        self._require_fence(binding, fence_receipt)
        recovery = self._provider_state.recover(binding)
        evidence_ref = self._artifacts.put(
            canonical_business_benchmark_model_bytes(recovery),
            "application/json",
            namespace="provider-recovery",
        )
        return BusinessBenchmarkRecoveryObservationV1(
            schema="captain.business-benchmark-recovery-observation.v1",
            effect_id=binding.effect_id,
            runtime_session_id=binding.runtime_session_id,
            claim_id=binding.claim_id,
            fence=binding.fence,
            fence_receipt=fence_receipt,
            checked_at=self._utc_now(),
            evidence_ref=evidence_ref,
            outcome=recovery.outcome,
            receipt=recovery.receipt,
        )

    def _scope_for(
        self, envelope: BusinessBenchmarkExecutionEnvelopeV1
    ) -> BusinessBenchmarkTeamRuntimeScopeV1:
        try:
            scope = self._scopes[envelope.job_id]
        except KeyError as exc:
            raise ValueError("Captain benchmark runtime scope is unavailable") from exc
        case_binding = (envelope.case.case_id, envelope.case_sha256)
        try:
            case_policy = scope.benchmark_policies[case_binding]
        except KeyError as exc:
            raise ValueError("benchmark case is not bound to the selected suite") from exc
        expected_policy_sha = hashlib.sha256(
            canonical_business_benchmark_model_bytes(case_policy)
        ).hexdigest()
        expected_redaction_sha = _digest_json(
            {"redaction_policy_version": case_policy.redaction_policy_version}
        )
        expected_candidate = (
            scope.candidate_ref if envelope.variant == "candidate" else None
        )
        expected_variant_policy_sha256 = _digest_json(
            {
                "candidate_ref_sha256": (
                    scope.candidate_ref.sha256
                    if envelope.variant == "candidate"
                    else None
                ),
                "baseline_system_policy_version": (
                    case_policy.baseline_system_policy_version
                    if envelope.variant == "single_agent_baseline"
                    else None
                ),
                "variant": envelope.variant,
            }
        )
        idempotency_key = _envelope_idempotency_key(envelope)
        expected_request_id = uuid5(
            NAMESPACE_URL,
            f"captain.business-benchmark-execution:{idempotency_key}",
        )
        if (
            envelope.correlation_id != scope.job.correlation_id
            or envelope.subject_version != scope.job.subject_version
            or envelope.attempt != scope.invocation.attempt
            or envelope.model_version != scope.model
            or envelope.suite_ref != scope.suite_ref
            or envelope.suite_id != scope.suite_id
            or envelope.candidate_ref != expected_candidate
            or envelope.execution_policy_sha256 != expected_policy_sha
            or envelope.redaction_policy_sha256 != expected_redaction_sha
            or envelope.allowed_tool_intents
            != case_policy.allowed_tool_intents
            or envelope.maximum_cost_micro_usd
            != case_policy.maximum_cost_micro_usd
            or envelope.maximum_latency_ms
            != case_policy.maximum_latency_ms
            or envelope.variant_policy_sha256
            != expected_variant_policy_sha256
            or envelope.idempotency_key != idempotency_key
            or envelope.request_id != expected_request_id
        ):
            raise ValueError("benchmark envelope model or authority scope is stale")
        try:
            validate_factory_lease(
                scope.invocation.lease,
                job=scope.job,
                role=FactoryRole.REAL_CASE_TESTER,
                attempt=scope.invocation.attempt,
                now=self._utc_now(),
            )
        except FactoryLeaseDenied as exc:
            raise ValueError("runtime REAL_CASE_TESTER lease is not active") from exc
        _verify_candidate_archive(
            scope.resolved_candidate.source_archive,
            scope.candidate_ref,
        )
        return scope

    @staticmethod
    def _identity_for(
        envelope: BusinessBenchmarkExecutionEnvelopeV1,
    ) -> BusinessBenchmarkEffectIdentityV1:
        return BusinessBenchmarkEffectIdentityV1.create(
            request_id=envelope.request_id,
            job_id=envelope.job_id,
            correlation_id=envelope.correlation_id,
            subject_version=envelope.subject_version,
            attempt=envelope.attempt,
            suite_ref=envelope.suite_ref,
            suite_id=envelope.suite_id,
            case_id=envelope.case.case_id,
            variant=envelope.variant,
            execution_policy_sha256=envelope.execution_policy_sha256,
            variant_policy_sha256=envelope.variant_policy_sha256,
        )

    @staticmethod
    def _case_reference(redacted_case_task: str):
        task_sha256 = hashlib.sha256(redacted_case_task.encode("utf-8")).hexdigest()
        holdout_id = f"holdout-{task_sha256[:12]}"
        return PrivateHoldoutRef(
            holdout_id=holdout_id,
            uri=f"holdout://{holdout_id}",
            sha256=task_sha256,
        )

    @staticmethod
    def _redacted_case_task(
        envelope: BusinessBenchmarkExecutionEnvelopeV1,
        allowed_tools: tuple[str, ...],
    ) -> str:
        return json.dumps(
            {
                "schema": "captain.business-benchmark-redacted-task.v1",
                "case_id": envelope.case.case_id,
                "profile_id": envelope.case.profile_id,
                "redacted_input": envelope.case.redacted_input,
                "allowed_tool_intents": [
                    item.value for item in envelope.allowed_tool_intents
                ],
                "allowed_tools": list(allowed_tools),
                "required_output_schema": "captain.business-benchmark-terminal.v1",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    async def _provider_result(
        self,
        *,
        envelope: BusinessBenchmarkExecutionEnvelopeV1,
        claim: BusinessBenchmarkEffectClaimV1,
        provider_binding: BusinessBenchmarkProviderBindingV1,
        observed: HostAutoGenSessionResult,
    ) -> ProviderBenchmarkExecutionV1:
        if not observed.provider_started or observed.provider_usage_unresolved:
            raise ValueError("provider session has unresolved paid-effect evidence")
        terminal_output = self._terminal_output(observed)
        terminal_ref = self._artifacts.put(
            terminal_output.encode("utf-8"),
            "application/json",
            namespace="provider-terminal",
        )
        binding = BenchmarkEvidenceBindingV1.from_execution(envelope, claim)
        n8n_by_name: dict[str, list[object]] = {}
        for item in observed.n8n_executions:
            n8n_by_name.setdefault(item.tool_name, []).append(item)
        tool_evidence: list[BoundBenchmarkToolEvidenceV1] = []
        for item in observed.tool_executions:
            candidates = n8n_by_name.get(item.tool_name, [])
            n8n_item = candidates.pop(0) if candidates else None
            tool_evidence.append(
                BoundBenchmarkToolEvidenceV1(
                    binding=binding,
                    execution=item,
                    n8n_execution=n8n_item,
                )
            )
        if any(n8n_by_name.values()):
            raise ValueError("n8n evidence has no matching host tool execution")
        handoffs = [
            BoundBenchmarkHandoffEvidenceV1(
                binding=binding,
                handoff=item,
                status="observed",
            )
            for item in observed.handoffs
        ]
        terminal = BenchmarkTerminalOutputV1.model_validate_json(terminal_output)
        if (
            envelope.variant == "candidate"
            and envelope.case.human_handoff_required
        ):
            request = CaptainHumanReviewRequestV1(
                schema="captain.business-benchmark-human-review-request.v1",
                review_request_id=uuid5(
                    NAMESPACE_URL,
                    f"captain-benchmark-human-review:{provider_binding.effect_id}:{provider_binding.fence}",
                ),
                binding=provider_binding,
                reason_code="mandatory_human_review",
                requested_at=self._utc_now(),
            )
            receipt = validate_captain_human_review_receipt(
                request,
                await self._human_review.request_review(request),
            )
            receipt_record_ref = self._artifacts.put(
                canonical_business_benchmark_model_bytes(receipt),
                "application/json",
                namespace="human-review-receipt",
            )
            self._artifacts.bind(
                "human-review-receipt",
                f"{provider_binding.effect_id}:{provider_binding.fence}",
                receipt_record_ref,
            )
            handoffs.append(
                BoundBenchmarkHandoffEvidenceV1(
                    binding=binding,
                    handoff=FactoryHandoffEvidenceV1(
                        from_agent="captain",
                        to_agent="human_review",
                        evidence_ref=receipt_record_ref,
                    ),
                    authority=(
                        "captain_human_review"
                        if receipt.status == "completed"
                        else None
                    ),
                    status=(
                        "completed" if receipt.status == "completed" else "observed"
                    ),
                )
            )
        return ProviderBenchmarkExecutionV1(
            request_id=envelope.request_id,
            runtime_session_id=envelope.runtime_session_id,
            model_version=envelope.model_version,
            variant=envelope.variant,
            candidate_ref=envelope.candidate_ref,
            case_sha256=envelope.case_sha256,
            maximum_cost_micro_usd=envelope.maximum_cost_micro_usd,
            maximum_latency_ms=envelope.maximum_latency_ms,
            redaction_policy_sha256=envelope.redaction_policy_sha256,
            status="succeeded",
            terminal_output=terminal_output,
            usage_receipts=tuple(
                BoundBenchmarkUsageEvidenceV1(binding=binding, receipt=item)
                for item in observed.usage_receipts
            ),
            runtime_evidence_ref=observed.runtime_evidence_ref,
            terminal_evidence_ref=terminal_ref,
            tool_executions=tuple(tool_evidence),
            handoffs=tuple(handoffs),
            completed_at=self._utc_now(),
        )

    @staticmethod
    def _terminal_output(observed: HostAutoGenSessionResult) -> str:
        if not observed.task_result.messages or not isinstance(
            observed.task_result.messages[-1], TextMessage
        ):
            raise ValueError("provider terminal JSON must be the last task event")
        terminal_message = observed.task_result.messages[-1]
        if not isinstance(terminal_message.content, str):
            raise ValueError("provider terminal output is missing")
        raw = terminal_message.content
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("provider terminal output is not strict JSON") from exc
        terminal = BenchmarkTerminalOutputV1.model_validate(parsed)
        return json.dumps(
            terminal.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _interrupted(
        self,
        envelope: BusinessBenchmarkExecutionEnvelopeV1,
        claim: BusinessBenchmarkEffectClaimV1,
        interruption: HostAutoGenSessionTimeoutError,
        *,
        status: str,
    ) -> ProviderBenchmarkExecutionV1:
        evidence_ref = self._artifacts.put(
            json.dumps(
                {
                    "schema": "captain.business-benchmark-provider-interruption.v1",
                    "effect_id": claim.identity.effect_id,
                    "provider_started": interruption.provider_started,
                    "usage_resolved": not interruption.provider_usage_unresolved,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            "application/json",
            namespace="provider-interruption",
        )
        binding = BenchmarkEvidenceBindingV1.from_execution(envelope, claim)
        return ProviderBenchmarkExecutionV1(
            request_id=envelope.request_id,
            runtime_session_id=envelope.runtime_session_id,
            model_version=envelope.model_version,
            variant=envelope.variant,
            candidate_ref=envelope.candidate_ref,
            case_sha256=envelope.case_sha256,
            maximum_cost_micro_usd=envelope.maximum_cost_micro_usd,
            maximum_latency_ms=envelope.maximum_latency_ms,
            redaction_policy_sha256=envelope.redaction_policy_sha256,
            status=status,
            usage_receipts=tuple(
                BoundBenchmarkUsageEvidenceV1(binding=binding, receipt=item)
                for item in interruption.usage_receipts
            ),
            runtime_evidence_ref=evidence_ref,
            completed_at=self._utc_now(),
        )

    @staticmethod
    def _require_fence(
        binding: BusinessBenchmarkProviderBindingV1,
        receipt: BusinessBenchmarkFenceReceiptV1,
    ) -> None:
        if (
            receipt.effect_id != binding.effect_id
            or receipt.runtime_session_id != binding.runtime_session_id
            or receipt.claim_id != binding.claim_id
            or receipt.fence != binding.fence
        ):
            raise ValueError("provider fence does not match the prepared effect")

    def _utc_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("business benchmark runtime clock must be timezone-aware")
        return value.astimezone(timezone.utc)


class BusinessBenchmarkDurableFenceAdapter:
    """Async live-adapter facade over Captain's append-only provider store."""

    def __init__(
        self,
        *,
        provider_state: BusinessBenchmarkProviderStateStore,
        artifacts: BusinessBenchmarkContentAddressedArtifactStore,
        preparation_for_effect: Callable[
            [BusinessBenchmarkPreparedEffectV1, BusinessBenchmarkEffectClaimV1],
            BusinessBenchmarkProviderBindingV1,
        ],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._provider_state = provider_state
        self._artifacts = artifacts
        self._preparation_for_effect = preparation_for_effect
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def binding_for(
        self,
        prepared: BusinessBenchmarkPreparedEffectV1,
        claim: BusinessBenchmarkEffectClaimV1,
    ) -> BusinessBenchmarkProviderBindingV1:
        return self._preparation_for_effect(prepared, claim)

    async def register_fence(
        self,
        prepared: BusinessBenchmarkPreparedEffectV1,
        claim: BusinessBenchmarkEffectClaimV1,
    ) -> BusinessBenchmarkFenceReceiptV1:
        binding = self.binding_for(prepared, claim)
        state = self._provider_state.register_fence(
            binding, registered_at=claim.acquired_at
        )
        evidence_ref = self._artifacts.put(
            canonical_business_benchmark_model_bytes(state),
            "application/json",
            namespace="provider-fence",
        )
        self._artifacts.bind(
            "provider-binding", f"{binding.effect_id}:{binding.fence}", evidence_ref
        )
        return BusinessBenchmarkFenceReceiptV1(
            schema="captain.business-benchmark-fence-receipt.v1",
            effect_id=binding.effect_id,
            runtime_session_id=binding.runtime_session_id,
            claim_id=binding.claim_id,
            fence=binding.fence,
            registered_at=state.recorded_at,
            evidence_ref=evidence_ref,
        )

    async def assert_current(
        self,
        prepared: BusinessBenchmarkPreparedEffectV1,
        claim: BusinessBenchmarkEffectClaimV1,
        receipt: BusinessBenchmarkFenceReceiptV1,
    ) -> None:
        binding = self.binding_for(prepared, claim)
        BusinessBenchmarkProviderRuntimeBridge._require_fence(binding, receipt)
        try:
            encoded = self._artifacts.read_bytes(receipt.evidence_ref)
            fenced = BusinessBenchmarkProviderStateV1.model_validate_json(encoded)
            if (
                canonical_business_benchmark_model_bytes(fenced) != encoded
                or fenced.stage != "fenced"
                or fenced.binding != binding
                or fenced.recorded_at != receipt.registered_at
            ):
                raise ValueError("provider fence evidence does not match its receipt")
        except ValueError as exc:
            raise ValueError("provider fence evidence is invalid") from exc
        self._provider_state.assert_current(binding)

    async def begin_dispatch(
        self,
        prepared: BusinessBenchmarkPreparedEffectV1,
        claim: BusinessBenchmarkEffectClaimV1,
        receipt: BusinessBenchmarkFenceReceiptV1,
    ) -> None:
        await self.assert_current(prepared, claim, receipt)
        self._provider_state.begin_dispatch(
            self.binding_for(prepared, claim), started_at=self._utc_now()
        )

    async def record_provider_terminal(
        self,
        prepared: BusinessBenchmarkPreparedEffectV1,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
        run_receipt: BusinessBenchmarkRunReceiptV1,
    ) -> None:
        await self.assert_current(prepared, claim, fence_receipt)
        self._provider_state.record_provider_terminal(
            self.binding_for(prepared, claim),
            run_receipt,
            recorded_at=self._utc_now(),
        )

    async def finalize(
        self,
        prepared: BusinessBenchmarkPreparedEffectV1,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
        run_receipt: BusinessBenchmarkRunReceiptV1,
    ) -> BusinessBenchmarkRunReceiptV1:
        await self.assert_current(prepared, claim, fence_receipt)
        return self._provider_state.finalize(
            self.binding_for(prepared, claim),
            run_receipt,
            finalized_at=self._utc_now(),
        )

    def _utc_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("provider fence clock must be timezone-aware")
        return value.astimezone(timezone.utc)


def _digest_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _verify_candidate_archive(source_archive: Path, reference: ArtifactRef) -> None:
    try:
        resolved = source_archive.resolve(strict=True)
        if not resolved.is_file():
            raise ValueError("runtime candidate artifact is not a file")
        with resolved.open("rb") as stream:
            actual_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    except (OSError, ValueError) as exc:
        raise ValueError("runtime candidate artifact is unavailable") from exc
    if actual_sha256 != reference.sha256:
        raise ValueError("runtime candidate artifact digest does not match its authority")


def _envelope_idempotency_key(
    envelope: BusinessBenchmarkExecutionEnvelopeV1,
) -> str:
    return _digest_json(
        {
            "job_id": str(envelope.job_id),
            "correlation_id": str(envelope.correlation_id),
            "subject_version": envelope.subject_version,
            "attempt": envelope.attempt,
            "suite_ref": envelope.suite_ref.model_dump(mode="json", by_alias=True),
            "suite_id": envelope.suite_id,
            "case_id": envelope.case.case_id,
            "case_sha256": envelope.case_sha256,
            "variant": envelope.variant,
            "execution_policy_sha256": envelope.execution_policy_sha256,
            "variant_policy_sha256": envelope.variant_policy_sha256,
        }
    )


__all__ = [
    "BusinessBenchmarkDurableFenceAdapter",
    "BusinessBenchmarkPreparationBindingV1",
    "BusinessBenchmarkProviderRuntimeBridge",
    "BusinessBenchmarkSessionFactoryPort",
    "BusinessBenchmarkSessionRequestV1",
    "BusinessBenchmarkTeamRuntimeScopeV1",
]
