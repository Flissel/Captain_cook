"""Gateway-backed adapter for Captain's factory coordinator port."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException

from datetime import datetime

from agenten.agent_factory.contracts import AgentFactoryJob, FactoryEvidenceBlock, FactoryLease, FactoryRole
from agenten.agent_factory.leases import FactoryLeaseDenied, FactoryLeasePort, validate_factory_lease
from agenten.agent_factory.service import FactoryRepository, FactoryRepositoryError
from gateway.store import GatewayStore


class GatewayFactoryRepository(FactoryRepository):
    """Use GatewayStore as the sole durable factory lifecycle authority."""

    def __init__(self, store: GatewayStore) -> None:
        self._store = store

    def register(self, job: AgentFactoryJob) -> None:
        self._translate(lambda: self._store.record_factory_job(job))

    def job(self, job_id: UUID) -> AgentFactoryJob:
        return self._translate(lambda: self._store.factory_job(job_id).job)

    def append(self, block: FactoryEvidenceBlock) -> bool:
        receipt = self._translate(lambda: self._store.record_factory_block(block))
        return not receipt.replayed

    def blocks(self, job_id: UUID) -> tuple[FactoryEvidenceBlock, ...]:
        return self._translate(lambda: self._store.factory_job(job_id).blocks)

    @staticmethod
    def _translate(operation):
        try:
            return operation()
        except HTTPException as exc:
            raise FactoryRepositoryError(str(exc.detail)) from exc


class GatewayFactoryLeases(FactoryLeasePort):
    """Resolve the current valid role lease only from Captain's ledger."""

    def __init__(self, store: GatewayStore) -> None:
        self._store = store

    def active(
        self,
        job: AgentFactoryJob,
        role: FactoryRole,
        attempt: int,
        now: datetime,
    ) -> FactoryLease:
        try:
            leases = self._store.factory_job(job.job_id).leases
        except HTTPException as exc:
            raise FactoryLeaseDenied(str(exc.detail)) from exc
        candidates = [
            lease for lease in leases
            if lease.role is role and lease.attempt == attempt and lease.subject_version == job.subject_version
        ]
        if not candidates:
            raise FactoryLeaseDenied("no active factory lease exists for the requested role")
        candidates.sort(key=lambda lease: lease.issued_at, reverse=True)
        return validate_factory_lease(
            candidates[0], job=job, role=role, attempt=attempt, now=now
        )
