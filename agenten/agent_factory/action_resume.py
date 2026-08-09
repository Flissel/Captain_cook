"""Nonblocking Captain coordination for one answered Factory action."""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Protocol
from uuid import UUID

from agenten.agent_factory.forge_contracts import (
    FactoryActionRequestV1,
    FactoryActionResponseV1,
)


class FactoryActionConsumer(Protocol):
    def get(
        self, request_id: UUID, *, tenant_id: str, now: datetime
    ) -> FactoryActionRequestV1: ...

    def consume(
        self,
        request_id: UUID,
        *,
        tenant_id: str,
        factory_job_id: UUID,
        creation_job_id: UUID,
        correlation_id: UUID,
        expected_subject_version: int,
        safe_checkpoint_sha256: str,
        now: datetime,
    ) -> FactoryActionResponseV1: ...


class FactoryActionProjection(Protocol):
    def project_factory_action(self, projection: dict[str, object]) -> None: ...


class FactoryContinuationScheduler(Protocol):
    def schedule_factory_continuation(self, **continuation: object) -> None: ...


class FactoryActionResumeCoordinator:
    """Consume once, redact the projection, and enqueue the exact continuation."""

    def __init__(
        self,
        *,
        store: FactoryActionConsumer,
        projection: FactoryActionProjection,
        scheduler: FactoryContinuationScheduler,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._projection = projection
        self._scheduler = scheduler
        self._clock = clock

    def resume(self, *, request_id: UUID, tenant_id: str) -> FactoryActionResponseV1:
        now = self._clock()
        request = self._store.get(request_id, tenant_id=tenant_id, now=now)
        response = self._store.consume(
            request_id,
            tenant_id=tenant_id,
            factory_job_id=request.factory_job_id,
            creation_job_id=request.creation_job_id,
            correlation_id=request.correlation_id,
            expected_subject_version=request.expected_subject_version,
            safe_checkpoint_sha256=request.safe_checkpoint_sha256,
            now=now,
        )
        self._projection.project_factory_action(
            {
                "schema": "minibook.factory-action-projection.v1",
                "request_id": str(request.request_id),
                "factory_job_id": str(request.factory_job_id),
                "creation_job_id": str(request.creation_job_id),
                "correlation_id": str(request.correlation_id),
                "request_type": request.request_type,
                "status": "consumed",
                "safe_checkpoint_sha256": request.safe_checkpoint_sha256,
            }
        )
        self._scheduler.schedule_factory_continuation(
            factory_job_id=request.factory_job_id,
            creation_job_id=request.creation_job_id,
            correlation_id=request.correlation_id,
            expected_subject_version=request.expected_subject_version,
            safe_checkpoint_ref=request.safe_checkpoint_ref,
            approved_scope=response.approved_scope,
            credential_ref=response.credential_ref,
            answer_metadata=response.answer_metadata,
        )
        return response
