"""Ports that connect Captain's factory policy to Hermes and Minibook Forge."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from agenten.agent_factory.contracts import AgentFactoryJob, FactoryEvidenceBlock, FactoryLease, FactoryRole
from agenten.agent_factory.leases import FactoryLeasePort
from agenten.agent_factory.service import FactoryCoordinator
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind


class FactoryDispatchError(RuntimeError):
    """A provider cannot perform the Captain-authorized factory action."""


@dataclass(frozen=True)
class FactoryDispatch:
    job: AgentFactoryJob
    action: FactoryAction
    role: FactoryRole | None
    lease: FactoryLease | None


class FactoryClock(Protocol):
    def now(self) -> datetime: ...


class HermesFactoryPort(Protocol):
    """Execute one role step and return evidence through the gateway separately."""

    async def dispatch(self, request: FactoryDispatch) -> FactoryEvidenceBlock:
        """Run one leased Hermes role and return its untrusted evidence payload."""


class MinibookForgePort(Protocol):
    """Submit the approved build to Minibook's existing SwarmPipeline."""

    async def submit(self, request: FactoryDispatch) -> None:
        """Submit only; Forge must later post an immutable evidence block."""


class FactoryCandidateValidationPort(Protocol):
    """Evaluate the sealed generated candidate in an isolated workspace."""

    async def dispatch(self, request: FactoryDispatch) -> FactoryEvidenceBlock:
        """Return build, real-case, or quality evidence for the leased role."""


_ROLE_ACTIONS: dict[FactoryActionKind, FactoryRole] = {
    FactoryActionKind.DISPATCH_AGENT_ARCHITECT: FactoryRole.AGENT_ARCHITECT,
    FactoryActionKind.DISPATCH_TOOL_INTEGRATOR: FactoryRole.TOOL_INTEGRATOR,
    FactoryActionKind.DISPATCH_BUILD_VALIDATOR: FactoryRole.TOOL_INTEGRATOR,
    FactoryActionKind.DISPATCH_REAL_CASE_TESTER: FactoryRole.REAL_CASE_TESTER,
    FactoryActionKind.DISPATCH_QUALITY_WARDEN: FactoryRole.QUALITY_WARDEN,
}


class FactoryDispatcher:
    """Dispatch one allowed side effect; persistence remains in FactoryCoordinator."""

    def __init__(
        self,
        *,
        coordinator: FactoryCoordinator,
        hermes: HermesFactoryPort,
        forge: MinibookForgePort,
        candidate_validator: FactoryCandidateValidationPort | None = None,
        leases: FactoryLeasePort,
        clock: FactoryClock,
    ) -> None:
        self._coordinator = coordinator
        self._hermes = hermes
        self._forge = forge
        self._candidate_validator = candidate_validator
        self._leases = leases
        self._clock = clock

    async def dispatch_next(self, job_id: UUID) -> FactoryAction:
        action = self._coordinator.next_action(job_id)
        job = self._coordinator.projection(job_id).job
        if action.kind in _ROLE_ACTIONS:
            role = _ROLE_ACTIONS[action.kind]
            request = FactoryDispatch(
                job=job,
                action=action,
                role=role,
                lease=self._leases.active(job, role, action.attempt, self._clock.now()),
            )
            if action.kind is FactoryActionKind.DISPATCH_BUILD_VALIDATOR:
                if self._candidate_validator is None:
                    raise FactoryDispatchError("candidate build validator is not configured")
                evidence = await self._candidate_validator.dispatch(request)
            elif (
                action.kind
                in {
                    FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
                    FactoryActionKind.DISPATCH_QUALITY_WARDEN,
                }
                and self._candidate_validator is not None
            ):
                evidence = await self._candidate_validator.dispatch(request)
            else:
                evidence = await self._hermes.dispatch(request)
            self._coordinator.record(evidence)
            return action
        if action.kind is FactoryActionKind.SUBMIT_FORGE_JOB:
            await self._forge.submit(FactoryDispatch(job=job, action=action, role=None, lease=None))
            return action
        raise FactoryDispatchError(
            f"{action.kind.value} is a Captain state transition, not an external dispatch"
        )
