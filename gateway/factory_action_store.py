"""Durable, tenant-scoped state machine for Captain Factory Action Inbox."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import ContextManager, Literal, Protocol
from uuid import UUID

from agenten.agent_factory.forge_contracts import (
    FactoryActionRequestV1,
    FactoryActionResponseV1,
    validate_action_response_binding,
)
from blockchain.mariadb_storage import MariaDBStorage


ActionStatus = Literal["pending", "answered", "expired", "consumed"]


@dataclass
class FactoryActionRow:
    """Mutable row held exclusively by a repository transaction."""

    request: FactoryActionRequestV1
    tenant_id: str
    status: ActionStatus = "pending"
    response: FactoryActionResponseV1 | None = None
    response_sha256: str | None = None
    consumed_at: datetime | None = None


class FactoryActionRepository(Protocol):
    def create(self, row: FactoryActionRow) -> FactoryActionRow: ...

    def list_rows(
        self, *, tenant_id: str, status: str
    ) -> tuple[FactoryActionRow, ...]: ...

    def locked(
        self, request_id: UUID, *, tenant_id: str
    ) -> ContextManager[FactoryActionRow | None]: ...

    def expire(
        self,
        *,
        tenant_id: str,
        now: datetime,
        request_id: UUID | None = None,
    ) -> None: ...


class FactoryActionStore:
    """Enforce replay, tenant, version, deadline, scope and consume fences."""

    def __init__(
        self,
        storage: MariaDBStorage | None = None,
        *,
        repository: FactoryActionRepository | None = None,
    ) -> None:
        if (storage is None) == (repository is None):
            raise ValueError("exactly one factory action repository is required")
        self._repository = (
            repository
            if repository is not None
            else MariaDBFactoryActionRepository(storage)  # type: ignore[arg-type]
        )

    def create(self, request: FactoryActionRequestV1, *, tenant_id: str) -> FactoryActionRequestV1:
        if request.status != "pending":
            raise ValueError("new factory action must be pending")
        row = self._repository.create(FactoryActionRow(request=request, tenant_id=tenant_id))
        if row.tenant_id != tenant_id or _canonical(row.request) != _canonical(request):
            raise ValueError("conflicting factory action request")
        return request

    def list(
        self, *, tenant_id: str, status: ActionStatus, now: datetime
    ) -> tuple[FactoryActionRequestV1, ...]:
        self._repository.expire(tenant_id=tenant_id, now=_aware_utc(now))
        return tuple(
            row.request.model_copy(update={"status": row.status})
            for row in self._repository.list_rows(tenant_id=tenant_id, status=status)
        )

    def get(
        self, request_id: UUID, *, tenant_id: str, now: datetime
    ) -> FactoryActionRequestV1:
        self._repository.expire(
            tenant_id=tenant_id, now=_aware_utc(now), request_id=request_id
        )
        with self._repository.locked(request_id, tenant_id=tenant_id) as row:
            if row is None:
                raise LookupError("factory action not found")
            return row.request.model_copy(update={"status": row.status})

    def answer(
        self,
        response: FactoryActionResponseV1,
        *,
        tenant_id: str,
        now: datetime,
    ) -> FactoryActionResponseV1:
        now_utc = _aware_utc(now)
        canonical = _canonical(response)
        outcome: FactoryActionResponseV1 | None = None
        error: Exception | None = None
        with self._repository.locked(response.request_id, tenant_id=tenant_id) as row:
            if row is None:
                error = LookupError("factory action not found")
            elif row.status in {"answered", "consumed"}:
                if row.response is not None and _canonical(row.response) == canonical:
                    outcome = row.response
                else:
                    error = ValueError("conflicting action response")
            elif row.status == "expired" or row.request.deadline_at <= now_utc:
                row.status = "expired"
                error = TimeoutError("factory action expired")
            elif response.expected_subject_version != row.request.expected_subject_version:
                error = ValueError("stale subject version")
            else:
                try:
                    validate_action_response_binding(row.request, response)
                except ValueError as caught:
                    error = ValueError(str(caught))
                if error is None and response.credential_ref is not None and not response.credential_ref.startswith(
                    f"credential://{tenant_id}/"
                ):
                    error = ValueError("credential reference is not tenant scoped")
                if error is None:
                    row.response = response
                    row.response_sha256 = hashlib.sha256(canonical).hexdigest()
                    row.status = "answered"
                    outcome = response
        if error is not None:
            raise error
        if outcome is None:
            raise RuntimeError("factory action answer produced no outcome")
        return outcome

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
    ) -> FactoryActionResponseV1:
        outcome: FactoryActionResponseV1 | None = None
        error: Exception | None = None
        with self._repository.locked(request_id, tenant_id=tenant_id) as row:
            if row is None:
                error = LookupError("factory action not found")
            elif row.status == "consumed":
                error = ValueError("factory action already consumed")
            elif row.status != "answered":
                error = ValueError("factory action is not answered")
            else:
                request = row.request
                expected = (
                    factory_job_id,
                    creation_job_id,
                    correlation_id,
                    expected_subject_version,
                    safe_checkpoint_sha256,
                )
                actual = (
                    request.factory_job_id,
                    request.creation_job_id,
                    request.correlation_id,
                    request.expected_subject_version,
                    request.safe_checkpoint_sha256,
                )
                if actual != expected:
                    error = ValueError("factory action resume fence mismatch")
                elif request.deadline_at <= _aware_utc(now):
                    row.status = "expired"
                    error = TimeoutError("factory action expired")
                elif row.response is None:
                    error = ValueError("factory action response missing")
                else:
                    row.status = "consumed"
                    row.consumed_at = _aware_utc(now)
                    outcome = row.response
        if error is not None:
            raise error
        if outcome is None:
            raise RuntimeError("factory action consume produced no outcome")
        return outcome


class MariaDBFactoryActionRepository:
    """MariaDB adapter; row locks make state transitions interprocess safe."""

    def __init__(self, storage: MariaDBStorage) -> None:
        self.storage = storage
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS factory_action_inbox (
                        request_id CHAR(36) NOT NULL PRIMARY KEY,
                        tenant_id VARCHAR(128) NOT NULL,
                        factory_job_id CHAR(36) NOT NULL,
                        creation_job_id CHAR(36) NOT NULL,
                        correlation_id CHAR(36) NOT NULL,
                        expected_subject_version BIGINT NOT NULL,
                        safe_checkpoint_sha256 CHAR(64) NOT NULL,
                        deadline_at DATETIME(6) NOT NULL,
                        status VARCHAR(16) NOT NULL,
                        request_payload JSON NOT NULL,
                        response_payload JSON NULL,
                        response_sha256 CHAR(64) NULL,
                        consumed_at DATETIME(6) NULL,
                        created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                        updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                            ON UPDATE CURRENT_TIMESTAMP(6),
                        INDEX idx_factory_action_tenant_status
                            (tenant_id, status, deadline_at),
                        INDEX idx_factory_action_job
                            (tenant_id, factory_job_id, creation_job_id)
                    ) ENGINE=InnoDB
                    """
                )

    def create(self, row: FactoryActionRow) -> FactoryActionRow:
        request = row.request
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """INSERT IGNORE INTO factory_action_inbox
                       (request_id, tenant_id, factory_job_id, creation_job_id,
                        correlation_id, expected_subject_version,
                        safe_checkpoint_sha256, deadline_at, status,
                        request_payload, response_payload, response_sha256, consumed_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s,
                               NULL, NULL, NULL)""",
                    (
                        str(request.request_id), row.tenant_id,
                        str(request.factory_job_id), str(request.creation_job_id),
                        str(request.correlation_id), request.expected_subject_version,
                        request.safe_checkpoint_sha256, _naive_utc(request.deadline_at),
                        _canonical(request).decode("utf-8"),
                    ),
                )
                cursor.execute(
                    "SELECT * FROM factory_action_inbox WHERE request_id = %s FOR UPDATE",
                    (str(request.request_id),),
                )
                persisted = cursor.fetchone()
                if persisted is None:
                    raise RuntimeError("factory action insert was not persisted")
                return _db_row(persisted)

    def list_rows(
        self, *, tenant_id: str, status: str
    ) -> tuple[FactoryActionRow, ...]:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM factory_action_inbox WHERE tenant_id = %s "
                    "AND status = %s ORDER BY created_at, request_id",
                    (tenant_id, status),
                )
                return tuple(_db_row(row) for row in cursor.fetchall())

    @contextmanager
    def locked(self, request_id: UUID, *, tenant_id: str):
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM factory_action_inbox WHERE request_id = %s "
                    "AND tenant_id = %s FOR UPDATE",
                    (str(request_id), tenant_id),
                )
                payload = cursor.fetchone()
                row = None if payload is None else _db_row(payload)
                yield row
                if row is not None:
                    cursor.execute(
                        """UPDATE factory_action_inbox SET status = %s,
                           response_payload = %s, response_sha256 = %s,
                           consumed_at = %s WHERE request_id = %s AND tenant_id = %s""",
                        (
                            row.status,
                            None if row.response is None else _canonical(row.response).decode("utf-8"),
                            row.response_sha256,
                            None if row.consumed_at is None else _naive_utc(row.consumed_at),
                            str(request_id), tenant_id,
                        ),
                    )

    def expire(
        self,
        *,
        tenant_id: str,
        now: datetime,
        request_id: UUID | None = None,
    ) -> None:
        query = (
            "UPDATE factory_action_inbox SET status = 'expired' WHERE tenant_id = %s "
            "AND status = 'pending' AND deadline_at <= %s"
        )
        parameters: tuple[object, ...] = (tenant_id, _naive_utc(now))
        if request_id is not None:
            query += " AND request_id = %s"
            parameters += (str(request_id),)
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, parameters)


def _canonical(model: FactoryActionRequestV1 | FactoryActionResponseV1) -> bytes:
    return json.dumps(
        model.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _db_row(row: dict[str, object]) -> FactoryActionRow:
    request = FactoryActionRequestV1.model_validate_json(_json_bytes(row["request_payload"]))
    response_payload = row.get("response_payload")
    response = (
        None
        if response_payload is None
        else FactoryActionResponseV1.model_validate_json(_json_bytes(response_payload))
    )
    consumed = row.get("consumed_at")
    return FactoryActionRow(
        request=request,
        tenant_id=str(row["tenant_id"]),
        status=str(row["status"]),  # type: ignore[arg-type]
        response=response,
        response_sha256=(None if row.get("response_sha256") is None else str(row["response_sha256"])),
        consumed_at=(None if consumed is None else _aware_utc(consumed)),  # type: ignore[arg-type]
    )


def _json_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _naive_utc(value: datetime) -> datetime:
    return _aware_utc(value).replace(tzinfo=None)
