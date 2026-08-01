"""Gateway-owned sole-writer adapter for benchmark demo provisioning."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar
from uuid import UUID

from fastapi import HTTPException
from pymysql.err import MySQLError

from blockchain.mariadb_storage import MariaDBStorage
from agenten.agent_factory.business_benchmark_demo_provisioning import (
    BusinessBenchmarkDemoResumeStateV1,
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
from agenten.validation.contracts import WorkBatch
from gateway.factory_repository import GatewayFactoryRepository
from gateway.store import GatewayStore


_T = TypeVar("_T")


@dataclass(frozen=True)
class _RootWorkBatchWrite:
    block_type: str
    data: dict[str, Any]
    status: str = "pending"
    parent_index: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


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

    def resume_state(self, job_id: UUID) -> BusinessBenchmarkDemoResumeStateV1 | None:
        try:
            stored = self._store.factory_job(job_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return None
            raise GatewayBusinessBenchmarkDemoError(str(exc.detail)) from exc
        except MySQLError as exc:
            raise GatewayBusinessBenchmarkDemoError(
                "Captain Gateway MariaDB operation failed"
            ) from exc
        if not isinstance(stored.job, AgentFactoryJobV3):
            raise GatewayBusinessBenchmarkDemoError(
                "existing stable benchmark job is not a V3 job"
            )
        return BusinessBenchmarkDemoResumeStateV1(
            job=stored.job,
            phase=stored.projection.phase,
            attempt=stored.projection.attempt,
        )

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

    def persist_work_batch(
        self,
        job: AgentFactoryJobV3,
        batch: WorkBatch,
    ) -> None:
        if (
            batch.batch_id != f"renewal-{job.job_id.hex[:24]}"
            or f"factory-job-id:{job.job_id}" not in batch.constraints
        ):
            raise GatewayBusinessBenchmarkDemoError(
                "Renewal WorkBatch is not bound to the factory job"
            )
        request = _RootWorkBatchWrite(
            block_type="work_batch",
            data=batch.model_dump(mode="json"),
            metadata={
                "factory_job_id": str(job.job_id),
                "purpose": "business_benchmark_renewal_n8n",
            },
        )
        self._translate(lambda: self._store.append(request, None))

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


def resolve_current_factory_attempts(
    dsn: str,
    job_ids: tuple[UUID, ...],
) -> dict[UUID, int]:
    """Read current Captain attempts through the Gateway-owned DB boundary."""

    if not job_ids or len(job_ids) != len(set(job_ids)):
        raise GatewayBusinessBenchmarkDemoError(
            "benchmark attempt job IDs must be non-empty and unique"
        )
    authority = GatewayBusinessBenchmarkDemoAuthority(dsn)
    resolved: dict[UUID, int] = {}
    for job_id in job_ids:
        state = authority.resume_state(job_id)
        if state is None or not 1 <= state.attempt <= 5:
            raise GatewayBusinessBenchmarkDemoError(
                "current Captain Factory attempt is unavailable"
            )
        resolved[job_id] = state.attempt
    return resolved


__all__ = [
    "GatewayBusinessBenchmarkDemoAuthority",
    "GatewayBusinessBenchmarkDemoError",
    "resolve_current_factory_attempts",
]
