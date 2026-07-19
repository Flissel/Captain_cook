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
