"""Ports that connect Captain's factory policy to Hermes and Minibook Forge."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from agenten.agent_factory.contracts import AgentFactoryJob, FactoryRole
from agenten.agent_factory.service import FactoryCoordinator
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind


class FactoryDispatchError(RuntimeError):
    """A provider cannot perform the Captain-authorized factory action."""


@dataclass(frozen=True)
class FactoryDispatch:
    job: AgentFactoryJob
    action: FactoryAction
    role: FactoryRole | None


class HermesFactoryPort(Protocol):
    """Execute one role step and return evidence through the gateway separately."""

    async def dispatch(self, request: FactoryDispatch) -> None:
        """Start the leased Hermes role action without bypassing Captain evidence."""


class MinibookForgePort(Protocol):
    """Submit the approved build to Minibook's existing SwarmPipeline."""

    async def submit(self, request: FactoryDispatch) -> None:
        """Submit only; Forge must later post an immutable evidence block."""


_ROLE_ACTIONS: dict[FactoryActionKind, FactoryRole] = {
    FactoryActionKind.DISPATCH_AGENT_ARCHITECT: FactoryRole.AGENT_ARCHITECT,
    FactoryActionKind.DISPATCH_TOOL_INTEGRATOR: FactoryRole.TOOL_INTEGRATOR,
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
    ) -> None:
        self._coordinator = coordinator
        self._hermes = hermes
        self._forge = forge

    async def dispatch_next(self, job_id: UUID) -> FactoryAction:
        action = self._coordinator.next_action(job_id)
        job = self._coordinator.projection(job_id).job
        if action.kind in _ROLE_ACTIONS:
            await self._hermes.dispatch(
                FactoryDispatch(job=job, action=action, role=_ROLE_ACTIONS[action.kind])
            )
            return action
        if action.kind is FactoryActionKind.SUBMIT_FORGE_JOB:
            await self._forge.submit(FactoryDispatch(job=job, action=action, role=None))
            return action
        raise FactoryDispatchError(
            f"{action.kind.value} is a Captain state transition, not an external dispatch"
        )
