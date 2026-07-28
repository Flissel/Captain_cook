from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from agenten.agent_factory.contracts import FactoryRole
from agenten.agent_factory.production_dispatch_runner import (
    ProductionFactoryDispatchRunner,
)
from agenten.agent_factory.state_machine import (
    FactoryAction,
    FactoryActionKind,
    FactoryLifecycleStatus,
)


NOW = datetime(2026, 7, 28, 12, tzinfo=timezone.utc)
JOB_ID = UUID("91000000-0000-0000-0000-000000000001")
OTHER_JOB_ID = UUID("91000000-0000-0000-0000-000000000002")


class ScriptedCoordinator:
    def __init__(self, actions: tuple[FactoryActionKind, ...]) -> None:
        self._actions = list(actions)
        self.job = type("Job", (), {"job_id": JOB_ID})()

    def next_action(self, job_id: UUID) -> FactoryAction:
        assert job_id == JOB_ID
        return FactoryAction(kind=self._actions[0], attempt=1, job_id=job_id)

    def projection(self, job_id: UUID):
        assert job_id == JOB_ID
        return type(
            "Projection",
            (),
            {"job": self.job, "status": FactoryLifecycleStatus.RUNNING},
        )()

    def advance(self) -> None:
        self._actions.pop(0)


class RecordingDispatcher:
    def __init__(self, coordinator: ScriptedCoordinator) -> None:
        self._coordinator = coordinator
        self.actions: list[FactoryActionKind] = []

    async def dispatch_next(self, job_id: UUID) -> FactoryAction:
        action = self._coordinator.next_action(job_id)
        self.actions.append(action.kind)
        self._coordinator.advance()
        return action


class RecordingLeaseIssuer:
    def __init__(self) -> None:
        self.roles: list[tuple[FactoryActionKind, FactoryRole]] = []

    def ensure_for(self, job, action: FactoryAction, role: FactoryRole, now: datetime):
        assert now == NOW
        self.roles.append((action.kind, role))


@pytest.mark.asyncio
async def test_runner_resumes_external_actions_and_stops_before_captain_transition() -> None:
    coordinator = ScriptedCoordinator(
        (
            FactoryActionKind.DISPATCH_AGENT_ARCHITECT,
            FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
            FactoryActionKind.SUBMIT_FORGE_JOB,
            FactoryActionKind.DISPATCH_BUILD_VALIDATOR,
            FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
            FactoryActionKind.DISPATCH_QUALITY_WARDEN,
            FactoryActionKind.VALIDATE_FOR_PROMOTION,
        )
    )
    dispatcher = RecordingDispatcher(coordinator)
    leases = RecordingLeaseIssuer()

    result = await ProductionFactoryDispatchRunner(
        coordinator=coordinator,
        dispatcher=dispatcher,
        lease_issuer=leases,
        clock=lambda: NOW,
    ).run(JOB_ID)

    assert result.status == "captain_action_required"
    assert result.next_action.kind is FactoryActionKind.VALIDATE_FOR_PROMOTION
    assert result.dispatched_actions == tuple(dispatcher.actions)
    assert leases.roles == [
        (FactoryActionKind.DISPATCH_AGENT_ARCHITECT, FactoryRole.AGENT_ARCHITECT),
        (FactoryActionKind.DISPATCH_TOOL_INTEGRATOR, FactoryRole.TOOL_INTEGRATOR),
        (FactoryActionKind.SUBMIT_FORGE_JOB, FactoryRole.TOOL_INTEGRATOR),
        (FactoryActionKind.DISPATCH_BUILD_VALIDATOR, FactoryRole.TOOL_INTEGRATOR),
        (FactoryActionKind.DISPATCH_REAL_CASE_TESTER, FactoryRole.REAL_CASE_TESTER),
        (FactoryActionKind.DISPATCH_QUALITY_WARDEN, FactoryRole.QUALITY_WARDEN),
    ]


@pytest.mark.asyncio
async def test_runner_never_dispatches_or_promotes_a_captain_only_action() -> None:
    coordinator = ScriptedCoordinator((FactoryActionKind.APPEND_IMPROVEMENT_REQUESTED,))
    dispatcher = RecordingDispatcher(coordinator)
    leases = RecordingLeaseIssuer()

    result = await ProductionFactoryDispatchRunner(
        coordinator=coordinator,
        dispatcher=dispatcher,
        lease_issuer=leases,
        clock=lambda: NOW,
    ).run(JOB_ID)

    assert result.status == "captain_action_required"
    assert result.next_action.kind is FactoryActionKind.APPEND_IMPROVEMENT_REQUESTED
    assert result.dispatched_actions == ()
    assert dispatcher.actions == []
    assert leases.roles == []


@pytest.mark.asyncio
async def test_runner_is_bounded_without_reclassifying_next_action() -> None:
    coordinator = ScriptedCoordinator(
        (
            FactoryActionKind.DISPATCH_AGENT_ARCHITECT,
            FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
        )
    )
    dispatcher = RecordingDispatcher(coordinator)

    result = await ProductionFactoryDispatchRunner(
        coordinator=coordinator,
        dispatcher=dispatcher,
        lease_issuer=RecordingLeaseIssuer(),
        clock=lambda: NOW,
    ).run(JOB_ID, maximum_dispatches=1)

    assert result.status == "dispatch_limit_reached"
    assert result.next_action.kind is FactoryActionKind.DISPATCH_TOOL_INTEGRATOR
    assert result.dispatched_actions == (FactoryActionKind.DISPATCH_AGENT_ARCHITECT,)


@pytest.mark.asyncio
async def test_dispatch_limit_rejects_job_identity_drift() -> None:
    coordinator = ScriptedCoordinator(
        (
            FactoryActionKind.DISPATCH_AGENT_ARCHITECT,
            FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
        )
    )

    class DriftingDispatcher(RecordingDispatcher):
        async def dispatch_next(self, job_id: UUID) -> FactoryAction:
            action = await super().dispatch_next(job_id)
            coordinator.job = type("Job", (), {"job_id": OTHER_JOB_ID})()
            return action

    dispatcher = DriftingDispatcher(coordinator)
    leases = RecordingLeaseIssuer()

    with pytest.raises(ValueError, match="job identity"):
        await ProductionFactoryDispatchRunner(
            coordinator=coordinator,
            dispatcher=dispatcher,
            lease_issuer=leases,
            clock=lambda: NOW,
        ).run(JOB_ID, maximum_dispatches=1)

    assert dispatcher.actions == [FactoryActionKind.DISPATCH_AGENT_ARCHITECT]


@pytest.mark.asyncio
async def test_runner_rejects_job_identity_drift_before_lease_or_dispatch() -> None:
    coordinator = ScriptedCoordinator((FactoryActionKind.DISPATCH_AGENT_ARCHITECT,))
    coordinator.job = type("Job", (), {"job_id": OTHER_JOB_ID})()
    dispatcher = RecordingDispatcher(coordinator)
    leases = RecordingLeaseIssuer()

    with pytest.raises(ValueError, match="job identity"):
        await ProductionFactoryDispatchRunner(
            coordinator=coordinator,
            dispatcher=dispatcher,
            lease_issuer=leases,
            clock=lambda: NOW,
        ).run(JOB_ID)

    assert dispatcher.actions == []
    assert leases.roles == []


def test_runner_rejects_a_dispatcher_with_a_different_lease_authority() -> None:
    coordinator = ScriptedCoordinator((FactoryActionKind.DISPATCH_AGENT_ARCHITECT,))
    dispatcher = RecordingDispatcher(coordinator)
    dispatcher.lease_authority = object()

    with pytest.raises(ValueError, match="same lease authority"):
        ProductionFactoryDispatchRunner(
            coordinator=coordinator,
            dispatcher=dispatcher,
            lease_issuer=RecordingLeaseIssuer(),
            clock=lambda: NOW,
        )
