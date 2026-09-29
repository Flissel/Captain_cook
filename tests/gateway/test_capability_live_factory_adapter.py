from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from agenten.agent_factory.capability_live_runner import (
    CapabilityLiveInput,
    CapabilityLiveReservation,
    CapabilityLiveStage,
    CapabilityLiveStageRequest,
)
from agenten.agent_factory.contracts import (
    AgentFactoryJobV3,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
)
from agenten.agent_factory.execution_budget import FactoryBudgetProjection
from agenten.agent_factory.execution_policy import (
    FactoryExecutionMode,
    FactoryExecutionPolicyV1,
    FactoryLiveCapability,
)
from agenten.agent_factory.holdout_store import InMemoryPrivateHoldoutStore
from agenten.agent_factory.input_compiler import FactoryInputCompiler
from agenten.agent_factory.input_document import parse_factory_input_bytes
from agenten.agent_factory.job_builder import build_factory_job_v3
from agenten.agent_factory.production_dispatch_runner import (
    ProductionFactoryDispatchResult,
)
from agenten.agent_factory.skill_workflow_contracts import (
    FACTORY_SKILL_ID_BY_STEP,
    CodebaseInventoryV1,
    CodexBuildEvidenceV1,
    FactoryFeedbackRecommendation,
    FactorySkillStep,
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
from gateway.agent_factory_live_composition import AgentFactoryLiveComposition
from gateway.capability_live_factory_adapter import (
    CapabilityFactoryJobBinding,
    CurrentCapabilityFactoryAdapter,
)
from gateway.capability_live_pipeline import CapabilityLiveAuthorityUnavailable


NOW = datetime(2026, 8, 8, 15, 0, tzinfo=timezone.utc)
RUN_ID = UUID("82000000-0000-0000-0000-000000000071")
CORRELATION_ID = UUID("82000000-0000-0000-0000-000000000072")
FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "agent_factory"
    / "TO_BE_BUILT.valid.md"
)


def _uuid(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, value)


def _ref(namespace: str, value: str, *, media_type: str = "application/json") -> ArtifactRef:
    digest = hashlib.sha256(f"{namespace}:{value}".encode("utf-8")).hexdigest()
    return ArtifactRef(
        uri=f"artifact://{namespace}/{digest}",
        sha256=digest,
        media_type=media_type,
    )


def _request(
    *,
    stage: CapabilityLiveStage = CapabilityLiveStage.SCAFFOLD_AND_TEST_HARNESS,
    suite_ref: ArtifactRef | None = None,
    hermes_result_ref: ArtifactRef | None = None,
    controlled_recovery_required: bool = True,
) -> CapabilityLiveStageRequest:
    source = FIXTURE.read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    source_ref = ArtifactRef(
        uri=f"artifact://sha256/{digest}",
        sha256=digest,
        media_type="text/markdown",
    )
    return CapabilityLiveStageRequest(
        run_id=RUN_ID,
        input=CapabilityLiveInput(
            input_id="sequential-n8n",
            conversation_pattern="sequential",
            source_ref=source_ref,
            integration_intent="n8n",
            conflicting_input=False,
            human_escalation_required=False,
            required_tool_marker=None,
        ),
        correlation_id=CORRELATION_ID,
        reservation=CapabilityLiveReservation(
            reservation_id=_uuid("reservation"),
            run_id=RUN_ID,
            input_id="sequential-n8n",
            maximum_cost_usd=Decimal("1.00"),
        ),
        stage=stage,
        controlled_recovery_required=controlled_recovery_required,
        suite_ref=suite_ref,
        hermes_result_ref=hermes_result_ref,
    )


def _job(request: CapabilityLiveStageRequest) -> AgentFactoryJobV3:
    document = parse_factory_input_bytes(
        FIXTURE.read_bytes(), logical_name="TO_BE_BUILT.md"
    ).model_copy(update={"input_ref": request.input.source_ref})
    compiled = FactoryInputCompiler(
        holdout_store=InMemoryPrivateHoldoutStore()
    ).compile(document, subject_version=1)
    return build_factory_job_v3(
        compiled,
        correlation_id=request.correlation_id,
        now=NOW,
        execution_policy=FactoryExecutionPolicyV1(
            schema="captain.factory-execution-policy.v1",
            mode=FactoryExecutionMode.RELEASE,
            live_execution=True,
            max_cost_usd="1.00",
            max_runtime_seconds=3600,
            required_live_runs=3,
            allowed_models=("gpt-5.6-terra",),
            live_capabilities=(FactoryLiveCapability.MODEL_INVOKE,),
        ),
    )


def _block(job: AgentFactoryJobV3, phase: FactoryPhase) -> FactoryEvidenceBlock:
    prior = _uuid(f"block:{job.job_id}:{phase.value}:prior")
    return FactoryEvidenceBlock.model_construct(
        schema="captain.agent-factory-block.v1",
        event_id=_uuid(f"block:{job.job_id}:{phase.value}"),
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        causation_id=prior,
        occurred_at=NOW,
        producer="captain" if phase is FactoryPhase.FORGE_REQUESTED else "hermes",
        subject_version=job.subject_version,
        attempt=1,
        phase=phase,
        status=FactoryBlockStatus.SUCCEEDED,
        artifact_refs=(_ref("factory-stage", phase.value),),
        evidence_refs=(_ref("factory-evidence", phase.value),),
        assertion_ids=(
            job.acceptance_assertion_ids
            if phase
            in {
                FactoryPhase.REAL_CASE_EVIDENCE,
                FactoryPhase.QUALITY_REVIEWED,
                FactoryPhase.CAPABILITY_PROMOTED,
            }
            else ()
        ),
    )


class _Repository:
    def __init__(self, job: AgentFactoryJobV3, *, recovery: bool = True) -> None:
        self._job = job
        self.blocks_by_job = {job.job_id: [_block(job, FactoryPhase.FORGE_REQUESTED)]}
        self.artifacts: list[object] = []
        self.recovery = recovery
        self.summary = None
        self.missing_skill: FactorySkillStep | None = None

    def job(self, job_id: UUID) -> AgentFactoryJobV3:
        assert job_id == self._job.job_id
        return self._job

    def blocks(self, job_id: UUID) -> tuple[FactoryEvidenceBlock, ...]:
        return tuple(self.blocks_by_job[job_id])

    def workflow_artifacts(self, job_id: UUID) -> tuple[object, ...]:
        assert job_id == self._job.job_id
        return tuple(self.artifacts)

    def workflow_budget_projection(self, job_id: UUID) -> FactoryBudgetProjection:
        assert job_id == self._job.job_id
        return FactoryBudgetProjection(
            job_id=job_id,
            limit_usd=Decimal("1.00"),
            consumed_usd=Decimal("0.41"),
            reserved_usd=Decimal("0"),
            remaining_usd=Decimal("0.59"),
        )

    def business_benchmark_summary_by_artifact(self, reference: ArtifactRef):
        if self.summary is None or self.summary.artifact_ref != reference:
            return None
        return self.summary

    def released_for(self, job: AgentFactoryJobV3, step: FactorySkillStep):
        assert job == self._job
        if step is self.missing_skill:
            raise RuntimeError("missing released skill")
        return SimpleNamespace(skill_id=FACTORY_SKILL_ID_BY_STEP[step])


class _Coordinator:
    def __init__(self, repository: _Repository) -> None:
        self.repository = repository
        self.phase = FactoryPhase.FORGE_REQUESTED
        self.status = FactoryLifecycleStatus.RUNNING
        self.workflow_evaluation_ref: ArtifactRef | None = None
        self.feedback_ref: ArtifactRef | None = None
        self.feedback_recommendation: FactoryFeedbackRecommendation | None = None

    def projection(self, job_id: UUID) -> FactoryProjection:
        job = self.repository.job(job_id)
        return FactoryProjection(
            job=job,
            status=self.status,
            phase=self.phase,
            attempt=1,
            observed_assertion_ids=(
                job.acceptance_assertion_ids
                if self.phase
                in {FactoryPhase.QUALITY_REVIEWED, FactoryPhase.CAPABILITY_PROMOTED}
                else ()
            ),
            block_ids=tuple(block.event_id for block in self.repository.blocks(job_id)),
            workflow_evaluation_ref=self.workflow_evaluation_ref,
            feedback_ref=self.feedback_ref,
            feedback_recommendation=self.feedback_recommendation,
        )

    def record(self, block: FactoryEvidenceBlock) -> bool:
        existing = next(
            (
                item
                for item in self.repository.blocks(block.job_id)
                if item.event_id == block.event_id
            ),
            None,
        )
        if existing is not None:
            assert existing == block
            return False
        assert block.phase is FactoryPhase.CAPABILITY_PROMOTED
        self.repository.blocks_by_job[block.job_id].append(block)
        self.phase = block.phase
        self.status = FactoryLifecycleStatus.READY_TO_USE
        return True

    def advance_quality(self, *, recovery: bool | None = None, summary: bool = True) -> None:
        job = self.repository._job
        if recovery is not None:
            self.repository.recovery = recovery
        for run_number in range(1, 4):
            self.repository.artifacts.append(
                TeamExecutionEvidenceV1.model_construct(
                    invocation_id=_uuid(f"team-run:{run_number}"),
                    job_id=job.job_id,
                    correlation_id=job.correlation_id,
                    subject_version=job.subject_version,
                    attempt=1,
                    run_number=run_number,
                    status="succeeded",
                    artifact_ref=_ref("team-execution", str(run_number)),
                    evidence_refs=(_ref("team-evidence", str(run_number)),),
                    usage_receipt_refs=(_ref("usage", str(run_number)),),
                )
            )
        summary_ref = _ref("business-benchmark-summary", str(job.job_id))
        evaluation_ref = _ref("team-evaluation", str(job.job_id))
        feedback_ref = _ref("factory-feedback", str(job.job_id))
        self.repository.artifacts.extend(
            (
                TeamEvaluationV1.model_construct(
                    invocation_id=_uuid("evaluation"),
                    job_id=job.job_id,
                    correlation_id=job.correlation_id,
                    subject_version=job.subject_version,
                    attempt=1,
                    artifact_ref=evaluation_ref,
                    evidence_refs=(evaluation_ref,),
                    benchmark_summary_ref=summary_ref,
                ),
            )
        )
        if summary:
            self.repository.summary = SimpleNamespace(
                summary_id=_uuid("benchmark-summary"),
                job_id=job.job_id,
                correlation_id=job.correlation_id,
                subject_version=job.subject_version,
                attempt=1,
                artifact_ref=summary_ref,
            )
        self.repository.blocks_by_job[job.job_id].extend(
            (
                _block(job, FactoryPhase.REAL_CASE_EVIDENCE),
                FactoryEvidenceBlock.model_construct(
                    **(
                        _block(job, FactoryPhase.QUALITY_REVIEWED)
                        .model_copy(
                            update={"artifact_refs": (evaluation_ref, feedback_ref)}
                        )
                        .model_dump()
                    ),
                ),
            )
        )
        self.phase = FactoryPhase.QUALITY_REVIEWED
        self.workflow_evaluation_ref = evaluation_ref
        self.feedback_ref = feedback_ref
        self.feedback_recommendation = FactoryFeedbackRecommendation.PROMOTE_CANDIDATE


class _Runner:
    def __init__(self, repository: _Repository, coordinator: _Coordinator) -> None:
        self.repository = repository
        self.coordinator = coordinator
        self.calls: list[FactoryActionKind] = []

    async def run(
        self,
        job_id: UUID,
        *,
        maximum_dispatches: int,
        stop_before_action: FactoryActionKind,
    ) -> ProductionFactoryDispatchResult:
        del maximum_dispatches
        self.calls.append(stop_before_action)
        job = self.repository.job(job_id)
        if stop_before_action is FactoryActionKind.DISPATCH_TOOL_INTEGRATOR:
            self.repository.blocks_by_job[job_id].append(
                _block(job, FactoryPhase.BLUEPRINT_CREATED)
            )
            inventory_ref = _ref("factory-inventory", str(job.job_id))
            self.repository.artifacts.append(
                CodebaseInventoryV1.model_construct(
                    invocation_id=_uuid("inventory"),
                    job_id=job.job_id,
                    correlation_id=job.correlation_id,
                    subject_version=job.subject_version,
                    attempt=1,
                    artifact_ref=inventory_ref,
                    evidence_refs=(inventory_ref,),
                )
            )
            self.coordinator.phase = FactoryPhase.BLUEPRINT_CREATED
        elif stop_before_action is FactoryActionKind.SUBMIT_FORGE_JOB:
            self.repository.blocks_by_job[job_id].append(
                _block(job, FactoryPhase.TOOL_CANDIDATE_TESTED)
            )
            retry_binding = (
                SimpleNamespace(
                    invocation_id=_uuid("controlled-recovery"),
                    job_id=job.job_id,
                    correlation_id=job.correlation_id,
                    subject_version=job.subject_version,
                    attempt=1,
                )
                if self.repository.recovery
                else None
            )
            self.repository.artifacts.append(
                CodexBuildEvidenceV1.model_construct(
                    invocation_id=_uuid("codex-build"),
                    job_id=job.job_id,
                    correlation_id=job.correlation_id,
                    subject_version=job.subject_version,
                    attempt=1,
                    artifact_ref=_ref("codex-build", str(job.job_id)),
                    evidence_refs=(_ref("codex-evidence", str(job.job_id)),),
                    runtime_retry_binding=retry_binding,
                )
            )
            self.coordinator.phase = FactoryPhase.TOOL_CANDIDATE_TESTED
        elif stop_before_action is FactoryActionKind.DISPATCH_REAL_CASE_TESTER:
            self.repository.blocks_by_job[job_id].extend(
                (
                    _block(job, FactoryPhase.AGENT_CODE_CREATED),
                    _block(job, FactoryPhase.BUILD_PASSED),
                )
            )
            self.coordinator.phase = FactoryPhase.BUILD_PASSED
        else:  # pragma: no cover - protects the fake boundary
            raise AssertionError(f"unexpected stop point: {stop_before_action}")
        return ProductionFactoryDispatchResult(
            schema="captain.factory-dispatch-run-result.v1",
            job_id=job_id,
            status="stop_point_reached",
            lifecycle_status=FactoryLifecycleStatus.RUNNING,
            next_action=FactoryAction(kind=stop_before_action, attempt=1),
            dispatched_actions=(),
        )


class _Provisioning:
    def __init__(self, binding: CapabilityFactoryJobBinding | None) -> None:
        self.binding = binding
        self.provision_calls = 0
        self.lookup_calls = 0

    def provision(
        self, request: CapabilityLiveStageRequest
    ) -> CapabilityFactoryJobBinding | None:
        del request
        self.provision_calls += 1
        return self.binding

    def binding_for(
        self, request: CapabilityLiveStageRequest
    ) -> CapabilityFactoryJobBinding | None:
        del request
        self.lookup_calls += 1
        return self.binding


def _fixture(
    *,
    recovery: bool = True,
) -> tuple[
    CurrentCapabilityFactoryAdapter,
    _Repository,
    _Coordinator,
    _Runner,
    _Provisioning,
]:
    request = _request()
    job = _job(request)
    repository = _Repository(job, recovery=recovery)
    coordinator = _Coordinator(repository)
    runner = _Runner(repository, coordinator)
    composition = AgentFactoryLiveComposition(
        repository=cast(object, repository),
        coordinator=cast(object, coordinator),
        lease_issuer=cast(object, None),
        hermes=cast(object, None),
        team_execution=cast(object, None),
        candidate_validator=cast(object, None),
        business_benchmark=cast(object, None),
        dispatcher=cast(object, None),
        runner=cast(object, runner),
        improvement_authority=cast(object, None),
        runtime_retry_authority=None,
    )
    binding = CapabilityFactoryJobBinding(
        input_id=request.input.input_id,
        source_ref=request.input.source_ref,
        job=job,
        suite_ref=_ref("benchmark-suite", str(job.job_id)),
        forge_requested_event_id=repository.blocks(job.job_id)[0].event_id,
        n8n_work_batch_id=f"n8n-{job.job_id.hex[:24]}",
    )
    provisioning = _Provisioning(binding)
    return (
        CurrentCapabilityFactoryAdapter(
            composition=composition,
            provisioning=provisioning,
            clock=lambda: NOW + timedelta(minutes=5),
        ),
        repository,
        coordinator,
        runner,
        provisioning,
    )


def _next_request(
    prior,
    stage: CapabilityLiveStage,
) -> CapabilityLiveStageRequest:
    return _request(
        stage=stage,
        suite_ref=prior.suite_ref,
        hermes_result_ref=prior.hermes_result_ref,
    )


def test_adapter_advances_only_current_factory_stage_boundaries_and_replays() -> None:
    adapter, repository, coordinator, runner, provisioning = _fixture()

    scaffold = asyncio.run(adapter.scaffold(_request()))
    assert scaffold is not None
    assert scaffold.stage is CapabilityLiveStage.SCAFFOLD_AND_TEST_HARNESS
    assert scaffold.job_id == repository._job.job_id
    assert scaffold.correlation_id == CORRELATION_ID
    assert scaffold.source_ref == _request().input.source_ref
    assert coordinator.phase is FactoryPhase.BLUEPRINT_CREATED
    assert adapter.job_id_for(_request()) == repository._job.job_id

    replay = asyncio.run(adapter.scaffold(_request()))
    assert replay == scaffold
    assert runner.calls == [FactoryActionKind.DISPATCH_TOOL_INTEGRATOR]

    implementation = asyncio.run(
        adapter.implement(
            _next_request(scaffold, CapabilityLiveStage.CODEX_IMPLEMENTATION)
        )
    )
    assert implementation is not None
    assert implementation.hermes_result_ref == scaffold.hermes_result_ref
    assert coordinator.phase is FactoryPhase.TOOL_CANDIDATE_TESTED

    sealed = asyncio.run(adapter.seal(_next_request(implementation, CapabilityLiveStage.SEAL)))
    assert sealed is not None
    assert sealed.suite_ref == scaffold.suite_ref
    assert coordinator.phase is FactoryPhase.BUILD_PASSED
    assert runner.calls == [
        FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
        FactoryActionKind.SUBMIT_FORGE_JOB,
        FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
    ]
    assert provisioning.provision_calls == 1
    assert provisioning.lookup_calls >= 3


def test_adapter_promotes_with_current_domain_block_and_reads_same_release() -> None:
    adapter, repository, coordinator, _runner, _provisioning = _fixture()
    scaffold = asyncio.run(adapter.scaffold(_request()))
    assert scaffold is not None
    implementation = asyncio.run(
        adapter.implement(
            _next_request(scaffold, CapabilityLiveStage.CODEX_IMPLEMENTATION)
        )
    )
    assert implementation is not None
    asyncio.run(adapter.seal(_next_request(implementation, CapabilityLiveStage.SEAL)))
    coordinator.advance_quality()

    promoted = asyncio.run(
        adapter.promote(_next_request(implementation, CapabilityLiveStage.PROMOTE))
    )
    assert promoted is not None
    assert promoted.stage is CapabilityLiveStage.PROMOTE
    assert promoted.release_authority_id is not None
    assert promoted.benchmark_authority_id == _uuid("benchmark-summary")
    assert promoted.normal_trace_ids == tuple(
        _uuid(f"team-run:{number}") for number in range(1, 4)
    )
    assert promoted.controlled_recovery_id == _uuid("controlled-recovery")
    assert promoted.booked_cost_usd == Decimal("0.41")
    promotions = tuple(
        block
        for block in repository.blocks(repository._job.job_id)
        if block.phase is FactoryPhase.CAPABILITY_PROMOTED
    )
    assert len(promotions) == 1
    assert promotions[0].event_id == promoted.release_authority_id

    released = asyncio.run(
        adapter.released(_next_request(implementation, CapabilityLiveStage.EXECUTE))
    )
    assert released == promoted
    assert len(
        tuple(
            block
            for block in repository.blocks(repository._job.job_id)
            if block.phase is FactoryPhase.CAPABILITY_PROMOTED
        )
    ) == 1


@pytest.mark.parametrize("missing", ["recovery", "summary"])
def test_adapter_fails_before_promotion_when_release_evidence_is_missing(
    missing: str,
) -> None:
    adapter, repository, coordinator, _runner, _provisioning = _fixture(
        recovery=missing != "recovery"
    )
    scaffold = asyncio.run(adapter.scaffold(_request()))
    assert scaffold is not None
    implementation = asyncio.run(
        adapter.implement(
            _next_request(scaffold, CapabilityLiveStage.CODEX_IMPLEMENTATION)
        )
    )
    assert implementation is not None
    asyncio.run(adapter.seal(_next_request(implementation, CapabilityLiveStage.SEAL)))
    coordinator.advance_quality(summary=missing != "summary")

    with pytest.raises(CapabilityLiveAuthorityUnavailable):
        asyncio.run(
            adapter.promote(
                _next_request(implementation, CapabilityLiveStage.PROMOTE)
            )
        )

    assert all(
        block.phase is not FactoryPhase.CAPABILITY_PROMOTED
        for block in repository.blocks(repository._job.job_id)
    )


def test_adapter_rejects_foreign_binding_and_missing_n8n_batch_before_dispatch() -> None:
    adapter, repository, _coordinator, runner, provisioning = _fixture()
    assert provisioning.binding is not None
    provisioning.binding = provisioning.binding.model_copy(
        update={"n8n_work_batch_id": None}
    )
    with pytest.raises(CapabilityLiveAuthorityUnavailable, match="n8n WorkBatch"):
        asyncio.run(adapter.scaffold(_request()))
    assert runner.calls == []

    foreign_payload = provisioning.binding.model_dump()
    foreign_payload["source_ref"] = _ref(
        "sha256", "foreign", media_type="text/markdown"
    )
    provisioning.binding = CapabilityFactoryJobBinding.model_construct(
        **foreign_payload,
    )
    with pytest.raises(CapabilityLiveAuthorityUnavailable, match="source"):
        adapter.job_id_for(_request())
    assert repository.blocks(repository._job.job_id)[0].phase is FactoryPhase.FORGE_REQUESTED


def test_adapter_fails_closed_when_binding_or_skill_assignment_is_unavailable() -> None:
    adapter, repository, _coordinator, runner, provisioning = _fixture()
    provisioning.binding = None
    with pytest.raises(CapabilityLiveAuthorityUnavailable, match="provisioning binding"):
        asyncio.run(adapter.scaffold(_request()))
    assert adapter.job_id_for(_request()) is None
    assert runner.calls == []

    provisioning.binding = CapabilityFactoryJobBinding(
        input_id=_request().input.input_id,
        source_ref=_request().input.source_ref,
        job=repository._job,
        suite_ref=_ref("benchmark-suite", str(repository._job.job_id)),
        forge_requested_event_id=repository.blocks(repository._job.job_id)[0].event_id,
        n8n_work_batch_id=f"n8n-{repository._job.job_id.hex[:24]}",
    )
    repository.missing_skill = FactorySkillStep.SEAL_CODEX_BUILD
    with pytest.raises(CapabilityLiveAuthorityUnavailable, match="released skill"):
        asyncio.run(adapter.scaffold(_request()))
    assert runner.calls == []
