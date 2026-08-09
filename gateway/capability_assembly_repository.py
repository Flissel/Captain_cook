"""MariaDB sole-writer repository for durable capability assemblies."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pymysql.err import IntegrityError, OperationalError

from agenten.agent_runtime.contracts import ArtifactRef
from gateway.capability_assembly_contracts import (
    CapabilityAssemblyCheckpoint,
    CapabilityAssemblyNoEffectRetryAuthorization,
    CapabilityAssemblyWriteConflict,
    canonical_assembly_model_bytes,
)
from gateway.store import GatewayStore


@dataclass(frozen=True)
class CapabilityAssemblyRecord:
    checkpoint: CapabilityAssemblyCheckpoint
    artifact_ref: ArtifactRef


class GatewayCapabilityAssemblyRepository:
    """Persist assembly checkpoints through the Gateway-owned MariaDB connection."""

    def __init__(self, store: GatewayStore) -> None:
        self._store = store
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._store.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS capability_assembly_heads (
                        assembly_id CHAR(36) NOT NULL PRIMARY KEY,
                        idempotency_key CHAR(64) NOT NULL UNIQUE,
                        latest_revision INT NOT NULL
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS capability_assembly_checkpoints (
                        assembly_id CHAR(36) NOT NULL,
                        revision INT NOT NULL,
                        checkpoint_sha256 CHAR(64) NOT NULL,
                        payload JSON NOT NULL,
                        created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                        PRIMARY KEY (assembly_id, revision),
                        CONSTRAINT fk_capability_assembly_checkpoint_head
                            FOREIGN KEY (assembly_id)
                            REFERENCES capability_assembly_heads (assembly_id)
                            ON DELETE CASCADE
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS capability_assembly_retry_authorizations (
                        authorization_receipt_id CHAR(36) NOT NULL PRIMARY KEY,
                        authority_sha256 CHAR(64) NOT NULL,
                        authority_payload JSON NOT NULL,
                        assembly_id CHAR(36) NOT NULL,
                        checkpoint_revision INT NOT NULL,
                        created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                        UNIQUE KEY uq_capability_assembly_retry_checkpoint
                            (assembly_id, checkpoint_revision),
                        CONSTRAINT fk_capability_assembly_retry_checkpoint
                            FOREIGN KEY (assembly_id, checkpoint_revision)
                            REFERENCES capability_assembly_checkpoints
                                (assembly_id, revision)
                            ON DELETE CASCADE
                    ) ENGINE=InnoDB
                    """
                )

    def history(self, assembly_id: UUID) -> tuple[CapabilityAssemblyRecord, ...]:
        with self._store.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT revision, checkpoint_sha256, payload
                    FROM capability_assembly_checkpoints
                    WHERE assembly_id = %s
                    ORDER BY revision
                    """,
                    (str(assembly_id),),
                )
                rows = cursor.fetchall()
        return tuple(self._record_from_row(row) for row in rows)

    def find_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> CapabilityAssemblyRecord | None:
        with self._store.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT checkpoint.revision, checkpoint.checkpoint_sha256,
                           checkpoint.payload
                    FROM capability_assembly_heads AS head
                    JOIN capability_assembly_checkpoints AS checkpoint
                      ON checkpoint.assembly_id = head.assembly_id
                     AND checkpoint.revision = head.latest_revision
                    WHERE head.idempotency_key = %s
                    """,
                    (idempotency_key,),
                )
                row = cursor.fetchone()
        return None if row is None else self._record_from_row(row)

    def append(
        self,
        checkpoint: CapabilityAssemblyCheckpoint,
        *,
        expected_revision: int,
    ) -> CapabilityAssemblyRecord:
        return self._write_checkpoint(
            checkpoint,
            expected_revision=expected_revision,
            authorization=None,
        )

    def consume_retry_authorization_and_append(
        self,
        checkpoint: CapabilityAssemblyCheckpoint,
        authorization: CapabilityAssemblyNoEffectRetryAuthorization,
        *,
        expected_revision: int,
    ) -> CapabilityAssemblyRecord:
        if not any(
            outcome.retry_authorization == authorization
            for outcome in checkpoint.reconciliation_outcomes
        ):
            raise CapabilityAssemblyWriteConflict(
                "retry authorization is not bound to the appended checkpoint"
            )
        return self._write_checkpoint(
            checkpoint,
            expected_revision=expected_revision,
            authorization=authorization,
        )

    def _write_checkpoint(
        self,
        checkpoint: CapabilityAssemblyCheckpoint,
        *,
        expected_revision: int,
        authorization: CapabilityAssemblyNoEffectRetryAuthorization | None,
    ) -> CapabilityAssemblyRecord:
        if checkpoint.revision != expected_revision + 1:
            raise CapabilityAssemblyWriteConflict("checkpoint revision is not monotone")
        checkpoint_bytes = canonical_assembly_model_bytes(checkpoint)
        checkpoint_sha256 = hashlib.sha256(checkpoint_bytes).hexdigest()
        try:
            with self._store.storage.transaction() as connection:
                with connection.cursor() as cursor:
                    self._lock_and_require_revision(
                        cursor,
                        checkpoint,
                        expected_revision=expected_revision,
                    )
                    cursor.execute(
                        """
                        INSERT INTO capability_assembly_checkpoints
                            (assembly_id, revision, checkpoint_sha256, payload)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (
                            str(checkpoint.assembly_id),
                            checkpoint.revision,
                            checkpoint_sha256,
                            checkpoint_bytes.decode("utf-8"),
                        ),
                    )
                    if authorization is not None:
                        authority_bytes = canonical_assembly_model_bytes(authorization)
                        cursor.execute(
                            """
                            INSERT INTO capability_assembly_retry_authorizations
                                (authorization_receipt_id, authority_sha256,
                                 authority_payload, assembly_id, checkpoint_revision)
                            VALUES (%s, %s, %s, %s, %s)
                            """,
                            (
                                str(authorization.authorization_receipt_id),
                                hashlib.sha256(authority_bytes).hexdigest(),
                                authority_bytes.decode("utf-8"),
                                str(checkpoint.assembly_id),
                                checkpoint.revision,
                            ),
                        )
                    cursor.execute(
                        """
                        UPDATE capability_assembly_heads
                        SET latest_revision = %s
                        WHERE assembly_id = %s AND latest_revision = %s
                        """,
                        (
                            checkpoint.revision,
                            str(checkpoint.assembly_id),
                            expected_revision,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise CapabilityAssemblyWriteConflict(
                            "checkpoint revision compare-and-swap failed"
                        )
        except (IntegrityError, OperationalError) as exc:
            raise CapabilityAssemblyWriteConflict(
                "checkpoint or authorization lost sole-writer fencing"
            ) from exc
        return self._record(checkpoint, checkpoint_sha256)

    @staticmethod
    def _lock_and_require_revision(
        cursor,
        checkpoint: CapabilityAssemblyCheckpoint,
        *,
        expected_revision: int,
    ) -> None:
        cursor.execute(
            """
            SELECT idempotency_key, latest_revision
            FROM capability_assembly_heads
            WHERE assembly_id = %s
            FOR UPDATE
            """,
            (str(checkpoint.assembly_id),),
        )
        head = cursor.fetchone()
        if head is None:
            if expected_revision != 0:
                raise CapabilityAssemblyWriteConflict(
                    "checkpoint revision compare-and-swap failed"
                )
            cursor.execute(
                """
                INSERT INTO capability_assembly_heads
                    (assembly_id, idempotency_key, latest_revision)
                VALUES (%s, %s, 0)
                """,
                (str(checkpoint.assembly_id), checkpoint.idempotency_key),
            )
            return
        if (
            int(head["latest_revision"]) != expected_revision
            or head["idempotency_key"] != checkpoint.idempotency_key
        ):
            raise CapabilityAssemblyWriteConflict(
                "checkpoint revision compare-and-swap failed"
            )

    @classmethod
    def _record_from_row(cls, row: dict[str, Any]) -> CapabilityAssemblyRecord:
        payload = row["payload"]
        encoded = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if isinstance(payload, dict)
            else payload.decode("utf-8")
            if isinstance(payload, (bytes, bytearray))
            else payload
        )
        if not isinstance(encoded, str):
            raise CapabilityAssemblyWriteConflict("checkpoint payload is malformed")
        try:
            checkpoint = CapabilityAssemblyCheckpoint.model_validate_json(encoded)
        except ValueError as exc:
            raise CapabilityAssemblyWriteConflict("checkpoint payload is malformed") from exc
        digest = hashlib.sha256(canonical_assembly_model_bytes(checkpoint)).hexdigest()
        if digest != row["checkpoint_sha256"]:
            raise CapabilityAssemblyWriteConflict(
                "checkpoint digest does not bind persisted payload"
            )
        return cls._record(checkpoint, digest)

    @staticmethod
    def _record(
        checkpoint: CapabilityAssemblyCheckpoint,
        digest: str,
    ) -> CapabilityAssemblyRecord:
        return CapabilityAssemblyRecord(
            checkpoint=checkpoint,
            artifact_ref=ArtifactRef(
                uri=f"artifact://capability-assembly-checkpoint/{digest}",
                sha256=digest,
                media_type="application/json",
            ),
        )


__all__ = ["CapabilityAssemblyRecord", "GatewayCapabilityAssemblyRepository"]
