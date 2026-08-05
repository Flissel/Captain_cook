from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Callable
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from pydantic import ValidationError

from agenten.agent_factory.business_benchmark import BusinessBenchmarkEvaluator
from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkCaseV1,
    BusinessBenchmarkPolicyV1,
    BusinessBenchmarkRunReceiptV1,
    BusinessBenchmarkSuiteV1,
    BusinessCaseCategory,
)
from agenten.agent_factory.business_benchmark_execution import (
    BenchmarkExecutionPolicyV1,
    BusinessBenchmarkExecutionEnvelopeV1,
    BusinessBenchmarkExecutionError,
    PairedBusinessBenchmarkCoordinator,
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
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_runtime.contracts import ArtifactRef, IntegrationIntent


NOW = datetime(2026, 7, 26, 10, tzinfo=timezone.utc)
JOB_ID = UUID("00000000-0000-0000-0000-000000000301")
CORRELATION_ID = UUID("00000000-0000-0000-0000-000000000302")


def artifact(label: str) -> ArtifactRef:
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()
    return ArtifactRef(
        uri=f"artifact://business-benchmark-test/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def suite_ref() -> PrivateHoldoutRef:
    digest = hashlib.sha256(b"private-suite").hexdigest()
    holdout_id = f"holdout-{digest[:12]}"
    return PrivateHoldoutRef(
        holdout_id=holdout_id,
        uri=f"holdout://{holdout_id}",
        sha256=digest,
    )


def benchmark_case() -> BusinessBenchmarkCaseV1:
    return BusinessBenchmarkCaseV1(
        schema="captain.business-benchmark-case.v1",
        case_id="claims-ordinary-01",
        profile_id="insurance_claims_resolution_swarm",
        category="ordinary",
        redacted_input={"test_organization_id": "test-org"},
        expected_decision="route_standard_review",
        required_rationale_fact_ids=("fact-policy-state",),
        allowed_tool_intents=("none",),
        human_handoff_required=False,
        severity="normal",
    )


def execution_policy() -> BenchmarkExecutionPolicyV1:
    return BenchmarkExecutionPolicyV1(
        schema="captain.business-benchmark-execution-policy.v1",
        model_version="approved-model-v1",
        allowed_tool_intents=("none",),
        maximum_cost_micro_usd=100,
        maximum_latency_ms=200,
        redaction_policy_version="business-redaction-v1",
        baseline_system_policy_version="single-agent-baseline-v1",
    )


class RecordingExecutor:
    def __init__(
        self,
        receipt_transform: (
            Callable[
                [BusinessBenchmarkRunReceiptV1, BusinessBenchmarkExecutionEnvelopeV1],
                BusinessBenchmarkRunReceiptV1,
            ]
            | None
        ) = None,
    ) -> None:
        self.envelopes: list[BusinessBenchmarkExecutionEnvelopeV1] = []
        self.prepared_effects: list[BusinessBenchmarkPreparedEffectV1] = []
        self._receipt_transform = receipt_transform

    async def prepare(
        self, envelope: BusinessBenchmarkExecutionEnvelopeV1
    ) -> BusinessBenchmarkRuntimePreparationV1:
        return BusinessBenchmarkRuntimePreparationV1(
            schema="captain.business-benchmark-runtime-preparation.v1",
            runtime_session_id=envelope.runtime_session_id,
        )

    async def execute(
        self,
        envelope: BusinessBenchmarkExecutionEnvelopeV1,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
    ) -> BusinessBenchmarkRunReceiptV1:
        assert claim.prepared_effect.runtime_session_id == envelope.runtime_session_id
        assert fence_receipt.claim_id == claim.claim_id
        self.envelopes.append(envelope)
        receipt = BusinessBenchmarkRunReceiptV1(
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
            observed_decision="route_standard_review",
            observed_rationale_fact_ids=("fact-policy-state",),
            observed_tool_intents=envelope.allowed_tool_intents,
            unsafe_tool_use=False,
            human_handoff_completed=False,
            cost_micro_usd=40,
            latency_ms=80,
            evidence_refs=(artifact(f"evidence-{envelope.variant}"),),
            completed_at=NOW,
        )
        if self._receipt_transform is not None:
            return self._receipt_transform(receipt, envelope)
        return receipt

    async def register_fence(
        self,
        prepared: BusinessBenchmarkPreparedEffectV1,
        claim: BusinessBenchmarkEffectClaimV1,
    ) -> BusinessBenchmarkFenceReceiptV1:
        self.prepared_effects.append(prepared)
        return BusinessBenchmarkFenceReceiptV1(
            schema="captain.business-benchmark-fence-receipt.v1",
            effect_id=prepared.identity.effect_id,
            runtime_session_id=prepared.runtime_session_id,
            claim_id=claim.claim_id,
            fence=claim.fence,
            registered_at=NOW,
            evidence_ref=artifact(
                f"fence-{prepared.identity.effect_id}-{claim.fence}"
            ),
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
            evidence_ref=artifact(
                f"recovery-{prepared.identity.effect_id}-{claim.fence}"
            ),
            outcome="no_effect",
        )


def coordinator(executor: RecordingExecutor) -> PairedBusinessBenchmarkCoordinator:
    return PairedBusinessBenchmarkCoordinator(
        job_id=JOB_ID,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        attempt=1,
        suite_id="claims-suite-v1",
        executor=executor,
        replay_store=InMemoryBusinessBenchmarkReplayStore(),
    )


async def run_pair(
    target: PairedBusinessBenchmarkCoordinator,
    *,
    case: BusinessBenchmarkCaseV1 | None = None,
    policy: BenchmarkExecutionPolicyV1 | None = None,
) -> tuple[BusinessBenchmarkRunReceiptV1, BusinessBenchmarkRunReceiptV1]:
    return await target.run_case_pair(
        case=case or benchmark_case(),
        suite_ref=suite_ref(),
        candidate_ref=artifact("candidate-package"),
        execution_policy=policy or execution_policy(),
    )


@pytest.mark.asyncio
async def test_pair_uses_identical_controlled_inputs_and_distinct_deterministic_ids() -> None:
    first_executor = RecordingExecutor()
    first = coordinator(first_executor)
    candidate, baseline = await run_pair(first)

    candidate_envelope, baseline_envelope = first_executor.envelopes
    shared_fields = (
        "suite_ref",
        "case",
        "case_sha256",
        "model_version",
        "allowed_tool_intents",
        "maximum_cost_micro_usd",
        "maximum_latency_ms",
        "redaction_policy_sha256",
        "execution_policy_sha256",
    )
    for field in shared_fields:
        assert getattr(candidate_envelope, field) == getattr(baseline_envelope, field)
    assert candidate_envelope.request_id != baseline_envelope.request_id
    assert candidate_envelope.idempotency_key != baseline_envelope.idempotency_key
    assert candidate_envelope.variant_policy_sha256 != baseline_envelope.variant_policy_sha256
    assert candidate.request_id == candidate_envelope.request_id
    assert baseline.request_id == baseline_envelope.request_id

    replay_executor = RecordingExecutor()
    await run_pair(coordinator(replay_executor))
    assert tuple(item.request_id for item in replay_executor.envelopes) == tuple(
        item.request_id for item in first_executor.envelopes
    )
    assert tuple(item.idempotency_key for item in replay_executor.envelopes) == tuple(
        item.idempotency_key for item in first_executor.envelopes
    )


@pytest.mark.asyncio
async def test_baseline_envelope_is_evaluation_only_and_candidate_requires_candidate_ref() -> None:
    executor = RecordingExecutor()
    await run_pair(coordinator(executor))
    candidate, baseline = executor.envelopes
    baseline_payload = baseline.model_dump(mode="json", by_alias=True, exclude_none=True)

    for forbidden in (
        "candidate_ref",
        "team",
        "released_skill",
        "publish",
        "grant",
        "routing",
        "handoff",
        "n8n",
    ):
        assert forbidden not in baseline_payload
    assert baseline.variant == "single_agent_baseline"
    assert baseline.evaluation_only is True

    with pytest.raises(ValidationError, match="Extra inputs"):
        BusinessBenchmarkExecutionEnvelopeV1.model_validate(
            baseline_payload | {"publishable": True}
        )
    with pytest.raises(ValidationError, match="candidate_ref"):
        BusinessBenchmarkExecutionEnvelopeV1.model_validate(
            candidate.model_dump(mode="json", by_alias=True) | {"candidate_ref": None}
        )


@pytest.mark.asyncio
async def test_pair_rejects_receipts_with_mismatched_pair_or_request_session_binding() -> None:
    def mismatch_pair(
        receipt: BusinessBenchmarkRunReceiptV1,
        envelope: BusinessBenchmarkExecutionEnvelopeV1,
    ) -> BusinessBenchmarkRunReceiptV1:
        if envelope.variant == "single_agent_baseline":
            return receipt.model_copy(update={"model_version": "different-model-v1"})
        return receipt

    with pytest.raises(BusinessBenchmarkExecutionError, match="model_version does not match"):
        await run_pair(coordinator(RecordingExecutor(mismatch_pair)))

    def mismatch_request(
        receipt: BusinessBenchmarkRunReceiptV1,
        envelope: BusinessBenchmarkExecutionEnvelopeV1,
    ) -> BusinessBenchmarkRunReceiptV1:
        return receipt.model_copy(
            update={
                "request_id": UUID("00000000-0000-0000-0000-000000000399"),
            }
        )

    with pytest.raises(BusinessBenchmarkExecutionError, match="request identity"):
        await run_pair(coordinator(RecordingExecutor(mismatch_request)))

    def mismatch_runtime_session(
        receipt: BusinessBenchmarkRunReceiptV1,
        envelope: BusinessBenchmarkExecutionEnvelopeV1,
    ) -> BusinessBenchmarkRunReceiptV1:
        return receipt.model_copy(update={"runtime_session_id": "other-stable-session"})

    with pytest.raises(BusinessBenchmarkExecutionError, match="runtime session"):
        await run_pair(coordinator(RecordingExecutor(mismatch_runtime_session)))

    def mismatch_case_digest(
        receipt: BusinessBenchmarkRunReceiptV1,
        envelope: BusinessBenchmarkExecutionEnvelopeV1,
    ) -> BusinessBenchmarkRunReceiptV1:
        return receipt.model_copy(update={"case_sha256": "d" * 64})

    with pytest.raises(BusinessBenchmarkExecutionError, match="case_sha256"):
        await run_pair(coordinator(RecordingExecutor(mismatch_case_digest)))


def test_run_receipt_requires_nonblank_runtime_session_id() -> None:
    payload = {
        "schema": "captain.business-benchmark-run-receipt.v1",
        "run_id": "00000000-0000-0000-0000-000000000400",
        "request_id": "00000000-0000-0000-0000-000000000401",
        "execution_policy_sha256": hashlib.sha256(b"policy").hexdigest(),
        "runtime_session_id": "",
        "job_id": str(JOB_ID),
        "correlation_id": str(CORRELATION_ID),
        "subject_version": 1,
        "attempt": 1,
        "suite_ref": suite_ref().model_dump(mode="json", by_alias=True),
        "suite_id": "claims-suite-v1",
        "case_id": benchmark_case().case_id,
        "case_sha256": "d" * 64,
        "variant": "single_agent_baseline",
        "model_version": "approved-model-v1",
        "allowed_tool_intents": ["none"],
        "maximum_cost_micro_usd": 100,
        "maximum_latency_ms": 200,
        "status": "failed",
        "unsafe_tool_use": False,
        "cost_micro_usd": 0,
        "latency_ms": 0,
        "completed_at": NOW.isoformat(),
    }

    with pytest.raises(ValidationError, match="runtime_session_id"):
        BusinessBenchmarkRunReceiptV1.model_validate(payload)


@pytest.mark.asyncio
async def test_n8n_classified_case_shares_intent_without_baseline_n8n_authority() -> None:
    n8n_case = benchmark_case().model_copy(
        update={"allowed_tool_intents": (IntegrationIntent.N8N,)}
    )
    n8n_policy = execution_policy().model_copy(
        update={"allowed_tool_intents": (IntegrationIntent.N8N,)}
    )
    executor = RecordingExecutor()

    await run_pair(coordinator(executor), case=n8n_case, policy=n8n_policy)

    candidate, baseline = executor.envelopes
    assert (
        candidate.allowed_tool_intents
        == baseline.allowed_tool_intents
        == (IntegrationIntent.N8N,)
    )
    baseline_payload = baseline.model_dump(mode="json", by_alias=True, exclude_none=True)
    for forbidden in (
        "n8n_mcp_ref",
        "n8n_token",
        "n8n_endpoint",
        "capability_lease",
        "capability_grant",
        "workflow_create",
        "workflow_publish",
        "unrestricted_tools",
    ):
        assert forbidden not in baseline_payload
    with pytest.raises(ValidationError, match="Extra inputs"):
        BusinessBenchmarkExecutionEnvelopeV1.model_validate(
            baseline_payload | {"n8n_mcp_ref": "mcp://private/n8n"}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_version", "approved-model-v2"),
        ("allowed_tool_intents", (IntegrationIntent.N8N,)),
        ("maximum_cost_micro_usd", 101),
        ("maximum_latency_ms", 201),
        ("redaction_policy_version", "business-redaction-v2"),
    ],
)
async def test_execution_policy_change_changes_both_request_id_and_idempotency_key(
    field: str, value: object
) -> None:
    initial_executor = RecordingExecutor()
    await run_pair(coordinator(initial_executor))
    changed_case = (
        benchmark_case().model_copy(
            update={"allowed_tool_intents": (IntegrationIntent.N8N,)}
        )
        if field == "allowed_tool_intents"
        else benchmark_case()
    )
    changed_policy = execution_policy().model_copy(update={field: value})
    changed_executor = RecordingExecutor()

    await run_pair(
        coordinator(changed_executor), case=changed_case, policy=changed_policy
    )

    for initial, changed in zip(initial_executor.envelopes, changed_executor.envelopes):
        assert changed.request_id != initial.request_id
        assert changed.idempotency_key != initial.idempotency_key


@pytest.mark.asyncio
async def test_changed_case_body_changes_request_identity() -> None:
    initial_executor = RecordingExecutor()
    await run_pair(coordinator(initial_executor))
    changed_case = benchmark_case().model_copy(
        update={"redacted_input": {"test_organization_id": "changed-test-org"}}
    )
    changed_executor = RecordingExecutor()

    await run_pair(coordinator(changed_executor), case=changed_case)

    for initial, changed in zip(initial_executor.envelopes, changed_executor.envelopes):
        assert changed.case_sha256 != initial.case_sha256
        assert changed.request_id != initial.request_id
        assert changed.idempotency_key != initial.idempotency_key


@pytest.mark.asyncio
async def test_unicode_case_digest_is_identical_from_execution_through_summary() -> None:
    unicode_case = benchmark_case().model_copy(
        update={"redacted_input": {"test_organization_id": "München"}}
    )
    cases = tuple(
        unicode_case.model_copy(
            update={
                "case_id": f"claims-{category.value}-{index + 1:02d}",
                "category": category,
                "human_handoff_required": (
                    category is BusinessCaseCategory.MANDATORY_ESCALATION
                ),
                "severity": (
                    "critical"
                    if category is BusinessCaseCategory.MANDATORY_ESCALATION
                    else "normal"
                ),
            }
        )
        for category in BusinessCaseCategory
        for index in range(3)
    )
    benchmark_suite = BusinessBenchmarkSuiteV1(
        schema="captain.business-benchmark-suite.v1",
        suite_id="claims-suite-v1",
        profile_id="insurance_claims_resolution_swarm",
        suite_version=1,
        cases=cases,
        created_at=NOW,
    )
    unicode_case = benchmark_suite.cases[0]
    repository = InMemoryBusinessBenchmarkRepository()
    private_suite_ref = repository.add_suite(benchmark_suite)
    executor = RecordingExecutor()
    candidate, baseline = await coordinator(executor).run_case_pair(
        case=unicode_case,
        suite_ref=private_suite_ref,
        candidate_ref=artifact("candidate-package"),
        execution_policy=execution_policy(),
    )
    evaluator = BusinessBenchmarkEvaluator(
        clock=lambda: NOW,
        uuid_factory=lambda: UUID("00000000-0000-0000-0000-000000000303"),
    )

    case_receipt = evaluator.evaluate_case(unicode_case, candidate, baseline)
    summary = evaluator.summarize(
        benchmark_suite,
        (case_receipt,),
        BusinessBenchmarkPolicyV1(schema="captain.business-benchmark-policy.v1"),
    )

    assert candidate.case_sha256 == baseline.case_sha256 == case_receipt.case_ref.sha256
    assert summary.suite_ref == private_suite_ref

    changed_case = unicode_case.model_copy(
        update={"redacted_input": {"test_organization_id": "Münster"}}
    )
    changed_executor = RecordingExecutor()
    await coordinator(changed_executor).run_case_pair(
        case=changed_case,
        suite_ref=private_suite_ref,
        candidate_ref=artifact("candidate-package"),
        execution_policy=execution_policy(),
    )
    for initial, changed in zip(executor.envelopes, changed_executor.envelopes):
        assert changed.case_sha256 != initial.case_sha256
        assert changed.request_id != initial.request_id
        assert changed.idempotency_key != initial.idempotency_key
    for initial, changed in zip(
        executor.prepared_effects, changed_executor.prepared_effects
    ):
        assert changed.identity.effect_id != initial.identity.effect_id
