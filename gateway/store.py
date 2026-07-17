"""MariaDB-backed append-only persistence for the gateway lifecycle."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol, TypeVar
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError
from pymysql.err import OperationalError

from agenten.validation.contracts import HoldoutSuite, WorkBatch
from agenten.agent_runtime.capabilities import CapabilityDenied, validate_grant
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    CapabilityGrant,
)
from blockchain.Blockchain_modell import Block
from blockchain.mariadb_storage import MariaDBStorage
from gateway.contracts import (
    ArtifactBuiltPayload,
    BatchDoneEvent,
    BatchProjection,
    ClaimEvent,
    CodexProcessEvent,
    DeliveryEventEnvelope,
    HeartbeatEvent,
    ReasoningSliceEvent,
    RecoveryDecisionEvent,
    ReleaseProjection,
    RuntimeOperationProjection,
    RuntimeWriteReceipt,
    ReviewDecisionEvent,
    project_batch,
    project_release,
)


CAPTAIN_BLOCK_TYPES = frozenset({"problem", "work_batch", "holdout"})
GATEWAY_OWNED_EVENT_TYPES = frozenset(
    {"batch_claimed", "batch_heartbeat", "batch_approved", "recovery_decision", "review_decision"}
)
TRANSIENT_TRANSACTION_ERRORS = frozenset({1020, 1213})
TRANSACTION_ATTEMPTS = 3
WriteResult = TypeVar("WriteResult")


class BlockWrite(Protocol):
    block_type: str
    data: dict[str, Any]
    status: str
    parent_index: int | None
    metadata: dict[str, Any]


class LegacyImportWrite(Protocol):
    legacy_record_id: str
    batch_id: str
    record_type: str
    data: dict[str, Any]


class _IdempotentReplay(Exception):
    def __init__(self, block: dict[str, Any]):
        super().__init__("identical Captain block replay")
        self.block = block


@dataclass(frozen=True)
class AppendResult:
    event: DeliveryEventEnvelope
    replayed: bool


class _DeliveryEventReplay(Exception):
    def __init__(self, event: DeliveryEventEnvelope):
        super().__init__("identical delivery event replay")
        self.event = event


class _RuntimeReplay(Exception):
    def __init__(self, operation_id: UUID):
        super().__init__("identical runtime write replay")
        self.operation_id = operation_id


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GatewayStore:
    """Own all gateway queries and append-only ledger writes."""

    def __init__(self, storage: MariaDBStorage):
        self.storage = storage
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ledger_state (
                        id TINYINT NOT NULL PRIMARY KEY,
                        next_block_index BIGINT NOT NULL
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute("INSERT IGNORE INTO ledger_state (id, next_block_index) VALUES (1, 0)")
                cursor.execute("SELECT COALESCE(MAX(`index`) + 1, 0) AS next_index FROM blocks")
                next_index = cursor.fetchone()["next_index"]
                cursor.execute(
                    "UPDATE ledger_state SET next_block_index = GREATEST(next_block_index, %s) WHERE id = 1",
                    (next_index,),
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS validated_capabilities (
                        batch_id VARCHAR(32) NOT NULL PRIMARY KEY,
                        descriptor TEXT NOT NULL,
                        artifact_ref TEXT NULL,
                        block_index BIGINT NOT NULL,
                        payload JSON NOT NULL,
                        created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                        FULLTEXT INDEX idx_capability_descriptor (descriptor),
                        CONSTRAINT fk_capability_block
                            FOREIGN KEY (block_index) REFERENCES blocks (`index`) ON DELETE CASCADE
                    ) ENGINE=InnoDB
                    """
                )

    def append_delivery_event(
        self,
        event: DeliveryEventEnvelope,
        *,
        require_current_claim: bool = False,
    ) -> AppendResult:
        try:
            stored = self._retry_write(
                lambda: self._append_delivery_event_once(
                    event,
                    require_current_claim=require_current_claim,
                )
            )
            return AppendResult(event=stored, replayed=False)
        except _DeliveryEventReplay as replay:
            return AppendResult(event=replay.event, replayed=True)

    def _append_delivery_event_once(
        self,
        event: DeliveryEventEnvelope,
        *,
        require_current_claim: bool,
    ) -> DeliveryEventEnvelope:
        canonical = event.model_dump(mode="json")
        trace = event.trace
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                index = self._next_index(cursor)
                batch_id = trace.batch_id
                if require_current_claim:
                    if batch_id is None:
                        raise HTTPException(
                            status_code=409,
                            detail="delivery event must match the current claim",
                        )
                    _, _, projection = self._batch_context(
                        cursor,
                        batch_id,
                        for_update=True,
                        now=_utcnow(),
                    )
                    if (
                        projection.status != "claimed"
                        or trace.claim_id != projection.claim_id
                        or trace.fencing_token != projection.fencing_token
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail="delivery event must match the current claim",
                        )
                cursor.execute(
                    """
                    SELECT data FROM blocks
                    WHERE block_type = 'delivery_event'
                      AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.event_id')) = %s
                    ORDER BY `index` LIMIT 1 FOR UPDATE
                    """,
                    (str(event.event_id),),
                )
                existing_row = cursor.fetchone()
                if existing_row is not None:
                    existing_data = existing_row["data"]
                    if isinstance(existing_data, str):
                        existing_data = json.loads(existing_data)
                    existing = DeliveryEventEnvelope.model_validate(existing_data)
                    if existing == event:
                        raise _DeliveryEventReplay(existing)
                    raise HTTPException(
                        status_code=409,
                        detail="event_id already exists with different content",
                    )

                if batch_id is not None:
                    cursor.execute(
                        """
                        SELECT data FROM blocks
                        WHERE block_type = 'delivery_event'
                          AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.trace.project_id')) = %s
                          AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.trace.run_id')) = %s
                          AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.trace.batch_id')) = %s
                        ORDER BY `index` FOR UPDATE
                        """,
                        (trace.project_id, trace.run_id, batch_id),
                    )
                    prior_tokens: list[int] = []
                    for row in cursor.fetchall():
                        data = row["data"]
                        if isinstance(data, str):
                            data = json.loads(data)
                        token = data.get("trace", {}).get("fencing_token")
                        if isinstance(token, int) and not isinstance(token, bool):
                            prior_tokens.append(token)
                    if prior_tokens and (
                        trace.fencing_token is None
                        or trace.fencing_token < max(prior_tokens)
                    ):
                        raise HTTPException(status_code=409, detail="stale fencing token")

                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="delivery_event",
                    data=canonical,
                    status="recorded",
                    parent_index=None,
                    metadata={"schema": "captain-delivery-event/v1"},
                )
                self._insert(cursor, block)
        return event

    def delivery_events(
        self,
        *,
        project_id: str,
        run_id: str,
    ) -> tuple[DeliveryEventEnvelope, ...]:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT data FROM blocks
                    WHERE block_type = 'delivery_event'
                      AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.trace.project_id')) = %s
                      AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.trace.run_id')) = %s
                    ORDER BY `index`
                    """,
                    (project_id, run_id),
                )
                rows = cursor.fetchall()
        return tuple(
            DeliveryEventEnvelope.model_validate(
                json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
            )
            for row in rows
        )

    def accept_runtime_command(
        self,
        command: AgentRuntimeCommand,
    ) -> RuntimeWriteReceipt:
        try:
            self._retry_write(lambda: self._accept_runtime_command_once(command))
        except _RuntimeReplay as replay:
            return RuntimeWriteReceipt(operation_id=replay.operation_id, replayed=True)
        return RuntimeWriteReceipt(operation_id=command.event_id, replayed=False)

    def _accept_runtime_command_once(self, command: AgentRuntimeCommand) -> None:
        canonical = command.model_dump(mode="json", by_alias=True)
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                index = self._next_index(cursor)
                existing = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_runtime_command",
                    field="event_id",
                    value=str(command.event_id),
                    for_update=True,
                )
                if existing is not None:
                    if existing["data"] == canonical:
                        raise _RuntimeReplay(command.event_id)
                    raise HTTPException(
                        status_code=409,
                        detail="runtime command event_id already has different content",
                    )

                cursor.execute(
                    """
                    SELECT MAX(
                        CAST(JSON_UNQUOTE(JSON_EXTRACT(data, '$.subject_version')) AS UNSIGNED)
                    ) AS max_version
                    FROM blocks
                    WHERE block_type = 'agent_runtime_command'
                      AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.subject_id')) = %s
                    FOR UPDATE
                    """,
                    (command.subject_id,),
                )
                row = cursor.fetchone()
                max_version = row["max_version"] if row is not None else None
                if max_version is not None and command.subject_version < int(max_version):
                    raise HTTPException(status_code=409, detail="stale runtime subject version")

                payload = command.payload
                if payload.batch_id is not None:
                    parent = self._batch_row(cursor, payload.batch_id, for_update=True)
                    if parent is None:
                        raise HTTPException(status_code=409, detail="released batch not found")
                    batch = WorkBatch.model_validate(parent["data"])
                    if payload.subtask_id not in batch.subtask_ids:
                        raise HTTPException(
                            status_code=409,
                            detail="runtime subtask was not released in the batch",
                        )
                    if payload.capability_profile.value not in batch.capability_tags:
                        raise HTTPException(
                            status_code=409,
                            detail="runtime capability profile was not released",
                        )

                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="agent_runtime_command",
                    data=canonical,
                    status="accepted",
                    parent_index=None,
                    metadata={"schema": "captain.agent-runtime-command.v1"},
                )
                self._insert(cursor, block)

    def record_capability_grant(
        self,
        grant: CapabilityGrant,
    ) -> RuntimeWriteReceipt:
        try:
            self._retry_write(lambda: self._record_capability_grant_once(grant))
        except _RuntimeReplay as replay:
            return RuntimeWriteReceipt(operation_id=replay.operation_id, replayed=True)
        return RuntimeWriteReceipt(operation_id=grant.command_id, replayed=False)

    def _record_capability_grant_once(self, grant: CapabilityGrant) -> None:
        canonical = grant.model_dump(mode="json", by_alias=True)
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                index = self._next_index(cursor)
                command_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_runtime_command",
                    field="event_id",
                    value=str(grant.command_id),
                    for_update=True,
                )
                if command_block is None:
                    raise HTTPException(status_code=409, detail="runtime command not found")
                command = AgentRuntimeCommand.model_validate(command_block["data"])
                try:
                    validate_grant(grant, command, grant.issued_at)
                except CapabilityDenied as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc

                existing = self._runtime_grant_block(cursor, grant, for_update=True)
                if existing is not None:
                    if existing["data"] == canonical:
                        raise _RuntimeReplay(grant.command_id)
                    raise HTTPException(
                        status_code=409,
                        detail="runtime command or grant already has different grant content",
                    )
                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="agent_runtime_grant",
                    data=canonical,
                    status="active",
                    parent_index=command_block["index"],
                    metadata={"schema": "captain.capability-grant.v1"},
                )
                self._insert(cursor, block)

    def record_runtime_result(
        self,
        result: AgentRuntimeResult,
    ) -> RuntimeWriteReceipt:
        try:
            self._retry_write(lambda: self._record_runtime_result_once(result))
        except _RuntimeReplay as replay:
            return RuntimeWriteReceipt(operation_id=replay.operation_id, replayed=True)
        return RuntimeWriteReceipt(operation_id=result.command_id, replayed=False)

    def _record_runtime_result_once(self, result: AgentRuntimeResult) -> None:
        canonical = result.model_dump(mode="json", by_alias=True)
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                index = self._next_index(cursor)
                command_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_runtime_command",
                    field="event_id",
                    value=str(result.command_id),
                    for_update=True,
                )
                if command_block is None:
                    raise HTTPException(status_code=409, detail="runtime command not found")
                command = AgentRuntimeCommand.model_validate(command_block["data"])
                self._assert_result_matches_command(result, command)

                grant_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_runtime_grant",
                    field="grant_id",
                    value=result.grant_id,
                    for_update=True,
                )
                if grant_block is None:
                    raise HTTPException(status_code=409, detail="runtime grant not found")
                grant = CapabilityGrant.model_validate(grant_block["data"])
                if grant.command_id != command.event_id:
                    raise HTTPException(
                        status_code=409,
                        detail="runtime grant belongs to a different command",
                    )

                existing = self._runtime_result_block(cursor, result, for_update=True)
                if existing is not None:
                    if existing["data"] == canonical:
                        raise _RuntimeReplay(result.command_id)
                    raise HTTPException(
                        status_code=409,
                        detail="runtime command or result event already has different content",
                    )
                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="agent_runtime_result",
                    data=canonical,
                    status=result.status.value,
                    parent_index=command_block["index"],
                    metadata={"schema": "captain.agent-runtime-result.v1"},
                )
                self._insert(cursor, block)

    def runtime_operation(self, operation_id: UUID) -> RuntimeOperationProjection:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                command_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_runtime_command",
                    field="event_id",
                    value=str(operation_id),
                )
                if command_block is None:
                    raise HTTPException(status_code=404, detail="runtime operation not found")
                grant_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_runtime_grant",
                    field="command_id",
                    value=str(operation_id),
                )
                result_block = self._runtime_block_by_json_value(
                    cursor,
                    block_type="agent_runtime_result",
                    field="command_id",
                    value=str(operation_id),
                )
        return RuntimeOperationProjection(
            operation_id=operation_id,
            command=AgentRuntimeCommand.model_validate(command_block["data"]),
            grant=(
                CapabilityGrant.model_validate(grant_block["data"])
                if grant_block is not None
                else None
            ),
            result=(
                AgentRuntimeResult.model_validate(result_block["data"])
                if result_block is not None
                else None
            ),
        )

    def _runtime_grant_block(
        self,
        cursor: Any,
        grant: CapabilityGrant,
        *,
        for_update: bool,
    ) -> dict[str, Any] | None:
        return self._runtime_block_by_two_json_values(
            cursor,
            block_type="agent_runtime_grant",
            first_field="grant_id",
            first_value=grant.grant_id,
            second_field="command_id",
            second_value=str(grant.command_id),
            for_update=for_update,
        )

    def _runtime_result_block(
        self,
        cursor: Any,
        result: AgentRuntimeResult,
        *,
        for_update: bool,
    ) -> dict[str, Any] | None:
        return self._runtime_block_by_two_json_values(
            cursor,
            block_type="agent_runtime_result",
            first_field="event_id",
            first_value=str(result.event_id),
            second_field="command_id",
            second_value=str(result.command_id),
            for_update=for_update,
        )

    def _runtime_block_by_json_value(
        self,
        cursor: Any,
        *,
        block_type: str,
        field: str,
        value: str,
        for_update: bool = False,
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
        cursor.execute(sql, (block_type, f"$.{field}", value))
        row = cursor.fetchone()
        return self.storage._decode_row(row) if row is not None else None

    def _runtime_block_by_two_json_values(
        self,
        cursor: Any,
        *,
        block_type: str,
        first_field: str,
        first_value: str,
        second_field: str,
        second_value: str,
        for_update: bool,
    ) -> dict[str, Any] | None:
        sql = """
            SELECT `index`, parent_index, block_type, data, status, children,
                   metadata, hash, previous_hash
            FROM blocks
            WHERE block_type = %s
              AND (
                JSON_UNQUOTE(JSON_EXTRACT(data, %s)) = %s
                OR JSON_UNQUOTE(JSON_EXTRACT(data, %s)) = %s
              )
            ORDER BY `index` LIMIT 1
        """
        if for_update:
            sql += " FOR UPDATE"
        cursor.execute(
            sql,
            (
                block_type,
                f"$.{first_field}",
                first_value,
                f"$.{second_field}",
                second_value,
            ),
        )
        row = cursor.fetchone()
        return self.storage._decode_row(row) if row is not None else None

    @staticmethod
    def _assert_result_matches_command(
        result: AgentRuntimeResult,
        command: AgentRuntimeCommand,
    ) -> None:
        if (
            result.command_id != command.event_id
            or result.correlation_id != command.correlation_id
            or result.subject_id != command.subject_id
            or result.subject_version != command.subject_version
            or result.operation is not command.payload.operation
        ):
            raise HTTPException(
                status_code=409,
                detail="runtime result does not match its command",
            )

    def release_projection(self, *, project_id: str, run_id: str) -> ReleaseProjection:
        return project_release(self.delivery_events(project_id=project_id, run_id=run_id))

    def delivery_holdout_case(
        self,
        *,
        project_id: str,
        run_id: str,
        case_id: str,
    ) -> dict[str, Any]:
        events = self.delivery_events(project_id=project_id, run_id=run_id)
        sealed_batches = {
            event.trace.batch_id
            for event in events
            if isinstance(event.payload, ArtifactBuiltPayload)
            and event.trace.batch_id is not None
        }
        if not sealed_batches:
            raise HTTPException(status_code=404, detail="holdout not released")

        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                for batch_id in sealed_batches:
                    cursor.execute(
                        """
                        SELECT data FROM blocks
                        WHERE block_type = 'holdout'
                          AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.batch_id')) = %s
                        ORDER BY `index` DESC LIMIT 1
                        """,
                        (batch_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        continue
                    data = row["data"]
                    if isinstance(data, str):
                        data = json.loads(data)
                    for case in data.get("cases", []):
                        if case.get("case_id") == case_id:
                            return dict(case)
        raise HTTPException(status_code=404, detail="holdout not found")

    def _batch_row(
        self,
        cursor: Any,
        batch_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        sql = """
            SELECT `index`, parent_index, block_type, data, status, children,
                   metadata, hash, previous_hash
            FROM blocks
            WHERE block_type = 'work_batch'
              AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.batch_id')) = %s
            ORDER BY `index` DESC LIMIT 1
        """
        if for_update:
            sql += " FOR UPDATE"
        cursor.execute(sql, (batch_id,))
        row = cursor.fetchone()
        return self.storage._decode_row(row) if row is not None else None

    def _row_by_index(self, cursor: Any, index: int) -> dict[str, Any] | None:
        cursor.execute(
            """
            SELECT `index`, parent_index, block_type, data, status, children,
                   metadata, hash, previous_hash
            FROM blocks
            WHERE `index` = %s
            """,
            (index,),
        )
        row = cursor.fetchone()
        return self.storage._decode_row(row) if row is not None else None

    def _child_rows(
        self,
        cursor: Any,
        parent_index: int,
        *,
        for_update: bool = False,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT `index`, parent_index, block_type, data, status, children,
                   metadata, hash, previous_hash
            FROM blocks
            WHERE parent_index = %s
            ORDER BY `index`
        """
        if for_update:
            sql += " FOR UPDATE"
        cursor.execute(sql, (parent_index,))
        return [self.storage._decode_row(row) for row in cursor.fetchall()]

    def _batch_context(
        self,
        cursor: Any,
        batch_id: str,
        *,
        for_update: bool = False,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], BatchProjection]:
        parent = self._batch_row(cursor, batch_id, for_update=for_update)
        if parent is None:
            raise HTTPException(status_code=404, detail="batch not found")
        children = self._child_rows(cursor, parent["index"], for_update=for_update)
        projection = project_batch([parent, *children], batch_id, now=now)
        return parent, children, projection

    @staticmethod
    def _next_index(cursor: Any) -> int:
        cursor.execute("SELECT next_block_index FROM ledger_state WHERE id = 1 FOR UPDATE")
        index = int(cursor.fetchone()["next_block_index"])
        cursor.execute("UPDATE ledger_state SET next_block_index = next_block_index + 1 WHERE id = 1")
        return index

    @staticmethod
    def _insert(cursor: Any, block: dict[str, Any]) -> None:
        cursor.execute(
            """
            INSERT INTO blocks
                (`index`, parent_index, block_type, data, status, children,
                 metadata, hash, previous_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                block["index"],
                block["parent_index"],
                block["block_type"],
                json.dumps(block["data"], sort_keys=True),
                block["status"],
                json.dumps(block["children"]),
                json.dumps(block["metadata"], sort_keys=True),
                block["hash"],
                block["previous_hash"],
            ),
        )

    def _new_block(
        self,
        cursor: Any,
        *,
        index: int,
        block_type: str,
        data: dict[str, Any],
        status: str,
        parent_index: int | None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cursor.execute("SELECT hash FROM blocks ORDER BY `index` DESC LIMIT 1 FOR UPDATE")
        previous = cursor.fetchone()
        return Block(
            index=index,
            block_type=block_type,
            data=data,
            status=status,
            previous_hash=previous["hash"] if previous else "0",
            parent_index=parent_index,
            metadata=metadata,
        ).to_dict()

    @staticmethod
    def _assert_live_claim(
        projection: BatchProjection,
        token: str | None,
        *,
        now: datetime,
    ) -> None:
        presented_hash = hashlib.sha256((token or "").encode("utf-8")).hexdigest()
        expires = projection.claim_expires_at
        if (
            projection.status != "claimed"
            or not token
            or projection.claim_token_sha256 is None
            or not secrets.compare_digest(projection.claim_token_sha256, presented_hash)
            or expires is None
            or expires <= now
        ):
            raise HTTPException(status_code=409, detail="invalid or expired claim token")

    @staticmethod
    def _validate_candidate(
        parent: dict[str, Any],
        children: list[dict[str, Any]],
        block: dict[str, Any],
        batch_id: str,
        *,
        now: datetime,
    ) -> None:
        try:
            project_batch([parent, *children, block], batch_id, now=now)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @staticmethod
    def _retry_write(operation: Callable[[], WriteResult]) -> WriteResult:
        for attempt in range(TRANSACTION_ATTEMPTS):
            try:
                return operation()
            except OperationalError as exc:
                error_code = exc.args[0] if exc.args else None
                if (
                    error_code not in TRANSIENT_TRANSACTION_ERRORS
                    or attempt == TRANSACTION_ATTEMPTS - 1
                ):
                    raise
        raise RuntimeError("unreachable transaction retry state")

    def append(self, request: BlockWrite, claim_token: str | None) -> dict[str, Any]:
        try:
            return self._retry_write(lambda: self._append_once(request, claim_token))
        except _IdempotentReplay as replay:
            return replay.block

    @staticmethod
    def _has_identical_canonical_data(
        existing: dict[str, Any],
        data: dict[str, Any],
    ) -> bool:
        return existing["data"] == data

    def _append_once(self, request: BlockWrite, claim_token: str | None) -> dict[str, Any]:
        block_type = request.block_type
        if block_type in GATEWAY_OWNED_EVENT_TYPES:
            raise HTTPException(
                status_code=422,
                detail=f"{block_type} must use its dedicated gateway route",
            )
        data = dict(request.data)
        try:
            if block_type == "work_batch":
                data = WorkBatch.model_validate(data).model_dump(mode="json")
            elif block_type == "holdout":
                data = HoldoutSuite.model_validate(data).model_dump(mode="json")
            elif block_type == "codex_process":
                data = CodexProcessEvent.model_validate(data).model_dump(mode="json")
            elif block_type == "reasoning_slice":
                data = ReasoningSliceEvent.model_validate(data).model_dump(mode="json")
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc

        batch_id = data.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id:
            raise HTTPException(status_code=422, detail="data.batch_id is required")

        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                index = self._next_index(cursor)
                parent_index = request.parent_index
                parent: dict[str, Any] | None = None
                children: list[dict[str, Any]] = []
                now = _utcnow()

                if block_type not in CAPTAIN_BLOCK_TYPES:
                    parent, children, projection = self._batch_context(
                        cursor,
                        batch_id,
                        for_update=True,
                        now=now,
                    )
                    self._assert_live_claim(projection, claim_token, now=now)
                    if parent_index is None:
                        parent_index = parent["index"]
                    elif parent_index != parent["index"]:
                        raise HTTPException(status_code=409, detail="parent belongs to another batch")

                if block_type == "work_batch":
                    if parent_index is not None:
                        raise HTTPException(status_code=422, detail="work_batch must be a root block")
                    existing_batch = self._batch_row(cursor, batch_id, for_update=True)
                    if existing_batch is not None and self._has_identical_canonical_data(
                        existing_batch,
                        data,
                    ):
                        raise _IdempotentReplay(existing_batch)
                    if existing_batch is not None:
                        raise HTTPException(status_code=409, detail="batch_id already exists")

                if block_type == "holdout" and parent_index is None:
                    raise HTTPException(status_code=422, detail="holdout requires its work_batch parent")

                if block_type == "holdout":
                    parent, children, _ = self._batch_context(
                        cursor,
                        batch_id,
                        for_update=True,
                        now=now,
                    )
                    if parent_index != parent["index"]:
                        raise HTTPException(status_code=409, detail="holdout parent must be its work_batch")
                    existing_holdout = next(
                        (child for child in children if child["block_type"] == "holdout"),
                        None,
                    )
                    if existing_holdout is not None and self._has_identical_canonical_data(
                        existing_holdout,
                        data,
                    ):
                        raise _IdempotentReplay(existing_holdout)
                    if existing_holdout is not None:
                        raise HTTPException(status_code=409, detail="holdout suite already exists")
                elif parent_index is not None and block_type in CAPTAIN_BLOCK_TYPES - {"work_batch"}:
                    referenced_parent = self._row_by_index(cursor, parent_index)
                    if referenced_parent is None:
                        raise HTTPException(status_code=404, detail="parent block not found")
                    if referenced_parent["data"].get("batch_id") != batch_id:
                        raise HTTPException(status_code=409, detail="parent belongs to another batch")

                if block_type == "batch_done":
                    try:
                        done = BatchDoneEvent.model_validate(data)
                    except ValidationError as exc:
                        raise HTTPException(status_code=422, detail=exc.errors()) from exc
                    if request.status != done.outcome:
                        raise HTTPException(status_code=422, detail="batch_done status must match outcome")

                block = self._new_block(
                    cursor,
                    index=index,
                    block_type=block_type,
                    data=data,
                    status=request.status,
                    parent_index=parent_index,
                    metadata=dict(request.metadata),
                )
                if block_type == "work_batch":
                    try:
                        project_batch([block], batch_id, now=now)
                    except ValueError as exc:
                        raise HTTPException(status_code=422, detail=str(exc)) from exc
                elif parent is not None:
                    self._validate_candidate(parent, children, block, batch_id, now=now)
                self._insert(cursor, block)
                if block_type == "batch_done" and data["outcome"] == "succeeded":
                    self._upsert_capability(cursor, block)
        return block

    def recover(self, request: RecoveryDecisionEvent) -> dict[str, Any]:
        try:
            return self._retry_write(lambda: self._recover_once(request))
        except _IdempotentReplay as replay:
            return replay.block

    def _recover_once(self, request: RecoveryDecisionEvent) -> dict[str, Any]:
        data = request.model_dump(mode="json")
        now = _utcnow()
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                parent, children, projection = self._batch_context(
                    cursor,
                    request.batch_id,
                    for_update=True,
                    now=now,
                )
                existing = next(
                    (
                        child
                        for child in children
                        if child["block_type"] == "recovery_decision"
                        and child["data"].get("iteration") == request.iteration
                    ),
                    None,
                )
                if existing is not None and self._has_identical_canonical_data(existing, data):
                    raise _IdempotentReplay(existing)
                if existing is not None:
                    raise HTTPException(status_code=409, detail="recovery decision already exists")
                if (
                    projection.status != "pending"
                    or projection.claim_iteration != request.iteration
                    or projection.claim_expires_at is None
                    or projection.claim_expires_at > now
                ):
                    raise HTTPException(status_code=409, detail="claim is not expired")
                block = self._new_block(
                    cursor,
                    index=self._next_index(cursor),
                    block_type="recovery_decision",
                    data=data,
                    status=request.decision,
                    parent_index=parent["index"],
                )
                self._validate_candidate(
                    parent,
                    children,
                    block,
                    request.batch_id,
                    now=now,
                )
                self._insert(cursor, block)
        return block

    def batch_projection(self, batch_id: str) -> BatchProjection:
        now = _utcnow()
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                _, _, projection = self._batch_context(cursor, batch_id, now=now)
        return projection

    def review(self, request: ReviewDecisionEvent) -> dict[str, Any]:
        try:
            return self._retry_write(lambda: self._review_once(request))
        except _IdempotentReplay as replay:
            return replay.block

    def _review_once(self, request: ReviewDecisionEvent) -> dict[str, Any]:
        data = request.model_dump(mode="json")
        now = _utcnow()
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                parent, children, projection = self._batch_context(
                    cursor, request.batch_id, for_update=True, now=now
                )
                existing = next(
                    (
                        child
                        for child in children
                        if child["block_type"] == "review_decision"
                        and child["data"].get("review_id") == request.review_id
                    ),
                    None,
                )
                if existing is not None and self._has_identical_canonical_data(existing, data):
                    raise _IdempotentReplay(existing)
                if existing is not None:
                    raise HTTPException(status_code=409, detail="review decision already exists")
                if (
                    projection.status != "claimed"
                    or projection.claim_iteration != request.iteration
                    or not projection.validation_run_recorded
                ):
                    raise HTTPException(status_code=409, detail="review is not current")
                validation_refs = {
                    child["data"].get("artifact_ref")
                    for child in children
                    if child["block_type"] == "validation_run"
                    and child["data"].get("iteration") == request.iteration
                    and isinstance(child["data"].get("artifact_ref"), str)
                }
                if not set(request.evidence_refs).issubset(validation_refs):
                    raise HTTPException(status_code=409, detail="review evidence is not authoritative")
                block = self._new_block(
                    cursor,
                    index=self._next_index(cursor),
                    block_type="review_decision",
                    data=data,
                    status=request.decision,
                    parent_index=parent["index"],
                )
                self._validate_candidate(parent, children, block, request.batch_id, now=now)
                self._insert(cursor, block)
                projected = project_batch(
                    [parent, *children, block], request.batch_id, now=now
                )
                if request.decision == "failed" and projected.failed_review_count == 5:
                    terminal = self._new_block(
                        cursor,
                        index=self._next_index(cursor),
                        block_type="batch_done",
                        data={
                            "batch_id": request.batch_id,
                            "outcome": "failed_after_max_iterations",
                        },
                        status="failed_after_max_iterations",
                        parent_index=parent["index"],
                    )
                    self._validate_candidate(
                        parent,
                        [*children, block],
                        terminal,
                        request.batch_id,
                        now=now,
                    )
                    self._insert(cursor, terminal)
        return block

    def claim(self, batch_id: str) -> dict[str, str | int]:
        return self._retry_write(lambda: self._claim_once(batch_id))

    def _claim_once(self, batch_id: str) -> dict[str, str]:
        now = _utcnow()
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                index = self._next_index(cursor)
                parent, children, projection = self._batch_context(
                    cursor,
                    batch_id,
                    for_update=True,
                    now=now,
                )
                if projection.status != "pending":
                    raise HTTPException(status_code=409, detail="batch is not claimable")
                token = secrets.token_urlsafe(32)
                claim_id = secrets.token_urlsafe(18)
                fencing_token = projection.claim_iteration + 1
                expiry = now + timedelta(minutes=90)
                event = ClaimEvent(
                    batch_id=batch_id,
                    claim_id=claim_id,
                    fencing_token=fencing_token,
                    claim_token_sha256=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                    claim_expires_at=expiry,
                )
                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="batch_claimed",
                    data=event.model_dump(mode="json"),
                    status="recorded",
                    parent_index=parent["index"],
                )
                self._validate_candidate(parent, children, block, batch_id, now=now)
                self._insert(cursor, block)
        return {
            "claim_token": token,
            "claim_id": claim_id,
            "fencing_token": fencing_token,
            "claim_expires_at": expiry.isoformat(),
        }

    def heartbeat(self, batch_id: str, token: str | None) -> dict[str, str]:
        return self._retry_write(lambda: self._heartbeat_once(batch_id, token))

    def _heartbeat_once(self, batch_id: str, token: str | None) -> dict[str, str]:
        now = _utcnow()
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                index = self._next_index(cursor)
                parent, children, projection = self._batch_context(
                    cursor,
                    batch_id,
                    for_update=True,
                    now=now,
                )
                self._assert_live_claim(projection, token, now=now)
                expiry = now + timedelta(minutes=30)
                event = HeartbeatEvent(batch_id=batch_id, claim_expires_at=expiry)
                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="batch_heartbeat",
                    data=event.model_dump(mode="json"),
                    status="recorded",
                    parent_index=parent["index"],
                )
                self._validate_candidate(parent, children, block, batch_id, now=now)
                self._insert(cursor, block)
        return {"claim_expires_at": expiry.isoformat()}

    def approve(self, batch_id: str) -> None:
        self._retry_write(lambda: self._approve_once(batch_id))

    def _approve_once(self, batch_id: str) -> None:
        now = _utcnow()
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                index = self._next_index(cursor)
                parent, children, projection = self._batch_context(
                    cursor,
                    batch_id,
                    for_update=True,
                    now=now,
                )
                if projection.status != "pending_review":
                    raise HTTPException(status_code=409, detail="batch is not pending review")
                block = self._new_block(
                    cursor,
                    index=index,
                    block_type="batch_approved",
                    data={"batch_id": batch_id},
                    status="recorded",
                    parent_index=parent["index"],
                )
                self._validate_candidate(parent, children, block, batch_id, now=now)
                self._insert(cursor, block)

    def list_batches(self, requested_status: str) -> list[dict[str, str]]:
        now = _utcnow()
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT `index`, parent_index, block_type, data, status, children,
                           metadata, hash, previous_hash
                    FROM blocks
                    WHERE block_type = 'work_batch'
                    ORDER BY `index`
                    """
                )
                parents = [self.storage._decode_row(row) for row in cursor.fetchall()]
                result: list[dict[str, str]] = []
                for parent in parents:
                    batch_id = parent["data"]["batch_id"]
                    children = self._child_rows(cursor, parent["index"])
                    projection = project_batch([parent, *children], batch_id, now=now)
                    if projection.status == requested_status:
                        result.append(
                            {
                                "batch_id": batch_id,
                                "title": str(parent["data"].get("title", "")),
                            }
                        )
        return result

    def bundle(self, batch_id: str) -> dict[str, Any]:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                batch = self._batch_row(cursor, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="batch not found")
        return {key: value for key, value in batch["data"].items() if "holdout" not in key.lower()}

    def blocks(self, batch_id: str, *, include_holdout: bool = False) -> list[dict[str, Any]]:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT `index`, parent_index, block_type, data, status, children,
                           metadata, hash, previous_hash
                    FROM blocks
                    WHERE JSON_UNQUOTE(JSON_EXTRACT(data, '$.batch_id')) = %s
                    ORDER BY `index`
                    """,
                    (batch_id,),
                )
                rows = cursor.fetchall()
        decoded = [self.storage._decode_row(row) for row in rows]
        for row in decoded:
            row["metadata"] = {
                key: value for key, value in row["metadata"].items() if not key.startswith("claim_")
            }
        return decoded if include_holdout else [row for row in decoded if row["block_type"] != "holdout"]

    def holdout(self, batch_id: str, token: str | None) -> dict[str, Any]:
        now = _utcnow()
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                parent, _, projection = self._batch_context(
                    cursor,
                    batch_id,
                    for_update=True,
                    now=now,
                )
                self._assert_live_claim(projection, token, now=now)
                if not projection.codex_session_recorded:
                    raise HTTPException(status_code=404, detail="holdout not released")
                cursor.execute(
                    """
                    SELECT data FROM blocks
                    WHERE block_type = 'holdout' AND parent_index = %s
                    ORDER BY `index` DESC LIMIT 1
                    """,
                    (parent["index"],),
                )
                row = cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="holdout not found")
        return json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]

    @staticmethod
    def _upsert_capability(cursor: Any, block: dict[str, Any]) -> None:
        data = block["data"]
        capabilities = data.get("capabilities", [])
        descriptor = " ".join(str(value) for value in capabilities)
        cursor.execute(
            """
            INSERT INTO validated_capabilities
                (batch_id, descriptor, artifact_ref, block_index, payload)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE descriptor=VALUES(descriptor),
                artifact_ref=VALUES(artifact_ref), block_index=VALUES(block_index), payload=VALUES(payload)
            """,
            (
                data["batch_id"],
                descriptor,
                data.get("artifact_ref"),
                block["index"],
                json.dumps(data, sort_keys=True),
            ),
        )

    def capabilities(self, need: str) -> list[dict[str, Any]]:
        now = _utcnow()
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT batch_id, artifact_ref, payload FROM validated_capabilities
                    WHERE MATCH(descriptor) AGAINST (%s IN NATURAL LANGUAGE MODE)
                    ORDER BY MATCH(descriptor) AGAINST (%s IN NATURAL LANGUAGE MODE) DESC
                    """,
                    (need, need),
                )
                rows = cursor.fetchall()
                result: list[dict[str, Any]] = []
                for row in rows:
                    _, _, projection = self._batch_context(cursor, row["batch_id"], now=now)
                    if projection.status != "succeeded":
                        continue
                    result.append(
                        {
                            "batch_id": row["batch_id"],
                            "artifact_ref": row["artifact_ref"],
                            "data": (
                                json.loads(row["payload"])
                                if isinstance(row["payload"], str)
                                else row["payload"]
                            ),
                        }
                    )
        return result

    def import_legacy_record(
        self,
        request: LegacyImportWrite,
    ) -> tuple[dict[str, Any], bool]:
        try:
            block = self._retry_write(lambda: self._import_legacy_record_once(request))
            return block, True
        except _IdempotentReplay as replay:
            return replay.block, False

    def _import_legacy_record_once(self, request: LegacyImportWrite) -> dict[str, Any]:
        data = dict(request.data)
        if data.get("batch_id") != request.batch_id:
            raise HTTPException(status_code=422, detail="legacy data.batch_id must match batch_id")
        supplied_record_id = data.get("legacy_record_id")
        if supplied_record_id not in {None, request.legacy_record_id}:
            raise HTTPException(status_code=422, detail="legacy_record_id is reserved")
        data["legacy_record_id"] = request.legacy_record_id
        block_type = (
            "legacy_delivery_todo"
            if request.record_type == "todo"
            else "legacy_delivery_event"
        )

        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                index = self._next_index(cursor)
                cursor.execute(
                    """
                    SELECT `index`, parent_index, block_type, data, status, children,
                           metadata, hash, previous_hash
                    FROM blocks
                    WHERE block_type IN ('legacy_delivery_todo', 'legacy_delivery_event')
                      AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.legacy_record_id')) = %s
                    ORDER BY `index` LIMIT 1 FOR UPDATE
                    """,
                    (request.legacy_record_id,),
                )
                existing_row = cursor.fetchone()
                if existing_row is not None:
                    existing = self.storage._decode_row(existing_row)
                    if existing["block_type"] == block_type and existing["data"] == data:
                        raise _IdempotentReplay(existing)
                    raise HTTPException(
                        status_code=409,
                        detail="legacy_record_id already exists with different content",
                    )

                cursor.execute(
                    """
                    SELECT `index`, parent_index, block_type, data, status, children,
                           metadata, hash, previous_hash
                    FROM blocks
                    WHERE block_type = 'legacy_delivery_todo'
                      AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.batch_id')) = %s
                    ORDER BY `index` LIMIT 1 FOR UPDATE
                    """,
                    (request.batch_id,),
                )
                root_row = cursor.fetchone()
                root = self.storage._decode_row(root_row) if root_row is not None else None
                if request.record_type == "todo" and root is not None:
                    raise HTTPException(
                        status_code=409,
                        detail="legacy batch already belongs to another todo record",
                    )
                if request.record_type == "event" and root is None:
                    raise HTTPException(
                        status_code=409,
                        detail="legacy todo must be imported before its events",
                    )

                block = self._new_block(
                    cursor,
                    index=index,
                    block_type=block_type,
                    data=data,
                    status="archived",
                    parent_index=root["index"] if root is not None else None,
                    metadata={"source": "sqlite-delivery-legacy-import/v1"},
                )
                self._insert(cursor, block)
        return block
