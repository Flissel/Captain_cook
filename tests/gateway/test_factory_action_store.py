from __future__ import annotations

import os
import hashlib
import json
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, RLock, Thread
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
from gateway.portal_store import (
    FactoryCredentialAmbiguousError,
    FactoryCredentialCoordinator,
    FactoryCredentialReceipt,
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

    def list_rows(self, *, tenant_id: str, status: str, after_request_id=None, limit: int = 100) -> tuple[FactoryActionRow, ...]:
        rows = tuple(
            row for row in self.rows.values()
            if row.tenant_id == tenant_id and row.status == status
            and (after_request_id is None or str(row.request.request_id) > str(after_request_id))
        )
        return tuple(sorted(rows, key=lambda row: str(row.request.request_id))[:limit])

    def resume_rows(self, *, after_request_id=None, limit: int = 100) -> tuple[FactoryActionRow, ...]:
        rows = tuple(
            row for row in self.rows.values()
            if row.status == "answered"
            and (after_request_id is None or str(row.request.request_id) > str(after_request_id))
        )
        return tuple(sorted(rows, key=lambda row: str(row.request.request_id))[:limit])

    def credential_rows(self, *, after_request_id=None, limit: int = 100):
        rows = tuple(
            row for row in self.rows.values()
            if row.credential_reservation_key is not None
            and (after_request_id is None or str(row.request.request_id) > str(after_request_id))
        )
        return tuple(sorted(rows, key=lambda row: str(row.request.request_id))[:limit])

    @contextmanager
    def locked(self, request_id, *, tenant_id: str):
        row = self.rows.get(str(request_id))
        yield row if row is not None and row.tenant_id == tenant_id else None
        self.locked_writes += 1

    def expire(self, *, tenant_id: str, now: datetime, request_id=None) -> None:
        for row in self.rows.values():
            if (
                row.tenant_id == tenant_id
                and row.status in {"pending", "answered"}
                and row.request.deadline_at <= now
                and (request_id is None or row.request.request_id == request_id)
            ):
                row.status = "expired"
                if row.credential_reservation_key is not None:
                    row.credential_revoke_phase = "revoke_pending"
                    row.credential_revoke_key = row.credential_reservation_key
                    row.credential_revoke_attempt += 1
                    row.credential_revoke_at = now


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
    if phase == "denial_acknowledged":
        request = pending_request()
        response = action_response(request).model_copy(
            update={"decision": "deny", "credential_ref": None}
        )
        encoded = _canonical(response).decode()
        acknowledged_at = (NOW + timedelta(seconds=2)).replace(tzinfo=None)
        row.update(
            {
                "status": "consumed",
                "response_payload": encoded,
                "response_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
                "projection_idempotency_key": expected_effect_key(
                    request, "projection", hashlib.sha256(encoded.encode()).hexdigest()
                ),
                "projection_acknowledged_at": acknowledged_at,
                "consumed_at": acknowledged_at,
            }
        )
    return row


def advance_to_phase(
    store: FactoryActionStore,
    request: FactoryActionRequestV1,
    phase: str,
    *,
    now: datetime,
) -> None:
    response = action_response(request, answered_at=now)
    store.answer(response, tenant_id="tenant-a", now=now)
    if phase == "answered":
        return
    projection = store.prepare_projection(
        request.request_id, tenant_id="tenant-a", now=now
    )
    if phase == "projection_intent":
        return
    store.acknowledge_projection(
        request.request_id,
        tenant_id="tenant-a",
        idempotency_key=projection.projection_idempotency_key,
        now=now,
    )
    if phase == "projection_acknowledged":
        return
    store.prepare_continuation(
        request.request_id, tenant_id="tenant-a", now=now
    )
    if phase != "continuation_intent":
        raise AssertionError(f"unsupported test phase: {phase}")


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


@pytest.mark.parametrize("observation", ["answer", "get", "list"])
def test_deadline_equality_converges_to_clean_expired_state_across_race_orders(
    memory_repository: TransactionalMemoryActionRepository,
    observation: str,
) -> None:
    request = pending_request()
    deadline = request.deadline_at
    store = FactoryActionStore(repository=memory_repository)
    store.create(request, tenant_id="tenant-a")

    if observation == "get":
        assert store.get(request.request_id, tenant_id="tenant-a", now=deadline).status == "expired"
    elif observation == "list":
        assert store.list(tenant_id="tenant-a", status="pending", now=deadline) == ()

    with pytest.raises(TimeoutError, match="expired"):
        store.answer(
            action_response(request, answered_at=deadline),
            tenant_id="tenant-a",
            now=deadline,
        )

    restarted = FactoryActionStore(repository=memory_repository)
    assert restarted.get(request.request_id, tenant_id="tenant-a", now=deadline).status == "expired"
    durable = memory_repository.rows[str(request.request_id)]
    assert durable.response is None
    assert durable.response_sha256 is None
    assert durable.resume_phase is None
    assert durable.projection_idempotency_key is None
    assert durable.continuation_idempotency_key is None


def test_restart_recovery_excludes_answered_action_at_deadline(
    memory_repository: TransactionalMemoryActionRepository,
) -> None:
    request = pending_request()
    before_deadline = request.deadline_at - timedelta(microseconds=1)
    store = FactoryActionStore(repository=memory_repository)
    store.create(request, tenant_id="tenant-a")
    store.answer(
        action_response(request, answered_at=before_deadline),
        tenant_id="tenant-a",
        now=before_deadline,
    )

    restarted = FactoryActionStore(repository=memory_repository)

    assert restarted.resume_candidates(now=request.deadline_at) == ()
    assert memory_repository.rows[str(request.request_id)].status == "expired"


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
def test_deadline_expires_every_nonterminal_phase_and_preserves_audit_evidence(
    memory_repository: TransactionalMemoryActionRepository,
    phase: str,
) -> None:
    request = pending_request()
    before_deadline = request.deadline_at - timedelta(microseconds=1)
    store = FactoryActionStore(repository=memory_repository)
    store.create(request, tenant_id="tenant-a")
    advance_to_phase(
        store,
        request,
        "continuation_intent" if phase == "continuation_acknowledged" else phase,
        now=before_deadline,
    )
    durable = memory_repository.rows[str(request.request_id)]
    if phase == "continuation_acknowledged":
        durable.resume_phase = "continuation_acknowledged"
        durable.continuation_acknowledged_at = before_deadline
    audit_before = (
        durable.response,
        durable.response_sha256,
        durable.resume_phase,
        durable.projection_idempotency_key,
        durable.projection_intent_at,
        durable.projection_acknowledged_at,
        durable.continuation_idempotency_key,
        durable.continuation_intent_at,
        durable.continuation_acknowledged_at,
        durable.consumed_at,
    )

    restarted = FactoryActionStore(repository=memory_repository)
    observed = restarted.get(
        request.request_id, tenant_id="tenant-a", now=request.deadline_at
    )

    assert observed.status == "expired"
    assert restarted.list(
        tenant_id="tenant-a", status="answered", now=request.deadline_at
    ) == ()
    assert restarted.list(
        tenant_id="tenant-a", status="expired", now=request.deadline_at
    ) == (request.model_copy(update={"status": "expired"}),)
    assert restarted.resume_candidates(now=request.deadline_at) == ()
    assert (
        durable.response,
        durable.response_sha256,
        durable.resume_phase,
        durable.projection_idempotency_key,
        durable.projection_intent_at,
        durable.projection_acknowledged_at,
        durable.continuation_idempotency_key,
        durable.continuation_intent_at,
        durable.continuation_acknowledged_at,
        durable.consumed_at,
    ) == audit_before


def test_expired_answered_action_rejects_every_resume_operation(
    memory_repository: TransactionalMemoryActionRepository,
) -> None:
    request = pending_request()
    before_deadline = request.deadline_at - timedelta(microseconds=1)
    store = FactoryActionStore(repository=memory_repository)
    store.create(request, tenant_id="tenant-a")
    advance_to_phase(store, request, "projection_intent", now=before_deadline)
    durable = memory_repository.rows[str(request.request_id)]
    assert durable.projection_idempotency_key is not None
    store.get(request.request_id, tenant_id="tenant-a", now=request.deadline_at)

    operations = (
        lambda: store.prepare_projection(
            request.request_id, tenant_id="tenant-a", now=request.deadline_at
        ),
        lambda: store.acknowledge_projection(
            request.request_id,
            tenant_id="tenant-a",
            idempotency_key=str(durable.projection_idempotency_key),
            now=request.deadline_at,
        ),
        lambda: store.prepare_continuation(
            request.request_id, tenant_id="tenant-a", now=request.deadline_at
        ),
        lambda: store.acknowledge_continuation(
            request.request_id,
            tenant_id="tenant-a",
            idempotency_key="f" * 64,
            now=request.deadline_at,
        ),
        lambda: store.consume(
            request.request_id,
            tenant_id="tenant-a",
            factory_job_id=request.factory_job_id,
            creation_job_id=request.creation_job_id,
            correlation_id=request.correlation_id,
            expected_subject_version=request.expected_subject_version,
            safe_checkpoint_sha256=request.safe_checkpoint_sha256,
            now=request.deadline_at,
        ),
    )
    for operation in operations:
        with pytest.raises(TimeoutError, match="expired"):
            operation()


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
def test_db_row_accepts_exact_audit_preserving_expired_phase_matrix(
    phase: str,
) -> None:
    row = db_row_for_phase(phase)
    row["status"] = "expired"
    row["consumed_at"] = None

    durable = _db_row(row)

    assert durable.status == "expired"
    assert durable.resume_phase == phase
    assert durable.response == action_response(pending_request())


def test_db_row_reloads_consumed_denial_with_exact_acknowledgement_time() -> None:
    durable = _db_row(db_row_for_phase("denial_acknowledged"))

    assert durable.status == "consumed"
    assert durable.resume_phase == "denial_acknowledged"
    assert durable.response is not None and durable.response.decision == "deny"
    assert durable.consumed_at == durable.projection_acknowledged_at
    assert durable.continuation_idempotency_key is None


def test_db_row_roundtrips_revoke_pending_reservation_metadata() -> None:
    request = pending_request()
    row = canonical_db_row()
    row.update(
        {
            "status": "expired",
            "response_payload": None,
            "response_sha256": None,
            "resume_phase": None,
            "credential_reservation_key": "a" * 64,
            "credential_alias": "n8n-crm-read",
            "credential_reserved_at": NOW.replace(tzinfo=None),
            "credential_revoke_phase": "revoke_pending",
            "credential_revoke_key": "a" * 64,
            "credential_revoke_attempt": 2,
            "credential_revoke_at": (NOW + timedelta(seconds=1)).replace(tzinfo=None),
        }
    )

    durable = _db_row(row)

    assert durable.request == request
    assert durable.credential_revoke_phase == "revoke_pending"
    assert durable.credential_revoke_key == durable.credential_reservation_key
    assert durable.credential_revoke_attempt == 2


@pytest.mark.parametrize(
    ("phase", "column", "value"),
    [
        ("answered", "projection_intent_at", NOW.replace(tzinfo=None)),
        ("projection_intent", "projection_acknowledged_at", NOW.replace(tzinfo=None)),
        ("projection_acknowledged", "continuation_intent_at", NOW.replace(tzinfo=None)),
        ("continuation_intent", "continuation_acknowledged_at", NOW.replace(tzinfo=None)),
        ("continuation_acknowledged", "consumed_at", NOW.replace(tzinfo=None)),
    ],
)
def test_db_row_rejects_extraneous_expired_phase_columns(
    phase: str, column: str, value: object
) -> None:
    row = db_row_for_phase(phase)
    row["status"] = "expired"
    row["consumed_at"] = None
    row[column] = value

    with pytest.raises(ValueError, match="factory action durable row"):
        _db_row(row)


def test_mariadb_expiry_updates_pending_and_answered_rows_only() -> None:
    calls: list[tuple[str, object]] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, query, parameters=None):
            calls.append((" ".join(str(query).split()), parameters))

    class Connection:
        def cursor(self):
            return Cursor()

    class Storage:
        @contextmanager
        def transaction(self):
            yield Connection()

    repository = object.__new__(MariaDBFactoryActionRepository)
    repository.storage = Storage()  # type: ignore[assignment]
    request = pending_request()

    repository.expire(
        tenant_id="tenant-a", now=request.deadline_at, request_id=request.request_id
    )

    query, parameters = calls[0]
    assert "status IN ('pending', 'answered')" in query
    assert parameters == (
        request.deadline_at.replace(tzinfo=None),
        "tenant-a",
        request.deadline_at.replace(tzinfo=None),
        str(request.request_id),
    )


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

        def fetchmany(self, limit):
            del limit
            return [legacy] if not any("UPDATE factory_action_inbox SET" in call[0] for call in self.calls) else []

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

        def fetchmany(self, limit):
            del limit
            return [legacy] if not any("UPDATE factory_action_inbox SET" in call[0] for call in self.calls) else []

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


def test_db_row_rejects_phase_timestamp_at_action_deadline() -> None:
    row = db_row_for_phase("projection_intent")
    row["projection_intent_at"] = pending_request().deadline_at.replace(tzinfo=None)

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


def test_list_and_recovery_read_repository_in_bounded_pages_without_loss() -> None:
    class PagingRepository(TransactionalMemoryActionRepository):
        def __init__(self) -> None:
            super().__init__()
            self.page_sizes: list[int] = []

        def list_rows(self, **kwargs):
            self.page_sizes.append(kwargs["limit"])
            return super().list_rows(**kwargs)

        def resume_rows(self, **kwargs):
            self.page_sizes.append(kwargs["limit"])
            return super().resume_rows(**kwargs)

    repository = PagingRepository()
    store = FactoryActionStore(repository=repository, page_size=2)
    template = pending_request()
    expected: list[UUID] = []
    for offset in range(5):
        request = template.model_copy(
            update={"request_id": UUID(f"00000000-0000-0000-0000-{700 + offset:012d}")}
        )
        expected.append(request.request_id)
        store.create(request, tenant_id="tenant-a")
        store.answer(action_response(request, key=f"{offset + 1:064x}"), tenant_id="tenant-a", now=NOW)

    first = store.list(tenant_id="tenant-a", status="answered", now=NOW)
    second = store.list(
        tenant_id="tenant-a", status="answered", now=NOW,
        after_request_id=first[-1].request_id,
    )
    third = store.list(
        tenant_id="tenant-a", status="answered", now=NOW,
        after_request_id=second[-1].request_id,
    )
    listed = first + second + third
    recovered = store.resume_candidates(now=NOW)

    assert [row.request_id for row in listed] == expected
    assert [request_id for request_id, _ in recovered] == expected
    assert repository.page_sizes == [2, 2, 2, 2, 2, 2]


def test_action_list_is_one_stable_bounded_page_with_explicit_cursor() -> None:
    class BoundedRepository(TransactionalMemoryActionRepository):
        limits: list[int] = []

        def list_rows(self, **kwargs):
            self.limits.append(kwargs["limit"])
            return super().list_rows(**kwargs)

    repository = BoundedRepository()
    store = FactoryActionStore(repository=repository, page_size=2)
    template = pending_request()
    expected = []
    for offset in range(5):
        request = template.model_copy(
            update={"request_id": UUID(f"00000000-0000-0000-0000-{800 + offset:012d}")}
        )
        expected.append(request.request_id)
        store.create(request, tenant_id="tenant-a")

    first = store.list(tenant_id="tenant-a", status="pending", now=NOW, limit=2)
    second = store.list(
        tenant_id="tenant-a", status="pending", now=NOW,
        after_request_id=first[-1].request_id, limit=2,
    )

    assert [item.request_id for item in first + second] == expected[:4]
    assert repository.limits == [2, 2]
    with pytest.raises(ValueError, match="limit"):
        store.list(tenant_id="tenant-a", status="pending", now=NOW, limit=101)


def test_credential_reservation_is_version_fenced_idempotent_and_releasable() -> None:
    repository = TransactionalMemoryActionRepository()
    store = FactoryActionStore(repository=repository)
    request = pending_request()
    store.create(request, tenant_id="tenant-a")

    first = store.reserve_credential(
        request.request_id, tenant_id="tenant-a",
        expected_subject_version=request.expected_subject_version,
        credential_alias="n8n-crm-read", now=NOW,
    )
    replay = store.reserve_credential(
        request.request_id, tenant_id="tenant-a",
        expected_subject_version=request.expected_subject_version,
        credential_alias="n8n-crm-read", now=NOW,
    )

    assert replay == first
    with pytest.raises(ValueError, match="reservation conflict"):
        store.reserve_credential(
            request.request_id, tenant_id="tenant-a",
            expected_subject_version=request.expected_subject_version,
            credential_alias="different", now=NOW,
        )
    store.release_credential_reservation(
        request.request_id, tenant_id="tenant-a", reservation_key=first
    )
    assert repository.rows[str(request.request_id)].credential_reservation_key is None


def test_post_write_deadline_revalidation_expires_without_linking_reference() -> None:
    repository = TransactionalMemoryActionRepository()
    store = FactoryActionStore(repository=repository)
    request = pending_request()
    before_deadline = request.deadline_at - timedelta(microseconds=1)
    store.create(request, tenant_id="tenant-a")
    reservation_key = store.reserve_credential(
        request.request_id, tenant_id="tenant-a",
        expected_subject_version=request.expected_subject_version,
        credential_alias="n8n-crm-read", now=before_deadline,
    )

    with pytest.raises(TimeoutError, match="expired"):
        store.complete_credential_reservation(
            request.request_id, tenant_id="tenant-a",
            reservation_key=reservation_key,
            credential_ref="credential://tenant-a/n8n-crm-read",
            now=request.deadline_at,
        )

    durable = repository.rows[str(request.request_id)]
    assert durable.status == "expired"
    assert durable.response is None
    assert durable.credential_reservation_key == reservation_key
    assert durable.credential_revoke_phase == "revoke_pending"
    assert durable.credential_revoke_key == reservation_key
    assert durable.credential_revoke_attempt == 1


def test_credential_revoke_pending_survives_restart_until_revoke_succeeds() -> None:
    repository = TransactionalMemoryActionRepository()
    store = FactoryActionStore(repository=repository)
    request = pending_request()
    store.create(request, tenant_id="tenant-a")
    key = store.reserve_credential(
        request.request_id, tenant_id="tenant-a",
        expected_subject_version=request.expected_subject_version,
        credential_alias="n8n-crm-read", now=NOW,
    )
    reference = "credential://tenant-a/n8n-crm-read"
    receipt = FactoryCredentialReceipt(
        key, "tenant-a", request.request_id, request.expected_subject_version,
        "n8n-crm-read", reference, hashlib.sha256(reference.encode()).hexdigest(),
    )

    class FlakyRevokeAdapter:
        attempts = 0

        def lookup_credential(self, reservation_key):
            assert reservation_key == key
            return receipt

        def store_credential(self, **kwargs):
            raise AssertionError(f"recovery must not write: {kwargs!r}")

        def revoke_credential(self, **kwargs):
            self.attempts += 1
            if self.attempts < 3:
                raise RuntimeError("temporary revoke failure")

    adapter = FlakyRevokeAdapter()
    # Deadline makes receipt completion fail, which begins durable revocation.
    coordinator = FactoryCredentialCoordinator(
        store=store, adapter=adapter, clock=lambda: request.deadline_at
    )
    coordinator.recover()
    durable = repository.rows[str(request.request_id)]
    assert durable.status == "expired"
    assert durable.credential_revoke_phase == "revoke_pending"
    assert durable.credential_reservation_key == key
    assert adapter.attempts == 1

    FactoryCredentialCoordinator(
        store=FactoryActionStore(repository=repository), adapter=adapter,
        clock=lambda: request.deadline_at,
    ).recover()
    assert repository.rows[str(request.request_id)].credential_revoke_phase == "revoke_pending"
    assert adapter.attempts == 2

    FactoryCredentialCoordinator(
        store=FactoryActionStore(repository=repository), adapter=adapter,
        clock=lambda: request.deadline_at,
    ).recover()
    durable = repository.rows[str(request.request_id)]
    assert adapter.attempts == 3
    assert durable.credential_reservation_key is None
    assert durable.credential_revoke_phase is None


def test_unexpired_revoke_pending_restart_never_links_typed_receipt() -> None:
    repository = TransactionalMemoryActionRepository()
    store = FactoryActionStore(repository=repository)
    request = pending_request()
    store.create(request, tenant_id="tenant-a")
    key = store.reserve_credential(
        request.request_id, tenant_id="tenant-a",
        expected_subject_version=request.expected_subject_version,
        credential_alias="n8n-crm-read", now=NOW,
    )
    store.mark_credential_revoke_pending(
        request.request_id, tenant_id="tenant-a", reservation_key=key, now=NOW
    )
    reference = "credential://tenant-a/n8n-crm-read"
    revoked: list[tuple[str, str | None]] = []
    receipt = FactoryCredentialReceipt(
        key, "tenant-a", request.request_id, request.expected_subject_version,
        "n8n-crm-read", reference, hashlib.sha256(reference.encode()).hexdigest(),
    )

    class FlakyAdapter:
        attempts = 0

        def lookup_credential(self, reservation_key):
            assert reservation_key == key
            return receipt

        def store_credential(self, **kwargs):
            raise AssertionError(f"recovery must not write: {kwargs!r}")

        def revoke_credential(self, *, reservation_key, credential_ref):
            revoked.append((reservation_key, credential_ref))
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("temporary revoke failure")

    adapter = FlakyAdapter()
    FactoryCredentialCoordinator(
        store=store, adapter=adapter, clock=lambda: NOW
    ).recover()
    durable = repository.rows[str(request.request_id)]
    assert adapter.attempts == 1
    assert durable.credential_ref is None
    assert durable.credential_revoke_phase == "revoke_pending"

    FactoryCredentialCoordinator(
        store=FactoryActionStore(repository=repository), adapter=adapter,
        clock=lambda: NOW,
    ).recover()
    durable = repository.rows[str(request.request_id)]
    assert adapter.attempts == 2
    assert revoked == [(key, reference), (key, reference)]
    assert durable.credential_ref is None
    assert durable.credential_reservation_key is None
    assert durable.credential_revoke_phase is None


def test_revoke_pending_restart_never_uses_conflicting_receipt_reference() -> None:
    repository = TransactionalMemoryActionRepository()
    store = FactoryActionStore(repository=repository)
    request = pending_request()
    store.create(request, tenant_id="tenant-a")
    key = store.reserve_credential(
        request.request_id,
        tenant_id="tenant-a",
        expected_subject_version=request.expected_subject_version,
        credential_alias="n8n-crm-read",
        now=NOW,
    )
    store.mark_credential_revoke_pending(
        request.request_id,
        tenant_id="tenant-a",
        reservation_key=key,
        now=NOW,
    )
    foreign_reference = "credential://tenant-b/another-request"
    conflicting_receipt = FactoryCredentialReceipt(
        key,
        "tenant-b",
        UUID(int=999),
        request.expected_subject_version,
        "n8n-crm-read",
        foreign_reference,
        hashlib.sha256(foreign_reference.encode()).hexdigest(),
    )
    revoked: list[tuple[str, str | None]] = []

    class KeyOnlyUnavailableAdapter:
        def lookup_credential(self, reservation_key):
            assert reservation_key == key
            return conflicting_receipt

        def store_credential(self, **kwargs):
            raise AssertionError(f"recovery must not write: {kwargs!r}")

        def revoke_credential(self, *, reservation_key, credential_ref):
            revoked.append((reservation_key, credential_ref))
            if credential_ref is None:
                raise RuntimeError("authoritative key-only revoke unavailable")
            raise AssertionError("unvalidated credential reference was used")

    FactoryCredentialCoordinator(
        store=store,
        adapter=KeyOnlyUnavailableAdapter(),
        clock=lambda: NOW,
    ).recover()

    durable = repository.rows[str(request.request_id)]
    assert revoked == [(key, None)]
    assert durable.credential_ref is None
    assert durable.credential_reservation_key == key
    assert durable.credential_revoke_phase == "revoke_pending"


def test_blocked_credential_write_that_crosses_deadline_is_revoked_and_not_persisted() -> None:
    repository = TransactionalMemoryActionRepository()
    store = FactoryActionStore(repository=repository)
    request = pending_request()
    store.create(request, tenant_id="tenant-a")
    entered = Event()
    release = Event()
    revoked: list[tuple[str, str]] = []

    class BlockingAdapter:
        def lookup_credential(self, reservation_key):
            return None

        def store_credential(self, *, reservation_key, tenant_id, request_id,
                             expected_subject_version, purpose, **kwargs):
            del kwargs
            entered.set()
            assert release.wait(timeout=5)
            reference = "credential://tenant-a/n8n-crm-read"
            return FactoryCredentialReceipt(
                reservation_key, tenant_id, request_id, expected_subject_version,
                purpose, reference, hashlib.sha256(reference.encode()).hexdigest(),
            )

        def revoke_credential(self, *, reservation_key, credential_ref):
            revoked.append((reservation_key, credential_ref))

    coordinator = FactoryCredentialCoordinator(
        store=store, adapter=BlockingAdapter(), clock=lambda: request.deadline_at
    )
    errors: list[Exception] = []
    thread = Thread(
        target=lambda: _capture_error(
            errors,
            lambda: coordinator.store_and_answer(
                request_id=request.request_id, tenant_id="tenant-a",
                expected_subject_version=request.expected_subject_version,
                credential_alias="n8n-crm-read", secret_value="private-fixture-value",
                reserved_at=request.deadline_at - timedelta(microseconds=1),
            ),
        )
    )
    thread.start()
    assert entered.wait(timeout=2)
    release.set()
    thread.join(timeout=5)

    assert len(errors) == 1 and isinstance(errors[0], TimeoutError)
    assert len(revoked) == 1
    durable = repository.rows[str(request.request_id)]
    assert durable.status == "expired"
    assert durable.response is None
    assert "private-fixture-value" not in repr(durable)


def test_credential_lookup_none_preserves_reservation_for_later_authoritative_retry() -> None:
    repository = TransactionalMemoryActionRepository()
    store = FactoryActionStore(repository=repository)
    request = pending_request()
    store.create(request, tenant_id="tenant-a")

    class RetryAdapter:
        calls = 0

        def lookup_credential(self, reservation_key):
            return None

        def store_credential(self, *, reservation_key, tenant_id, request_id,
                             expected_subject_version, purpose, **kwargs):
            del kwargs
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("credential boundary unavailable")
            reference = "credential://tenant-a/n8n-crm-read"
            return FactoryCredentialReceipt(
                reservation_key, tenant_id, request_id, expected_subject_version,
                purpose, reference, hashlib.sha256(reference.encode()).hexdigest(),
            )

        def revoke_credential(self, **kwargs):
            raise AssertionError(f"write did not commit: {kwargs!r}")

    adapter = RetryAdapter()
    coordinator = FactoryCredentialCoordinator(store=store, adapter=adapter, clock=lambda: NOW)
    with pytest.raises(FactoryCredentialAmbiguousError, match="unresolved"):
        coordinator.store_and_answer(
            request_id=request.request_id, tenant_id="tenant-a",
            expected_subject_version=request.expected_subject_version,
            credential_alias="n8n-crm-read", secret_value="private-fixture-value",
            reserved_at=NOW,
        )
    assert repository.rows[str(request.request_id)].credential_reservation_key is not None

    assert coordinator.store_and_answer(
        request_id=request.request_id, tenant_id="tenant-a",
        expected_subject_version=request.expected_subject_version,
        credential_alias="n8n-crm-read", secret_value="private-fixture-value",
        reserved_at=NOW,
    ) == "credential://tenant-a/n8n-crm-read"
    assert repository.rows[str(request.request_id)].status == "pending"


def test_untyped_credential_receipt_preserves_reservation_without_an_opaque_reference() -> None:
    repository = TransactionalMemoryActionRepository()
    store = FactoryActionStore(repository=repository)
    request = pending_request()
    store.create(request, tenant_id="tenant-a")
    revoked: list[tuple[str, str]] = []

    class UntypedReceiptAdapter:
        def lookup_credential(self, reservation_key):
            return None

        def store_credential(self, **kwargs):
            return "credential://tenant-a/unverified"

        def revoke_credential(self, *, reservation_key, credential_ref):
            revoked.append((reservation_key, credential_ref))

    with pytest.raises(FactoryCredentialAmbiguousError, match="unresolved"):
        FactoryCredentialCoordinator(
            store=store, adapter=UntypedReceiptAdapter(), clock=lambda: NOW
        ).store_and_answer(
            request_id=request.request_id,
            tenant_id="tenant-a",
            expected_subject_version=request.expected_subject_version,
            credential_alias="n8n-crm-read",
            secret_value="private-fixture-value",
            reserved_at=NOW,
        )

    durable = repository.rows[str(request.request_id)]
    assert durable.status == "pending"
    assert durable.credential_reservation_key is not None
    assert revoked == []
    assert "private-fixture-value" not in repr(durable)


def test_credential_reaper_preserves_untyped_receipt_for_authoritative_reconciliation() -> None:
    repository = TransactionalMemoryActionRepository()
    store = FactoryActionStore(repository=repository)
    request = pending_request()
    store.create(request, tenant_id="tenant-a")
    store.reserve_credential(
        request.request_id,
        tenant_id="tenant-a",
        expected_subject_version=request.expected_subject_version,
        credential_alias="n8n-crm-read",
        now=NOW,
    )
    revoked: list[tuple[str, str]] = []

    class UntypedReceiptAdapter:
        def lookup_credential(self, reservation_key):
            return "credential://tenant-a/unverified"

        def store_credential(self, **kwargs):
            raise AssertionError(f"recovery must not write: {kwargs!r}")

        def revoke_credential(self, *, reservation_key, credential_ref):
            revoked.append((reservation_key, credential_ref))

    FactoryCredentialCoordinator(
        store=store, adapter=UntypedReceiptAdapter(), clock=lambda: NOW
    ).recover()

    durable = repository.rows[str(request.request_id)]
    assert durable.status == "pending"
    assert durable.credential_reservation_key is not None
    assert revoked == []


def test_credential_commit_then_raise_reconciles_exact_receipt_without_duplicate_write() -> None:
    repository = TransactionalMemoryActionRepository()
    store = FactoryActionStore(repository=repository)
    request = pending_request()
    store.create(request, tenant_id="tenant-a")

    class CommitThenRaiseAdapter:
        def __init__(self) -> None:
            self.receipts: dict[str, FactoryCredentialReceipt] = {}
            self.calls = 0

        def lookup_credential(self, reservation_key):
            return self.receipts.get(reservation_key)

        def store_credential(self, *, reservation_key, tenant_id, request_id,
                             expected_subject_version, purpose, **kwargs):
            del kwargs
            self.calls += 1
            reference = "credential://tenant-a/n8n-crm-read"
            self.receipts[reservation_key] = FactoryCredentialReceipt(
                reservation_key=reservation_key,
                tenant_id=tenant_id,
                request_id=request_id,
                request_version=expected_subject_version,
                purpose=purpose,
                opaque_reference=reference,
                reference_sha256=hashlib.sha256(reference.encode()).hexdigest(),
            )
            raise RuntimeError("connection lost after commit")

        def revoke_credential(self, **kwargs):
            raise AssertionError(f"valid committed receipt must survive: {kwargs!r}")

    adapter = CommitThenRaiseAdapter()
    reference = FactoryCredentialCoordinator(
        store=store, adapter=adapter, clock=lambda: NOW
    ).store_and_answer(
        request_id=request.request_id,
        tenant_id="tenant-a",
        expected_subject_version=request.expected_subject_version,
        credential_alias="n8n-crm-read",
        secret_value="private-fixture-value",
        reserved_at=NOW,
    )

    assert reference == "credential://tenant-a/n8n-crm-read"
    assert adapter.calls == 1
    assert repository.rows[str(request.request_id)].status == "pending"
    assert repository.rows[str(request.request_id)].credential_ref == reference
    assert "private-fixture-value" not in repr(repository.rows)


def test_credential_restart_reconciles_valid_receipt_and_revokes_expired_receipt() -> None:
    repository = TransactionalMemoryActionRepository()
    store = FactoryActionStore(repository=repository, page_size=2)
    template = pending_request()
    valid = template.model_copy(update={"request_id": UUID(int=901)})
    expired = template.model_copy(
        update={"request_id": UUID(int=902), "deadline_at": NOW}
    )
    receipts: dict[str, FactoryCredentialReceipt] = {}
    revoked: list[str] = []
    for request, reserved_at in (
        (valid, NOW),
        (expired, NOW - timedelta(microseconds=1)),
    ):
        store.create(request, tenant_id="tenant-a")
        key = store.reserve_credential(
            request.request_id, tenant_id="tenant-a",
            expected_subject_version=request.expected_subject_version,
            credential_alias="n8n-crm-read", now=reserved_at,
        )
        reference = f"credential://tenant-a/{request.request_id}"
        receipts[key] = FactoryCredentialReceipt(
            key, "tenant-a", request.request_id, request.expected_subject_version,
            "n8n-crm-read", reference,
            hashlib.sha256(reference.encode()).hexdigest(),
        )

    class Adapter:
        def lookup_credential(self, reservation_key):
            return receipts.get(reservation_key)

        def store_credential(self, **kwargs):
            raise AssertionError(f"recovery must not rewrite: {kwargs!r}")

        def revoke_credential(self, *, reservation_key, credential_ref):
            revoked.append(credential_ref)

    FactoryCredentialCoordinator(
        store=FactoryActionStore(repository=repository, page_size=2),
        adapter=Adapter(), clock=lambda: NOW,
    ).recover()

    assert repository.rows[str(valid.request_id)].status == "pending"
    assert repository.rows[str(valid.request_id)].credential_ref == f"credential://tenant-a/{valid.request_id}"
    assert repository.rows[str(expired.request_id)].status == "expired"
    assert revoked == [f"credential://tenant-a/{expired.request_id}"]
    assert all(row.credential_reservation_key is None for row in repository.rows.values())


def test_concurrent_duplicate_credential_submissions_create_one_sink_effect() -> None:
    class LockedRepository(TransactionalMemoryActionRepository):
        def __init__(self) -> None:
            super().__init__()
            self.guard = RLock()

        @contextmanager
        def locked(self, request_id, *, tenant_id):
            with self.guard:
                with super().locked(request_id, tenant_id=tenant_id) as row:
                    yield row

    repository = LockedRepository()
    store = FactoryActionStore(repository=repository)
    request = pending_request()
    store.create(request, tenant_id="tenant-a")

    class IdempotentAdapter:
        def __init__(self) -> None:
            self.guard = RLock()
            self.effects: dict[str, FactoryCredentialReceipt] = {}

        def lookup_credential(self, reservation_key):
            with self.guard:
                return self.effects.get(reservation_key)

        def store_credential(self, *, reservation_key, tenant_id, request_id,
                             expected_subject_version, purpose, **kwargs):
            del kwargs
            with self.guard:
                reference = "credential://tenant-a/n8n-crm-read"
                return self.effects.setdefault(
                    reservation_key,
                    FactoryCredentialReceipt(
                        reservation_key, tenant_id, request_id,
                        expected_subject_version, purpose, reference,
                        hashlib.sha256(reference.encode()).hexdigest(),
                    ),
                )

        def revoke_credential(self, **kwargs):
            raise AssertionError(f"valid duplicate must not revoke: {kwargs!r}")

    adapter = IdempotentAdapter()
    coordinator = FactoryCredentialCoordinator(store=store, adapter=adapter, clock=lambda: NOW)
    results: list[str] = []
    errors: list[Exception] = []

    def submit() -> None:
        _capture_error(errors, lambda: results.append(coordinator.store_and_answer(
            request_id=request.request_id, tenant_id="tenant-a",
            expected_subject_version=request.expected_subject_version,
            credential_alias="n8n-crm-read", secret_value="private-fixture-value",
            reserved_at=NOW,
        )))

    threads = (Thread(target=submit), Thread(target=submit))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert results == ["credential://tenant-a/n8n-crm-read"] * 2
    assert len(adapter.effects) == 1
    assert repository.rows[str(request.request_id)].status == "pending"
    assert repository.rows[str(request.request_id)].credential_ref == "credential://tenant-a/n8n-crm-read"


def _capture_error(errors: list[Exception], operation) -> None:
    try:
        operation()
    except Exception as error:
        errors.append(error)
