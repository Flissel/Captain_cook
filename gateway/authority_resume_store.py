"""Sole-writer MariaDB persistence for the authority resume flow."""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from blockchain.mariadb_storage import MariaDBStorage
from gateway.authority_resume_contracts import (
    MAX_AUTHORIZATION_TTL,
    AuthorityReadbackV1,
    AuthorityResumeError,
    DispatchRecordV1,
    ResumeAuthorizationV1,
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class AuthorityResumeStore:
    """Owns the resume-evidence tables; the Gateway process is the sole writer."""

    def __init__(self, storage: MariaDBStorage) -> None:
        self.storage = storage
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS authority_resume_authorizations (
                        authorization_id CHAR(36) NOT NULL PRIMARY KEY,
                        assembly_id CHAR(64) NOT NULL,
                        token_sha256 CHAR(64) NOT NULL UNIQUE,
                        issued_at DATETIME(6) NOT NULL,
                        expires_at DATETIME(6) NOT NULL,
                        consumed_at DATETIME(6) NULL,
                        created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                        INDEX idx_authority_resume_assembly (assembly_id)
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS authority_dispatch_evidence (
                        dispatch_id CHAR(36) NOT NULL PRIMARY KEY,
                        assembly_id CHAR(64) NOT NULL,
                        authorization_id CHAR(36) NOT NULL UNIQUE,
                        revision BIGINT NOT NULL,
                        dispatched_at DATETIME(6) NOT NULL,
                        created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                        UNIQUE KEY uq_authority_dispatch_revision (assembly_id, revision),
                        CONSTRAINT fk_authority_dispatch_authorization
                            FOREIGN KEY (authorization_id)
                            REFERENCES authority_resume_authorizations (authorization_id)
                            ON DELETE RESTRICT
                    ) ENGINE=InnoDB
                    """
                )

    def authorize(
        self,
        assembly_id: str,
        *,
        now: datetime,
        ttl: timedelta = MAX_AUTHORIZATION_TTL,
    ) -> tuple[ResumeAuthorizationV1, str]:
        issued_at = _utc(now)
        record = ResumeAuthorizationV1(
            authorization_id=uuid4(),
            assembly_id=assembly_id,
            issued_at=issued_at,
            expires_at=issued_at + ttl,
        )
        raw_token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO authority_resume_authorizations
                       (authorization_id, assembly_id, token_sha256,
                        issued_at, expires_at)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (
                        str(record.authorization_id),
                        record.assembly_id,
                        digest,
                        record.issued_at.replace(tzinfo=None),
                        record.expires_at.replace(tzinfo=None),
                    ),
                )
        return record, raw_token

    def dispatch(
        self,
        assembly_id: str,
        raw_token: str,
        *,
        now: datetime,
    ) -> DispatchRecordV1:
        digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        moment = _utc(now)
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT authorization_id, assembly_id, token_sha256,
                              expires_at, consumed_at
                       FROM authority_resume_authorizations
                       WHERE token_sha256 = %s FOR UPDATE""",
                    (digest,),
                )
                row = cursor.fetchone()
                if row is None or not secrets.compare_digest(
                    str(row["token_sha256"]), digest
                ):
                    raise AuthorityResumeError("unknown_authorization")
                if str(row["assembly_id"]) != assembly_id:
                    raise AuthorityResumeError("assembly_mismatch")
                if row["consumed_at"] is not None:
                    raise AuthorityResumeError("already_consumed")
                if _utc(row["expires_at"]) <= moment:
                    raise AuthorityResumeError("expired")
                cursor.execute(
                    """SELECT COALESCE(MAX(revision), 0) AS revision
                       FROM authority_dispatch_evidence
                       WHERE assembly_id = %s FOR UPDATE""",
                    (assembly_id,),
                )
                revision = int(cursor.fetchone()["revision"]) + 1
                record = DispatchRecordV1(
                    dispatch_id=uuid4(),
                    assembly_id=assembly_id,
                    authorization_id=UUID(str(row["authorization_id"])),
                    revision=revision,
                    dispatched_at=moment,
                )
                cursor.execute(
                    """UPDATE authority_resume_authorizations
                       SET consumed_at = %s
                       WHERE authorization_id = %s AND consumed_at IS NULL""",
                    (
                        moment.replace(tzinfo=None),
                        str(record.authorization_id),
                    ),
                )
                if cursor.rowcount != 1:
                    raise AuthorityResumeError("already_consumed")
                cursor.execute(
                    """INSERT INTO authority_dispatch_evidence
                       (dispatch_id, assembly_id, authorization_id,
                        revision, dispatched_at)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (
                        str(record.dispatch_id),
                        record.assembly_id,
                        str(record.authorization_id),
                        record.revision,
                        record.dispatched_at.replace(tzinfo=None),
                    ),
                )
        return record

    def readback(self, assembly_id: str) -> AuthorityReadbackV1 | None:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT COUNT(*) AS total
                       FROM authority_resume_authorizations
                       WHERE assembly_id = %s""",
                    (assembly_id,),
                )
                authorization_count = int(cursor.fetchone()["total"])
                cursor.execute(
                    """SELECT dispatch_id, assembly_id, authorization_id,
                              revision, dispatched_at
                       FROM authority_dispatch_evidence
                       WHERE assembly_id = %s
                       ORDER BY revision ASC""",
                    (assembly_id,),
                )
                rows = cursor.fetchall()
        if authorization_count == 0 and not rows:
            return None
        dispatches = tuple(
            DispatchRecordV1(
                dispatch_id=UUID(str(row["dispatch_id"])),
                assembly_id=str(row["assembly_id"]),
                authorization_id=UUID(str(row["authorization_id"])),
                revision=int(row["revision"]),
                dispatched_at=_utc(row["dispatched_at"]),
            )
            for row in rows
        )
        revision = dispatches[-1].revision if dispatches else 0
        return AuthorityReadbackV1(
            assembly_id=assembly_id,
            revision=revision,
            authorization_count=authorization_count,
            dispatches=dispatches,
        )
