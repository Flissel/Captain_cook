from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from time import monotonic

import pytest

from agenten.agent_factory.action_resume import (
    FactoryActionResumeCoordinator,
    FactoryActionResumeService,
)
from agenten.agent_factory.forge_contracts import FactoryActionRequestV1, FactoryActionResponseV1
from gateway.factory_action_store import FactoryActionStore
from tests.gateway.test_factory_action_store import TransactionalMemoryActionRepository


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parents[1] / "fixtures" / "contracts"


class RecordingProjection:
    def __init__(self) -> None:
        self.values: list[dict[str, object]] = []

    def project_factory_action(self, projection: dict[str, object]) -> None:
        self.values.append(projection)


class RecordingScheduler:
    def __init__(self) -> None:
        self.values: list[dict[str, object]] = []

    def schedule_factory_continuation(self, **continuation: object) -> None:
        self.values.append(continuation)


class IdempotentCrashableProjection:
    def __init__(self, *, fail_before: bool = False, crash_after: bool = False) -> None:
        self.fail_before = fail_before
        self.crash_after = crash_after
        self.effects: dict[str, dict[str, object]] = {}

    def project_factory_action(self, projection: dict[str, object]) -> None:
        if self.fail_before:
            self.fail_before = False
            raise RuntimeError("projection unavailable")
        key = str(projection["idempotency_key"])
        self.effects.setdefault(key, projection)
        if self.crash_after:
            self.crash_after = False
            raise RuntimeError("crash after projection")


class IdempotentCrashableScheduler:
    def __init__(self, *, fail_before: bool = False, crash_after: bool = False) -> None:
        self.fail_before = fail_before
        self.crash_after = crash_after
        self.effects: dict[str, dict[str, object]] = {}

    def schedule_factory_continuation(self, **continuation: object) -> None:
        if self.fail_before:
            self.fail_before = False
            raise RuntimeError("scheduler unavailable")
        key = str(continuation["idempotency_key"])
        self.effects.setdefault(key, continuation)
        if self.crash_after:
            self.crash_after = False
            raise RuntimeError("crash after schedule")


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
