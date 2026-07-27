from __future__ import annotations

import pytest

from agenten.agent_factory.service import (
    FactoryCoordinator,
    FactoryRepositoryError,
    InMemoryFactoryRepository,
)
from agenten.agent_factory.state_machine import FactoryActionKind, FactoryLifecycleError
from agenten.agent_factory.contracts import FactoryPhase
from agenten.agent_factory.factory_feedback import FactoryFeedbackBuilder
from tests.agent_factory.test_factory_feedback import (
    _budget as workflow_budget,
    _candidate as workflow_candidate,
    _evaluation as workflow_evaluation,
    _report_invocation,
)
from tests.agent_factory.test_state_machine import (
    accepted_evaluation,
    accepted_release_decision,
    block,
    job,
    job_v2,
    job_v3,
    workflow_block,
)
from tests.agent_factory.test_team_evaluation import _benchmark as state_workflow_benchmark
from tests.agent_factory.test_release_gate import (
    workflow_benchmark,
    workflow_budget as release_workflow_budget,
    workflow_evaluation as release_workflow_evaluation,
    workflow_job as release_workflow_job,
    workflow_receipts,
    workflow_run,
)


class EvaluationLookupRepository(InMemoryFactoryRepository):
    def __init__(self, evaluation, decision=None):
        super().__init__()
        self.evaluation = evaluation
        self.decision = decision
        self.lookup_job_ids = []
        self.decision_lookup_job_ids = []

    def evaluation_for_job(self, job_id):
        self.lookup_job_ids.append(job_id)
        return self.evaluation

    def release_decision_for_job(self, job_id):
        self.decision_lookup_job_ids.append(job_id)
        return self.decision


class WorkflowLookupRepository(InMemoryFactoryRepository):
    def __init__(self, *, artifacts=(), budget=None, receipts=(), summaries=()):
        super().__init__()
        self.artifacts = tuple(artifacts)
        self.budget = budget
        self.receipts = tuple(receipts)
        self.summaries = tuple(summaries)

    def workflow_artifacts(self, job_id):
        self.job(job_id)
        return self.artifacts

    def workflow_budget_projection(self, job_id):
        self.job(job_id)
        return self.budget

    def workflow_usage_receipts(self, job_id):
        self.job(job_id)
        return self.receipts

    def business_benchmark_summary_by_artifact(self, artifact_ref):
        return next(
            (summary for summary in self.summaries if summary.artifact_ref == artifact_ref),
            None,
        )


def test_repository_rebuilds_state_and_returns_next_captain_action() -> None:
    coordinator = FactoryCoordinator(InMemoryFactoryRepository())
    factory_job = job()

    coordinator.register(factory_job)
    coordinator.record(block(FactoryPhase.FORGE_REQUESTED))

    action = coordinator.next_action(factory_job.job_id)

    assert action.kind is FactoryActionKind.DISPATCH_AGENT_ARCHITECT
    assert action.job_id == factory_job.job_id
    assert coordinator.blocks(factory_job.job_id) == (block(FactoryPhase.FORGE_REQUESTED),)


def test_duplicate_evidence_is_idempotent_but_conflicting_event_id_is_rejected() -> None:
    coordinator = FactoryCoordinator(InMemoryFactoryRepository())
    factory_job = job()
    forge_block = block(FactoryPhase.FORGE_REQUESTED)
    coordinator.register(factory_job)

    coordinator.record(forge_block)
    coordinator.record(forge_block)
    conflict = forge_block.model_copy(update={"status": "failed"})

    with pytest.raises(FactoryRepositoryError, match="different content"):
        coordinator.record(conflict)

    assert coordinator.blocks(factory_job.job_id) == (forge_block,)


def test_invalid_transition_is_never_persisted() -> None:
    coordinator = FactoryCoordinator(InMemoryFactoryRepository())
    factory_job = job()
    coordinator.register(factory_job)

    with pytest.raises(FactoryLifecycleError, match="illegal phase"):
        coordinator.record(block(FactoryPhase.BUILD_PASSED))

    assert coordinator.blocks(factory_job.job_id) == ()


def test_promotion_reads_accepted_evaluation_from_repository() -> None:
    factory_job = job()
    evaluation = accepted_evaluation()
    repository = EvaluationLookupRepository(
        evaluation,
        accepted_release_decision(evaluation),
    )
    coordinator = FactoryCoordinator(repository)
    coordinator.register(factory_job)
    for phase, assertions in (
        (FactoryPhase.FORGE_REQUESTED, ()),
        (FactoryPhase.BLUEPRINT_CREATED, ()),
        (FactoryPhase.TOOL_CANDIDATE_TESTED, ()),
        (FactoryPhase.AGENT_CODE_CREATED, ()),
        (FactoryPhase.BUILD_PASSED, ()),
        (FactoryPhase.REAL_CASE_EVIDENCE, ("real_case_green",)),
        (FactoryPhase.QUALITY_REVIEWED, ("schema_valid",)),
    ):
        coordinator.record(block(phase, assertions=assertions))

    coordinator.record(
        block(
            FactoryPhase.CAPABILITY_PROMOTED,
            assertions=factory_job.acceptance_assertion_ids,
        )
    )

    assert repository.lookup_job_ids == [factory_job.job_id]
    assert repository.decision_lookup_job_ids == [factory_job.job_id]
    assert coordinator.projection(factory_job.job_id).evaluation_id == evaluation.evidence.evidence_id


def test_coordinator_routes_failed_skill_evaluation_through_improvement() -> None:
    evaluation = accepted_evaluation()
    failed = evaluation.evidence.model_copy(update={"outcome": "failed"})
    repository = EvaluationLookupRepository(
        type(evaluation)(**{**evaluation.__dict__, "evidence": failed})
    )
    coordinator = FactoryCoordinator(repository)
    factory_job = job()
    coordinator.register(factory_job)
    for phase, assertions in (
        (FactoryPhase.FORGE_REQUESTED, ()),
        (FactoryPhase.BLUEPRINT_CREATED, ()),
        (FactoryPhase.TOOL_CANDIDATE_TESTED, ()),
        (FactoryPhase.AGENT_CODE_CREATED, ()),
        (FactoryPhase.BUILD_PASSED, ()),
        (FactoryPhase.REAL_CASE_EVIDENCE, ("real_case_green",)),
        (FactoryPhase.QUALITY_REVIEWED, ("schema_valid",)),
    ):
        coordinator.record(block(phase, assertions=assertions))

    assert coordinator.next_action(factory_job.job_id).kind is FactoryActionKind.APPEND_IMPROVEMENT_REQUESTED
    assert repository.lookup_job_ids == [factory_job.job_id]


@pytest.mark.parametrize("factory_job", [job(), job_v2(), job_v3()])
def test_repository_replays_v1_v2_and_v3_without_changing_phase_order(factory_job) -> None:
    coordinator = FactoryCoordinator(InMemoryFactoryRepository())
    coordinator.register(factory_job)
    for phase in (
        FactoryPhase.FORGE_REQUESTED,
        FactoryPhase.BLUEPRINT_CREATED,
        FactoryPhase.TOOL_CANDIDATE_TESTED,
    ):
        current = block(phase)
        if current.job_id != factory_job.job_id:
            current = current.model_copy(
                update={
                    "job_id": factory_job.job_id,
                    "correlation_id": factory_job.correlation_id,
                    "causation_id": factory_job.event_id,
                }
            )
        coordinator.record(current)

    projection = coordinator.projection(factory_job.job_id)

    assert projection.job == factory_job
    assert projection.phase is FactoryPhase.TOOL_CANDIDATE_TESTED
    assert coordinator.next_action(factory_job.job_id).kind is FactoryActionKind.SUBMIT_FORGE_JOB


def test_coordinator_reads_gateway_workflow_artifacts_for_v3_feedback() -> None:
    factory_job = job_v3()
    evaluation = workflow_evaluation(failed=True)
    feedback = FactoryFeedbackBuilder(clock=lambda: evaluation.occurred_at).build(
        invocation=_report_invocation(evaluation),
        candidate_ref=workflow_candidate(),
        evaluation=evaluation,
        budget_projection=workflow_budget(),
    )
    repository = WorkflowLookupRepository(
        artifacts=(evaluation, feedback),
        summaries=(state_workflow_benchmark(),),
    )
    coordinator = FactoryCoordinator(repository)
    coordinator.register(factory_job)
    for phase in (
        FactoryPhase.FORGE_REQUESTED,
        FactoryPhase.BLUEPRINT_CREATED,
        FactoryPhase.TOOL_CANDIDATE_TESTED,
        FactoryPhase.AGENT_CODE_CREATED,
        FactoryPhase.BUILD_PASSED,
        FactoryPhase.REAL_CASE_EVIDENCE,
    ):
        coordinator.record(workflow_block(phase))
    reviewed = workflow_block(
        FactoryPhase.QUALITY_REVIEWED,
        assertions=factory_job.acceptance_assertion_ids,
    ).model_copy(
        update={"artifact_refs": (evaluation.artifact_ref, feedback.artifact_ref)}
    )
    coordinator.record(reviewed)

    projection = coordinator.projection(factory_job.job_id)

    assert projection.workflow_evaluation_ref == evaluation.artifact_ref
    assert projection.feedback_ref == feedback.artifact_ref
    assert coordinator.next_action(factory_job.job_id).kind is FactoryActionKind.APPEND_IMPROVEMENT_REQUESTED


def test_coordinator_resolves_gateway_benchmark_summary_for_promotion() -> None:
    factory_job = release_workflow_job(mode="release")
    runs = tuple(workflow_run(number) for number in range(1, 4))
    evaluation = release_workflow_evaluation(runs)
    feedback = FactoryFeedbackBuilder(clock=lambda: evaluation.occurred_at).build(
        invocation=_report_invocation(evaluation),
        candidate_ref=runs[0].candidate_ref,
        evaluation=evaluation,
        budget_projection=release_workflow_budget(),
    )
    repository = WorkflowLookupRepository(
        artifacts=(*runs, evaluation, feedback),
        budget=release_workflow_budget(),
        receipts=workflow_receipts(runs),
        summaries=(workflow_benchmark(runs),),
    )
    coordinator = FactoryCoordinator(repository)
    coordinator.register(factory_job)
    for phase in (
        FactoryPhase.FORGE_REQUESTED,
        FactoryPhase.BLUEPRINT_CREATED,
        FactoryPhase.TOOL_CANDIDATE_TESTED,
        FactoryPhase.AGENT_CODE_CREATED,
        FactoryPhase.BUILD_PASSED,
        FactoryPhase.REAL_CASE_EVIDENCE,
    ):
        coordinator.record(workflow_block(phase))
    reviewed = workflow_block(
        FactoryPhase.QUALITY_REVIEWED,
        assertions=factory_job.acceptance_assertion_ids,
    ).model_copy(
        update={"artifact_refs": (evaluation.artifact_ref, feedback.artifact_ref)}
    )
    coordinator.record(reviewed)

    assert coordinator.next_action(factory_job.job_id).kind is FactoryActionKind.VALIDATE_FOR_PROMOTION
    assert coordinator.projection(factory_job.job_id).status.value == "running"
