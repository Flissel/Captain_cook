"""Append-only Gateway repository and authority source for capability resume."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Any, TypeVar
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel

from gateway.capability_live_pipeline import CapabilityResumeExecutionRequest
from gateway.capability_resume_contracts import (
    CapabilityResumeAuthorizationRecordV1,
    CapabilityResumeDispatchStartedV1,
    CapabilityResumeSourceSnapshotV1,
    CapabilityResumeTerminalV1,
)
from gateway.capability_resume_service import (
    CapabilityResumeConflict,
    CapabilityResumeNotFound,
    CapabilityResumeUnavailable,
)
from gateway.store import GatewayStore


AUTHORIZATION_BLOCK = "capability_resume_authorization"
DISPATCH_BLOCK = "capability_resume_dispatch_started"
TERMINAL_BLOCK = "capability_resume_terminal"
ResumeRecord = TypeVar("ResumeRecord", bound=BaseModel)


class GatewayCapabilityResumeRepository:
    """Persist resume records in the existing hash-chained Gateway ledger."""

    def __init__(self, store: GatewayStore) -> None:
        self._store = store

    def authorization_for_reservation(
        self, reservation_id: UUID
    ) -> CapabilityResumeAuthorizationRecordV1 | None:
        row = self._find(
            AUTHORIZATION_BLOCK,
            "$.request.reservation_id",
            str(reservation_id),
        )
        return self._parse(row, CapabilityResumeAuthorizationRecordV1)

    def authorization(
        self, receipt_id: UUID
    ) -> CapabilityResumeAuthorizationRecordV1 | None:
        row = self._find(
            AUTHORIZATION_BLOCK,
            "$.authorization.authorization_receipt_id",
            str(receipt_id),
        )
        return self._parse(row, CapabilityResumeAuthorizationRecordV1)

    def record_authorization(
        self, record: CapabilityResumeAuthorizationRecordV1
    ) -> CapabilityResumeAuthorizationRecordV1:
        persisted, _ = self._append_unique(
            AUTHORIZATION_BLOCK,
            record,
            keys=(
                ("$.request.reservation_id", str(record.request.reservation_id)),
                (
                    "$.authorization.authorization_receipt_id",
                    str(record.authorization.authorization_receipt_id),
                ),
            ),
            model=CapabilityResumeAuthorizationRecordV1,
        )
        return persisted

    def dispatch_started(self, receipt_id: UUID) -> bool:
        return self._find(
            DISPATCH_BLOCK,
            "$.authorization_receipt_id",
            str(receipt_id),
        ) is not None

    def record_dispatch_started(self, receipt_id: UUID) -> bool:
        authorization = self.authorization(receipt_id)
        if authorization is None:
            raise CapabilityResumeNotFound(
                "capability resume authorization not found"
            )
        record = CapabilityResumeDispatchStartedV1(
            authorization_receipt_id=receipt_id,
            execution_command_id=authorization.resume_command.event_id,
        )
        _, replayed = self._append_unique(
            DISPATCH_BLOCK,
            record,
            keys=(("$.authorization_receipt_id", str(receipt_id)),),
            model=CapabilityResumeDispatchStartedV1,
        )
        return not replayed

    def terminal(self, receipt_id: UUID) -> CapabilityResumeTerminalV1 | None:
        row = self._find(
            TERMINAL_BLOCK,
            "$.authorization_receipt_id",
            str(receipt_id),
        )
        return self._parse(row, CapabilityResumeTerminalV1)

    def record_terminal(
        self, terminal: CapabilityResumeTerminalV1
    ) -> CapabilityResumeTerminalV1:
        if not self.dispatch_started(terminal.authorization_receipt_id):
            raise CapabilityResumeConflict(
                "capability resume terminal has no dispatch fence"
            )
        persisted, _ = self._append_unique(
            TERMINAL_BLOCK,
            terminal,
            keys=(
                (
                    "$.authorization_receipt_id",
                    str(terminal.authorization_receipt_id),
                ),
            ),
            model=CapabilityResumeTerminalV1,
        )
        return persisted

    def _find(
        self,
        block_type: str,
        json_path: str,
        value: str,
    ) -> dict[str, Any] | None:
        with self._store.storage.transaction() as connection:
            with connection.cursor() as cursor:
                return self._find_with_cursor(
                    cursor,
                    block_type,
                    json_path,
                    value,
                    for_update=False,
                )

    def _append_unique(
        self,
        block_type: str,
        record: ResumeRecord,
        *,
        keys: tuple[tuple[str, str], ...],
        model: type[ResumeRecord],
    ) -> tuple[ResumeRecord, bool]:
        canonical = record.model_dump(mode="json", by_alias=True)
        lock_seed = f"{block_type}:{keys[0][1]}".encode("utf-8")
        lock_name = f"captain:cap-resume:{hashlib.sha256(lock_seed).hexdigest()[:32]}"
        with self._record_lock(lock_name):
            with self._store.storage.transaction() as connection:
                with connection.cursor() as cursor:
                    existing_rows = tuple(
                        self._find_with_cursor(
                            cursor,
                            block_type,
                            path,
                            value,
                            for_update=True,
                        )
                        for path, value in keys
                    )
                    existing = next(
                        (row for row in existing_rows if row is not None), None
                    )
                    if existing is not None:
                        if any(
                            row != existing
                            for row in existing_rows
                            if row is not None
                        ):
                            raise CapabilityResumeConflict(
                                "capability resume stable identities resolve differently"
                            )
                        parsed = model.model_validate(existing["data"])
                        if parsed != record:
                            raise CapabilityResumeConflict(
                                "capability resume immutable record does not match"
                            )
                        return parsed, True

                    index = self._store._next_index(cursor)
                    block = self._store._new_block(
                        cursor,
                        index=index,
                        block_type=block_type,
                        data=canonical,
                        status="recorded",
                        parent_index=None,
                        metadata={"schema": canonical["schema"]},
                    )
                    self._store._insert(cursor, block)
            return record, False

    @contextmanager
    def _record_lock(self, lock_name: str):
        with self._store.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT GET_LOCK(%s, 30) AS acquired", (lock_name,))
                row = cursor.fetchone()
                if row is None or int(row["acquired"] or 0) != 1:
                    raise CapabilityResumeConflict(
                        "capability resume authority is busy"
                    )
                try:
                    yield
                finally:
                    cursor.execute(
                        "SELECT RELEASE_LOCK(%s) AS released", (lock_name,)
                    )

    def _find_with_cursor(
        self,
        cursor: Any,
        block_type: str,
        json_path: str,
        value: str,
        *,
        for_update: bool,
    ) -> dict[str, Any] | None:
        sql = """
            SELECT `index`, parent_index, block_type, data, status, children,
                   metadata, hash, previous_hash
            FROM blocks
            WHERE block_type = %s
              AND JSON_UNQUOTE(JSON_EXTRACT(data, %s)) = %s
            ORDER BY `index` LIMIT 1
        """
        if for_update:
            sql += " FOR UPDATE"
        cursor.execute(sql, (block_type, json_path, value))
        row = cursor.fetchone()
        return self._store.storage._decode_row(row) if row is not None else None

    @staticmethod
    def _parse(
        row: dict[str, Any] | None,
        model: type[ResumeRecord],
    ) -> ResumeRecord | None:
        return model.model_validate(row["data"]) if row is not None else None


class GatewayCapabilityResumeAuthoritySource:
    """Resolve resume inputs only from current Gateway projections."""

    def __init__(self, store: GatewayStore) -> None:
        self._store = store

    def resolve(
        self, request: CapabilityResumeExecutionRequest
    ) -> CapabilityResumeSourceSnapshotV1:
        try:
            job_projection = self._store.factory_job(request.job_id)
            operation = self._store.runtime_operation(
                request.original_execution_command_id
            )
            budget = self._store.factory_budget(request.job_id)
            artifacts = self._store.factory_workflow_artifacts(request.job_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                raise CapabilityResumeUnavailable(
                    "capability resume Gateway authority is unavailable"
                ) from None
            raise CapabilityResumeConflict(str(exc.detail)) from None

        job = job_projection.job
        result = operation.result
        matching_outcomes = tuple(
            outcome
            for artifact in artifacts
            for outcome in (getattr(artifact, "execution_outcome", None),)
            if outcome is not None
            and outcome.command_id == request.original_execution_command_id
            and outcome.result_id == request.original_execution_result_id
        )
        if len(matching_outcomes) != 1:
            raise CapabilityResumeConflict(
                "original execution is not an unambiguous Factory outcome"
            )
        outcome = matching_outcomes[0]
        if (
            job.correlation_id != request.correlation_id
            or job.required_capability != request.capability_id
            or job.subject_version != request.capability_version
            or operation.operation_id != request.original_execution_command_id
            or result is None
            or result.event_id != request.original_execution_result_id
            or outcome.correlation_id != request.correlation_id
            or outcome.capability_id != request.capability_id
            or outcome.capability_version != request.capability_version
        ):
            raise CapabilityResumeConflict(
                "capability resume request binding does not match Gateway truth"
            )
        return CapabilityResumeSourceSnapshotV1(
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            capability_id=job.required_capability,
            capability_version=job.subject_version,
            booked_cost_usd=budget.consumed_usd,
            original_operation=operation,
        )

    def runtime_operation(self, command_id: UUID):
        try:
            return self._store.runtime_operation(command_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return None
            raise CapabilityResumeConflict(str(exc.detail)) from None


__all__ = [
    "GatewayCapabilityResumeAuthoritySource",
    "GatewayCapabilityResumeRepository",
]
