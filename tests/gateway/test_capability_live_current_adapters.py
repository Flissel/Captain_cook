from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from agenten.agent_factory.business_benchmark_contracts import (
    BenchmarkDisposition,
    BusinessBenchmarkSummaryV1,
)
from agenten.agent_factory.capability_live_runner import (
    CapabilityLiveCapabilityEvidence,
    CapabilityLiveInput,
    CapabilityLiveReservation,
    CapabilityLiveRestartRequest,
    CapabilityLiveStage,
    CapabilityLiveStageRequest,
)
from agenten.agent_factory.contracts import (
    AgentFactoryJobV3,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
)
from agenten.agent_factory.execution_policy import FactoryExecutionPolicyV1
from agenten.agent_factory.outcome_contracts import ExecutionOutcomeV1
from agenten.agent_factory.production_dispatch_runner import (
    ProductionFactoryDispatchResult,
)
from agenten.agent_factory.skill_workflow_contracts import (
    FactoryFeedbackRecommendation,
    TeamEvaluationV1,
    TeamExecutionEvidenceV1,
)
from agenten.agent_factory.state_machine import (
    FactoryAction,
    FactoryActionKind,
    FactoryLifecycleStatus,
    FactoryProjection,
)
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.delivery.minibook_events import (
    MinibookProjectionAcknowledgementV1,
    minibook_projection_acknowledgement_id,
)
from agenten.delivery.projector import MinibookProjector
from gateway.capability_live_current_adapters import (
    CapabilityProjectionReadback,
    GatewayCapabilityBusinessBenchmarkAdapter,
    GatewayMinibookCapabilityProjectionAdapter,
    RecordedFactoryCapabilityExecutionAdapter,
)
from gateway.capability_live_pipeline import CapabilityFactoryAuthority
from gateway.capability_live_pipeline import (
    CapabilityExecutionAuthority,
    CapabilityLiveAuthorityUnavailable,
    CapabilityResumeAuthorization,
)
from gateway.contracts import FactoryJobProjection
from gateway.registry_feed import factory_promotion_projection


NOW = datetime(2026, 8, 8, 15, 0, tzinfo=timezone.utc)
JOB_ID = UUID("82000000-0000-0000-0000-000000000071")
CORRELATION_ID = UUID("82000000-0000-0000-0000-000000000072")
RUN_ID = UUID("82000000-0000-0000-0000-000000000073")


def _uuid(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, value)


def _ref(namespace: str, value: str) -> ArtifactRef:
    digest = _uuid(f"{namespace}:{value}").hex * 2
    return ArtifactRef(
        uri=f"artifact://{namespace}/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def _request(stage: CapabilityLiveStage) -> CapabilityLiveStageRequest:
    input_item = CapabilityLiveInput(
        input_id="sequential-n8n",
        conversation_pattern="sequential",
        source_ref=_ref("factory-input", "sequential"),
        integration_intent="n8n",
        conflicting_input=False,
        human_escalation_required=False,
        required_tool_marker=None,
    )
    return CapabilityLiveStageRequest(
        run_id=RUN_ID,
        input=input_item,
        correlation_id=CORRELATION_ID,
        reservation=CapabilityLiveReservation(
            reservation_id=_uuid("reservation"),
            run_id=RUN_ID,
            input_id=input_item.input_id,
            maximum_cost_usd=Decimal("1.00"),
        ),
        stage=stage,
        controlled_recovery_required=True,
        suite_ref=_ref("benchmark-suite", "sequential"),
        hermes_result_ref=_ref("hermes-result", "sequential"),
    )


def _job() -> AgentFactoryJobV3:
    return AgentFactoryJobV3.model_construct(
        job_id=JOB_ID,
        event_id=_uuid("job-event"),
        correlation_id=CORRELATION_ID,
        causation_id=None,
        occurred_at=NOW,
        producer="captain",
        subject_version=1,
        input_ref=_request(CapabilityLiveStage.EVALUATE).input.source_ref,
        compiled_spec_ref=_ref("compiled", "sequential"),
        dependency_graph_ref=_ref("graph", "sequential"),
        required_capability="capability-sequential-n8n",
        acceptance_assertion_ids=("business_value",),
        private_holdout_refs=(),
        max_behavioral_iterations=5,
        deadline_at=NOW,
        execution_policy=FactoryExecutionPolicyV1.model_construct(
            mode="release",
            live_execution=True,
            max_cost_usd=Decimal("1.00"),
            max_runtime_seconds=900,
            required_live_runs=3,
            allowed_models=("gpt-5.6-terra",),
            live_capabilities=("model.invoke",),
            sandbox_mode="workspace_write",
        ),
    )


def _runs() -> tuple[TeamExecutionEvidenceV1, ...]:
    return tuple(
        TeamExecutionEvidenceV1.model_construct(
            invocation=SimpleNamespace(),
            invocation_id=_uuid(f"invocation:{index}"),
            job_id=JOB_ID,
            correlation_id=CORRELATION_ID,
            subject_version=1,
            attempt=1,
            occurred_at=NOW,
            producer="hermes",
            artifact_ref=_ref("team-execution", str(index)),
            evidence_refs=(_ref("team-execution-evidence", str(index)),),
            acceptance_assertion_ids=("business_value",),
            run_number=index,
            candidate_ref=_ref("candidate", "sealed"),
            holdout_ref=SimpleNamespace(),
            execution_outcome=ExecutionOutcomeV1.model_construct(
                capability_id="capability-sequential-n8n",
                capability_version=1,
                team_version=1,
                correlation_id=CORRELATION_ID,
                command_id=_uuid(f"command:{index}"),
                result_id=_uuid(f"result:{index}"),
                business_output={"status": "ok"},
                output_ref=None,
                assertion_outcomes=(),
                evidence_refs=(_ref("runtime", str(index)),),
                status="succeeded",
            ),
            usage_receipt_refs=(_ref("usage", str(index)),),
            status="succeeded",
        )
        for index in range(1, 4)
    )


def _evaluation() -> TeamEvaluationV1:
    return TeamEvaluationV1.model_construct(
        invocation=SimpleNamespace(),
        invocation_id=_uuid("evaluation"),
        job_id=JOB_ID,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        attempt=1,
        occurred_at=NOW,
        producer="hermes",
        artifact_ref=_ref("evaluation", "accepted"),
        evidence_refs=tuple(run.artifact_ref for run in _runs()),
        acceptance_assertion_ids=("business_value",),
        benchmark_summary_ref=_ref("benchmark-summary", "accepted"),
        benchmark_disposition="passed",
    )


def _summary() -> BusinessBenchmarkSummaryV1:
    return BusinessBenchmarkSummaryV1.model_construct(
        summary_id=_uuid("benchmark-summary-id"),
        job_id=JOB_ID,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        attempt=1,
        artifact_ref=_evaluation().benchmark_summary_ref,
        disposition=BenchmarkDisposition.PASSED,
        reason_codes=(),
        candidate_correctness_bps=10000,
        baseline_correctness_bps=10000,
        candidate_completion_bps=10000,
        baseline_completion_bps=10000,
        cost_ratio_bps=10000,
        latency_ratio_bps=10000,
        unsafe_tool_uses=0,
        mandatory_handoff_misses=0,
    )


def _promotion_block() -> FactoryEvidenceBlock:
    return FactoryEvidenceBlock.model_construct(
        event_id=_uuid("promotion"),
        job_id=JOB_ID,
        correlation_id=CORRELATION_ID,
        causation_id=_uuid("quality"),
        occurred_at=NOW,
        producer="captain",
        subject_version=1,
        attempt=1,
        phase=FactoryPhase.CAPABILITY_PROMOTED,
        status=FactoryBlockStatus.SUCCEEDED,
        artifact_refs=(_evaluation().artifact_ref,),
        evidence_refs=(_ref("promotion", "evidence"),),
        assertion_ids=("business_value",),
    )


def _factory_projection(*, promoted: bool) -> FactoryProjection:
    return FactoryProjection.model_construct(
        job=_job(),
        status=(
            FactoryLifecycleStatus.READY_TO_USE
            if promoted
            else FactoryLifecycleStatus.RUNNING
        ),
        phase=(
            FactoryPhase.CAPABILITY_PROMOTED
            if promoted
            else FactoryPhase.QUALITY_REVIEWED
        ),
        attempt=1,
        feedback_recommendation=FactoryFeedbackRecommendation.PROMOTE_CANDIDATE,
        workflow_evaluation_ref=_evaluation().artifact_ref,
        feedback_ref=_ref("feedback", "promote"),
        block_ids=(_promotion_block().event_id,) if promoted else (),
    )


class _Bindings:
    def job_id_for(self, request: CapabilityLiveStageRequest) -> UUID | None:
        assert request.input.source_ref == _job().input_ref
        return JOB_ID


class _Composition:
    def __init__(self, *, promoted: bool = False) -> None:
        self.promoted = promoted
        self.run_calls: list[UUID] = []

    async def run(self, job_id: UUID, **_kwargs: object) -> ProductionFactoryDispatchResult:
        self.run_calls.append(job_id)
        return ProductionFactoryDispatchResult.model_construct(
            job_id=job_id,
            status="captain_action_required",
            lifecycle_status=FactoryLifecycleStatus.RUNNING,
            next_action=FactoryAction.model_construct(
                kind=FactoryActionKind.VALIDATE_FOR_PROMOTION,
                attempt=1,
                job_id=job_id,
            ),
            dispatched_actions=(),
        )

    def projection(self, job_id: UUID) -> FactoryProjection:
        assert job_id == JOB_ID
        return _factory_projection(promoted=self.promoted)

    def snapshot(self, job_id: UUID) -> FactoryJobProjection:
        projection = self.projection(job_id)
        return FactoryJobProjection.model_construct(
            job=projection.job,
            blocks=(_promotion_block(),) if self.promoted else (),
            leases=(),
            projection=projection,
        )

    def workflow_artifacts(self, job_id: UUID) -> tuple[object, ...]:
        assert job_id == JOB_ID
        return (*_runs(), _evaluation())

    def workflow_budget_projection(self, job_id: UUID) -> object:
        assert job_id == JOB_ID
        return SimpleNamespace(consumed_usd=Decimal("1.00"))

    def business_benchmark_summary_by_artifact(
        self,
        reference: ArtifactRef,
    ) -> BusinessBenchmarkSummaryV1 | None:
        return _summary() if reference == _summary().artifact_ref else None


def _release() -> CapabilityFactoryAuthority:
    request = _request(CapabilityLiveStage.PROMOTE)
    return CapabilityFactoryAuthority(
        stage=CapabilityLiveStage.PROMOTE,
        input_id=request.input.input_id,
        source_ref=request.input.source_ref,
        job_id=JOB_ID,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        capability_id="capability-sequential-n8n",
        capability_version=1,
        suite_ref=request.suite_ref,
        hermes_result_ref=request.hermes_result_ref,
        evidence_refs=(_ref("release", "accepted"),),
        booked_cost_usd=Decimal("1.00"),
        controlled_recovery_id=_uuid("recovery"),
        normal_trace_ids=tuple(run.execution_outcome.result_id for run in _runs()),
        benchmark_authority_id=_evaluation().invocation_id,
        benchmark_summary_ref=_summary().artifact_ref,
        release_authority_id=_promotion_block().event_id,
    )


def _capability() -> CapabilityLiveCapabilityEvidence:
    request = _request(CapabilityLiveStage.PROJECT)
    outcome = _runs()[2].execution_outcome
    return CapabilityLiveCapabilityEvidence(
        input_id=request.input.input_id,
        conversation_pattern=request.input.conversation_pattern,
        job_id=JOB_ID,
        correlation_id=CORRELATION_ID,
        capability_id="capability-sequential-n8n",
        capability_version=1,
        controlled_recovery_id=_uuid("recovery"),
        normal_trace_ids=tuple(run.execution_outcome.result_id for run in _runs()),
        release_authority_id=_promotion_block().event_id,
        execution_authority_id=_runs()[2].invocation_id,
        execution_command_id=outcome.command_id,
        execution_result_id=outcome.result_id,
        projection_event_id=_uuid("projection"),
        projection_readback_id=_uuid("projection-readback"),
        suite_ref=request.suite_ref,
        hermes_result_ref=request.hermes_result_ref,
        booked_cost_usd=Decimal("1.00"),
        checkpoint_refs=(),
    )


def test_benchmark_adapter_advances_current_composition_and_reads_authority() -> None:
    composition = _Composition()
    adapter = GatewayCapabilityBusinessBenchmarkAdapter(
        composition=composition,
        bindings=_Bindings(),
    )

    authority = asyncio.run(adapter.evaluate(_request(CapabilityLiveStage.EVALUATE)))

    assert authority is not None
    assert composition.run_calls == [JOB_ID]
    assert authority.benchmark_authority_id == _evaluation().invocation_id
    assert authority.benchmark_summary_ref == _summary().artifact_ref
    assert authority.normal_trace_ids == tuple(
        run.execution_outcome.result_id for run in _runs()
    )


def test_recorded_execution_adapter_uses_existing_third_release_gate_run() -> None:
    adapter = RecordedFactoryCapabilityExecutionAdapter(
        composition=_Composition(promoted=True),
    )

    authority = asyncio.run(
        adapter.execute(_request(CapabilityLiveStage.EXECUTE), _release())
    )

    assert authority is not None
    assert authority.execution_authority_id == _runs()[2].invocation_id
    assert authority.execution_command_id == _runs()[2].execution_outcome.command_id
    assert authority.execution_result_id == _runs()[2].execution_outcome.result_id


def _resume_authorization(
    capability: CapabilityLiveCapabilityEvidence,
) -> CapabilityResumeAuthorization:
    return CapabilityResumeAuthorization(
        job_id=JOB_ID,
        reservation_id=_request(CapabilityLiveStage.PROJECT).reservation.reservation_id,
        correlation_id=CORRELATION_ID,
        capability_id=capability.capability_id,
        capability_version=capability.capability_version,
        authorization_receipt_id=_uuid("resumed-authority"),
        evidence_refs=(_ref("resume-authority", "accepted"),),
    )


def _resumed_execution(
    capability: CapabilityLiveCapabilityEvidence,
    authorization: CapabilityResumeAuthorization,
) -> CapabilityExecutionAuthority:
    return CapabilityExecutionAuthority(
        job_id=JOB_ID,
        correlation_id=CORRELATION_ID,
        capability_id=capability.capability_id,
        capability_version=capability.capability_version,
        execution_authority_id=authorization.authorization_receipt_id,
        execution_command_id=_uuid("resumed-command"),
        execution_result_id=_uuid("resumed-result"),
        evidence_refs=(_ref("resumed-execution", "accepted"),),
        booked_cost_usd=Decimal("1.00"),
        terminal_status="succeeded",
        replayed=False,
    )


class _ResumeGateway:
    def __init__(
        self,
        *,
        authorization: CapabilityResumeAuthorization | None,
        dispatched: CapabilityExecutionAuthority | None,
        readback: CapabilityExecutionAuthority | None,
    ) -> None:
        self.authorization = authorization
        self.dispatched = dispatched
        self.readback = readback
        self.calls: list[str] = []

    async def authorize_resume(self, request: object) -> CapabilityResumeAuthorization | None:
        self.calls.append("authorize")
        assert getattr(request, "job_id") == JOB_ID
        assert getattr(request, "reservation_id") == _request(
            CapabilityLiveStage.PROJECT
        ).reservation.reservation_id
        assert getattr(request, "original_execution_command_id") == _capability().execution_command_id
        return self.authorization

    async def dispatch_resume(
        self,
        authorization: CapabilityResumeAuthorization,
    ) -> CapabilityExecutionAuthority | None:
        self.calls.append("dispatch")
        return self.dispatched

    async def readback_resume(
        self,
        authorization: CapabilityResumeAuthorization,
    ) -> CapabilityExecutionAuthority | None:
        self.calls.append("readback")
        return self.readback


def _restart(
    gateway: _ResumeGateway,
) -> CapabilityExecutionAuthority | None:
    capability = _capability()
    adapter = RecordedFactoryCapabilityExecutionAdapter(
        composition=_Composition(promoted=True),
        resume_execution=gateway,
    )
    return asyncio.run(
        adapter.restart_and_resume(
            CapabilityLiveRestartRequest(
                run_id=RUN_ID,
                input=_request(CapabilityLiveStage.PROJECT).input,
                reservation=_request(CapabilityLiveStage.PROJECT).reservation,
                capability=capability,
            ),
            _release(),
        )
    )


def test_restart_dispatches_a_new_gateway_authorized_execution_and_reads_it_back() -> None:
    capability = _capability()
    authorization = _resume_authorization(capability)
    execution = _resumed_execution(capability, authorization)
    gateway = _ResumeGateway(
        authorization=authorization,
        dispatched=execution,
        readback=execution,
    )

    authority = _restart(gateway)

    assert authority is not None
    assert gateway.calls == ["authorize", "dispatch", "readback"]
    assert authority.execution_command_id != capability.execution_command_id
    assert authority.execution_result_id != capability.execution_result_id


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("missing_authority", "authorization receipt"),
        ("wrong_correlation", "stable release binding"),
        ("wrong_capability", "stable release binding"),
        ("missing_dispatch_result", "dispatch result"),
        ("reused_command", "reused original command"),
        ("reused_result", "reused original result"),
        ("replayed", "evidence replay"),
        ("cancelled", "successful terminal result"),
        ("timed_out", "successful terminal result"),
        ("missing_readback", "read-back result"),
        ("drifted_readback", "read-back does not match"),
    ],
)
def test_restart_fails_closed_without_a_fresh_verified_gateway_execution(
    fault: str,
    message: str,
) -> None:
    capability = _capability()
    authorization = _resume_authorization(capability)
    execution = _resumed_execution(capability, authorization)
    if fault == "missing_authority":
        authorization = None
    elif fault == "wrong_correlation":
        authorization = authorization.model_copy(
            update={"correlation_id": _uuid("foreign-correlation")}
        )
    elif fault == "wrong_capability":
        authorization = authorization.model_copy(update={"capability_id": "foreign"})
    elif fault == "missing_dispatch_result":
        execution = None
    elif fault == "reused_command":
        execution = execution.model_copy(
            update={"execution_command_id": capability.execution_command_id}
        )
    elif fault == "reused_result":
        execution = execution.model_copy(
            update={"execution_result_id": capability.execution_result_id}
        )
    elif fault == "replayed":
        execution = execution.model_copy(update={"replayed": True})
    elif fault in {"cancelled", "timed_out"}:
        execution = execution.model_copy(update={"terminal_status": fault})

    readback = execution
    if fault == "missing_readback":
        readback = None
    elif fault == "drifted_readback":
        readback = execution.model_copy(
            update={"execution_result_id": _uuid("drifted-result")}
        )
    gateway = _ResumeGateway(
        authorization=authorization,
        dispatched=execution,
        readback=readback,
    )

    with pytest.raises(CapabilityLiveAuthorityUnavailable, match=message):
        _restart(gateway)


class _ProjectionEffects:
    def __init__(self) -> None:
        self.current: CapabilityProjectionReadback | None = None

    async def project_and_ack(self, event: object) -> CapabilityProjectionReadback:
        rendered = MinibookProjector.render(event)
        post_id = "captain-projection-" + hashlib.sha256(
            str(event.event_id).encode("utf-8")
        ).hexdigest()[:32]
        acknowledgement = MinibookProjectionAcknowledgementV1(
            acknowledgement_id=minibook_projection_acknowledgement_id(
                event.event_id,
                post_id=post_id,
                content_sha256=rendered.content_hash,
            ),
            projection_event_id=event.event_id,
            correlation_id=event.correlation_id,
            subject_id=event.subject_id,
            subject_version=event.subject_version,
            project_id=MinibookProjector.PROJECTION_PROJECT_ID,
            post_id=post_id,
            content_sha256=rendered.content_hash,
            acknowledged_at=NOW,
            outcome="mirrored",
        )
        self.current = CapabilityProjectionReadback(
            event=event,
            acknowledgement=acknowledgement,
            evidence_ref=_ref("projection-ack", str(event.event_id)),
        )
        return self.current

    async def readback(self, event_id: UUID) -> CapabilityProjectionReadback | None:
        if self.current is None or self.current.event.event_id != event_id:
            return None
        return self.current


def test_projection_adapter_uses_gateway_event_and_minibook_acknowledgement() -> None:
    composition = _Composition(promoted=True)
    effects = _ProjectionEffects()
    adapter = GatewayMinibookCapabilityProjectionAdapter(
        composition=composition,
        effects=effects,
    )
    execution = asyncio.run(
        RecordedFactoryCapabilityExecutionAdapter(
            composition=composition,
        ).execute(_request(CapabilityLiveStage.EXECUTE), _release())
    )
    assert execution is not None

    authority = asyncio.run(
        adapter.project_and_readback(
            _request(CapabilityLiveStage.PROJECT),
            _release(),
            execution,
        )
    )

    expected_event = factory_promotion_projection(
        _promotion_block().model_dump(mode="json", by_alias=True),
        _job().model_dump(mode="json", by_alias=True),
        benchmark_summary=_summary(),
    )
    assert authority is not None
    assert authority.projection_event_id == expected_event.event_id
    assert effects.current is not None
    assert authority.projection_readback_id == effects.current.acknowledgement.acknowledgement_id
