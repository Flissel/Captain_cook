"""Gateway-backed adapter for Captain's factory coordinator port."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException

from agenten.agent_factory.contracts import AgentFactoryJob, FactoryEvidenceBlock
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
