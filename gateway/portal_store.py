"""MariaDB persistence for opaque, tenant-bound portal setup tickets."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Final
from uuid import UUID, uuid4

from blockchain.mariadb_storage import MariaDBStorage
from gateway.portal_contracts import (
    PortalPrincipalV1,
    PortalSetupTicketV1,
    PortalTicketAction,
)


_INVALID_TICKET: Final = "invalid portal setup ticket"


class PortalTicketStore:
    """Owns hashed setup tickets and the first tenant binding for a setup."""

    def __init__(self, storage: MariaDBStorage) -> None:
        self.storage = storage
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS portal_setup_bindings (
                        job_id CHAR(36) NOT NULL PRIMARY KEY,
                        organization_id VARCHAR(128) NOT NULL,
                        created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                        updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                            ON UPDATE CURRENT_TIMESTAMP(6),
                        INDEX idx_portal_setup_binding_org (organization_id, job_id)
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS portal_setup_tickets (
                        ticket_id CHAR(36) NOT NULL PRIMARY KEY,
                        token_sha256 CHAR(64) NOT NULL,
                        job_id CHAR(36) NOT NULL,
                        organization_id VARCHAR(128) NOT NULL,
                        subject_id VARCHAR(128) NOT NULL,
                        credential_alias VARCHAR(128) NOT NULL,
                        action VARCHAR(32) NOT NULL,
                        expires_at DATETIME(6) NOT NULL,
                        used_at DATETIME(6) NULL,
                        created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                        updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                            ON UPDATE CURRENT_TIMESTAMP(6),
                        INDEX idx_portal_ticket_job (job_id, organization_id),
                        CONSTRAINT fk_portal_ticket_binding FOREIGN KEY (job_id)
                            REFERENCES portal_setup_bindings (job_id)
                            ON DELETE RESTRICT
                    ) ENGINE=InnoDB
                    """
                )

    def bind_or_require_organization(self, job_id: UUID, organization_id: str) -> None:
        """Bind an unclaimed setup once, then fail closed for every other tenant."""

        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """INSERT IGNORE INTO portal_setup_bindings
                       (job_id, organization_id) VALUES (%s, %s)""",
                    (str(job_id), organization_id),
                )
                cursor.execute(
                    """SELECT organization_id FROM portal_setup_bindings
                       WHERE job_id = %s FOR UPDATE""",
                    (str(job_id),),
                )
                row = cursor.fetchone()
                if row is None or str(row["organization_id"]) != organization_id:
                    raise LookupError("portal integration setup not found")

    def organization_owns_setup(self, job_id: UUID, organization_id: str) -> bool:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT 1 AS owned FROM portal_setup_bindings
                       WHERE job_id = %s AND organization_id = %s""",
                    (str(job_id), organization_id),
                )
                return cursor.fetchone() is not None

    def issue(
        self,
        *,
        job_id: UUID,
        principal: PortalPrincipalV1,
        credential_alias: str,
        action: PortalTicketAction,
        now: datetime,
        lifetime: timedelta = timedelta(minutes=10),
    ) -> PortalSetupTicketV1:
        now = _utc(now)
        if lifetime <= timedelta(0) or lifetime > timedelta(minutes=10):
            raise ValueError("portal ticket lifetime must be at most ten minutes")
        self.bind_or_require_organization(job_id, principal.organization_id)
        raw_ticket = secrets.token_urlsafe(32)
        token_sha256 = hashlib.sha256(raw_ticket.encode("utf-8")).hexdigest()
        ticket_id = uuid4()
        expires_at = now + lifetime
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO portal_setup_tickets
                       (ticket_id, token_sha256, job_id, organization_id, subject_id,
                        credential_alias, action, expires_at, used_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL)""",
                    (
                        str(ticket_id),
                        token_sha256,
                        str(job_id),
                        principal.organization_id,
                        principal.subject_id,
                        credential_alias,
                        action,
                        expires_at.replace(tzinfo=None),
                    ),
                )
        return PortalSetupTicketV1(
            ticket_id=ticket_id,
            ticket=raw_ticket,
            job_id=job_id,
            credential_alias=credential_alias,
            expires_at=expires_at,
        )

    def consume(
        self,
        *,
        job_id: UUID,
        principal: PortalPrincipalV1,
        ticket_id: UUID,
        raw_ticket: str,
        credential_alias: str,
        action: PortalTicketAction,
        now: datetime,
    ) -> None:
        """Validate and consume exactly once while holding the ticket row lock."""

        current = _utc(now).replace(tzinfo=None)
        supplied_digest = hashlib.sha256(raw_ticket.encode("utf-8")).hexdigest()
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT token_sha256, job_id, organization_id, subject_id,
                              credential_alias, action, expires_at, used_at
                       FROM portal_setup_tickets
                       WHERE ticket_id = %s FOR UPDATE""",
                    (str(ticket_id),),
                )
                row = cursor.fetchone()
                valid = row is not None
                if row is not None:
                    valid = all(
                        (
                            secrets.compare_digest(str(row["token_sha256"]), supplied_digest),
                            str(row["job_id"]) == str(job_id),
                            str(row["organization_id"]) == principal.organization_id,
                            str(row["subject_id"]) == principal.subject_id,
                            str(row["credential_alias"]) == credential_alias,
                            str(row["action"]) == action,
                            row["used_at"] is None,
                            row["expires_at"] > current,
                        )
                    )
                if not valid:
                    raise PermissionError(_INVALID_TICKET)
                cursor.execute(
                    """UPDATE portal_setup_tickets SET used_at = %s
                       WHERE ticket_id = %s AND used_at IS NULL""",
                    (current, str(ticket_id)),
                )
                if cursor.rowcount != 1:
                    raise PermissionError(_INVALID_TICKET)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("portal ticket time must be timezone-aware")
    return value.astimezone(timezone.utc)
