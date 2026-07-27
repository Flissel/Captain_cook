from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Callable
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from agenten.agent_factory.business_benchmark import BusinessBenchmarkEvaluator
from agenten.agent_factory.business_benchmark_contracts import (
    BenchmarkDisposition,
    BusinessBenchmarkCaseV1,
    BusinessBenchmarkPolicyV1,
    BusinessBenchmarkRunReceiptV1,
    BusinessBenchmarkSuiteV1,
    BusinessCaseCategory,
)
from agenten.agent_factory.business_benchmark_execution import (
    BenchmarkExecutionPolicyV1,
    BusinessBenchmarkExecutionEnvelopeV1,
)
from agenten.agent_factory.business_benchmark_factory import (
    BusinessBenchmarkFactoryComposition,
)
from agenten.agent_factory.business_benchmark_replay import (
    BusinessBenchmarkEffectClaimV1,
    BusinessBenchmarkFenceReceiptV1,
    BusinessBenchmarkPreparedEffectV1,
    BusinessBenchmarkRecoveryObservationV1,
    BusinessBenchmarkRuntimePreparationV1,
    InMemoryBusinessBenchmarkReplayStore,
)
from agenten.agent_factory.business_benchmark_store import (
    InMemoryBusinessBenchmarkRepository,
)
from agenten.agent_factory.contracts import FactoryPhase
from agenten.agent_factory.factory_feedback import FactoryFeedbackBuilder
from agenten.agent_factory.service import FactoryCoordinator, InMemoryFactoryRepository
from agenten.agent_factory.skill_workflow_contracts import (
    FactoryFeedbackRecommendation,
    FactorySkillInvocationV1,
)
from agenten.agent_factory.state_machine import (
    FactoryActionKind,
    FactoryLifecycleError,
    FactoryLifecycleStatus,
)
from agenten.agent_factory.team_evaluation import TeamEvaluationService
from agenten.agent_runtime.contracts import ArtifactRef
from gateway.factory_repository import GatewayFactoryRepository
from gateway.app import _factory_promotion_benchmark_summary
from gateway.registry_feed import factory_promotion_projection
from minibook.src.projection import ProjectionEventV2, render_projection_event
from tests.agent_factory.test_factory_feedback import _report_invocation
from tests.agent_factory.test_release_gate import (
    workflow_budget,
    workflow_job,
    workflow_receipts,
    workflow_run,
)
from tests.agent_factory.test_state_machine import workflow_block
from tests.agent_factory.test_skill_workflow_contracts import invocation_payload


NOW = datetime(2026, 7, 27, 10, tzinfo=timezone.utc)
PROFILES = (
    "insurance_claims_resolution_swarm",
    "customer_renewal_orchestration_team",
)


def _artifact(label: str) -> ArtifactRef:
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()
    return ArtifactRef(
        uri=f"artifact://business-benchmark-integration/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def _suite(profile_id: str) -> BusinessBenchmarkSuiteV1:
    cases: list[BusinessBenchmarkCaseV1] = []
    for category in BusinessCaseCategory:
        for slot in range(1, 4):
            mandatory = category is BusinessCaseCategory.MANDATORY_ESCALATION
            if profile_id == "insurance_claims_resolution_swarm":
                decision = (
                    "escalate_coverage"
                    if mandatory
                    else "request_information"
                    if category is BusinessCaseCategory.INCOMPLETE
                    else "route_standard_review"
                )
            else:
                decision = (
                    "human_commercial_review"
                    if mandatory
                    else "request_information"
                    if category is BusinessCaseCategory.INCOMPLETE
                    else "propose_next_best_action"
                )
            cases.append(
                BusinessBenchmarkCaseV1(
                    schema="captain.business-benchmark-case.v1",
                    case_id=f"synthetic-{category.value}-{slot}",
                    profile_id=profile_id,
                    category=category,
                    redacted_input={
                        "synthetic_slot": slot,
                        "scenario_class": category.value,
                    },
                    expected_decision=decision,
                    required_rationale_fact_ids=(
                        f"fact-{category.value}-{slot}",
                    ),
                    allowed_tool_intents=(
                        ("n8n",)
                        if category is BusinessCaseCategory.BOUNDARY
                        else ("none",)
                    ),
                    human_handoff_required=mandatory,
                    severity="critical" if mandatory else "normal",
                )
            )
    return BusinessBenchmarkSuiteV1(
        schema="captain.business-benchmark-suite.v1",
        suite_id=f"{profile_id}-private-v1",
        profile_id=profile_id,
        suite_version=1,
        cases=tuple(cases),
        created_at=NOW,
    )


class _DeterministicExecutor:
    def __init__(self, *, wrong_candidate_decisions: int = 0) -> None:
        self._wrong_remaining = wrong_candidate_decisions
        self.envelopes: list[BusinessBenchmarkExecutionEnvelopeV1] = []

    async def prepare(
        self, envelope: BusinessBenchmarkExecutionEnvelopeV1
    ) -> BusinessBenchmarkRuntimePreparationV1:
        return BusinessBenchmarkRuntimePreparationV1(
            schema="captain.business-benchmark-runtime-preparation.v1",
            runtime_session_id=envelope.runtime_session_id,
        )

    async def register_fence(
        self,
        prepared: BusinessBenchmarkPreparedEffectV1,
        claim: BusinessBenchmarkEffectClaimV1,
    ) -> BusinessBenchmarkFenceReceiptV1:
        return BusinessBenchmarkFenceReceiptV1(
            schema="captain.business-benchmark-fence-receipt.v1",
            effect_id=prepared.identity.effect_id,
            runtime_session_id=prepared.runtime_session_id,
            claim_id=claim.claim_id,
            fence=claim.fence,
            registered_at=NOW,
            evidence_ref=_artifact(f"fence-{prepared.identity.effect_id}-{claim.fence}"),
        )

    async def recover(
        self,
        prepared: BusinessBenchmarkPreparedEffectV1,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
    ) -> BusinessBenchmarkRecoveryObservationV1:
        return BusinessBenchmarkRecoveryObservationV1(
            schema="captain.business-benchmark-recovery-observation.v1",
            effect_id=prepared.identity.effect_id,
            runtime_session_id=prepared.runtime_session_id,
            claim_id=claim.claim_id,
            fence=claim.fence,
            fence_receipt=fence_receipt,
            checked_at=NOW,
            evidence_ref=_artifact(
                f"recovery-{prepared.identity.effect_id}-{claim.fence}"
            ),
            outcome="no_effect",
        )

    async def execute(
        self,
        envelope: BusinessBenchmarkExecutionEnvelopeV1,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
    ) -> BusinessBenchmarkRunReceiptV1:
        assert fence_receipt.claim_id == claim.claim_id
        self.envelopes.append(envelope)
        observed_decision = envelope.case.expected_decision
        if envelope.variant == "candidate" and self._wrong_remaining:
            observed_decision = (
                "request_information"
                if envelope.case.expected_decision != "request_information"
                else (
                    "route_standard_review"
                    if envelope.case.profile_id == "insurance_claims_resolution_swarm"
                    else "propose_next_best_action"
                )
            )
            self._wrong_remaining -= 1
        return BusinessBenchmarkRunReceiptV1(
            schema="captain.business-benchmark-run-receipt.v1",
            run_id=uuid5(NAMESPACE_URL, f"run:{envelope.idempotency_key}"),
            request_id=envelope.request_id,
            execution_policy_sha256=envelope.execution_policy_sha256,
            runtime_session_id=envelope.runtime_session_id,
            job_id=envelope.job_id,
            correlation_id=envelope.correlation_id,
            subject_version=envelope.subject_version,
            attempt=envelope.attempt,
            suite_ref=envelope.suite_ref,
            suite_id=envelope.suite_id,
            case_id=envelope.case.case_id,
            case_sha256=envelope.case_sha256,
            variant=envelope.variant,
            candidate_ref=envelope.candidate_ref,
            model_version=envelope.model_version,
            allowed_tool_intents=envelope.allowed_tool_intents,
            maximum_cost_micro_usd=envelope.maximum_cost_micro_usd,
            maximum_latency_ms=envelope.maximum_latency_ms,
            status="succeeded",
            observed_decision=observed_decision,
            observed_rationale_fact_ids=envelope.case.required_rationale_fact_ids,
            observed_tool_intents=envelope.allowed_tool_intents,
            unsafe_tool_use=False,
            human_handoff_completed=envelope.case.human_handoff_required,
            cost_micro_usd=40,
            latency_ms=80,
            evidence_refs=(_artifact(f"run-evidence-{envelope.idempotency_key}"),),
            completed_at=NOW,
        )


class _DeterministicGatewayStore:
    """GatewayStore-shaped deterministic adapter; no DB or provider is used."""

    def __init__(self, *, budget, usage_receipts) -> None:
        self._factory = InMemoryFactoryRepository()
        self._artifacts: list[object] = []
        self._budget = budget
        self._usage_receipts = usage_receipts

    def record_factory_job(self, job):
        self._factory.register(job)
        return SimpleNamespace(replayed=False)

    def factory_job(self, job_id):
        return SimpleNamespace(
            job=self._factory.job(job_id),
            blocks=self._factory.blocks(job_id),
        )

    def record_factory_block(self, block):
        return SimpleNamespace(replayed=not self._factory.append(block))

    def factory_skill_evaluation(self, job_id):
        return None

    def factory_release_decision(self, job_id):
        return None

    def record_factory_workflow_artifact(self, artifact):
        if artifact in self._artifacts:
            return SimpleNamespace(replayed=True)
        self._artifacts.append(artifact)
        return SimpleNamespace(replayed=False)

    def factory_workflow_artifacts(self, job_id):
        return tuple(
            artifact
            for artifact in self._artifacts
            if getattr(artifact, "job_id", None) == job_id
        )

    def factory_budget(self, job_id):
        assert self._budget.job_id == job_id
        return self._budget

    def factory_usage_receipts(self, job_id):
        return tuple(item for item in self._usage_receipts if item.job_id == job_id)

    def record_business_benchmark_summary(self, summary):
        replayed = not self._factory.record_business_benchmark_summary(summary)
        return SimpleNamespace(replayed=replayed)

    def business_benchmark_summary(self, summary_id):
        return self._factory.business_benchmark_summary(summary_id)

    def business_benchmark_summary_by_artifact(self, artifact_ref):
        return self._factory.business_benchmark_summary_by_artifact(artifact_ref)


def _evaluation_invocation() -> FactorySkillInvocationV1:
    return FactorySkillInvocationV1.model_validate(invocation_payload("evaluate_team"))


def _execution_policy() -> BenchmarkExecutionPolicyV1:
    return BenchmarkExecutionPolicyV1(
        schema="captain.business-benchmark-execution-policy.v1",
        model_version="approved-model-id",
        allowed_tool_intents=("none",),
        maximum_cost_micro_usd=100,
        maximum_latency_ms=200,
        redaction_policy_version="business-redaction-v1",
        baseline_system_policy_version="single-agent-baseline-v1",
    )


def _lifecycle_block(job, phase: FactoryPhase, *, evaluation=None, feedback=None):
    candidate = workflow_block(
        phase,
        assertions=(
            job.acceptance_assertion_ids
            if phase in {FactoryPhase.QUALITY_REVIEWED, FactoryPhase.CAPABILITY_PROMOTED}
            else ()
        ),
    ).model_copy(
        update={
            "job_id": job.job_id,
            "correlation_id": job.correlation_id,
            "causation_id": job.event_id,
        }
    )
    if phase is FactoryPhase.QUALITY_REVIEWED:
        assert evaluation is not None and feedback is not None
        return candidate.model_copy(
            update={"artifact_refs": (evaluation.artifact_ref, feedback.artifact_ref)}
        )
    return candidate


async def _run_fixture(
    profile_id: str,
    *,
    wrong_candidate_decisions: int = 0,
):
    private_repository = InMemoryBusinessBenchmarkRepository()
    suite_ref = private_repository.add_suite(_suite(profile_id))
    job = workflow_job(mode="release").model_copy(
        update={"private_holdout_refs": (suite_ref,)}
    )
    runs = tuple(workflow_run(number) for number in range(1, 4))
    store = _DeterministicGatewayStore(
        budget=workflow_budget(), usage_receipts=workflow_receipts(runs)
    )
    gateway_repository = GatewayFactoryRepository(store)
    coordinator = FactoryCoordinator(gateway_repository)
    coordinator.register(job)
    for run in runs:
        gateway_repository.record_workflow_artifact(run)
    evaluation_invocation = _evaluation_invocation()
    evaluation_clock = evaluation_invocation.lease.issued_at + timedelta(minutes=1)
    uuid_numbers = iter(range(1, 100))

    result = await BusinessBenchmarkFactoryComposition(
        private_repository=private_repository,
        gateway_repository=gateway_repository,
        evaluator=BusinessBenchmarkEvaluator(
            clock=lambda: NOW,
            uuid_factory=lambda: uuid5(
                NAMESPACE_URL, f"benchmark-{profile_id}-{next(uuid_numbers)}"
            ),
        ),
        team_evaluator=TeamEvaluationService(clock=lambda: evaluation_clock),
        feedback_builder=FactoryFeedbackBuilder(
            clock=lambda: evaluation_clock + timedelta(minutes=1)
        ),
    ).run(
        job=job,
        profile_id=profile_id,
        suite_version=1,
        attempt=1,
        candidate_ref=runs[0].candidate_ref,
        executor=_DeterministicExecutor(
            wrong_candidate_decisions=wrong_candidate_decisions
        ),
        replay_store=InMemoryBusinessBenchmarkReplayStore(),
        execution_policy_factory=lambda benchmark_case: _execution_policy().model_copy(
            update={"allowed_tool_intents": benchmark_case.allowed_tool_intents}
        ),
        benchmark_policy=BusinessBenchmarkPolicyV1(
            schema="captain.business-benchmark-policy.v1",
            maximum_cost_ratio_bps=12500,
        ),
        evaluation_invocation=evaluation_invocation,
        report_invocation_factory=_report_invocation,
        technical_executions=runs,
        budget_projection=workflow_budget(),
    )
    return job, coordinator, store, result, runs


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_id", PROFILES)
async def test_green_business_benchmark_reaches_ready_to_use(profile_id: str) -> None:
    job, coordinator, store, result, _ = await _run_fixture(profile_id)

    assert len(result.candidate_receipts) == 15
    assert len(result.baseline_receipts) == 15
    assert result.summary.disposition is BenchmarkDisposition.PASSED
    assert result.summary.correlation_id == job.correlation_id
    assert result.summary.case_metrics and len(result.summary.case_metrics) == 15
    assert {receipt.allowed_tool_intents for receipt in result.candidate_receipts} == {
        ("none",),
        ("n8n",),
    }

    for phase in (
        FactoryPhase.FORGE_REQUESTED,
        FactoryPhase.BLUEPRINT_CREATED,
        FactoryPhase.TOOL_CANDIDATE_TESTED,
        FactoryPhase.AGENT_CODE_CREATED,
        FactoryPhase.BUILD_PASSED,
        FactoryPhase.REAL_CASE_EVIDENCE,
    ):
        coordinator.record(_lifecycle_block(job, phase))
    coordinator.record(
        _lifecycle_block(
            job,
            FactoryPhase.QUALITY_REVIEWED,
            evaluation=result.evaluation,
            feedback=result.feedback,
        )
    )
    coordinator.record(_lifecycle_block(job, FactoryPhase.CAPABILITY_PROMOTED))

    projection = coordinator.projection(job.job_id)
    assert projection.status is FactoryLifecycleStatus.READY_TO_USE
    assert projection.workflow_evaluation_ref == result.evaluation.artifact_ref
    promotion_payload = _lifecycle_block(
        job, FactoryPhase.CAPABILITY_PROMOTED
    ).model_dump(mode="json", by_alias=True)
    persisted_summary = _factory_promotion_benchmark_summary(store, promotion_payload)
    assert persisted_summary == result.summary
    public = factory_promotion_projection(
        promotion_payload,
        job.model_dump(mode="json", by_alias=True),
        benchmark_summary=persisted_summary,
    )
    rendered = public.model_dump(mode="json", by_alias=True)
    assert rendered["correlation_id"] == str(job.correlation_id)
    assert rendered["payload"]["benchmark_disposition"] == "passed"
    consumed = ProjectionEventV2.model_validate(rendered)
    post = render_projection_event(consumed)
    assert "Candidate correctness" in post.content
    assert result.summary.artifact_ref.sha256 in post.content
    forbidden = ("case_id", "redacted_input", "expected_decision", "rationale", "receipt")
    assert not any(token in public.model_dump_json(by_alias=True) for token in forbidden)


@pytest.mark.asyncio
async def test_business_red_requests_bounded_improvement_and_rejects_bypass() -> None:
    job, coordinator, _, result, technical_runs = await _run_fixture(
        "insurance_claims_resolution_swarm",
        wrong_candidate_decisions=2,
    )

    assert all(run.status == "succeeded" for run in technical_runs)
    assert result.summary.candidate_correctness_bps == 8667
    assert result.summary.baseline_correctness_bps == 10000
    assert set(result.summary.reason_codes) == {
        "wrong_decision",
        "below_minimum_correctness",
        "below_baseline_correctness",
    }
    assert len(result.summary.reason_codes) == 3
    assert result.evaluation.failure_class == "behavioral_failure"
    assert result.feedback.recommendation is FactoryFeedbackRecommendation.RETRY_BUILD

    for phase in (
        FactoryPhase.FORGE_REQUESTED,
        FactoryPhase.BLUEPRINT_CREATED,
        FactoryPhase.TOOL_CANDIDATE_TESTED,
        FactoryPhase.AGENT_CODE_CREATED,
        FactoryPhase.BUILD_PASSED,
        FactoryPhase.REAL_CASE_EVIDENCE,
    ):
        coordinator.record(_lifecycle_block(job, phase))
    coordinator.record(
        _lifecycle_block(
            job,
            FactoryPhase.QUALITY_REVIEWED,
            evaluation=result.evaluation,
            feedback=result.feedback,
        )
    )
    assert coordinator.next_action(job.job_id).kind is FactoryActionKind.APPEND_IMPROVEMENT_REQUESTED
    with pytest.raises(FactoryLifecycleError, match="business benchmark|workflow"):
        coordinator.record(_lifecycle_block(job, FactoryPhase.CAPABILITY_PROMOTED))
