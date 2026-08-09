from __future__ import annotations

import os
import hashlib
import json
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
from gateway.factory_action_store import (
    FactoryActionRow,
    FactoryActionStore,
    _canonical,
    _canonical_request_payload,
    _db_row,
    _normalize_legacy_db_row,
    MariaDBFactoryActionRepository,
)
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
    answered_at: datetime = NOW,
) -> FactoryActionResponseV1:
    return FactoryActionResponseV1(
        request_id=request.request_id,
        expected_subject_version=request.expected_subject_version,
        decision="approve",
        approved_scope=scope or request.required_scope,
        credential_ref=credential_ref,
        answer_metadata={"credential_alias": "n8n-crm-read"},
        answered_at=answered_at,
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
        self.locked_writes = 0

    def create(self, row: FactoryActionRow) -> FactoryActionRow | None:
        row.created_at = NOW - timedelta(hours=1)
        return self.rows.setdefault(str(row.request.request_id), row)

    def list_rows(self, *, tenant_id: str, status: str) -> tuple[FactoryActionRow, ...]:
        return tuple(
            row for row in self.rows.values()
            if row.tenant_id == tenant_id and row.status == status
        )

    def resume_rows(self) -> tuple[FactoryActionRow, ...]:
        return tuple(row for row in self.rows.values() if row.status == "answered")

    @contextmanager
    def locked(self, request_id, *, tenant_id: str):
        row = self.rows.get(str(request_id))
        yield row if row is not None and row.tenant_id == tenant_id else None
        self.locked_writes += 1

    def expire(self, *, tenant_id: str, now: datetime, request_id=None) -> None:
        for row in self.rows.values():
            if (
                row.tenant_id == tenant_id
                and row.status == "pending"
                and row.request.deadline_at <= now
                and (request_id is None or row.request.request_id == request_id)
            ):
                row.status = "expired"


def canonical_db_row() -> dict[str, object]:
    request = pending_request()
    response = action_response(request)
    request_payload = _canonical_request_payload(request, "tenant-a").decode()
    response_payload = _canonical(response).decode()
    import hashlib

    return {
        "request_id": str(request.request_id),
        "tenant_id": "tenant-a",
        "factory_job_id": str(request.factory_job_id),
        "creation_job_id": str(request.creation_job_id),
        "correlation_id": str(request.correlation_id),
        "expected_subject_version": request.expected_subject_version,
        "safe_checkpoint_sha256": request.safe_checkpoint_sha256,
        "deadline_at": request.deadline_at.replace(tzinfo=None),
        "status": "answered",
        "request_payload": request_payload,
        "request_sha256": hashlib.sha256(_canonical(request)).hexdigest(),
        "response_payload": response_payload,
        "response_sha256": hashlib.sha256(response_payload.encode()).hexdigest(),
        "resume_phase": "answered",
        "projection_idempotency_key": None,
        "projection_intent_at": None,
        "projection_acknowledged_at": None,
        "continuation_idempotency_key": None,
        "continuation_intent_at": None,
        "continuation_acknowledged_at": None,
        "consumed_at": None,
    }


def expected_effect_key(request: FactoryActionRequestV1, phase: str, response_sha256: str) -> str:
    material = {
        "phase": phase,
        "request_sha256": hashlib.sha256(_canonical(request)).hexdigest(),
        "response_sha256": response_sha256,
        "safe_checkpoint_sha256": request.safe_checkpoint_sha256,
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def db_row_for_phase(phase: str) -> dict[str, object]:
    row = canonical_db_row()
    response_sha256 = str(row["response_sha256"])
    projection_key = expected_effect_key(pending_request(), "projection", response_sha256)
    continuation_key = expected_effect_key(pending_request(), "continuation", response_sha256)
    row["resume_phase"] = phase
    if phase != "answered":
        row["projection_idempotency_key"] = projection_key
        row["projection_intent_at"] = (NOW + timedelta(seconds=1)).replace(tzinfo=None)
    if phase in {"projection_acknowledged", "continuation_intent", "continuation_acknowledged"}:
        row["projection_acknowledged_at"] = (NOW + timedelta(seconds=2)).replace(tzinfo=None)
    if phase in {"continuation_intent", "continuation_acknowledged"}:
        row["continuation_idempotency_key"] = continuation_key
        row["continuation_intent_at"] = (NOW + timedelta(seconds=3)).replace(tzinfo=None)
    if phase == "continuation_acknowledged":
        acknowledged_at = (NOW + timedelta(seconds=4)).replace(tzinfo=None)
        row.update(
            {
                "status": "consumed",
                "continuation_acknowledged_at": acknowledged_at,
                "consumed_at": acknowledged_at,
            }
        )
    return row


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


def test_deterministic_repository_covers_answer_fences_and_blocks_early_consume(
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
    with pytest.raises(ValueError, match="effects are not acknowledged"):
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
    assert store.get(expired.request_id, tenant_id="tenant-a", now=NOW).status == "expired"


@pytest.mark.parametrize(
    ("answered_at", "server_now", "message"),
    [
        (NOW + timedelta(microseconds=1), NOW, "future"),
        (
            pending_request().deadline_at + timedelta(microseconds=1),
            pending_request().deadline_at + timedelta(seconds=1),
            "deadline",
        ),
        (NOW - timedelta(hours=1, microseconds=1), NOW, "created"),
        (NOW.replace(tzinfo=None), NOW, "UTC"),
        (NOW.astimezone(timezone(timedelta(hours=2))), NOW, "UTC"),
    ],
)
def test_answer_rejects_invalid_timestamp_without_a_durable_write(
    memory_repository: TransactionalMemoryActionRepository,
    answered_at: datetime,
    server_now: datetime,
    message: str,
) -> None:
    request = pending_request()
    store = FactoryActionStore(repository=memory_repository)
    store.create(request, tenant_id="tenant-a")
    writes_before = memory_repository.locked_writes

    with pytest.raises(ValueError, match=message):
        store.answer(
            action_response(request).model_copy(update={"answered_at": answered_at}),
            tenant_id="tenant-a",
            now=server_now,
        )

    assert memory_repository.locked_writes == writes_before
    restarted = FactoryActionStore(repository=memory_repository)
    assert restarted.list(tenant_id="tenant-a", status="pending", now=NOW) == (request,)


@pytest.mark.parametrize(
    ("answered_at", "server_now", "created_at"),
    [
        (NOW, NOW, NOW),
        (pending_request().deadline_at, pending_request().deadline_at, NOW),
    ],
)
def test_answer_accepts_exact_timestamp_boundaries(
    memory_repository: TransactionalMemoryActionRepository,
    answered_at: datetime,
    server_now: datetime,
    created_at: datetime,
) -> None:
    request = pending_request()
    store = FactoryActionStore(repository=memory_repository)
    store.create(request, tenant_id="tenant-a")
    memory_repository.rows[str(request.request_id)].created_at = created_at
    response = action_response(request, answered_at=answered_at)

    assert store.answer(response, tenant_id="tenant-a", now=server_now) == response


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        ("request_id", "00000000-0000-0000-0000-000000000999"),
        ("tenant_id", "tenant-b"),
        ("factory_job_id", "00000000-0000-0000-0000-000000000999"),
        ("creation_job_id", "00000000-0000-0000-0000-000000000999"),
        ("correlation_id", "00000000-0000-0000-0000-000000000999"),
        ("expected_subject_version", 99),
        ("safe_checkpoint_sha256", "f" * 64),
        ("deadline_at", datetime(2027, 1, 1)),
        ("status", "consumed"),
        ("request_sha256", "f" * 64),
        ("response_sha256", "f" * 64),
        ("resume_phase", "continuation_intent"),
    ],
)
def test_db_row_rejects_hash_or_denormalized_column_tampering(
    column: str, replacement: object
) -> None:
    row = canonical_db_row()
    row[column] = replacement
    with pytest.raises(ValueError, match="factory action durable row"):
        _db_row(row)


def test_db_row_rejects_request_and_response_payload_tampering() -> None:
    import json

    request_row = canonical_db_row()
    request_envelope = json.loads(str(request_row["request_payload"]))
    request_envelope["tenant_id"] = "tenant-b"
    request_row["request_payload"] = json.dumps(
        request_envelope, sort_keys=True, separators=(",", ":")
    )
    with pytest.raises(ValueError, match="factory action durable row"):
        _db_row(request_row)

    response_row = canonical_db_row()
    response_payload = json.loads(str(response_row["response_payload"]))
    response_payload["idempotency_key"] = "d" * 64
    response_row["response_payload"] = json.dumps(
        response_payload, sort_keys=True, separators=(",", ":")
    )
    with pytest.raises(ValueError, match="factory action durable row"):
        _db_row(response_row)


def test_db_row_rejects_rehashed_response_identity_drift() -> None:
    import hashlib

    row = canonical_db_row()
    request = pending_request()
    drifted = action_response(request).model_copy(
        update={"request_id": UUID("00000000-0000-0000-0000-000000000999")}
    )
    encoded = _canonical(drifted)
    row["response_payload"] = encoded.decode()
    row["response_sha256"] = hashlib.sha256(encoded).hexdigest()
    with pytest.raises(ValueError, match="response binding"):
        _db_row(row)


def test_legacy_raw_request_row_is_canonically_backfilled_without_content_drift() -> None:
    request = pending_request()
    legacy = canonical_db_row()
    legacy["request_payload"] = _canonical(request).decode()
    legacy["request_sha256"] = None
    legacy["resume_phase"] = None

    normalized = _normalize_legacy_db_row(legacy)

    assert normalized["request_payload"] == _canonical_request_payload(
        request, "tenant-a"
    ).decode()
    assert normalized["request_sha256"] == hashlib.sha256(_canonical(request)).hexdigest()
    assert normalized["resume_phase"] == "answered"
    assert _db_row(normalized).request == request


def test_schema_migration_backfills_legacy_row_under_one_locked_transaction() -> None:
    request = pending_request()
    legacy = canonical_db_row()
    legacy["request_payload"] = _canonical(request).decode()
    legacy["request_sha256"] = None
    legacy["resume_phase"] = None

    class Cursor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, query, parameters=None):
            self.calls.append((" ".join(str(query).split()), parameters))

        def fetchall(self):
            return [legacy]

    cursor = Cursor()

    class Connection:
        def cursor(self):
            return cursor

    class Storage:
        @contextmanager
        def transaction(self):
            yield Connection()

    MariaDBFactoryActionRepository(Storage())  # type: ignore[arg-type]

    select_call = next(query for query, _ in cursor.calls if query.startswith("SELECT *"))
    update_call = next((query, params) for query, params in cursor.calls if query.startswith("UPDATE"))
    assert select_call.endswith("FOR UPDATE")
    assert update_call[1][0] == _canonical_request_payload(request, "tenant-a").decode()
    assert update_call[1][1] == hashlib.sha256(_canonical(request)).hexdigest()


def test_schema_migration_backfills_missing_intent_timestamp_only_at_startup() -> None:
    legacy = db_row_for_phase("projection_intent")
    legacy["projection_intent_at"] = None

    class Cursor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, query, parameters=None):
            self.calls.append((" ".join(str(query).split()), parameters))

        def fetchall(self):
            return [legacy]

    cursor = Cursor()

    class Connection:
        def cursor(self):
            return cursor

    class Storage:
        @contextmanager
        def transaction(self):
            yield Connection()

    MariaDBFactoryActionRepository(Storage())  # type: ignore[arg-type]

    update_parameters = next(
        parameters
        for query, parameters in cursor.calls
        if query.startswith("UPDATE")
    )
    assert update_parameters[6] == NOW.replace(tzinfo=None)


@pytest.mark.parametrize(
    ("phase", "status", "extra_column", "extra_value"),
    [
        ("answered", "answered", "consumed_at", NOW.replace(tzinfo=None)),
        ("projection_intent", "answered", "continuation_idempotency_key", "a" * 64),
        ("projection_acknowledged", "answered", "continuation_acknowledged_at", NOW.replace(tzinfo=None)),
        ("continuation_intent", "answered", "consumed_at", NOW.replace(tzinfo=None)),
    ],
)
def test_db_row_rejects_every_incoherent_extra_phase_column(
    phase: str, status: str, extra_column: str, extra_value: object
) -> None:
    row = canonical_db_row()
    response_sha256 = str(row["response_sha256"])
    row.update({"resume_phase": phase, "status": status})
    if phase != "answered":
        row["projection_idempotency_key"] = expected_effect_key(
            pending_request(), "projection", response_sha256
        )
    if phase in {"projection_acknowledged", "continuation_intent"}:
        row["projection_acknowledged_at"] = NOW.replace(tzinfo=None)
    if phase == "continuation_intent":
        row["continuation_idempotency_key"] = expected_effect_key(
            pending_request(), "continuation", response_sha256
        )
    row[extra_column] = extra_value
    with pytest.raises(ValueError, match="factory action durable row"):
        _db_row(row)


@pytest.mark.parametrize(
    ("phase", "extra_column", "extra_value"),
    [
        ("answered", "projection_intent_at", NOW.replace(tzinfo=None)),
        ("answered", "projection_idempotency_key", "a" * 64),
        ("projection_intent", "projection_acknowledged_at", NOW.replace(tzinfo=None)),
        ("projection_intent", "continuation_intent_at", NOW.replace(tzinfo=None)),
        ("projection_acknowledged", "continuation_idempotency_key", "b" * 64),
        ("projection_acknowledged", "continuation_intent_at", NOW.replace(tzinfo=None)),
        ("continuation_intent", "continuation_acknowledged_at", NOW.replace(tzinfo=None)),
        ("continuation_intent", "consumed_at", NOW.replace(tzinfo=None)),
    ],
)
def test_db_row_rejects_keys_and_timestamps_outside_exact_phase_matrix(
    phase: str, extra_column: str, extra_value: object
) -> None:
    row = db_row_for_phase(phase)
    row[extra_column] = extra_value

    with pytest.raises(ValueError, match="factory action durable row"):
        _db_row(row)


@pytest.mark.parametrize(
    ("column", "invalid_value"),
    [
        ("projection_intent_at", NOW - timedelta(seconds=1)),
        ("projection_acknowledged_at", NOW),
        ("continuation_intent_at", NOW + timedelta(seconds=1)),
        ("continuation_acknowledged_at", NOW + timedelta(seconds=2)),
        ("consumed_at", NOW + timedelta(seconds=5)),
    ],
)
def test_db_row_rejects_every_timestamp_ordering_drift(
    column: str, invalid_value: datetime
) -> None:
    row = db_row_for_phase("continuation_acknowledged")
    row[column] = invalid_value.replace(tzinfo=None)

    with pytest.raises(ValueError, match="timestamp"):
        _db_row(row)


def test_db_row_rejects_phase_timestamp_after_action_deadline() -> None:
    row = db_row_for_phase("continuation_acknowledged")
    row["continuation_acknowledged_at"] = pending_request().deadline_at.replace(tzinfo=None) + timedelta(microseconds=1)
    row["consumed_at"] = row["continuation_acknowledged_at"]

    with pytest.raises(ValueError, match="timestamp"):
        _db_row(row)


def test_db_row_rejects_missing_intent_timestamp_after_migration() -> None:
    row = db_row_for_phase("projection_intent")
    row["projection_intent_at"] = None

    with pytest.raises(ValueError, match="phase mismatch"):
        _db_row(row)


def test_db_row_rejects_arbitrary_effect_keys_even_when_phase_shape_is_valid() -> None:
    row = canonical_db_row()
    row.update(
        {
            "resume_phase": "projection_intent",
            "projection_idempotency_key": "f" * 64,
        }
    )
    with pytest.raises(ValueError, match="effect key mismatch"):
        _db_row(row)
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


def test_consume_cannot_bypass_durable_effect_acknowledgements(storage: MariaDBStorage) -> None:
    request = pending_request()
    store = FactoryActionStore(storage)
    store.create(request, tenant_id="tenant-a")
    response = store.answer(action_response(request), tenant_id="tenant-a", now=NOW)

    assert response == action_response(request)
    with pytest.raises(ValueError, match="effects are not acknowledged"):
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
