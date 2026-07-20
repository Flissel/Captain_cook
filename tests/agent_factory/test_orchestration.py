from __future__ import annotations

import pytest
from datetime import datetime, timezone

from agenten.agent_factory.contracts import FactoryPhase, FactoryRole
from agenten.agent_factory.orchestration import FactoryDispatchError, FactoryDispatcher
from agenten.agent_factory.leases import issue_factory_lease, validate_factory_lease
from agenten.agent_factory.service import FactoryCoordinator, InMemoryFactoryRepository
from agenten.agent_factory.state_machine import FactoryActionKind
from tests.agent_factory.test_state_machine import block, job


class Hermes:
    def __init__(self) -> None:
        self.requests = []

    async def dispatch(self, request: object):
        self.requests.append(request)
        return block(FactoryPhase.BLUEPRINT_CREATED)


class Forge:
    def __init__(self) -> None:
        self.requests = []

    async def submit(self, request: object) -> None:
        self.requests.append(request)


class CandidateValidator:
    def __init__(self) -> None:
        self.requests = []

    async def dispatch(self, request: object):
        self.requests.append(request)
        return block(FactoryPhase.BUILD_PASSED)


class Clock:
    def now(self) -> datetime:
        return datetime(2026, 7, 19, 10, tzinfo=timezone.utc)


class Leases:
    def active(self, factory_job, role, attempt, now):
        lease = issue_factory_lease(
            job=factory_job,
            role=role,
            attempt=attempt,
            workspace_ref="workspace://factory/support-triage",
            now=now,
        )
        return validate_factory_lease(lease, job=factory_job, role=role, attempt=attempt, now=now)


def dispatcher(coordinator, hermes, forge, validator=None) -> FactoryDispatcher:
    return FactoryDispatcher(
        coordinator=coordinator,
        hermes=hermes,
        forge=forge,
        candidate_validator=validator,
        leases=Leases(),
        clock=Clock(),
    )


@pytest.mark.asyncio
async def test_dispatches_architect_only_after_captain_forge_request() -> None:
    coordinator = FactoryCoordinator(InMemoryFactoryRepository())
    factory_job = job()
    coordinator.register(factory_job)
    coordinator.record(block(FactoryPhase.FORGE_REQUESTED))
    hermes, forge = Hermes(), Forge()

    action = await dispatcher(coordinator, hermes, forge).dispatch_next(factory_job.job_id)

    assert action.kind is FactoryActionKind.DISPATCH_AGENT_ARCHITECT
    assert hermes.requests[0].role is FactoryRole.AGENT_ARCHITECT
    assert hermes.requests[0].lease is not None
    assert coordinator.projection(factory_job.job_id).phase is FactoryPhase.BLUEPRINT_CREATED
    assert forge.requests == []


@pytest.mark.asyncio
async def test_dispatches_forge_only_after_tool_candidate_evidence() -> None:
    coordinator = FactoryCoordinator(InMemoryFactoryRepository())
    factory_job = job()
    coordinator.register(factory_job)
    coordinator.record(block(FactoryPhase.FORGE_REQUESTED))
    coordinator.record(block(FactoryPhase.BLUEPRINT_CREATED))
    coordinator.record(block(FactoryPhase.TOOL_CANDIDATE_TESTED))
    hermes, forge = Hermes(), Forge()

    action = await dispatcher(coordinator, hermes, forge).dispatch_next(factory_job.job_id)

    assert action.kind is FactoryActionKind.SUBMIT_FORGE_JOB
    assert len(forge.requests) == 1


@pytest.mark.asyncio
async def test_captain_transition_is_not_dispatched_externally() -> None:
    coordinator = FactoryCoordinator(InMemoryFactoryRepository())
    factory_job = job()
    coordinator.register(factory_job)
    hermes, forge = Hermes(), Forge()

    with pytest.raises(FactoryDispatchError, match="Captain state transition"):
        await dispatcher(coordinator, hermes, forge).dispatch_next(factory_job.job_id)


@pytest.mark.asyncio
async def test_dispatches_candidate_build_validator_after_agent_code_evidence() -> None:
    coordinator = FactoryCoordinator(InMemoryFactoryRepository())
    factory_job = job()
    coordinator.register(factory_job)
    for phase in (
        FactoryPhase.FORGE_REQUESTED,
        FactoryPhase.BLUEPRINT_CREATED,
        FactoryPhase.TOOL_CANDIDATE_TESTED,
        FactoryPhase.AGENT_CODE_CREATED,
    ):
        coordinator.record(block(phase))
    hermes, forge, validator = Hermes(), Forge(), CandidateValidator()

    action = await dispatcher(coordinator, hermes, forge, validator).dispatch_next(factory_job.job_id)

    assert action.kind is FactoryActionKind.DISPATCH_BUILD_VALIDATOR
    assert validator.requests[0].role is FactoryRole.TOOL_INTEGRATOR
    assert coordinator.projection(factory_job.job_id).phase is FactoryPhase.BUILD_PASSED
    assert hermes.requests == []
