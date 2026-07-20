from __future__ import annotations

import pytest

from agenten.agent_factory.service import (
    FactoryCoordinator,
    FactoryRepositoryError,
    InMemoryFactoryRepository,
)
from agenten.agent_factory.state_machine import FactoryActionKind, FactoryLifecycleError
from tests.agent_factory.test_state_machine import block, job
from agenten.agent_factory.contracts import FactoryPhase
from tests.agent_factory.test_state_machine import accepted_evaluation


class EvaluationLookupRepository(InMemoryFactoryRepository):
    def __init__(self, evaluation):
        super().__init__()
        self.evaluation = evaluation
        self.lookup_job_ids = []

    def evaluation_for_job(self, job_id):
        self.lookup_job_ids.append(job_id)
        return self.evaluation


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
    repository = EvaluationLookupRepository(evaluation)
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
