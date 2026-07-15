"""Worker adapter that routes one pipeline assignment to one householder role."""
from __future__ import annotations

from typing import Optional

from agenten.household.executor import (
    DeterministicHouseholderExecutor,
    HouseholderExecutionError,
    HouseholderExecutor,
)
from agenten.household.roles import HouseholderRoleSpec, load_householder_roles
from agenten.runtime.event_bus import EventBus
from agenten.tools.base import ToolRegistry
from agenten.workers.base import DescriptionResolver, WorkerAgent, WorkerExecutionError, WorkerFactory


class HouseholderWorker(WorkerAgent):
    """A generic pipeline worker parameterized by one role specification."""

    def __init__(
        self,
        bus: EventBus,
        tools: ToolRegistry,
        *,
        role: HouseholderRoleSpec,
        executor: Optional[HouseholderExecutor] = None,
        heartbeat_interval_seconds: float = 20.0,
        description_resolver: Optional[DescriptionResolver] = None,
    ) -> None:
        self.role = role
        self.agent_type = role.agent_type
        self.capability_tags = list(role.capability_tags)
        self._executor = executor if executor is not None else DeterministicHouseholderExecutor()
        super().__init__(
            bus,
            tools,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            description_resolver=description_resolver,
        )

    async def execute(self, subproblem_id: str, description: str) -> dict[str, object]:
        try:
            report = await self._executor.run(self.role, subproblem_id, description)
        except HouseholderExecutionError as exc:
            raise WorkerExecutionError(str(exc), retriable=exc.retriable) from exc
        except Exception as exc:  # noqa: BLE001 - external executors are isolated at this boundary
            raise WorkerExecutionError(
                f"householder {self.role.role_id!r} execution failed: {exc}", retriable=True
            ) from exc
        return report.as_result()


def create_householder_worker_factories(
    *,
    roles: Optional[tuple[HouseholderRoleSpec, ...]] = None,
    executor: Optional[HouseholderExecutor] = None,
) -> tuple[WorkerFactory, ...]:
    """Build the four default worker factories without starting external services."""
    configured_roles = roles if roles is not None else load_householder_roles()
    configured_executor = executor if executor is not None else DeterministicHouseholderExecutor()

    def factory_for(role: HouseholderRoleSpec) -> WorkerFactory:
        def factory(
            *,
            bus: EventBus,
            tools: ToolRegistry,
            heartbeat_interval_seconds: float,
            description_resolver: DescriptionResolver,
        ) -> WorkerAgent:
            return HouseholderWorker(
                bus,
                tools,
                role=role,
                executor=configured_executor,
                heartbeat_interval_seconds=heartbeat_interval_seconds,
                description_resolver=description_resolver,
            )

        return factory

    return tuple(factory_for(role) for role in configured_roles)
