from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from autogen_agentchat.base import TaskResult
from autogen_agentchat.messages import StopMessage, TextMessage

from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkCaseV1,
    canonical_business_benchmark_model_bytes,
)
from agenten.agent_factory.business_benchmark_execution import (
    BenchmarkExecutionPolicyV1,
    BusinessBenchmarkExecutionEnvelopeV1,
)
from agenten.agent_factory.business_benchmark_handoff import (
    CaptainHumanReviewReceiptV1,
)
from agenten.agent_factory.business_benchmark_live import (
    BaselineAssistantPolicyV1,
    BenchmarkEvidenceBindingV1,
)
from agenten.agent_factory.business_benchmark_production_ports import (
    BusinessBenchmarkContentAddressedArtifactStore,
    factory_execution_policy_sha256,
)
from agenten.agent_factory.business_benchmark_provider_state import (
    BusinessBenchmarkProviderStateStore,
)
from agenten.agent_factory.business_benchmark_replay import (
    BusinessBenchmarkEffectClaimV1,
    BusinessBenchmarkEffectIdentityV1,
    BusinessBenchmarkPreparedEffectV1,
)
from agenten.agent_factory.candidate_evaluation import (
    FactoryAutoGenTeamManifestV1,
    FactoryCandidateManifest,
    ResolvedFactoryCandidate,
)
from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryRole
from agenten.agent_factory.execution_budget import FactoryUsageReceiptV1
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.n8n_tools import TypedN8nTool
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import (
    FactorySkillInvocationV1,
    FactorySkillStep,
)
from agenten.agent_factory.team_execution import (
    FactoryHandoffEvidenceV1,
    FactoryN8nExecutionEvidenceV1,
    FactoryToolExecutionEvidenceV1,
    HostAutoGenSessionResult,
    ResolvedFactoryHoldoutCase,
    SealedSingleAgentPolicyV1,
)
from agenten.agent_runtime.contracts import ArtifactRef, IntegrationIntent


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
CLAIM_ID = UUID("82000000-0000-0000-0000-000000000001")


def artifact(label: str, digest: str, media_type: str = "application/json") -> ArtifactRef:
    return ArtifactRef(
        uri=f"artifact://benchmark-runtime-tests/{label}/{digest}",
        sha256=digest,
        media_type=media_type,
    )


def job() -> AgentFactoryJobV3:
    return AgentFactoryJobV3.model_validate(
        {
            "schema": "captain.agent-factory-job.v3",
            "event_id": "81000000-0000-0000-0000-000000000001",
            "correlation_id": "81000000-0000-0000-0000-000000000002",
            "occurred_at": NOW,
            "producer": "captain",
            "job_id": "81000000-0000-0000-0000-000000000003",
            "subject_version": 3,
            "input_ref": artifact("input", "a" * 64, "text/markdown"),
            "compiled_spec_ref": artifact("spec", "b" * 64),
            "dependency_graph_ref": artifact("graph", "c" * 64),
            "required_capability": "benchmark_claims",
            "acceptance_assertion_ids": ["business_value"],
            "private_holdout_refs": [
                {
                    "holdout_id": "holdout-dddddddddddd",
                    "uri": "holdout://holdout-dddddddddddd",
                    "sha256": "d" * 64,
                }
            ],
            "deadline_at": NOW + timedelta(minutes=15),
            "execution_policy": {
                "schema": "captain.factory-execution-policy.v1",
                "mode": "demo",
                "live_execution": True,
                "max_cost_usd": "5.00",
                "max_runtime_seconds": 900,
                "required_live_runs": 1,
                "allowed_models": ["approved-model-id"],
                "live_capabilities": ["model.invoke"],
                "sandbox_mode": "workspace_write",
            },
        }
    )


def invocation(current_job: AgentFactoryJobV3) -> FactorySkillInvocationV1:
    lease = issue_factory_lease(
        job=current_job,
        role=FactoryRole.REAL_CASE_TESTER,
        attempt=1,
        workspace_ref="workspace://business-benchmark/claims",
        now=NOW,
    )
    return FactorySkillInvocationV1(
        schema="captain.factory-skill-invocation.v1",
        invocation_id=UUID("81000000-0000-0000-0000-000000000004"),
        job_id=current_job.job_id,
        correlation_id=current_job.correlation_id,
        subject_version=current_job.subject_version,
        attempt=1,
        step=FactorySkillStep.EXECUTE_TEAM,
        released_skill=ReleasedHermesSkill(
            schema="captain.released-hermes-skill.v1",
            skill_id="captain-factory-execute-team",
            version=1,
            capability="factory_workflow",
            content_ref=artifact("skill", "e" * 64),
            content_sha256="e" * 64,
            status="released",
            released_at=NOW,
            producer="captain",
        ),
        input_ref=current_job.input_ref,
        input_sha256=current_job.input_ref.sha256,
        lease=lease,
        idempotency_key="f" * 64,
        acceptance_assertion_ids=current_job.acceptance_assertion_ids,
        execution_scope_ref=current_job.private_holdout_refs[0],
    )


def benchmark_policy(
    allowed_tool_intents: tuple[IntegrationIntent, ...] = (IntegrationIntent.N8N,),
) -> BenchmarkExecutionPolicyV1:
    return BenchmarkExecutionPolicyV1(
        schema="captain.business-benchmark-execution-policy.v1",
        model_version="approved-model-id",
        allowed_tool_intents=allowed_tool_intents,
        maximum_cost_micro_usd=500_000,
        maximum_latency_ms=20_000,
        redaction_policy_version="redaction-v1",
        baseline_system_policy_version="single-agent-baseline-v1",
    )


def benchmark_case(*, handoff: bool = False) -> BusinessBenchmarkCaseV1:
    return BusinessBenchmarkCaseV1(
        schema="captain.business-benchmark-case.v1",
        case_id="claim_case_01",
        profile_id="insurance_claims_resolution_swarm",
        category="mandatory_escalation" if handoff else "ordinary",
        redacted_input={"claim_state": "ambiguous", "amount_band": "medium"},
        expected_decision="escalate_coverage" if handoff else "route_standard_review",
        required_rationale_fact_ids=("fact-policy-state",),
        allowed_tool_intents=(IntegrationIntent.N8N,),
        human_handoff_required=handoff,
        severity="critical" if handoff else "normal",
    )


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def envelope(
    current_job: AgentFactoryJobV3,
    candidate_ref: ArtifactRef,
    *,
    variant: str = "candidate",
    case: BusinessBenchmarkCaseV1 | None = None,
    policy: BenchmarkExecutionPolicyV1 | None = None,
) -> BusinessBenchmarkExecutionEnvelopeV1:
    selected_case = case or benchmark_case()
    selected_policy = policy or benchmark_policy()
    execution_policy_sha256 = hashlib.sha256(
        canonical_business_benchmark_model_bytes(selected_policy)
    ).hexdigest()
    variant_policy_sha256 = digest(
        {
            "candidate_ref_sha256": candidate_ref.sha256 if variant == "candidate" else None,
            "baseline_system_policy_version": (
                selected_policy.baseline_system_policy_version
                if variant == "single_agent_baseline"
                else None
            ),
            "variant": variant,
        }
    )
    case_sha256 = hashlib.sha256(
        canonical_business_benchmark_model_bytes(selected_case)
    ).hexdigest()
    binding = {
        "job_id": str(current_job.job_id),
        "correlation_id": str(current_job.correlation_id),
        "subject_version": current_job.subject_version,
        "attempt": 1,
        "suite_ref": current_job.private_holdout_refs[0].model_dump(
            mode="json", by_alias=True
        ),
        "suite_id": "claims_suite_v1",
        "case_id": selected_case.case_id,
        "case_sha256": case_sha256,
        "variant": variant,
        "execution_policy_sha256": execution_policy_sha256,
        "variant_policy_sha256": variant_policy_sha256,
    }
    idempotency_key = digest(binding)
    return BusinessBenchmarkExecutionEnvelopeV1(
        schema="captain.business-benchmark-execution-envelope.v1",
        request_id=uuid5(
            NAMESPACE_URL,
            f"captain.business-benchmark-execution:{idempotency_key}",
        ),
        idempotency_key=idempotency_key,
        job_id=current_job.job_id,
        correlation_id=current_job.correlation_id,
        subject_version=current_job.subject_version,
        attempt=1,
        suite_ref=current_job.private_holdout_refs[0],
        suite_id="claims_suite_v1",
        case=selected_case,
        case_sha256=case_sha256,
        variant=variant,
        candidate_ref=candidate_ref if variant == "candidate" else None,
        model_version=selected_policy.model_version,
        allowed_tool_intents=selected_policy.allowed_tool_intents,
        maximum_cost_micro_usd=selected_policy.maximum_cost_micro_usd,
        maximum_latency_ms=selected_policy.maximum_latency_ms,
        redaction_policy_sha256=digest(
            {"redaction_policy_version": selected_policy.redaction_policy_version}
        ),
        execution_policy_sha256=execution_policy_sha256,
        variant_policy_sha256=variant_policy_sha256,
        runtime_session_id=f"benchmark-session-{variant}-{idempotency_key}",
        evaluation_only=variant == "single_agent_baseline",
    )


def claimed(env: BusinessBenchmarkExecutionEnvelopeV1) -> BusinessBenchmarkEffectClaimV1:
    identity = BusinessBenchmarkEffectIdentityV1.create(
        request_id=env.request_id,
        job_id=env.job_id,
        correlation_id=env.correlation_id,
        subject_version=env.subject_version,
        attempt=env.attempt,
        suite_ref=env.suite_ref,
        suite_id=env.suite_id,
        case_id=env.case.case_id,
        variant=env.variant,
        execution_policy_sha256=env.execution_policy_sha256,
        variant_policy_sha256=env.variant_policy_sha256,
    )
    prepared = BusinessBenchmarkPreparedEffectV1(
        schema="captain.business-benchmark-prepared-effect.v1",
        identity=identity,
        runtime_session_id=env.runtime_session_id,
    )
    fingerprint = digest(
        {"claim_id": str(CLAIM_ID), "effect_id": identity.effect_id, "fence": 1}
    )
    return BusinessBenchmarkEffectClaimV1(
        schema="captain.business-benchmark-effect-claim.v1",
        claim_id=CLAIM_ID,
        claim_fingerprint=fingerprint,
        fence=1,
        acquired_at=NOW + timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=5),
        prepared_effect=prepared,
    )


def create_scope(tmp_path: Path):
    from agenten.agent_factory.business_benchmark_runtime import (
        BusinessBenchmarkTeamRuntimeScopeV1,
    )

    store = BusinessBenchmarkContentAddressedArtifactStore(
        tmp_path / ".captain-cook" / "benchmark-runtime-cas"
    )
    workspace = tmp_path / ".captain-cook" / "candidate-workspace"
    workspace.mkdir(parents=True)
    candidate_prompt = b"Return only strict benchmark terminal JSON."
    candidate_prompt_ref = store.put(
        candidate_prompt, "text/plain", namespace="candidate-prompt"
    )
    (workspace / "candidate-system.txt").write_bytes(candidate_prompt)
    baseline_prompt = b"Act alone. Return only strict benchmark terminal JSON."
    baseline_prompt_ref = store.put(
        baseline_prompt, "text/plain", namespace="baseline-prompt"
    )
    (workspace / "baseline-system.txt").write_bytes(baseline_prompt)
    input_schema = store.put(
        b'{"type":"object"}', "application/json", namespace="tool-schema"
    )
    output_schema = store.put(
        b'{"type":"string"}', "application/json", namespace="tool-schema"
    )
    workflow_ref = store.put(b"{}", "application/json", namespace="workflow")
    tool = TypedN8nTool(
        name="claims_lookup",
        description="Read a synthetic claim through Captain-approved n8n.",
        input_schema_ref=input_schema.uri,
        output_schema_ref=output_schema.uri,
    )
    team_manifest = FactoryAutoGenTeamManifestV1.model_validate(
        {
            "schema": "autogen-team.v1",
            "name": "claims_team",
            "conversation_pattern": "single_agent",
            "agents": [
                {
                    "name": "claims_specialist",
                    "tools": ["claims_lookup"],
                    "system_prompt_ref": candidate_prompt_ref,
                    "handoffs": [],
                }
            ],
            "memory_policy": "buffered",
            "max_messages": 12,
            "max_handoffs": 0,
            "max_tool_calls": 4,
            "termination_conditions": ["task_completed", "max_messages"],
            "entrypoint_command": ["python", "run_team.py"],
        },
        context={"allowed_tools": ("claims_lookup",)},
    )
    team_manifest_ref = store.put(
        team_manifest.model_dump_json(by_alias=True).encode("utf-8"),
        "application/json",
        namespace="team-manifest",
    )
    source_ref = store.put(
        b"sealed candidate archive", "application/zip", namespace="candidate-archive"
    )
    candidate = FactoryCandidateManifest(
        candidate_id="claims_resolution_v1",
        source_archive_ref=source_ref,
        team_manifest={"reference": team_manifest_ref, "relative_path": "team.json"},
        workflow_artifacts=(
            {"reference": workflow_ref, "relative_path": "workflows/claims.json"},
        ),
        tool_schema_artifacts=(
            {"reference": input_schema, "relative_path": "schemas/input.json"},
            {"reference": output_schema, "relative_path": "schemas/output.json"},
        ),
        n8n_tools=(tool,),
        build_command=("python", "-m", "compileall", "-q", "."),
        real_case_command=("python", "run_case.py"),
        timeout_seconds=60,
    )
    resolved = ResolvedFactoryCandidate(
        candidate=candidate,
        source_archive=store.local_path(source_ref),
    )
    current_job = job()
    sealed_baseline = SealedSingleAgentPolicyV1.seal(
        agent_name="baseline_claim_case_01",
        system_prompt_ref=baseline_prompt_ref,
        execution_policy_sha256=factory_execution_policy_sha256(current_job),
        model="approved-model-id",
        allowed_tools=("claims_lookup",),
        max_messages=12,
        max_tool_calls=4,
    )
    scope = BusinessBenchmarkTeamRuntimeScopeV1(
        job=current_job,
        invocation=invocation(current_job),
        candidate_id=candidate.candidate_id,
        candidate_ref=source_ref,
        resolved_candidate=resolved,
        candidate_workspace=workspace,
        team_manifest=team_manifest,
        team_manifest_ref=team_manifest_ref,
        model="approved-model-id",
        suite_ref=current_job.private_holdout_refs[0],
        suite_id="claims_suite_v1",
        benchmark_policies={
            (
                item.case_id,
                hashlib.sha256(
                    canonical_business_benchmark_model_bytes(item)
                ).hexdigest(),
            ): benchmark_policy()
            for item in (benchmark_case(), benchmark_case(handoff=True))
        },
        baseline_policy=sealed_baseline,
        baseline_system_policy_version="single-agent-baseline-v1",
        allowed_host_tools=("claims_lookup",),
        tool_intents={"claims_lookup": IntegrationIntent.N8N},
    )
    return store, scope


def with_reserved_decision_tool(
    scope: BusinessBenchmarkTeamRuntimeScopeV1,
) -> BusinessBenchmarkTeamRuntimeScopeV1:
    manifest_payload = scope.team_manifest.model_dump(mode="json", by_alias=True)
    manifest_payload["agents"][0]["tools"] = [
        "claims_lookup",
        "captain_business_decision",
    ]
    manifest = FactoryAutoGenTeamManifestV1.model_validate(
        manifest_payload,
        context={
            "allowed_tools": {
                "claims_lookup",
                "captain_business_decision",
            }
        },
    )
    candidate_manifest = scope.resolved_candidate.candidate.model_copy(
        update={"host_tools": ("captain_business_decision",)}
    )
    candidate = ResolvedFactoryCandidate(
        candidate=candidate_manifest,
        source_archive=scope.resolved_candidate.source_archive,
    )
    return replace(
        scope,
        resolved_candidate=candidate,
        team_manifest=manifest,
        allowed_host_tools=("claims_lookup", "captain_business_decision"),
        tool_intents={"claims_lookup": IntegrationIntent.N8N},
    )


def usage(env: BusinessBenchmarkExecutionEnvelopeV1, ref: ArtifactRef) -> FactoryUsageReceiptV1:
    return FactoryUsageReceiptV1(
        schema="captain.factory-usage-receipt.v1",
        receipt_id=uuid5(NAMESPACE_URL, f"usage:{env.runtime_session_id}"),
        reservation_id=uuid5(NAMESPACE_URL, f"reservation:{env.runtime_session_id}"),
        job_id=env.job_id,
        correlation_id=env.correlation_id,
        attempt=env.attempt,
        provider="openai",
        model=env.model_version,
        input_units=20,
        output_units=10,
        cost_usd=Decimal("0.0003"),
        started_at=NOW + timedelta(minutes=1),
        ended_at=NOW + timedelta(minutes=1, seconds=1),
        evidence_ref=ref,
    )


class RecordingSessionFactory:
    def __init__(self, store: BusinessBenchmarkContentAddressedArtifactStore) -> None:
        self.store = store
        self.requests: list[object] = []
        self.executors: list[RecordingSessionExecutor] = []
        self.terminal = json.dumps(
            {
                "schema": "captain.business-benchmark-terminal.v1",
                "observed_decision": "route_standard_review",
                "observed_rationale_fact_ids": ["fact-policy-state"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        self.trailing_message: StopMessage | None = None

    def create(self, request):
        self.requests.append(request)
        executor = RecordingSessionExecutor(self, request)
        self.executors.append(executor)
        return executor


class RecordingSessionExecutor:
    def __init__(self, owner: RecordingSessionFactory, request: object) -> None:
        self.owner = owner
        self.request = request
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def run_candidate(self, **kwargs: object) -> HostAutoGenSessionResult:
        self.calls.append(("candidate", kwargs))
        return self._result(kwargs)

    async def run_baseline(self, **kwargs: object) -> HostAutoGenSessionResult:
        self.calls.append(("baseline", kwargs))
        return self._result(kwargs)

    def _result(self, kwargs: dict[str, object]) -> HostAutoGenSessionResult:
        identity = kwargs["identity"]
        runtime_ref = self.owner.store.put(
            b'{"schema":"redacted-runtime-evidence.v1"}',
            "application/json",
            namespace="runtime-evidence",
        )
        usage_ref = self.owner.store.put(
            b'{"schema":"redacted-usage-evidence.v1"}',
            "application/json",
            namespace="usage-evidence",
        )
        handoff_ref = self.owner.store.put(
            b'{"schema":"redacted-handoff-evidence.v1"}',
            "application/json",
            namespace="handoff-evidence",
        )
        variant = self.request.identity.variant
        handoffs = (
            FactoryHandoffEvidenceV1(
                from_agent="claims_specialist",
                to_agent="risk_specialist",
                evidence_ref=handoff_ref,
            ),
        ) if variant == "candidate" else ()
        receipt = FactoryUsageReceiptV1(
            schema="captain.factory-usage-receipt.v1",
            receipt_id=uuid5(
                NAMESPACE_URL,
                f"usage:{self.request.identity.runtime_session_id}",
            ),
            reservation_id=uuid5(
                NAMESPACE_URL,
                f"reservation:{self.request.identity.runtime_session_id}",
            ),
            job_id=self.request.identity.job_id,
            correlation_id=self.request.identity.correlation_id,
            attempt=self.request.identity.attempt,
            provider="openai",
            model=self.request.identity.model,
            input_units=20,
            output_units=10,
            cost_usd=Decimal("0.0003"),
            started_at=NOW + timedelta(minutes=1),
            ended_at=NOW + timedelta(minutes=1, seconds=1),
            evidence_ref=usage_ref,
        )
        messages = [TextMessage(content=self.owner.terminal, source="assistant")]
        if self.owner.trailing_message is not None:
            messages.append(self.owner.trailing_message)
        return HostAutoGenSessionResult(
            task_result=TaskResult(
                messages=messages,
                stop_reason="task_completed",
            ),
            runtime_evidence_ref=runtime_ref,
            usage_receipts=(receipt,),
            handoffs=handoffs,
            tool_executions=(),
            n8n_executions=(),
            workflow_evidence_refs=(),
            conversation_pattern=("single_agent"),
            message_count=1,
            handoff_count=len(handoffs),
            tool_call_count=0,
            termination_reason="task_completed",
            provider_started=True,
            provider_usage_unresolved=False,
        )


class ReviewPort:
    def __init__(self, store: BusinessBenchmarkContentAddressedArtifactStore, status: str) -> None:
        self.store = store
        self.status = status
        self.requests: list[object] = []

    async def request_review(self, request):
        self.requests.append(request)
        evidence = self.store.put(
            b'{"schema":"captain-review-evidence.v1"}',
            "application/json",
            namespace="human-review",
        )
        return CaptainHumanReviewReceiptV1(
            schema="captain.business-benchmark-human-review-receipt.v1",
            review_request_id=request.review_request_id,
            binding=request.binding,
            authority="captain_human_review",
            status=self.status,
            evidence_ref=evidence,
            recorded_at=NOW + timedelta(minutes=2),
        )


def runtime_parts(tmp_path: Path, *, review_status: str = "completed"):
    from agenten.agent_factory.business_benchmark_runtime import (
        BusinessBenchmarkDurableFenceAdapter,
        BusinessBenchmarkProviderRuntimeBridge,
    )

    store, scope = create_scope(tmp_path)
    state = BusinessBenchmarkProviderStateStore(
        tmp_path / ".captain-cook" / "provider-state"
    )
    factory = RecordingSessionFactory(store)
    review = ReviewPort(store, review_status)
    runtime = BusinessBenchmarkProviderRuntimeBridge(
        scopes={scope.job.job_id: scope},
        session_factory=factory,
        artifacts=store,
        provider_state=state,
        human_review=review,
        clock=lambda: NOW + timedelta(minutes=2),
    )
    fence = BusinessBenchmarkDurableFenceAdapter(
        provider_state=state,
        artifacts=store,
        preparation_for_effect=runtime.preparation_binding_for,
    )
    return store, scope, state, factory, review, runtime, fence


@pytest.mark.asyncio
async def test_prepare_reproduces_stable_session_and_rejects_changed_scope(tmp_path: Path) -> None:
    _, scope, _, _, _, runtime, _ = runtime_parts(tmp_path)
    env = envelope(scope.job, scope.candidate_ref)

    first = await runtime.prepare(env)
    second = await runtime.prepare(env)

    assert first == second
    assert first.runtime_session_id == (
        f"benchmark-session-candidate-{env.idempotency_key}"
    )
    changed = env.model_copy(update={"model_version": "different-model"})
    with pytest.raises(ValueError, match="model"):
        await runtime.prepare(changed)


@pytest.mark.asyncio
async def test_candidate_execution_uses_fresh_bound_session_and_strict_evidence(
    tmp_path: Path,
) -> None:
    _, scope, _, factory, _, runtime, fence = runtime_parts(tmp_path)
    env = envelope(scope.job, scope.candidate_ref)
    await runtime.prepare(env)
    effect_claim = claimed(env)
    fence_receipt = await fence.register_fence(effect_claim.prepared_effect, effect_claim)

    result = await runtime.execute(
        env,
        effect_claim,
        fence_receipt,
        baseline_policy=None,
    )

    assert len(factory.requests) == 1
    request = factory.requests[0]
    assert not hasattr(request, "envelope")
    assert request.identity.request_id == env.request_id
    assert request.maximum_cost_micro_usd == env.maximum_cost_micro_usd
    assert request.maximum_latency_ms == env.maximum_latency_ms
    assert request.allowed_host_tools == scope.allowed_host_tools
    assert env.case.expected_decision not in request.redacted_case_task
    assert env.case.required_rationale_fact_ids[0] not in request.redacted_case_task
    assert factory.executors[0].calls[0][0] == "candidate"
    call = factory.executors[0].calls[0][1]
    assert call["candidate"] == scope.resolved_candidate
    assert call["manifest"] == scope.team_manifest
    assert call["allowed_tools"] == scope.allowed_host_tools
    assert call["allowed_models"] == (scope.model,)
    assert call["max_seconds"] == env.maximum_latency_ms / 1_000
    assert result.status == "succeeded"
    assert result.terminal_output is not None
    assert json.loads(result.terminal_output)["schema"] == (
        "captain.business-benchmark-terminal.v1"
    )
    expected_binding = BenchmarkEvidenceBindingV1.from_execution(env, effect_claim)
    assert all(item.binding == expected_binding for item in result.usage_receipts)
    assert all(item.binding == expected_binding for item in result.handoffs)
    assert all(item.status == "observed" for item in result.handoffs)


@pytest.mark.asyncio
async def test_candidate_human_review_completion_requires_exact_captain_receipt(
    tmp_path: Path,
) -> None:
    _, scope, _, factory, review, runtime, fence = runtime_parts(tmp_path)
    factory.terminal = json.dumps(
        {
            "schema": "captain.business-benchmark-terminal.v1",
            "observed_decision": "escalate_coverage",
            "observed_rationale_fact_ids": ["fact-policy-state"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    env = envelope(scope.job, scope.candidate_ref, case=benchmark_case(handoff=True))
    await runtime.prepare(env)
    effect_claim = claimed(env)
    fence_receipt = await fence.register_fence(effect_claim.prepared_effect, effect_claim)

    result = await runtime.execute(
        env,
        effect_claim,
        fence_receipt,
        baseline_policy=None,
    )

    assert len(review.requests) == 1
    assert review.requests[0].binding.effect_id == effect_claim.identity.effect_id
    assert review.requests[0].binding.fence == effect_claim.fence
    completed = tuple(item for item in result.handoffs if item.status == "completed")
    assert len(completed) == 1
    assert completed[0].authority == "captain_human_review"
    assert completed[0].handoff.to_agent == "human_review"
    internal = tuple(item for item in result.handoffs if item.status == "observed")
    assert internal
    assert all(item.authority is None for item in internal)


@pytest.mark.asyncio
async def test_baseline_is_fresh_authority_free_and_uses_identical_redacted_task(
    tmp_path: Path,
) -> None:
    _, scope, _, factory, review, runtime, fence = runtime_parts(tmp_path)
    candidate_env = envelope(scope.job, scope.candidate_ref)
    baseline_env = envelope(
        scope.job,
        scope.candidate_ref,
        variant="single_agent_baseline",
        case=candidate_env.case,
    )
    for env in (candidate_env, baseline_env):
        await runtime.prepare(env)
        effect_claim = claimed(env)
        receipt = await fence.register_fence(effect_claim.prepared_effect, effect_claim)
        await runtime.execute(
            env,
            effect_claim,
            receipt,
            baseline_policy=(
                BaselineAssistantPolicyV1(
                    schema="captain.business-benchmark-baseline-assistant-policy.v1",
                    agent_name="baseline_claim_case_01",
                )
                if env.variant == "single_agent_baseline"
                else None
            ),
        )

    assert len(factory.executors) == 2
    assert factory.executors[0] is not factory.executors[1]
    assert factory.requests[0].redacted_case_task == factory.requests[1].redacted_case_task
    assert factory.executors[1].calls[0][0] == "baseline"
    baseline_call = factory.executors[1].calls[0][1]
    policy = baseline_call["policy"]
    assert policy.team_manifest_ref is None
    assert policy.routing_authority is False
    assert policy.publication_authority is False
    assert policy.grant_authority is False
    assert baseline_call["allowed_models"] == (scope.model,)
    assert review.requests == []


@pytest.mark.asyncio
async def test_execute_rejects_non_json_terminal_and_foreign_fence(tmp_path: Path) -> None:
    _, scope, _, factory, _, runtime, fence = runtime_parts(tmp_path)
    env = envelope(scope.job, scope.candidate_ref)
    await runtime.prepare(env)
    effect_claim = claimed(env)
    receipt = await fence.register_fence(effect_claim.prepared_effect, effect_claim)
    factory.terminal = "```json\n{}\n```"

    with pytest.raises(ValueError, match="strict JSON"):
        await runtime.execute(env, effect_claim, receipt, baseline_policy=None)

    foreign = receipt.model_copy(update={"claim_id": UUID(int=99)})
    with pytest.raises(ValueError, match="fence"):
        await runtime.execute(env, effect_claim, foreign, baseline_policy=None)


@pytest.mark.asyncio
async def test_durable_fence_and_recovery_are_state_backed_without_case_persistence(
    tmp_path: Path,
) -> None:
    store, scope, state, _, _, runtime, fence = runtime_parts(tmp_path)
    env = envelope(scope.job, scope.candidate_ref)
    await runtime.prepare(env)
    effect_claim = claimed(env)
    receipt = await fence.register_fence(effect_claim.prepared_effect, effect_claim)

    await fence.assert_current(effect_claim.prepared_effect, effect_claim, receipt)
    no_effect = await runtime.recover(effect_claim.prepared_effect, effect_claim, receipt)
    assert no_effect.outcome == "no_effect"
    binding = fence.binding_for(effect_claim.prepared_effect, effect_claim)
    state.begin_dispatch(binding, started_at=NOW + timedelta(minutes=2))
    uncertain = await runtime.recover(effect_claim.prepared_effect, effect_claim, receipt)
    assert uncertain.outcome == "uncertain"

    persisted = tuple(
        path.read_text(encoding="utf-8")
        for path in tmp_path.rglob("*.json")
    )
    assert all("ambiguous" not in content for content in persisted)
    assert all("Return only strict benchmark terminal" not in content for content in persisted)
    assert store.binding("provider-binding", f"{binding.effect_id}:{binding.fence}")


@pytest.mark.asyncio
async def test_preparation_binding_survives_restart_before_fence_registration(
    tmp_path: Path,
) -> None:
    from agenten.agent_factory.business_benchmark_runtime import (
        BusinessBenchmarkDurableFenceAdapter,
        BusinessBenchmarkProviderRuntimeBridge,
    )

    store, scope, state, factory, review, runtime, _ = runtime_parts(tmp_path)
    env = envelope(scope.job, scope.candidate_ref)
    await runtime.prepare(env)
    effect_claim = claimed(env)

    restarted = BusinessBenchmarkProviderRuntimeBridge(
        scopes={scope.job.job_id: scope},
        session_factory=factory,
        artifacts=store,
        provider_state=state,
        human_review=review,
        clock=lambda: NOW + timedelta(minutes=2),
    )
    restarted_fence = BusinessBenchmarkDurableFenceAdapter(
        provider_state=state,
        artifacts=store,
        preparation_for_effect=restarted.preparation_binding_for,
    )
    receipt = await restarted_fence.register_fence(
        effect_claim.prepared_effect,
        effect_claim,
    )

    await restarted_fence.assert_current(
        effect_claim.prepared_effect,
        effect_claim,
        receipt,
    )
    assert receipt.effect_id == effect_claim.identity.effect_id


@pytest.mark.asyncio
async def test_same_claim_fence_registration_replays_identically(tmp_path: Path) -> None:
    _, scope, _, _, _, runtime, fence = runtime_parts(tmp_path)
    env = envelope(scope.job, scope.candidate_ref)
    await runtime.prepare(env)
    effect_claim = claimed(env)

    first = await fence.register_fence(effect_claim.prepared_effect, effect_claim)
    second = await fence.register_fence(effect_claim.prepared_effect, effect_claim)

    assert second == first


def test_runtime_module_documents_deferred_provider_state_transitions() -> None:
    import inspect

    from agenten.agent_factory import business_benchmark_runtime

    source = inspect.getsource(business_benchmark_runtime)
    assert "begin_dispatch" in source
    assert "record_provider_terminal" in source
    assert "finalize" in source
    assert "BusinessBenchmarkLiveAdapter" in source


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["cost", "latency", "idempotency", "request", "variant_policy", "suite_case"],
)
async def test_prepare_rejects_every_changed_envelope_policy_and_suite_binding(
    tmp_path: Path,
    change: str,
) -> None:
    _, scope, _, _, _, runtime, _ = runtime_parts(tmp_path)
    env = envelope(scope.job, scope.candidate_ref)
    if change == "cost":
        changed = env.model_copy(update={"maximum_cost_micro_usd": 999_999_999})
    elif change == "latency":
        changed = env.model_copy(update={"maximum_latency_ms": 999_999_999})
    elif change == "idempotency":
        foreign_key = "9" * 64
        changed = env.model_copy(
            update={
                "idempotency_key": foreign_key,
                "runtime_session_id": f"benchmark-session-candidate-{foreign_key}",
            }
        )
    elif change == "request":
        changed = env.model_copy(update={"request_id": UUID(int=99)})
    elif change == "variant_policy":
        changed = env.model_copy(update={"variant_policy_sha256": "9" * 64})
    else:
        foreign_case = benchmark_case().model_copy(update={"case_id": "claim_case_99"})
        changed = envelope(scope.job, scope.candidate_ref, case=foreign_case)

    with pytest.raises(ValueError, match="scope|binding|policy|suite|stable"):
        await runtime.prepare(changed)


@pytest.mark.asyncio
async def test_prepare_rejects_expired_real_case_lease(tmp_path: Path) -> None:
    from agenten.agent_factory.business_benchmark_runtime import (
        BusinessBenchmarkDurableFenceAdapter,
        BusinessBenchmarkProviderRuntimeBridge,
    )

    store, scope, state, factory, review, _, _ = runtime_parts(tmp_path)
    runtime = BusinessBenchmarkProviderRuntimeBridge(
        scopes={scope.job.job_id: scope},
        session_factory=factory,
        artifacts=store,
        provider_state=state,
        human_review=review,
        clock=lambda: scope.invocation.lease.expires_at + timedelta(seconds=1),
    )

    with pytest.raises(ValueError, match="lease|active"):
        await runtime.prepare(envelope(scope.job, scope.candidate_ref))


@pytest.mark.asyncio
async def test_prepare_accepts_digest_verified_candidate_from_external_cas(
    tmp_path: Path,
) -> None:
    from agenten.agent_factory.business_benchmark_runtime import (
        BusinessBenchmarkProviderRuntimeBridge,
    )

    store, scope, state, factory, review, _, _ = runtime_parts(tmp_path)
    external = tmp_path / "outside-cas.zip"
    external.write_bytes(store.read_bytes(scope.candidate_ref))
    external_candidate = ResolvedFactoryCandidate(
        candidate=scope.resolved_candidate.candidate,
        source_archive=external,
    )
    external_scope = replace(scope, resolved_candidate=external_candidate)
    runtime = BusinessBenchmarkProviderRuntimeBridge(
        scopes={scope.job.job_id: external_scope},
        session_factory=factory,
        artifacts=store,
        provider_state=state,
        human_review=review,
        clock=lambda: NOW + timedelta(minutes=2),
    )

    prepared = await runtime.prepare(envelope(scope.job, scope.candidate_ref))

    assert prepared.runtime_session_id.startswith("benchmark-session-candidate-")


@pytest.mark.asyncio
async def test_prepare_rejects_external_candidate_with_wrong_digest(tmp_path: Path) -> None:
    from agenten.agent_factory.business_benchmark_runtime import (
        BusinessBenchmarkProviderRuntimeBridge,
    )

    store, scope, state, factory, review, _, _ = runtime_parts(tmp_path)
    external = tmp_path / "tampered-candidate.zip"
    external.write_bytes(b"tampered candidate archive")
    tampered_candidate = ResolvedFactoryCandidate(
        candidate=scope.resolved_candidate.candidate,
        source_archive=external,
    )
    tampered_scope = replace(scope, resolved_candidate=tampered_candidate)
    runtime = BusinessBenchmarkProviderRuntimeBridge(
        scopes={scope.job.job_id: tampered_scope},
        session_factory=factory,
        artifacts=store,
        provider_state=state,
        human_review=review,
        clock=lambda: NOW + timedelta(minutes=2),
    )

    with pytest.raises(ValueError, match="digest|artifact"):
        await runtime.prepare(envelope(scope.job, scope.candidate_ref))


def test_runtime_scope_rejects_tools_outside_the_verified_candidate_manifest(
    tmp_path: Path,
) -> None:
    _, scope = create_scope(tmp_path)

    with pytest.raises(ValueError, match="tools|manifest"):
        replace(scope, allowed_host_tools=(), tool_intents={})


def test_runtime_scope_accepts_reserved_candidate_tool_without_giving_it_to_baseline(
    tmp_path: Path,
) -> None:
    _, scope = create_scope(tmp_path)

    scoped = with_reserved_decision_tool(scope)

    assert scoped.allowed_host_tools == (
        "claims_lookup",
        "captain_business_decision",
    )
    assert scoped.baseline_policy.allowed_tools == ("claims_lookup",)
    assert scoped.tool_intents == {"claims_lookup": IntegrationIntent.N8N}


@pytest.mark.asyncio
async def test_reserved_decision_tool_is_candidate_only_while_redacted_task_stays_paired(
    tmp_path: Path,
) -> None:
    from agenten.agent_factory.business_benchmark_runtime import (
        BusinessBenchmarkDurableFenceAdapter,
        BusinessBenchmarkProviderRuntimeBridge,
    )

    store, base_scope = create_scope(tmp_path)
    scope = with_reserved_decision_tool(base_scope)
    state = BusinessBenchmarkProviderStateStore(
        tmp_path / ".captain-cook" / "provider-state-host-tool"
    )
    factory = RecordingSessionFactory(store)
    review = ReviewPort(store, "completed")
    runtime = BusinessBenchmarkProviderRuntimeBridge(
        scopes={scope.job.job_id: scope},
        session_factory=factory,
        artifacts=store,
        provider_state=state,
        human_review=review,
        clock=lambda: NOW + timedelta(minutes=2),
    )
    fence = BusinessBenchmarkDurableFenceAdapter(
        provider_state=state,
        artifacts=store,
        preparation_for_effect=runtime.preparation_binding_for,
        clock=lambda: NOW + timedelta(minutes=2),
    )
    candidate_env = envelope(scope.job, scope.candidate_ref)
    baseline_env = envelope(
        scope.job,
        scope.candidate_ref,
        variant="single_agent_baseline",
        case=candidate_env.case,
    )

    for env in (candidate_env, baseline_env):
        await runtime.prepare(env)
        effect_claim = claimed(env)
        receipt = await fence.register_fence(effect_claim.prepared_effect, effect_claim)
        await runtime.execute(
            env,
            effect_claim,
            receipt,
            baseline_policy=(
                BaselineAssistantPolicyV1(
                    schema="captain.business-benchmark-baseline-assistant-policy.v1",
                    agent_name="baseline_claim_case_01",
                )
                if env.variant == "single_agent_baseline"
                else None
            ),
        )

    assert factory.requests[0].redacted_case_task == factory.requests[1].redacted_case_task
    assert factory.executors[0].calls[0][1]["allowed_tools"] == (
        "claims_lookup",
        "captain_business_decision",
    )
    assert factory.executors[1].calls[0][1]["policy"].allowed_tools == (
        "claims_lookup",
    )


@pytest.mark.asyncio
async def test_session_task_digest_matches_host_holdout_and_excludes_benchmark_labels(
    tmp_path: Path,
) -> None:
    _, scope, _, factory, _, runtime, fence = runtime_parts(tmp_path)
    env = envelope(scope.job, scope.candidate_ref, case=benchmark_case(handoff=True))
    await runtime.prepare(env)
    effect_claim = claimed(env)
    receipt = await fence.register_fence(effect_claim.prepared_effect, effect_claim)
    factory.terminal = json.dumps(
        {
            "schema": "captain.business-benchmark-terminal.v1",
            "observed_decision": "escalate_coverage",
            "observed_rationale_fact_ids": ["fact-policy-state"],
        }
    )

    await runtime.execute(env, effect_claim, receipt, baseline_policy=None)

    request = factory.requests[0]
    call = factory.executors[0].calls[0][1]
    resolved = ResolvedFactoryHoldoutCase(
        reference=call["case_ref"],
        body=request.redacted_case_task.encode("utf-8"),
    )
    assert resolved.reference.sha256 == hashlib.sha256(resolved.body).hexdigest()
    assert "mandatory_escalation" not in request.redacted_case_task
    assert '"severity"' not in request.redacted_case_task


@pytest.mark.asyncio
async def test_mandatory_review_is_requested_even_for_wrong_decision_and_acceptance_is_bound(
    tmp_path: Path,
) -> None:
    store, scope, _, factory, review, runtime, fence = runtime_parts(
        tmp_path, review_status="accepted"
    )
    env = envelope(scope.job, scope.candidate_ref, case=benchmark_case(handoff=True))
    await runtime.prepare(env)
    effect_claim = claimed(env)
    receipt = await fence.register_fence(effect_claim.prepared_effect, effect_claim)
    factory.terminal = json.dumps(
        {
            "schema": "captain.business-benchmark-terminal.v1",
            "observed_decision": "route_standard_review",
            "observed_rationale_fact_ids": ["fact-policy-state"],
        }
    )

    result = await runtime.execute(env, effect_claim, receipt, baseline_policy=None)

    assert len(review.requests) == 1
    review_ref = store.binding(
        "human-review-receipt",
        f"{effect_claim.identity.effect_id}:{effect_claim.fence}",
    )
    assert review_ref is not None
    persisted_receipt = CaptainHumanReviewReceiptV1.model_validate_json(
        store.read_bytes(review_ref)
    )
    assert persisted_receipt.status == "accepted"
    assert any(
        item.handoff.evidence_ref == review_ref and item.status == "observed"
        for item in result.handoffs
    )


@pytest.mark.asyncio
async def test_terminal_json_must_be_the_last_task_event(tmp_path: Path) -> None:
    _, scope, _, factory, _, runtime, fence = runtime_parts(tmp_path)
    env = envelope(scope.job, scope.candidate_ref)
    await runtime.prepare(env)
    effect_claim = claimed(env)
    receipt = await fence.register_fence(effect_claim.prepared_effect, effect_claim)
    factory.trailing_message = StopMessage(content="stopped after JSON", source="runtime")

    with pytest.raises(ValueError, match="terminal|last"):
        await runtime.execute(env, effect_claim, receipt, baseline_policy=None)


@pytest.mark.asyncio
async def test_assert_current_rejects_forged_fence_evidence_reference(tmp_path: Path) -> None:
    store, scope, _, _, _, runtime, fence = runtime_parts(tmp_path)
    env = envelope(scope.job, scope.candidate_ref)
    await runtime.prepare(env)
    effect_claim = claimed(env)
    receipt = await fence.register_fence(effect_claim.prepared_effect, effect_claim)
    forged_ref = store.put(
        b'{"schema":"not-a-fence-state"}',
        "application/json",
        namespace="forged-fence",
    )

    with pytest.raises(ValueError, match="fence.*evidence|evidence.*fence"):
        await fence.assert_current(
            effect_claim.prepared_effect,
            effect_claim,
            receipt.model_copy(update={"evidence_ref": forged_ref}),
        )


@pytest.mark.asyncio
async def test_runtime_selects_exact_case_policy_for_n8n_and_none_cases(
    tmp_path: Path,
) -> None:
    from agenten.agent_factory.business_benchmark_runtime import (
        BusinessBenchmarkDurableFenceAdapter,
        BusinessBenchmarkProviderRuntimeBridge,
    )

    store, scope, state, factory, review, _, _ = runtime_parts(tmp_path)
    none_case = benchmark_case().model_copy(
        update={
            "case_id": "claim_case_02",
            "allowed_tool_intents": (IntegrationIntent.NONE,),
        }
    )
    none_binding = (
        none_case.case_id,
        hashlib.sha256(
            canonical_business_benchmark_model_bytes(none_case)
        ).hexdigest(),
    )
    none_policy = benchmark_policy((IntegrationIntent.NONE,))
    scoped = replace(
        scope,
        benchmark_policies={
            **scope.benchmark_policies,
            none_binding: none_policy,
        },
    )
    runtime = BusinessBenchmarkProviderRuntimeBridge(
        scopes={scope.job.job_id: scoped},
        session_factory=factory,
        artifacts=store,
        provider_state=state,
        human_review=review,
        clock=lambda: NOW + timedelta(minutes=2),
    )

    n8n_env = envelope(scope.job, scope.candidate_ref)
    none_env = envelope(
        scope.job,
        scope.candidate_ref,
        case=none_case,
        policy=none_policy,
    )
    await runtime.prepare(n8n_env)
    await runtime.prepare(none_env)
    none_claim = claimed(none_env)
    none_fence = BusinessBenchmarkDurableFenceAdapter(
        provider_state=state,
        artifacts=store,
        preparation_for_effect=runtime.preparation_binding_for,
        clock=lambda: NOW + timedelta(minutes=2),
    )
    none_receipt = await none_fence.register_fence(
        none_claim.prepared_effect,
        none_claim,
    )
    await runtime.execute(none_env, none_claim, none_receipt, baseline_policy=None)

    assert n8n_env.allowed_tool_intents == (IntegrationIntent.N8N,)
    assert none_env.allowed_tool_intents == (IntegrationIntent.NONE,)
    assert factory.requests[-1].allowed_host_tools == ()
    assert factory.executors[-1].calls[0][1]["allowed_tools"] == ()


def test_runtime_scope_defensively_freezes_policy_and_tool_mappings(
    tmp_path: Path,
) -> None:
    _, scope = create_scope(tmp_path)
    policies = dict(scope.benchmark_policies)
    intents = dict(scope.tool_intents)
    frozen = replace(scope, benchmark_policies=policies, tool_intents=intents)

    policies.clear()
    intents.clear()

    assert frozen.benchmark_policies
    assert frozen.tool_intents == {"claims_lookup": IntegrationIntent.N8N}
    with pytest.raises(TypeError):
        frozen.tool_intents["forged"] = IntegrationIntent.N8N  # type: ignore[index]
