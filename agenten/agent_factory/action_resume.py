"""Nonblocking Captain coordination for one answered Factory action."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
import hashlib
import json
from typing import Callable, Iterable, Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import TypeAdapter

from agenten.agent_factory.forge_contracts import (
    FactoryActionRequestV1,
    FactoryActionResponseV1,
)


@dataclass(frozen=True)
class FactoryActionEffectReceipt:
    """Durable sink acknowledgement for one canonical keyed effect."""

    effect_key: str
    payload_sha256: str

    def __post_init__(self) -> None:
        for value in (self.effect_key, self.payload_sha256):
            if len(value) != 64 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise ValueError("factory action effect receipt is invalid")


class FactoryActionEffectConflict(ValueError):
    """A sink receipt is bound to different canonical effect bytes."""


@dataclass(frozen=True)
class FactoryActionResumeRecord:
    request: FactoryActionRequestV1
    response: FactoryActionResponseV1
    phase: Literal[
        "answered",
        "projection_intent",
        "projection_acknowledged",
        "denial_acknowledged",
        "continuation_intent",
        "continuation_acknowledged",
    ]
    projection_idempotency_key: str
    continuation_idempotency_key: str | None


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


@runtime_checkable
class FactoryActionProjection(Protocol):
    def lookup_receipt(self, effect_key: str) -> FactoryActionEffectReceipt | None: ...

    def project_factory_action(
        self, *, effect_key: str, payload: dict[str, object], payload_sha256: str
    ) -> FactoryActionEffectReceipt: ...


@runtime_checkable
class FactoryContinuationScheduler(Protocol):
    def lookup_receipt(self, effect_key: str) -> FactoryActionEffectReceipt | None: ...

    def schedule_factory_continuation(
        self, *, effect_key: str, payload: dict[str, object], payload_sha256: str
    ) -> FactoryActionEffectReceipt: ...


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
            projection_payload = {
                    "schema": "minibook.factory-action-projection.v1",
                    "request_id": str(request.request_id),
                    "factory_job_id": str(request.factory_job_id),
                    "creation_job_id": str(request.creation_job_id),
                    "correlation_id": str(request.correlation_id),
                    "request_type": request.request_type,
                    "status": "answered" if state.response.decision == "approve" else "denied",
                    "decision": state.response.decision,
                    "safe_checkpoint_sha256": request.safe_checkpoint_sha256,
                }
            self._reconcile_effect(
                sink=self._projection,
                method=self._projection.project_factory_action,
                effect_key=state.projection_idempotency_key,
                payload=projection_payload,
            )
            state = self._store.acknowledge_projection(
                request_id,
                tenant_id=tenant_id,
                idempotency_key=state.projection_idempotency_key,
                now=self._clock(),
            )
        if state.response.decision == "deny":
            if state.phase != "denial_acknowledged":
                raise FactoryActionEffectConflict("denied action did not reach a terminal state")
            return state.response
        if state.phase in {"projection_acknowledged", "continuation_intent"}:
            state = self._store.prepare_continuation(
                request_id, tenant_id=tenant_id, now=self._clock()
            )
        if state.phase == "continuation_intent":
            response = state.response
            continuation_payload = {
                "factory_job_id": request.factory_job_id,
                "creation_job_id": request.creation_job_id,
                "correlation_id": request.correlation_id,
                "expected_subject_version": request.expected_subject_version,
                "safe_checkpoint_ref": request.safe_checkpoint_ref,
                "decision": response.decision,
                "approved_scope": response.approved_scope,
                "credential_ref": response.credential_ref,
                "answer_metadata": response.answer_metadata,
            }
            self._reconcile_effect(
                sink=self._scheduler,
                method=self._scheduler.schedule_factory_continuation,
                effect_key=_require_continuation_key(state),
                payload=continuation_payload,
            )
            state = self._store.acknowledge_continuation(
                request_id,
                tenant_id=tenant_id,
                idempotency_key=_require_continuation_key(state),
                now=self._clock(),
            )
        return state.response

    @staticmethod
    def _reconcile_effect(
        *, sink: object, method: Callable[..., FactoryActionEffectReceipt],
        effect_key: str, payload: dict[str, object]
    ) -> None:
        canonical = json.dumps(
            TypeAdapter(dict[str, object]).dump_python(payload, mode="json"),
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        payload_sha256 = hashlib.sha256(canonical).hexdigest()
        lookup = getattr(sink, "lookup_receipt")
        receipt = lookup(effect_key)
        if receipt is None:
            receipt = method(
                effect_key=effect_key,
                payload=payload,
                payload_sha256=payload_sha256,
            )
        if receipt.effect_key != effect_key or receipt.payload_sha256 != payload_sha256:
            raise FactoryActionEffectConflict(
                "factory action effect receipt conflicts with canonical payload"
            )


def _require_continuation_key(state: FactoryActionResumeRecord) -> str:
    if state.continuation_idempotency_key is None:
        raise FactoryActionEffectConflict("approved action continuation key is missing")
    return state.continuation_idempotency_key


class FactoryActionResumeService:
    """Submit durable resume work to a dedicated worker without blocking HTTP."""

    def __init__(
        self,
        *,
        coordinator: FactoryActionResumeCoordinator,
        recovery_source: Callable[[], Iterable[tuple[UUID, str]]] | None = None,
        max_in_flight: int = 8,
    ) -> None:
        if max_in_flight < 1 or max_in_flight > 128:
            raise ValueError("factory action max_in_flight is invalid")
        self._coordinator = coordinator
        self._recovery_source = recovery_source or (lambda: ())
        self._max_in_flight = max_in_flight
        self._executor = ThreadPoolExecutor(
            max_workers=max_in_flight, thread_name_prefix="factory-action-resume"
        )
        self._lock = Lock()
        self._futures: set[Future[object]] = set()

    def enqueue(self, *, request_id: UUID, tenant_id: str) -> None:
        with self._lock:
            if len(self._futures) >= self._max_in_flight:
                raise RuntimeError("factory action resume service is at capacity")
            future = self._executor.submit(
                self._coordinator.resume, request_id=request_id, tenant_id=tenant_id
            )
            self._futures.add(future)
        future.add_done_callback(self._completed)

    def recover(self) -> None:
        batch: list[Future[object]] = []
        for request_id, tenant_id in self._recovery_source():
            self.enqueue(request_id=request_id, tenant_id=tenant_id)
            with self._lock:
                batch = list(self._futures)
            if len(batch) >= self._max_in_flight:
                wait(batch)
                batch.clear()
        if batch:
            wait(batch)

    def _completed(self, future: Future[object]) -> None:
        with self._lock:
            self._futures.discard(future)
        # Retrieve the exception so the executor does not silently retain it.
        # Durable intent remains retryable and is recovered by a later enqueue.
        future.exception()

    def close(self) -> None:
        self._executor.shutdown(wait=True)
