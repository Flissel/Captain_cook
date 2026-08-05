"""MariaDB persistence for opaque, tenant-bound portal setup tickets."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Final
from uuid import UUID, uuid4

from blockchain.mariadb_storage import MariaDBStorage
from gateway.portal_contracts import (
    PortalPrincipalV1,
    PortalSetupTicketV1,
    PortalTicketFenceV1,
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
                for column, definition in (
                    ("setup_revision", "BIGINT NULL"),
                    ("setup_content_sha256", "CHAR(64) NULL"),
                    ("correlation_id", "CHAR(36) NULL"),
                    ("credential_type", "VARCHAR(128) NULL"),
                    ("requirement_project_id", "VARCHAR(256) NULL"),
                    ("selected_credential_id", "VARCHAR(256) NULL"),
                    ("verification_workflow_sha256", "CHAR(64) NULL"),
                ):
                    cursor.execute(
                        f"ALTER TABLE portal_setup_tickets ADD COLUMN IF NOT EXISTS {column} {definition}"
                    )

    def provision_organization(self, job_id: UUID, organization_id: str) -> bool:
        """Captain-only seam: create one immutable setup tenant binding."""

        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """INSERT IGNORE INTO portal_setup_bindings
                       (job_id, organization_id) VALUES (%s, %s)""",
                    (str(job_id), organization_id),
                )
                created = cursor.rowcount == 1
                cursor.execute(
                    """SELECT organization_id FROM portal_setup_bindings
                       WHERE job_id = %s FOR UPDATE""",
                    (str(job_id),),
                )
                row = cursor.fetchone()
                if row is None or str(row["organization_id"]) != organization_id:
                    raise ValueError("portal tenant binding conflict")
                return created

    def require_organization(self, job_id: UUID, organization_id: str) -> None:
        """Require an existing exact binding without creating or changing it."""

        if not self.organization_owns_setup(job_id, organization_id):
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
        fence: PortalTicketFenceV1 | None = None,
        now: datetime,
        lifetime: timedelta = timedelta(minutes=10),
    ) -> PortalSetupTicketV1:
        now = _utc(now)
        if lifetime <= timedelta(0) or lifetime > timedelta(minutes=10):
            raise ValueError("portal ticket lifetime must be at most ten minutes")
        raw_ticket = secrets.token_urlsafe(32)
        token_sha256 = hashlib.sha256(raw_ticket.encode("utf-8")).hexdigest()
        ticket_id = uuid4()
        expires_at = now + lifetime
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT 1 AS owned FROM portal_setup_bindings
                       WHERE job_id = %s AND organization_id = %s FOR UPDATE""",
                    (str(job_id), principal.organization_id),
                )
                if cursor.fetchone() is None:
                    raise LookupError("portal integration setup not found")
                current_fence = fence or _current_fence(
                    cursor,
                    job_id=job_id,
                    organization_id=principal.organization_id,
                    credential_alias=credential_alias,
                )
                if current_fence is None:
                    raise LookupError("portal integration setup not found")
                if current_fence.credential_alias != credential_alias:
                    raise ValueError("portal ticket fence alias mismatch")
                cursor.execute(
                    """INSERT INTO portal_setup_tickets
                       (ticket_id, token_sha256, job_id, organization_id, subject_id,
                        credential_alias, action, setup_revision, setup_content_sha256,
                        correlation_id, credential_type, requirement_project_id,
                        selected_credential_id, verification_workflow_sha256,
                        expires_at, used_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, NULL)""",
                    (
                        str(ticket_id),
                        token_sha256,
                        str(job_id),
                        principal.organization_id,
                        principal.subject_id,
                        credential_alias,
                        action,
                        current_fence.revision,
                        current_fence.content_sha256,
                        str(current_fence.correlation_id),
                        current_fence.credential_type,
                        current_fence.requirement_project_id,
                        current_fence.selected_credential_id,
                        current_fence.verification_workflow_sha256,
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
        current_fence: PortalTicketFenceV1 | None = None,
        now: datetime,
    ) -> None:
        """Validate and consume exactly once while holding the ticket row lock."""

        current = _utc(now).replace(tzinfo=None)
        supplied_digest = hashlib.sha256(raw_ticket.encode("utf-8")).hexdigest()
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT token_sha256, job_id, organization_id, subject_id,
                              credential_alias, action, setup_revision,
                              setup_content_sha256, correlation_id, credential_type,
                              requirement_project_id, selected_credential_id,
                              verification_workflow_sha256, expires_at, used_at
                       FROM portal_setup_tickets
                       WHERE ticket_id = %s FOR UPDATE""",
                    (str(ticket_id),),
                )
                row = cursor.fetchone()
                if current_fence is None:
                    current_fence = _current_fence(
                        cursor,
                        job_id=job_id,
                        organization_id=principal.organization_id,
                        credential_alias=credential_alias,
                    )
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
                            _row_fence(row) == current_fence,
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


def _row_fence(row: dict[str, object]) -> PortalTicketFenceV1 | None:
    try:
        return PortalTicketFenceV1(
            revision=row["setup_revision"],
            content_sha256=row["setup_content_sha256"],
            correlation_id=row["correlation_id"],
            credential_alias=row["credential_alias"],
            credential_type=row["credential_type"],
            requirement_project_id=row["requirement_project_id"],
            selected_credential_id=row["selected_credential_id"],
            verification_workflow_sha256=row["verification_workflow_sha256"],
        )
    except (KeyError, ValueError):
        return None


def _current_fence(
    cursor: object,
    *,
    job_id: UUID,
    organization_id: str,
    credential_alias: str,
) -> PortalTicketFenceV1 | None:
    cursor.execute(
        """SELECT 1 AS owned FROM portal_setup_bindings
           WHERE job_id = %s AND organization_id = %s FOR UPDATE""",
        (str(job_id), organization_id),
    )
    if cursor.fetchone() is None:
        return None
    cursor.execute(
        """SELECT revision, content_sha256, payload
           FROM factory_integration_setup_events
           WHERE job_id = %s ORDER BY revision DESC LIMIT 1 FOR UPDATE""",
        (str(job_id),),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    try:
        payload = row["payload"]
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        if isinstance(payload, str):
            payload = json.loads(payload)
        connections = payload["plan"]["connections"]
        matches = tuple(
            connection
            for connection in connections
            if connection["requirement"]["credential_alias"] == credential_alias
        )
        if len(matches) != 1:
            return None
        target = matches[0]
        requirement = target["requirement"]
        selected = target.get("selected_credential")
        receipt = target.get("verification_receipt")
        return PortalTicketFenceV1(
            revision=row["revision"],
            content_sha256=row["content_sha256"],
            correlation_id=payload["correlation_id"],
            credential_alias=credential_alias,
            credential_type=requirement["credential_type"],
            requirement_project_id=requirement.get("project_id"),
            selected_credential_id=(
                None if selected is None else selected["credential_id"]
            ),
            verification_workflow_sha256=(
                None if receipt is None else receipt["workflow_content_sha256"]
            ),
        )
    except (KeyError, TypeError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
