from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock
from time import sleep
from time import monotonic
from uuid import UUID

import pytest

from agenten.agent_factory.action_resume import (
    FactoryActionEffectReceipt,
    FactoryActionEffectConflict,
    FactoryActionResumeCoordinator,
    FactoryActionResumeService,
)
from agenten.agent_factory.forge_contracts import FactoryActionRequestV1, FactoryActionResponseV1
from gateway.factory_action_store import FactoryActionStore
from tests.gateway.test_factory_action_store import (
    TransactionalMemoryActionRepository,
    advance_to_phase,
)


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parents[1] / "fixtures" / "contracts"


class RecordingProjection:
    def __init__(self) -> None:
        self.values: list[dict[str, object]] = []

        self.receipts: dict[str, FactoryActionEffectReceipt] = {}

    def lookup_receipt(self, effect_key: str) -> FactoryActionEffectReceipt | None:
        return self.receipts.get(effect_key)

    def project_factory_action(self, *, effect_key: str, payload: dict[str, object], payload_sha256: str) -> FactoryActionEffectReceipt:
        projection = payload
        self.values.append(projection)
        receipt = FactoryActionEffectReceipt(effect_key=effect_key, payload_sha256=payload_sha256)
        self.receipts[effect_key] = receipt
        return receipt


class RecordingScheduler:
    def __init__(self) -> None:
        self.values: list[dict[str, object]] = []

        self.receipts: dict[str, FactoryActionEffectReceipt] = {}

    def lookup_receipt(self, effect_key: str) -> FactoryActionEffectReceipt | None:
        return self.receipts.get(effect_key)

    def schedule_factory_continuation(self, *, effect_key: str, payload: dict[str, object], payload_sha256: str) -> FactoryActionEffectReceipt:
        continuation = payload
        self.values.append(continuation)
        receipt = FactoryActionEffectReceipt(effect_key=effect_key, payload_sha256=payload_sha256)
        self.receipts[effect_key] = receipt
        return receipt


class IdempotentCrashableProjection:
    def __init__(self, *, fail_before: bool = False, crash_after: bool = False) -> None:
        self.fail_before = fail_before
        self.crash_after = crash_after
        self.effects: dict[str, dict[str, object]] = {}
        self.receipts: dict[str, FactoryActionEffectReceipt] = {}

    def lookup_receipt(self, effect_key: str) -> FactoryActionEffectReceipt | None:
        return self.receipts.get(effect_key)

    def project_factory_action(self, *, effect_key: str, payload: dict[str, object], payload_sha256: str) -> FactoryActionEffectReceipt:
        if self.fail_before:
            self.fail_before = False
            raise RuntimeError("projection unavailable")
        self.effects.setdefault(effect_key, payload)
        receipt = FactoryActionEffectReceipt(effect_key=effect_key, payload_sha256=payload_sha256)
        self.receipts[effect_key] = receipt
        if self.crash_after:
            self.crash_after = False
            raise RuntimeError("crash after projection")
        return receipt


class IdempotentCrashableScheduler:
    def __init__(self, *, fail_before: bool = False, crash_after: bool = False) -> None:
        self.fail_before = fail_before
        self.crash_after = crash_after
        self.effects: dict[str, dict[str, object]] = {}
        self.receipts: dict[str, FactoryActionEffectReceipt] = {}

    def lookup_receipt(self, effect_key: str) -> FactoryActionEffectReceipt | None:
        return self.receipts.get(effect_key)

    def schedule_factory_continuation(self, *, effect_key: str, payload: dict[str, object], payload_sha256: str) -> FactoryActionEffectReceipt:
        if self.fail_before:
            self.fail_before = False
            raise RuntimeError("scheduler unavailable")
        self.effects.setdefault(effect_key, payload)
        receipt = FactoryActionEffectReceipt(effect_key=effect_key, payload_sha256=payload_sha256)
        self.receipts[effect_key] = receipt
        if self.crash_after:
            self.crash_after = False
            raise RuntimeError("crash after schedule")
        return receipt


class ConflictingReceiptProjection(RecordingProjection):
    def lookup_receipt(self, effect_key: str) -> FactoryActionEffectReceipt | None:
        return FactoryActionEffectReceipt(effect_key=effect_key, payload_sha256="f" * 64)


def test_resume_consumes_exact_checkpoint_projects_redacted_status_and_schedules_once() -> None:
    request = FactoryActionRequestV1.model_validate_json(
        (FIXTURES / "factory_action_request.v1.json").read_text(encoding="utf-8")
    )
    response = FactoryActionResponseV1.model_validate_json(
        (FIXTURES / "factory_action_response.v1.json").read_text(encoding="utf-8")
    )
    repository = TransactionalMemoryActionRepository()
    store = FactoryActionStore(repository=repository)
    store.create(request, tenant_id="tenant-a")
    store.answer(response, tenant_id="tenant-a", now=NOW)
    projection = RecordingProjection()
    scheduler = RecordingScheduler()
    coordinator = FactoryActionResumeCoordinator(
        store=store,
        projection=projection,
        scheduler=scheduler,
        clock=lambda: NOW,
    )

    assert coordinator.resume(request_id=request.request_id, tenant_id="tenant-a") == response
    assert len(scheduler.values) == 1
    assert scheduler.values[0]["factory_job_id"] == request.factory_job_id
    assert scheduler.values[0]["safe_checkpoint_ref"] == request.safe_checkpoint_ref
    assert scheduler.values[0]["approved_scope"] == response.approved_scope
    serialized = repr(projection.values).lower()
    assert projection.values[0]["status"] == "answered"
    assert "credential://" not in serialized
    assert "credential_alias" not in serialized


def test_resume_does_not_schedule_when_action_is_not_answered() -> None:
    request = FactoryActionRequestV1.model_validate_json(
        (FIXTURES / "factory_action_request.v1.json").read_text(encoding="utf-8")
    )
    response = FactoryActionResponseV1.model_validate_json(
        (FIXTURES / "factory_action_response.v1.json").read_text(encoding="utf-8")
    )
    repository = TransactionalMemoryActionRepository()
    store = FactoryActionStore(repository=repository)
    store.create(request, tenant_id="tenant-a")
    scheduler = RecordingScheduler()
    coordinator = FactoryActionResumeCoordinator(
        store=store,
        projection=RecordingProjection(),
        scheduler=scheduler,
        clock=lambda: NOW,
    )
    with pytest.raises(ValueError, match="not answered"):
        coordinator.resume(request_id=request.request_id, tenant_id="tenant-a")
    assert scheduler.values == []


def test_resume_at_deadline_writes_no_projection_or_continuation_intent() -> None:
    request = FactoryActionRequestV1.model_validate_json(
        (FIXTURES / "factory_action_request.v1.json").read_text(encoding="utf-8")
    )
    before_deadline = request.deadline_at - timedelta(microseconds=1)
    response = FactoryActionResponseV1.model_validate_json(
        (FIXTURES / "factory_action_response.v1.json").read_text(encoding="utf-8")
    ).model_copy(update={"answered_at": before_deadline})
    repository = TransactionalMemoryActionRepository()
    store = FactoryActionStore(repository=repository)
    store.create(request, tenant_id="tenant-a")
    store.answer(response, tenant_id="tenant-a", now=before_deadline)
    projection = RecordingProjection()
    scheduler = RecordingScheduler()

    with pytest.raises(TimeoutError, match="expired"):
        FactoryActionResumeCoordinator(
            store=FactoryActionStore(repository=repository),
            projection=projection,
            scheduler=scheduler,
            clock=lambda: request.deadline_at,
        ).resume(request_id=request.request_id, tenant_id="tenant-a")

    durable = repository.rows[str(request.request_id)]
    assert durable.resume_phase == "answered"
    assert durable.projection_idempotency_key is None
    assert durable.continuation_idempotency_key is None
    assert projection.values == []
    assert scheduler.values == []


def test_answer_just_before_deadline_completes_after_restart() -> None:
    request = FactoryActionRequestV1.model_validate_json(
        (FIXTURES / "factory_action_request.v1.json").read_text(encoding="utf-8")
    )
    before_deadline = request.deadline_at - timedelta(microseconds=1)
    response = FactoryActionResponseV1.model_validate_json(
        (FIXTURES / "factory_action_response.v1.json").read_text(encoding="utf-8")
    ).model_copy(update={"answered_at": before_deadline})
    repository = TransactionalMemoryActionRepository()
    store = FactoryActionStore(repository=repository)
    store.create(request, tenant_id="tenant-a")
    store.answer(response, tenant_id="tenant-a", now=before_deadline)
    projection = RecordingProjection()
    scheduler = RecordingScheduler()

    restarted = FactoryActionResumeCoordinator(
        store=FactoryActionStore(repository=repository),
        projection=projection,
        scheduler=scheduler,
        clock=lambda: before_deadline,
    )

    assert restarted.resume(request_id=request.request_id, tenant_id="tenant-a") == response
    durable = repository.rows[str(request.request_id)]
    assert durable.status == "consumed"
    assert durable.resume_phase == "continuation_acknowledged"
    assert len(projection.values) == 1
    assert len(scheduler.values) == 1


@pytest.mark.parametrize(
    "phase",
    [
        "answered",
        "projection_intent",
        "projection_acknowledged",
        "continuation_intent",
        "continuation_acknowledged",
    ],
)
def test_restart_at_deadline_expires_partial_resume_without_duplicate_effects(
    phase: str,
) -> None:
    request = FactoryActionRequestV1.model_validate_json(
        (FIXTURES / "factory_action_request.v1.json").read_text(encoding="utf-8")
    )
    before_deadline = request.deadline_at - timedelta(microseconds=1)
    repository = TransactionalMemoryActionRepository()
    store = FactoryActionStore(repository=repository)
    store.create(request, tenant_id="tenant-a")
    advance_to_phase(
        store,
        request,
        "continuation_intent" if phase == "continuation_acknowledged" else phase,
        now=before_deadline,
    )
    if phase == "continuation_acknowledged":
        durable = repository.rows[str(request.request_id)]
        durable.resume_phase = "continuation_acknowledged"
        durable.continuation_acknowledged_at = before_deadline
    projection = RecordingProjection()
    scheduler = RecordingScheduler()

    with pytest.raises(TimeoutError, match="expired"):
        FactoryActionResumeCoordinator(
            store=FactoryActionStore(repository=repository),
            projection=projection,
            scheduler=scheduler,
            clock=lambda: request.deadline_at,
        ).resume(request_id=request.request_id, tenant_id="tenant-a")

    assert repository.rows[str(request.request_id)].status == "expired"
    assert projection.values == []
    assert scheduler.values == []


@pytest.mark.parametrize("failure_mode", ["before", "after"])
def test_resume_retries_projection_after_restart_without_duplicate_effect(
    failure_mode: str,
) -> None:
    request = FactoryActionRequestV1.model_validate_json(
        (FIXTURES / "factory_action_request.v1.json").read_text(encoding="utf-8")
    )
    response = FactoryActionResponseV1.model_validate_json(
        (FIXTURES / "factory_action_response.v1.json").read_text(encoding="utf-8")
    )
    repository = TransactionalMemoryActionRepository()
    store = FactoryActionStore(repository=repository)
    store.create(request, tenant_id="tenant-a")
    store.answer(response, tenant_id="tenant-a", now=NOW)
    projection = IdempotentCrashableProjection(
        fail_before=failure_mode == "before", crash_after=failure_mode == "after"
    )
    scheduler = IdempotentCrashableScheduler()

    with pytest.raises(RuntimeError):
        FactoryActionResumeCoordinator(
            store=store, projection=projection, scheduler=scheduler, clock=lambda: NOW
        ).resume(request_id=request.request_id, tenant_id="tenant-a")

    durable = repository.rows[str(request.request_id)]
    assert durable.status == "answered"
    assert durable.resume_phase == "projection_intent"
    restarted = FactoryActionResumeCoordinator(
        store=FactoryActionStore(repository=repository),
        projection=projection,
        scheduler=scheduler,
        clock=lambda: NOW,
    )
    assert restarted.resume(request_id=request.request_id, tenant_id="tenant-a") == response
    assert durable.status == "consumed"
    assert durable.resume_phase == "continuation_acknowledged"
    assert len(projection.effects) == 1
    assert len(scheduler.effects) == 1


@pytest.mark.parametrize("failure_mode", ["before", "after"])
def test_resume_retries_schedule_after_restart_without_duplicate_effect(
    failure_mode: str,
) -> None:
    request = FactoryActionRequestV1.model_validate_json(
        (FIXTURES / "factory_action_request.v1.json").read_text(encoding="utf-8")
    )
    response = FactoryActionResponseV1.model_validate_json(
        (FIXTURES / "factory_action_response.v1.json").read_text(encoding="utf-8")
    )
    repository = TransactionalMemoryActionRepository()
    store = FactoryActionStore(repository=repository)
    store.create(request, tenant_id="tenant-a")
    store.answer(response, tenant_id="tenant-a", now=NOW)
    projection = IdempotentCrashableProjection()
    scheduler = IdempotentCrashableScheduler(
        fail_before=failure_mode == "before", crash_after=failure_mode == "after"
    )

    with pytest.raises(RuntimeError):
        FactoryActionResumeCoordinator(
            store=store, projection=projection, scheduler=scheduler, clock=lambda: NOW
        ).resume(request_id=request.request_id, tenant_id="tenant-a")

    durable = repository.rows[str(request.request_id)]
    assert durable.status == "answered"
    assert durable.resume_phase == "continuation_intent"
    restarted = FactoryActionResumeCoordinator(
        store=FactoryActionStore(repository=repository),
        projection=projection,
        scheduler=scheduler,
        clock=lambda: NOW,
    )
    assert restarted.resume(request_id=request.request_id, tenant_id="tenant-a") == response
    assert durable.status == "consumed"
    assert durable.resume_phase == "continuation_acknowledged"
    assert len(projection.effects) == 1
    assert len(scheduler.effects) == 1


def test_resume_fails_closed_on_conflicting_projection_receipt() -> None:
    request = FactoryActionRequestV1.model_validate_json(
        (FIXTURES / "factory_action_request.v1.json").read_text(encoding="utf-8")
    )
    response = FactoryActionResponseV1.model_validate_json(
        (FIXTURES / "factory_action_response.v1.json").read_text(encoding="utf-8")
    )
    repository = TransactionalMemoryActionRepository()
    store = FactoryActionStore(repository=repository)
    store.create(request, tenant_id="tenant-a")
    store.answer(response, tenant_id="tenant-a", now=NOW)

    with pytest.raises(FactoryActionEffectConflict, match="receipt"):
        FactoryActionResumeCoordinator(
            store=store,
            projection=ConflictingReceiptProjection(),
            scheduler=RecordingScheduler(),
            clock=lambda: NOW,
        ).resume(request_id=request.request_id, tenant_id="tenant-a")

    durable = repository.rows[str(request.request_id)]
    assert durable.status == "answered"
    assert durable.resume_phase == "projection_intent"


def test_resume_service_enqueue_returns_without_waiting_for_coordinator() -> None:
    entered = Event()
    release = Event()

    class BlockingCoordinator:
        def resume(self, *, request_id, tenant_id):
            entered.set()
            release.wait(timeout=5)

    service = FactoryActionResumeService(coordinator=BlockingCoordinator())
    started = monotonic()
    service.enqueue(
        request_id=FactoryActionRequestV1.model_validate_json(
            (FIXTURES / "factory_action_request.v1.json").read_text(encoding="utf-8")
        ).request_id,
        tenant_id="tenant-a",
    )
    elapsed = monotonic() - started
    assert elapsed < 0.25
    assert entered.wait(timeout=1)
    release.set()
    service.close()


def test_resume_service_recovers_all_durable_answered_rows_on_restart() -> None:
    request = FactoryActionRequestV1.model_validate_json(
        (FIXTURES / "factory_action_request.v1.json").read_text(encoding="utf-8")
    )
    entered = Event()

    class RecordingCoordinator:
        def resume(self, *, request_id, tenant_id):
            assert request_id == request.request_id
            assert tenant_id == "tenant-a"
            entered.set()

    service = FactoryActionResumeService(
        coordinator=RecordingCoordinator(),
        recovery_source=lambda: ((request.request_id, "tenant-a"),),
    )
    service.recover()
    assert entered.wait(timeout=1)
    service.close()


def test_resume_recovery_bounds_concurrency_and_retained_futures_without_deadlock() -> None:
    request = FactoryActionRequestV1.model_validate_json(
        (FIXTURES / "factory_action_request.v1.json").read_text(encoding="utf-8")
    )
    guard = Lock()
    current = 0
    maximum = 0
    completed: list[UUID] = []

    class CountingCoordinator:
        def resume(self, *, request_id, tenant_id):
            nonlocal current, maximum
            assert tenant_id == "tenant-a"
            with guard:
                current += 1
                maximum = max(maximum, current)
            try:
                sleep(0.01)
                if request_id.int % 7 == 0:
                    raise RuntimeError("retryable fixture failure")
                completed.append(request_id)
            finally:
                with guard:
                    current -= 1

    identities = tuple(
        UUID(int=request.request_id.int + offset) for offset in range(1, 31)
    )
    service = FactoryActionResumeService(
        coordinator=CountingCoordinator(),
        recovery_source=lambda: ((identity, "tenant-a") for identity in identities),
        max_in_flight=3,
    )

    service.recover()

    assert maximum <= 3
    assert len(service._futures) <= 3
    assert set(completed) == {identity for identity in identities if identity.int % 7}
    service.close()
