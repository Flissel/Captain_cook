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
from agenten.agent_factory.action_resume import FactoryActionResumeRecord
from blockchain.mariadb_storage import MariaDBStorage


ActionStatus = Literal["pending", "answered", "expired", "consumed"]
ResumePhase = Literal[
    "answered",
    "projection_intent",
    "projection_acknowledged",
    "continuation_intent",
    "continuation_acknowledged",
]


class FactoryActionDurabilityError(ValueError):
    """A persisted Action Inbox row no longer matches its canonical payload."""


@dataclass
class FactoryActionRow:
    """Mutable row held exclusively by a repository transaction."""

    request: FactoryActionRequestV1
    tenant_id: str
    created_at: datetime | None = None
    status: ActionStatus = "pending"
    response: FactoryActionResponseV1 | None = None
    response_sha256: str | None = None
    resume_phase: ResumePhase | None = None
    projection_idempotency_key: str | None = None
    projection_intent_at: datetime | None = None
    projection_acknowledged_at: datetime | None = None
    continuation_idempotency_key: str | None = None
    continuation_intent_at: datetime | None = None
    continuation_acknowledged_at: datetime | None = None
    consumed_at: datetime | None = None


class FactoryActionRepository(Protocol):
    def create(self, row: FactoryActionRow) -> FactoryActionRow: ...

    def list_rows(
        self, *, tenant_id: str, status: str
    ) -> tuple[FactoryActionRow, ...]: ...

    def resume_rows(self) -> tuple[FactoryActionRow, ...]: ...

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

    def resume_candidates(self, *, now: datetime) -> tuple[tuple[UUID, str], ...]:
        now_utc = _aware_utc(now)
        candidates: list[tuple[UUID, str]] = []
        for row in self._repository.resume_rows():
            if row.request.deadline_at <= now_utc:
                self._repository.expire(
                    tenant_id=row.tenant_id,
                    now=now_utc,
                    request_id=row.request.request_id,
                )
            else:
                candidates.append((row.request.request_id, row.tenant_id))
        return tuple(candidates)

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
        answered_at = _exact_utc(response.answered_at, field="answered_at")
        if answered_at > now_utc:
            raise ValueError("factory action answered_at is in the future")
        canonical = _canonical(response)
        outcome: FactoryActionResponseV1 | None = None
        error: Exception | None = None
        with self._repository.locked(response.request_id, tenant_id=tenant_id) as row:
            if row is None:
                error = LookupError("factory action not found")
            elif (
                row.status == "expired"
                or (
                    row.status != "consumed"
                    and now_utc >= row.request.deadline_at
                )
                or answered_at >= row.request.deadline_at
            ):
                if row.status in {"pending", "answered"}:
                    row.status = "expired"
                error = TimeoutError("factory action expired")
            elif row.created_at is not None and answered_at < row.created_at:
                raise ValueError("factory action answered_at is before request created_at")
            elif row.status in {"answered", "consumed"}:
                if row.response is not None and _canonical(row.response) == canonical:
                    outcome = row.response
                else:
                    error = ValueError("conflicting action response")
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
                    row.resume_phase = "answered"
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
        self._repository.expire(
            tenant_id=tenant_id, now=_aware_utc(now), request_id=request_id
        )
        with self._repository.locked(request_id, tenant_id=tenant_id) as row:
            if row is None:
                raise LookupError("factory action not found")
            if row.status == "expired":
                raise TimeoutError("factory action expired")
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
                raise ValueError("factory action resume fence mismatch")
            if row.status == "consumed":
                raise ValueError("factory action already consumed")
            raise ValueError("factory action resume effects are not acknowledged")

    def prepare_projection(
        self, request_id: UUID, *, tenant_id: str, now: datetime
    ) -> FactoryActionResumeRecord:
        self._repository.expire(
            tenant_id=tenant_id, now=_aware_utc(now), request_id=request_id
        )
        with self._repository.locked(request_id, tenant_id=tenant_id) as row:
            self._validate_resumable(row, now=now)
            assert row is not None and row.response is not None
            if row.resume_phase == "answered":
                row.projection_idempotency_key = _effect_key(
                    row.request, "projection", row.response_sha256
                )
                row.projection_intent_at = _transition_time(row, now)
                row.resume_phase = "projection_intent"
            return _resume_record(row)

    def acknowledge_projection(
        self,
        request_id: UUID,
        *,
        tenant_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> FactoryActionResumeRecord:
        self._repository.expire(
            tenant_id=tenant_id, now=_aware_utc(now), request_id=request_id
        )
        with self._repository.locked(request_id, tenant_id=tenant_id) as row:
            self._validate_resumable(row, now=now)
            assert row is not None
            if row.projection_idempotency_key != idempotency_key:
                raise ValueError("factory action projection acknowledgement mismatch")
            if row.resume_phase == "projection_intent":
                row.projection_acknowledged_at = _transition_time(row, now)
                row.resume_phase = "projection_acknowledged"
            elif row.resume_phase not in {
                "projection_acknowledged",
                "continuation_intent",
                "continuation_acknowledged",
            }:
                raise ValueError("factory action projection is not pending")
            return _resume_record(row)

    def prepare_continuation(
        self, request_id: UUID, *, tenant_id: str, now: datetime
    ) -> FactoryActionResumeRecord:
        self._repository.expire(
            tenant_id=tenant_id, now=_aware_utc(now), request_id=request_id
        )
        with self._repository.locked(request_id, tenant_id=tenant_id) as row:
            self._validate_resumable(row, now=now)
            assert row is not None
            if row.resume_phase == "projection_acknowledged":
                row.continuation_idempotency_key = _effect_key(
                    row.request, "continuation", row.response_sha256
                )
                row.continuation_intent_at = _transition_time(row, now)
                row.resume_phase = "continuation_intent"
            if row.resume_phase not in {"continuation_intent", "continuation_acknowledged"}:
                raise ValueError("factory action projection is not acknowledged")
            return _resume_record(row)

    def acknowledge_continuation(
        self,
        request_id: UUID,
        *,
        tenant_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> FactoryActionResumeRecord:
        self._repository.expire(
            tenant_id=tenant_id, now=_aware_utc(now), request_id=request_id
        )
        with self._repository.locked(request_id, tenant_id=tenant_id) as row:
            self._validate_resumable(row, now=now)
            assert row is not None
            if row.continuation_idempotency_key != idempotency_key:
                raise ValueError("factory action continuation acknowledgement mismatch")
            if row.resume_phase == "continuation_intent":
                acknowledged_at = _transition_time(row, now)
                row.continuation_acknowledged_at = acknowledged_at
                row.resume_phase = "continuation_acknowledged"
                row.status = "consumed"
                row.consumed_at = acknowledged_at
            elif row.resume_phase != "continuation_acknowledged":
                raise ValueError("factory action continuation is not pending")
            return _resume_record(row)

    @staticmethod
    def _validate_resumable(row: FactoryActionRow | None, *, now: datetime) -> None:
        if row is None:
            raise LookupError("factory action not found")
        if row.status == "expired":
            raise TimeoutError("factory action expired")
        if row.status not in {"answered", "consumed"} or row.response is None:
            raise ValueError("factory action is not answered")
        if row.request.deadline_at <= _aware_utc(now) and row.status != "consumed":
            raise TimeoutError("factory action expired")
        if row.resume_phase is None:
            raise FactoryActionDurabilityError("factory action durable row has no resume phase")


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
                        request_sha256 CHAR(64) NULL,
                        response_payload JSON NULL,
                        response_sha256 CHAR(64) NULL,
                        resume_phase VARCHAR(32) NULL,
                        projection_idempotency_key CHAR(64) NULL,
                        projection_intent_at DATETIME(6) NULL,
                        projection_acknowledged_at DATETIME(6) NULL,
                        continuation_idempotency_key CHAR(64) NULL,
                        continuation_intent_at DATETIME(6) NULL,
                        continuation_acknowledged_at DATETIME(6) NULL,
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
                for definition in (
                    "request_sha256 CHAR(64) NULL",
                    "resume_phase VARCHAR(32) NULL",
                    "projection_idempotency_key CHAR(64) NULL",
                    "projection_intent_at DATETIME(6) NULL",
                    "projection_acknowledged_at DATETIME(6) NULL",
                    "continuation_idempotency_key CHAR(64) NULL",
                    "continuation_intent_at DATETIME(6) NULL",
                    "continuation_acknowledged_at DATETIME(6) NULL",
                ):
                    cursor.execute(
                        f"ALTER TABLE factory_action_inbox ADD COLUMN IF NOT EXISTS {definition}"
                    )
                cursor.execute("SELECT * FROM factory_action_inbox FOR UPDATE")
                for persisted in cursor.fetchall():
                    normalized = _normalize_legacy_db_row(
                        persisted, backfill_phase_timestamps=True
                    )
                    if normalized == persisted:
                        continue
                    cursor.execute(
                        """UPDATE factory_action_inbox SET request_payload = %s,
                           request_sha256 = %s, response_payload = %s,
                           response_sha256 = %s, resume_phase = %s,
                           projection_idempotency_key = %s,
                           projection_intent_at = %s, projection_acknowledged_at = %s,
                           continuation_idempotency_key = %s,
                           continuation_intent_at = %s, continuation_acknowledged_at = %s
                           WHERE request_id = %s""",
                        (
                            normalized["request_payload"], normalized["request_sha256"],
                            normalized.get("response_payload"), normalized.get("response_sha256"),
                            normalized.get("resume_phase"), normalized.get("projection_idempotency_key"),
                            normalized.get("projection_intent_at"),
                            normalized.get("projection_acknowledged_at"),
                            normalized.get("continuation_idempotency_key"),
                            normalized.get("continuation_intent_at"),
                            normalized.get("continuation_acknowledged_at"),
                            normalized["request_id"],
                        ),
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
                        request_payload, request_sha256, response_payload,
                        response_sha256, resume_phase, consumed_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s,
                               %s, NULL, NULL, NULL, NULL)""",
                    (
                        str(request.request_id), row.tenant_id,
                        str(request.factory_job_id), str(request.creation_job_id),
                        str(request.correlation_id), request.expected_subject_version,
                        request.safe_checkpoint_sha256, _naive_utc(request.deadline_at),
                        _canonical_request_payload(request, row.tenant_id).decode("utf-8"),
                        hashlib.sha256(_canonical(request)).hexdigest(),
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

    def resume_rows(self) -> tuple[FactoryActionRow, ...]:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM factory_action_inbox WHERE status = 'answered' "
                    "ORDER BY updated_at, request_id"
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
                           resume_phase = %s, projection_idempotency_key = %s,
                           projection_intent_at = %s, projection_acknowledged_at = %s,
                           continuation_idempotency_key = %s,
                           continuation_intent_at = %s,
                           continuation_acknowledged_at = %s, consumed_at = %s
                           WHERE request_id = %s AND tenant_id = %s""",
                        (
                            row.status,
                            None if row.response is None else _canonical(row.response).decode("utf-8"),
                            row.response_sha256,
                            row.resume_phase,
                            row.projection_idempotency_key,
                            None if row.projection_intent_at is None else _naive_utc(row.projection_intent_at),
                            None if row.projection_acknowledged_at is None else _naive_utc(row.projection_acknowledged_at),
                            row.continuation_idempotency_key,
                            None if row.continuation_intent_at is None else _naive_utc(row.continuation_intent_at),
                            None if row.continuation_acknowledged_at is None else _naive_utc(row.continuation_acknowledged_at),
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
            "AND status IN ('pending', 'answered') AND deadline_at <= %s"
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


def _normalize_legacy_db_row(
    row: dict[str, object], *, backfill_phase_timestamps: bool = False
) -> dict[str, object]:
    """Compatibility seam for pre-resume Action Inbox rows."""
    normalized = dict(row)
    request_bytes = _json_bytes(normalized["request_payload"])
    try:
        decoded = json.loads(request_bytes)
    except json.JSONDecodeError as error:
        raise FactoryActionDurabilityError(
            "factory action durable row request invalid"
        ) from error
    if isinstance(decoded, dict) and set(decoded) == {"request", "tenant_id"}:
        return (
            _backfill_phase_timestamps(normalized)
            if backfill_phase_timestamps
            else normalized
        )
    try:
        request = FactoryActionRequestV1.model_validate(decoded)
    except (TypeError, ValueError) as error:
        raise FactoryActionDurabilityError(
            "factory action durable row request invalid"
        ) from error
    tenant_id = str(normalized.get("tenant_id", ""))
    if not tenant_id:
        raise FactoryActionDurabilityError("factory action durable row tenant mismatch")
    request_digest = hashlib.sha256(_canonical(request)).hexdigest()
    existing_request_digest = normalized.get("request_sha256")
    if existing_request_digest not in {None, request_digest}:
        raise FactoryActionDurabilityError(
            "factory action durable row request hash mismatch"
        )
    response_payload = normalized.get("response_payload")
    response_digest: str | None = None
    if response_payload is not None:
        try:
            response = FactoryActionResponseV1.model_validate_json(
                _json_bytes(response_payload)
            )
        except ValueError as error:
            raise FactoryActionDurabilityError(
                "factory action durable row response invalid"
            ) from error
        response_payload = _canonical(response).decode("utf-8")
        response_digest = hashlib.sha256(response_payload.encode("utf-8")).hexdigest()
        existing_response_digest = normalized.get("response_sha256")
        if existing_response_digest not in {None, response_digest}:
            raise FactoryActionDurabilityError(
                "factory action durable row response hash mismatch"
            )
    status = str(normalized.get("status"))
    consumed_at = normalized.get("consumed_at")
    normalized.update(
        {
            "request_payload": _canonical_request_payload(request, tenant_id).decode("utf-8"),
            "request_sha256": request_digest,
            "response_payload": response_payload,
            "response_sha256": response_digest,
        }
    )
    if status == "answered" and response_digest is not None:
        normalized["resume_phase"] = "answered"
    elif status == "consumed" and response_digest is not None and consumed_at is not None:
        normalized.update(
            {
                "resume_phase": "continuation_acknowledged",
                "projection_idempotency_key": _effect_key(
                    request, "projection", response_digest
                ),
                "projection_intent_at": consumed_at,
                "projection_acknowledged_at": consumed_at,
                "continuation_idempotency_key": _effect_key(
                    request, "continuation", response_digest
                ),
                "continuation_intent_at": consumed_at,
                "continuation_acknowledged_at": consumed_at,
            }
        )
    elif status not in {"pending", "expired"}:
        raise FactoryActionDurabilityError(
            "factory action durable legacy row state mismatch"
        )
    return (
        _backfill_phase_timestamps(normalized)
        if backfill_phase_timestamps
        else normalized
    )


def _backfill_phase_timestamps(row: dict[str, object]) -> dict[str, object]:
    """Backfill intent timestamps for rows created before the exact phase clock."""

    normalized = dict(row)
    phase = normalized.get("resume_phase")
    response_payload = normalized.get("response_payload")
    if phase in {None, "answered"} or response_payload is None:
        return normalized
    try:
        response = FactoryActionResponseV1.model_validate_json(
            _json_bytes(response_payload)
        )
    except ValueError as error:
        raise FactoryActionDurabilityError(
            "factory action durable row response invalid"
        ) from error
    answered_at = _naive_utc(response.answered_at)
    projection_acknowledged_at = normalized.get("projection_acknowledged_at")
    continuation_acknowledged_at = normalized.get("continuation_acknowledged_at")
    if normalized.get("projection_intent_at") is None:
        normalized["projection_intent_at"] = (
            projection_acknowledged_at or answered_at
        )
    if phase in {"continuation_intent", "continuation_acknowledged"} and normalized.get(
        "continuation_intent_at"
    ) is None:
        normalized["continuation_intent_at"] = (
            continuation_acknowledged_at
            or projection_acknowledged_at
            or normalized["projection_intent_at"]
        )
    return normalized


def _db_row(row: dict[str, object]) -> FactoryActionRow:
    row = _normalize_legacy_db_row(row)
    request_bytes = _json_bytes(row["request_payload"])
    try:
        envelope = json.loads(request_bytes)
        if not isinstance(envelope, dict) or set(envelope) != {"request", "tenant_id"}:
            raise ValueError("invalid request envelope")
        request = FactoryActionRequestV1.model_validate(envelope["request"])
        payload_tenant = str(envelope["tenant_id"])
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise FactoryActionDurabilityError("factory action durable row request invalid") from error
    request_canonical = _canonical(request)
    response_payload = row.get("response_payload")
    try:
        response = (
            None
            if response_payload is None
            else FactoryActionResponseV1.model_validate_json(_json_bytes(response_payload))
        )
    except ValueError as error:
        raise FactoryActionDurabilityError(
            "factory action durable row response invalid"
        ) from error
    response_canonical = None if response is None else _canonical(response)
    consumed = row.get("consumed_at")
    status = str(row["status"])
    phase = row.get("resume_phase")
    expected_request = (
        str(request.request_id),
        str(request.factory_job_id),
        str(request.creation_job_id),
        str(request.correlation_id),
        request.expected_subject_version,
        request.safe_checkpoint_sha256,
        _naive_utc(request.deadline_at),
    )
    actual_request = (
        str(row["request_id"]),
        str(row["factory_job_id"]),
        str(row["creation_job_id"]),
        str(row["correlation_id"]),
        int(row["expected_subject_version"]),
        str(row["safe_checkpoint_sha256"]),
        _naive_utc(row["deadline_at"]),  # type: ignore[arg-type]
    )
    request_digest = hashlib.sha256(request_canonical).hexdigest()
    response_digest = None if response_canonical is None else hashlib.sha256(response_canonical).hexdigest()
    valid_state = (
        (status == "pending" and response is None and phase is None)
        or (status == "answered" and response is not None and phase in {
            "answered", "projection_intent", "projection_acknowledged", "continuation_intent"
        })
        or (status == "consumed" and response is not None and phase == "continuation_acknowledged")
        or (
            status == "expired"
            and (
                (response is None and phase is None)
                or (
                    response is not None
                    and phase in {
                        "answered",
                        "projection_intent",
                        "projection_acknowledged",
                        "continuation_intent",
                        "continuation_acknowledged",
                    }
                )
            )
        )
    )
    if (
        request_bytes != _canonical_request_payload(request, payload_tenant)
        or str(row.get("request_sha256")) != request_digest
        or payload_tenant != str(row["tenant_id"])
        or actual_request != expected_request
        or (response_payload is not None and _json_bytes(response_payload) != response_canonical)
        or (None if row.get("response_sha256") is None else str(row["response_sha256"])) != response_digest
        or not valid_state
    ):
        raise FactoryActionDurabilityError("factory action durable row integrity mismatch")
    if response is not None:
        try:
            validate_action_response_binding(request, response)
        except ValueError as error:
            raise FactoryActionDurabilityError(
                "factory action durable row response binding mismatch"
            ) from error
    if response is not None and response.credential_ref is not None:
        tenant_from_response = response.credential_ref.removeprefix("credential://").split("/", 1)[0]
        if tenant_from_response != str(row["tenant_id"]):
            raise FactoryActionDurabilityError("factory action durable row tenant mismatch")
    result = FactoryActionRow(
        request=request,
        tenant_id=str(row["tenant_id"]),
        created_at=_optional_aware(row.get("created_at")),
        status=status,  # type: ignore[arg-type]
        response=response,
        response_sha256=(None if row.get("response_sha256") is None else str(row["response_sha256"])),
        resume_phase=(None if phase is None else str(phase)),  # type: ignore[arg-type]
        projection_idempotency_key=(None if row.get("projection_idempotency_key") is None else str(row["projection_idempotency_key"])),
        projection_intent_at=_optional_aware(row.get("projection_intent_at")),
        projection_acknowledged_at=_optional_aware(row.get("projection_acknowledged_at")),
        continuation_idempotency_key=(None if row.get("continuation_idempotency_key") is None else str(row["continuation_idempotency_key"])),
        continuation_intent_at=_optional_aware(row.get("continuation_intent_at")),
        continuation_acknowledged_at=_optional_aware(row.get("continuation_acknowledged_at")),
        consumed_at=(None if consumed is None else _aware_utc(consumed)),  # type: ignore[arg-type]
    )
    expected_projection_key = (
        None
        if result.resume_phase in {None, "answered"}
        else _effect_key(result.request, "projection", result.response_sha256)
    )
    expected_continuation_key = (
        _effect_key(result.request, "continuation", result.response_sha256)
        if result.resume_phase in {"continuation_intent", "continuation_acknowledged"}
        else None
    )
    if (
        result.projection_idempotency_key != expected_projection_key
        or result.continuation_idempotency_key != expected_continuation_key
    ):
        raise FactoryActionDurabilityError(
            "factory action durable row effect key mismatch"
        )
    if result.tenant_id == "" or not _valid_phase_columns(result):
        raise FactoryActionDurabilityError("factory action durable row phase mismatch")
    if not _valid_phase_timestamps(result):
        raise FactoryActionDurabilityError("factory action durable row timestamp mismatch")
    return result


def _effect_key(
    request: FactoryActionRequestV1,
    phase: Literal["projection", "continuation"],
    response_sha256: str | None,
) -> str:
    if response_sha256 is None:
        raise FactoryActionDurabilityError("factory action durable row response hash missing")
    return hashlib.sha256(
        json.dumps(
            {
                "phase": phase,
                "request_sha256": hashlib.sha256(_canonical(request)).hexdigest(),
                "response_sha256": response_sha256,
                "safe_checkpoint_sha256": request.safe_checkpoint_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _canonical_request_payload(request: FactoryActionRequestV1, tenant_id: str) -> bytes:
    return json.dumps(
        {
            "request": request.model_dump(mode="json", by_alias=True),
            "tenant_id": tenant_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _resume_record(row: FactoryActionRow) -> FactoryActionResumeRecord:
    if (
        row.response is None
        or row.resume_phase is None
        or row.projection_idempotency_key is None
    ):
        raise FactoryActionDurabilityError("factory action durable row resume state incomplete")
    continuation_key = row.continuation_idempotency_key or _effect_key(
        row.request, "continuation", row.response_sha256
    )
    return FactoryActionResumeRecord(
        request=row.request,
        response=row.response,
        phase=row.resume_phase,
        projection_idempotency_key=row.projection_idempotency_key,
        continuation_idempotency_key=continuation_key,
    )


def _optional_aware(value: object) -> datetime | None:
    return None if value is None else _aware_utc(value)  # type: ignore[arg-type]


def _valid_phase_columns(row: FactoryActionRow) -> bool:
    projection_key = row.projection_idempotency_key
    projection_intent_at = row.projection_intent_at
    projection_at = row.projection_acknowledged_at
    continuation_key = row.continuation_idempotency_key
    continuation_intent_at = row.continuation_intent_at
    continuation_at = row.continuation_acknowledged_at
    consumed_at = row.consumed_at
    if row.resume_phase is None:
        return (
            row.status in {"pending", "expired"}
            and row.response is None
            and row.response_sha256 is None
            and projection_key is None
            and projection_intent_at is None
            and projection_at is None
            and continuation_key is None
            and continuation_intent_at is None
            and continuation_at is None
            and consumed_at is None
        )
    if row.resume_phase == "answered":
        return (
            row.status in {"answered", "expired"}
            and row.response is not None
            and projection_key is None
            and projection_intent_at is None
            and projection_at is None
            and continuation_key is None
            and continuation_intent_at is None
            and continuation_at is None
            and consumed_at is None
        )
    if row.resume_phase == "projection_intent":
        return (
            row.status in {"answered", "expired"} and projection_key is not None
            and projection_intent_at is not None
            and projection_at is None and continuation_key is None
            and continuation_intent_at is None
            and continuation_at is None and consumed_at is None
        )
    if row.resume_phase == "projection_acknowledged":
        return (
            row.status in {"answered", "expired"} and projection_key is not None
            and projection_intent_at is not None
            and projection_at is not None and continuation_key is None
            and continuation_intent_at is None
            and continuation_at is None and consumed_at is None
        )
    if row.resume_phase == "continuation_intent":
        return (
            row.status in {"answered", "expired"} and projection_key is not None
            and projection_at is not None and continuation_key is not None
            and projection_intent_at is not None and continuation_intent_at is not None
            and continuation_at is None and consumed_at is None
        )
    if row.resume_phase == "continuation_acknowledged":
        return (
            row.status in {"consumed", "expired"} and projection_key is not None
            and projection_at is not None and continuation_key is not None
            and projection_intent_at is not None and continuation_intent_at is not None
            and continuation_at is not None
            and (
                (row.status == "consumed" and consumed_at is not None)
                or (row.status == "expired" and consumed_at is None)
            )
        )
    return False


def _valid_phase_timestamps(row: FactoryActionRow) -> bool:
    if row.response is None:
        return True
    timestamps = tuple(
        timestamp
        for timestamp in (
            row.response.answered_at,
            row.projection_intent_at,
            row.projection_acknowledged_at,
            row.continuation_intent_at,
            row.continuation_acknowledged_at,
        )
        if timestamp is not None
    )
    if any(left > right for left, right in zip(timestamps, timestamps[1:])):
        return False
    if timestamps and timestamps[-1] >= row.request.deadline_at:
        return False
    if row.resume_phase == "continuation_acknowledged":
        return (
            row.consumed_at == row.continuation_acknowledged_at
            if row.status == "consumed"
            else row.status == "expired" and row.consumed_at is None
        )
    return row.consumed_at is None


def _transition_time(row: FactoryActionRow, now: datetime) -> datetime:
    transition_at = _aware_utc(now)
    if row.response is None:
        raise FactoryActionDurabilityError("factory action durable row response missing")
    prior = next(
        (
            timestamp
            for timestamp in (
                row.continuation_acknowledged_at,
                row.continuation_intent_at,
                row.projection_acknowledged_at,
                row.projection_intent_at,
                row.response.answered_at,
            )
            if timestamp is not None
        ),
        row.response.answered_at,
    )
    if transition_at < prior or transition_at > row.request.deadline_at:
        raise FactoryActionDurabilityError(
            "factory action durable row timestamp mismatch"
        )
    return transition_at


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


def _exact_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"factory action {field} must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


def _naive_utc(value: datetime) -> datetime:
    return _aware_utc(value).replace(tzinfo=None)
