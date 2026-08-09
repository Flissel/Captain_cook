from __future__ import annotations

import os
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from agenten.agent_factory.forge_contracts import (
    FactoryActionRequestV1,
    FactoryActionResponseV1,
)
from blockchain.mariadb_storage import MariaDBStorage
from gateway.factory_action_store import FactoryActionRow, FactoryActionStore
from tests.support.mariadb import assert_isolated_test_database


TEST_DSN = os.getenv("TEST_MARIADB_DSN")
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parents[1] / "fixtures" / "contracts"


def pending_request() -> FactoryActionRequestV1:
    return FactoryActionRequestV1.model_validate_json(
        (FIXTURES / "factory_action_request.v1.json").read_text(encoding="utf-8")
    )


def action_response(
    request: FactoryActionRequestV1,
    *,
    key: str = "c" * 64,
    scope: str | None = None,
    credential_ref: str | None = "credential://tenant-a/n8n-crm-read",
) -> FactoryActionResponseV1:
    return FactoryActionResponseV1(
        request_id=request.request_id,
        expected_subject_version=request.expected_subject_version,
        decision="approve",
        approved_scope=scope or request.required_scope,
        credential_ref=credential_ref,
        answer_metadata={"credential_alias": "n8n-crm-read"},
        answered_at=NOW,
        idempotency_key=key,
    )


@pytest.fixture
def storage() -> Iterator[MariaDBStorage]:
    if TEST_DSN is None:
        pytest.skip("TEST_MARIADB_DSN is not configured")
    assert_isolated_test_database(TEST_DSN)
    storage = MariaDBStorage(TEST_DSN)
    FactoryActionStore(storage)
    with storage.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM factory_action_inbox")
    yield storage
    with storage.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM factory_action_inbox")


class TransactionalMemoryActionRepository:
    """Deterministic restart-capable transactional repository test double."""

    def __init__(self) -> None:
        self.rows: dict[str, FactoryActionRow] = {}

    def create(self, row: FactoryActionRow) -> FactoryActionRow | None:
        return self.rows.setdefault(str(row.request.request_id), row)

    def list_rows(self, *, tenant_id: str, status: str) -> tuple[FactoryActionRow, ...]:
        return tuple(
            row for row in self.rows.values()
            if row.tenant_id == tenant_id and row.status == status
        )

    @contextmanager
    def locked(self, request_id, *, tenant_id: str):
        row = self.rows.get(str(request_id))
        yield row if row is not None and row.tenant_id == tenant_id else None

    def expire(self, *, tenant_id: str, now: datetime, request_id=None) -> None:
        for row in self.rows.values():
            if (
                row.tenant_id == tenant_id
                and row.status == "pending"
                and row.request.deadline_at <= now
                and (request_id is None or row.request.request_id == request_id)
            ):
                row.status = "expired"


@pytest.fixture
def memory_repository() -> TransactionalMemoryActionRepository:
    return TransactionalMemoryActionRepository()


def test_deterministic_repository_covers_replay_conflict_and_restart(
    memory_repository: TransactionalMemoryActionRepository,
) -> None:
    request = pending_request()
    store = FactoryActionStore(repository=memory_repository)
    assert store.create(request, tenant_id="tenant-a") == request
    assert store.create(request, tenant_id="tenant-a") == request
    with pytest.raises(ValueError, match="conflicting factory action request"):
        store.create(request.model_copy(update={"message": "Different safe message."}), tenant_id="tenant-a")

    restarted = FactoryActionStore(repository=memory_repository)
    assert restarted.list(tenant_id="tenant-a", status="pending", now=NOW) == (request,)
    assert restarted.list(tenant_id="tenant-b", status="pending", now=NOW) == ()


def test_deterministic_repository_covers_answer_fences_and_consume_once(
    memory_repository: TransactionalMemoryActionRepository,
) -> None:
    request = pending_request()
    store = FactoryActionStore(repository=memory_repository)
    store.create(request, tenant_id="tenant-a")
    response = action_response(request)
    assert store.answer(response, tenant_id="tenant-a", now=NOW) == response
    assert store.answer(response, tenant_id="tenant-a", now=NOW) == response
    with pytest.raises(ValueError, match="conflicting action response"):
        store.answer(response.model_copy(update={"idempotency_key": "d" * 64}), tenant_id="tenant-a", now=NOW)
    with pytest.raises(LookupError, match="factory action not found"):
        store.answer(response, tenant_id="tenant-b", now=NOW)

    with pytest.raises(ValueError, match="resume fence mismatch"):
        store.consume(
            request.request_id,
            tenant_id="tenant-a",
            factory_job_id=UUID("00000000-0000-0000-0000-000000000999"),
            creation_job_id=request.creation_job_id,
            correlation_id=request.correlation_id,
            expected_subject_version=request.expected_subject_version,
            safe_checkpoint_sha256=request.safe_checkpoint_sha256,
            now=NOW,
        )
    assert store.consume(
        request.request_id,
        tenant_id="tenant-a",
        factory_job_id=request.factory_job_id,
        creation_job_id=request.creation_job_id,
        correlation_id=request.correlation_id,
        expected_subject_version=request.expected_subject_version,
        safe_checkpoint_sha256=request.safe_checkpoint_sha256,
        now=NOW,
    ) == response
    with pytest.raises(ValueError, match="already consumed"):
        store.consume(
            request.request_id,
            tenant_id="tenant-a",
            factory_job_id=request.factory_job_id,
            creation_job_id=request.creation_job_id,
            correlation_id=request.correlation_id,
            expected_subject_version=request.expected_subject_version,
            safe_checkpoint_sha256=request.safe_checkpoint_sha256,
            now=NOW,
        )


def test_deterministic_repository_covers_stale_scope_and_expiry(
    memory_repository: TransactionalMemoryActionRepository,
) -> None:
    request = pending_request()
    store = FactoryActionStore(repository=memory_repository)
    store.create(request, tenant_id="tenant-a")
    with pytest.raises(ValueError, match="stale subject version"):
        store.answer(
            action_response(request).model_copy(update={"expected_subject_version": 2}),
            tenant_id="tenant-a",
            now=NOW,
        )
    with pytest.raises(ValueError, match="scope"):
        store.answer(action_response(request, scope="n8n.admin"), tenant_id="tenant-a", now=NOW)

    expired = request.model_copy(
        update={
            "request_id": UUID("00000000-0000-0000-0000-000000000604"),
            "deadline_at": NOW - timedelta(seconds=1),
        }
    )
    store.create(expired, tenant_id="tenant-a")
    with pytest.raises(TimeoutError, match="expired"):
        store.answer(action_response(expired), tenant_id="tenant-a", now=NOW)
    assert store.get(expired.request_id, tenant_id="tenant-a", now=NOW).status == "expired"
def test_create_list_read_and_restart_are_tenant_scoped(storage: MariaDBStorage) -> None:
    request = pending_request()
    first = FactoryActionStore(storage)
    assert first.create(request, tenant_id="tenant-a") == request
    assert first.create(request, tenant_id="tenant-a") == request

    restarted = FactoryActionStore(storage)
    assert restarted.list(tenant_id="tenant-a", status="pending", now=NOW) == (request,)
    assert restarted.get(request.request_id, tenant_id="tenant-a", now=NOW) == request
    assert restarted.list(tenant_id="tenant-b", status="pending", now=NOW) == ()
    with pytest.raises(LookupError, match="factory action not found"):
        restarted.get(request.request_id, tenant_id="tenant-b", now=NOW)


def test_answer_is_version_scope_expiry_and_replay_fenced(storage: MariaDBStorage) -> None:
    request = pending_request()
    store = FactoryActionStore(storage)
    store.create(request, tenant_id="tenant-a")
    response = action_response(request)

    answered = store.answer(response, tenant_id="tenant-a", now=NOW)
    assert answered == response
    assert store.answer(response, tenant_id="tenant-a", now=NOW) == response
    with pytest.raises(ValueError, match="conflicting action response"):
        store.answer(response.model_copy(update={"idempotency_key": "d" * 64}), tenant_id="tenant-a", now=NOW)
    with pytest.raises(LookupError, match="factory action not found"):
        store.answer(response, tenant_id="tenant-b", now=NOW)

    stale = request.model_copy(update={"request_id": UUID("00000000-0000-0000-0000-000000000602")})
    store.create(stale, tenant_id="tenant-a")
    with pytest.raises(ValueError, match="stale subject version"):
        store.answer(
            action_response(stale).model_copy(update={"expected_subject_version": 2}),
            tenant_id="tenant-a",
            now=NOW,
        )

    scoped = request.model_copy(update={"request_id": UUID("00000000-0000-0000-0000-000000000603")})
    store.create(scoped, tenant_id="tenant-a")
    with pytest.raises(ValueError, match="scope"):
        store.answer(action_response(scoped, scope="n8n.admin"), tenant_id="tenant-a", now=NOW)

    expired = request.model_copy(
        update={
            "request_id": UUID("00000000-0000-0000-0000-000000000604"),
            "deadline_at": NOW - timedelta(seconds=1),
        }
    )
    store.create(expired, tenant_id="tenant-a")
    with pytest.raises(TimeoutError, match="expired"):
        store.answer(action_response(expired), tenant_id="tenant-a", now=NOW)
    assert store.get(expired.request_id, tenant_id="tenant-a", now=NOW).status == "expired"


def test_consume_once_requires_exact_job_and_checkpoint(storage: MariaDBStorage) -> None:
    request = pending_request()
    store = FactoryActionStore(storage)
    store.create(request, tenant_id="tenant-a")
    response = store.answer(action_response(request), tenant_id="tenant-a", now=NOW)

    consumed = store.consume(
        request.request_id,
        tenant_id="tenant-a",
        factory_job_id=request.factory_job_id,
        creation_job_id=request.creation_job_id,
        correlation_id=request.correlation_id,
        expected_subject_version=request.expected_subject_version,
        safe_checkpoint_sha256=request.safe_checkpoint_sha256,
        now=NOW,
    )
    assert consumed == response
    with pytest.raises(ValueError, match="already consumed"):
        store.consume(
            request.request_id,
            tenant_id="tenant-a",
            factory_job_id=request.factory_job_id,
            creation_job_id=request.creation_job_id,
            correlation_id=request.correlation_id,
            expected_subject_version=request.expected_subject_version,
            safe_checkpoint_sha256=request.safe_checkpoint_sha256,
            now=NOW,
        )


def test_credential_reference_is_tenant_bound_and_secret_never_persists(storage: MariaDBStorage) -> None:
    request = pending_request()
    store = FactoryActionStore(storage)
    store.create(request, tenant_id="tenant-a")
    with pytest.raises(ValueError, match="credential reference"):
        store.answer(
            action_response(request, credential_ref="credential://tenant-b/n8n-crm-read"),
            tenant_id="tenant-a",
            now=NOW,
        )
    with storage.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT request_payload, response_payload FROM factory_action_inbox")
            serialized = repr(cursor.fetchall()).lower()
    assert "secret-value" not in serialized
