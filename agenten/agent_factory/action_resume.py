"""Nonblocking Captain coordination for one answered Factory action."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Callable, Literal, Protocol
from uuid import UUID

from agenten.agent_factory.forge_contracts import (
    FactoryActionRequestV1,
    FactoryActionResponseV1,
)


@dataclass(frozen=True)
class FactoryActionResumeRecord:
    request: FactoryActionRequestV1
    response: FactoryActionResponseV1
    phase: Literal[
        "answered",
        "projection_intent",
        "projection_acknowledged",
        "continuation_intent",
        "continuation_acknowledged",
    ]
    projection_idempotency_key: str
    continuation_idempotency_key: str


class FactoryActionConsumer(Protocol):
    def prepare_projection(
        self, request_id: UUID, *, tenant_id: str, now: datetime
    ) -> FactoryActionResumeRecord: ...

    def acknowledge_projection(
        self,
        request_id: UUID,
        *,
        tenant_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> FactoryActionResumeRecord: ...

    def prepare_continuation(
        self, request_id: UUID, *, tenant_id: str, now: datetime
    ) -> FactoryActionResumeRecord: ...

    def acknowledge_continuation(
        self,
        request_id: UUID,
        *,
        tenant_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> FactoryActionResumeRecord: ...


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
        state = self._store.prepare_projection(
            request_id, tenant_id=tenant_id, now=now
        )
        request = state.request
        if state.phase == "projection_intent":
            self._projection.project_factory_action(
                {
                    "schema": "minibook.factory-action-projection.v1",
                    "idempotency_key": state.projection_idempotency_key,
                    "request_id": str(request.request_id),
                    "factory_job_id": str(request.factory_job_id),
                    "creation_job_id": str(request.creation_job_id),
                    "correlation_id": str(request.correlation_id),
                    "request_type": request.request_type,
                    "status": "answered",
                    "safe_checkpoint_sha256": request.safe_checkpoint_sha256,
                }
            )
            state = self._store.acknowledge_projection(
                request_id,
                tenant_id=tenant_id,
                idempotency_key=state.projection_idempotency_key,
                now=self._clock(),
            )
        if state.phase in {"projection_acknowledged", "continuation_intent"}:
            state = self._store.prepare_continuation(
                request_id, tenant_id=tenant_id, now=self._clock()
            )
        if state.phase == "continuation_intent":
            response = state.response
            self._scheduler.schedule_factory_continuation(
                idempotency_key=state.continuation_idempotency_key,
                factory_job_id=request.factory_job_id,
                creation_job_id=request.creation_job_id,
                correlation_id=request.correlation_id,
                expected_subject_version=request.expected_subject_version,
                safe_checkpoint_ref=request.safe_checkpoint_ref,
                approved_scope=response.approved_scope,
                credential_ref=response.credential_ref,
                answer_metadata=response.answer_metadata,
            )
            state = self._store.acknowledge_continuation(
                request_id,
                tenant_id=tenant_id,
                idempotency_key=state.continuation_idempotency_key,
                now=self._clock(),
            )
        return state.response


class FactoryActionResumeService:
    """Submit durable resume work to a dedicated worker without blocking HTTP."""

    def __init__(
        self,
        *,
        coordinator: FactoryActionResumeCoordinator,
        recovery_source: Callable[[], tuple[tuple[UUID, str], ...]] | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._recovery_source = recovery_source or (lambda: ())
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="factory-action-resume"
        )
        self._lock = Lock()
        self._futures: set[Future[object]] = set()

    def enqueue(self, *, request_id: UUID, tenant_id: str) -> None:
        future = self._executor.submit(
            self._coordinator.resume, request_id=request_id, tenant_id=tenant_id
        )
        with self._lock:
            self._futures.add(future)
        future.add_done_callback(self._completed)

    def recover(self) -> None:
        for request_id, tenant_id in self._recovery_source():
            self.enqueue(request_id=request_id, tenant_id=tenant_id)

    def _completed(self, future: Future[object]) -> None:
        with self._lock:
            self._futures.discard(future)
        # Retrieve the exception so the executor does not silently retain it.
        # Durable intent remains retryable and is recovered by a later enqueue.
        future.exception()

    def close(self) -> None:
        self._executor.shutdown(wait=True)
