"""Gateway-owned sole-writer adapter for benchmark demo provisioning."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Callable, TypeVar
from uuid import UUID

from fastapi import HTTPException
from pymysql.err import MySQLError

from blockchain.mariadb_storage import MariaDBStorage
from agenten.agent_factory.business_benchmark_demo_provisioning import (
    assert_local_captain_test_dsn,
)
from agenten.agent_factory.contracts import (
    AgentFactoryJobV3,
    FactoryEvidenceBlock,
    FactoryLease,
)
from agenten.agent_factory.execution_budget import FactoryBudgetProjection
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import FactorySkillStep
from agenten.agent_factory.service import FactoryRepositoryError
from gateway.factory_repository import GatewayFactoryRepository
from gateway.store import GatewayStore


_T = TypeVar("_T")


class GatewayBusinessBenchmarkDemoError(RuntimeError):
    """Expected Gateway or MariaDB failure safe to report at the CLI boundary."""


class GatewayBusinessBenchmarkDemoAuthority:
    """Apply one legal initial Factory state through the Gateway repository."""

    def __init__(self, dsn: str) -> None:
        assert_local_captain_test_dsn(dsn)
        try:
            self._store = GatewayStore(MariaDBStorage(dsn))
        except MySQLError as exc:
            raise GatewayBusinessBenchmarkDemoError(
                "Captain Gateway MariaDB is unavailable"
            ) from exc
        self._repository = GatewayFactoryRepository(self._store)

    def register(self, job: AgentFactoryJobV3) -> None:
        self._translate(lambda: self._repository.register(job))

    def assign_released_skills(
        self,
        job: AgentFactoryJobV3,
        skills: Mapping[FactorySkillStep, ReleasedHermesSkill],
    ) -> None:
        class Catalog:
            def released_for(
                self,
                requested_job: AgentFactoryJobV3,
                step: FactorySkillStep,
            ) -> ReleasedHermesSkill:
                if requested_job != job:
                    raise ValueError("released skill catalog received a mixed job")
                return skills[step]

        self._translate(
            lambda: self._repository.seed_released_skill_assignments(job, Catalog())
        )

    def append_forge_requested(self, block: FactoryEvidenceBlock) -> None:
        self._translate(lambda: self._repository.append(block))

    def record_lease(self, lease: FactoryLease) -> None:
        self._translate(lambda: self._store.record_factory_lease(lease))

    def budget_projection(self, job_id: UUID) -> FactoryBudgetProjection:
        return self._translate(
            lambda: self._repository.workflow_budget_projection(job_id)
        )

    @staticmethod
    def _translate(operation: Callable[[], _T]) -> _T:
        try:
            return operation()
        except HTTPException as exc:
            raise GatewayBusinessBenchmarkDemoError(str(exc.detail)) from exc
        except FactoryRepositoryError as exc:
            raise GatewayBusinessBenchmarkDemoError(str(exc)) from exc
        except MySQLError as exc:
            raise GatewayBusinessBenchmarkDemoError(
                "Captain Gateway MariaDB operation failed"
            ) from exc


__all__ = [
    "GatewayBusinessBenchmarkDemoAuthority",
    "GatewayBusinessBenchmarkDemoError",
]
