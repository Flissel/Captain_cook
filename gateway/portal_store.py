"""MariaDB persistence for opaque, tenant-bound portal setup tickets."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Final, Iterator, Protocol, runtime_checkable
from uuid import UUID, uuid4

from blockchain.mariadb_storage import MariaDBStorage
from gateway.portal_contracts import (
    PortalPrincipalV1,
    PortalSetupTicketV1,
    PortalTicketFenceV1,
    PortalTicketAction,
)


_INVALID_TICKET: Final = "invalid portal setup ticket"


class FactoryCredentialAmbiguousError(RuntimeError):
    """The credential boundary cannot yet authoritatively settle a reservation."""


@runtime_checkable
class FactoryCredentialAdapter(Protocol):
    """Dedicated idempotent and reversible credential boundary."""

    def store_credential(
        self,
        *,
        reservation_key: str,
        tenant_id: str,
        request_id: UUID,
        expected_subject_version: int,
        purpose: str,
        secret_value: str,
    ) -> "FactoryCredentialReceipt": ...

    def lookup_credential(
        self, reservation_key: str
    ) -> "FactoryCredentialReceipt | None": ...

    def revoke_credential(
        self, *, reservation_key: str, credential_ref: str | None = None
    ) -> None: ...


@dataclass(frozen=True)
class FactoryCredentialReceipt:
    """Secret-free binding for one idempotent external credential effect."""

    reservation_key: str
    tenant_id: str
    request_id: UUID
    request_version: int
    purpose: str
    opaque_reference: str
    reference_sha256: str

    def __post_init__(self) -> None:
        if len(self.reservation_key) != 64:
            raise ValueError("credential receipt reservation key is invalid")
        expected = hashlib.sha256(self.opaque_reference.encode("utf-8")).hexdigest()
        if not secrets.compare_digest(self.reference_sha256, expected):
            raise ValueError("credential receipt reference digest conflicts")


class FactoryCredentialReservationStore(Protocol):
    def iter_credential_reservations(
        self,
    ) -> Iterator[tuple[UUID, str, int, str, str]]: ...

    def reserve_credential(
        self, request_id: UUID, *, tenant_id: str,
        expected_subject_version: int, credential_alias: str, now: datetime,
    ) -> str: ...

    def complete_credential_reservation(
        self, request_id: UUID, *, tenant_id: str, reservation_key: str,
        credential_ref: str, now: datetime,
    ) -> str: ...

    def credential_reference(
        self, request_id: UUID, *, tenant_id: str
    ) -> str | None: ...

    def release_credential_reservation(
        self, request_id: UUID, *, tenant_id: str, reservation_key: str
    ) -> None: ...

    def credential_revoke_pending(
        self, request_id: UUID, *, tenant_id: str, reservation_key: str
    ) -> bool: ...

    def mark_credential_revoke_pending(
        self, request_id: UUID, *, tenant_id: str, reservation_key: str, now: datetime
    ) -> None: ...

    def clear_credential_revocation(
        self, request_id: UUID, *, tenant_id: str, reservation_key: str
    ) -> None: ...


class FactoryCredentialCoordinator:
    """Fence, write, revalidate, and clean up one credential effect."""

    def __init__(
        self, *, store: FactoryCredentialReservationStore,
        adapter: FactoryCredentialAdapter, clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._adapter = adapter
        self._clock = clock

    def store_and_link(
        self, *, request_id: UUID, tenant_id: str, expected_subject_version: int,
        credential_alias: str, secret_value: str, reserved_at: datetime,
    ) -> str:
        reservation_key = self._store.reserve_credential(
            request_id, tenant_id=tenant_id,
            expected_subject_version=expected_subject_version,
            credential_alias=credential_alias, now=reserved_at,
        )
        receipt: FactoryCredentialReceipt | None = None
        try:
            receipt = self._adapter.lookup_credential(reservation_key)
            if receipt is None:
                try:
                    receipt = self._adapter.store_credential(
                        reservation_key=reservation_key, tenant_id=tenant_id,
                        request_id=request_id,
                        expected_subject_version=expected_subject_version,
                        purpose=credential_alias,
                        secret_value=secret_value,
                    )
                except Exception:
                    receipt = self._adapter.lookup_credential(reservation_key)
                    if receipt is None:
                        raise FactoryCredentialAmbiguousError(
                            "credential receipt is unresolved"
                        ) from None
            if not isinstance(receipt, FactoryCredentialReceipt):
                raise FactoryCredentialAmbiguousError("credential receipt is unresolved")
            self._validate_receipt(
                receipt,
                reservation_key=reservation_key, tenant_id=tenant_id,
                request_id=request_id, expected_subject_version=expected_subject_version,
                purpose=credential_alias,
            )
            self._store.complete_credential_reservation(
                request_id, tenant_id=tenant_id, reservation_key=reservation_key,
                credential_ref=receipt.opaque_reference, now=self._clock(),
            )
            return receipt.opaque_reference
        except FactoryCredentialAmbiguousError:
            # A timeout or malformed read is not proof the external write did not commit.
            # Keep the durable key for an authoritative later lookup/reconciliation.
            raise
        except Exception:
            try:
                linked_reference = self._store.credential_reference(
                    request_id, tenant_id=tenant_id
                )
                if (
                    isinstance(receipt, FactoryCredentialReceipt)
                    and linked_reference == receipt.opaque_reference
                ):
                    return linked_reference
                if isinstance(receipt, FactoryCredentialReceipt):
                    self._store.mark_credential_revoke_pending(
                        request_id, tenant_id=tenant_id,
                        reservation_key=reservation_key, now=self._clock(),
                    )
                    self._adapter.revoke_credential(
                        reservation_key=reservation_key,
                        credential_ref=receipt.opaque_reference,
                    )
                    self._store.clear_credential_revocation(
                        request_id, tenant_id=tenant_id, reservation_key=reservation_key
                    )
                # An unknown or untyped receipt is not authority to revoke or release.
                # Its original durable reservation is left for a later lookup.
            except Exception:
                raise
            else:
                if not isinstance(receipt, FactoryCredentialReceipt):
                    raise FactoryCredentialAmbiguousError(
                        "credential receipt is unresolved"
                    )
            finally:
                # The original operation failure must be preserved after successful cleanup.
                pass
            raise

    def _revoke_pending(
        self, *, request_id: UUID, tenant_id: str, reservation_key: str,
        credential_ref: str | None,
    ) -> None:
        self._store.mark_credential_revoke_pending(
            request_id, tenant_id=tenant_id, reservation_key=reservation_key,
            now=self._clock(),
        )
        self._adapter.revoke_credential(
            reservation_key=reservation_key, credential_ref=credential_ref,
        )
        self._store.clear_credential_revocation(
            request_id, tenant_id=tenant_id, reservation_key=reservation_key
        )

    def store_and_answer(
        self,
        *,
        request_id: UUID,
        tenant_id: str,
        expected_subject_version: int,
        credential_alias: str,
        secret_value: str,
        reserved_at: datetime,
    ) -> str:
        """Compatibility name; this only links a credential and never answers."""

        return self.store_and_link(
            request_id=request_id,
            tenant_id=tenant_id,
            expected_subject_version=expected_subject_version,
            credential_alias=credential_alias,
            secret_value=secret_value,
            reserved_at=reserved_at,
        )

    def recover(self) -> None:
        """Reconcile secret-free durable reservations after process restart."""

        for request_id, tenant_id, version, purpose, reservation_key in (
            self._store.iter_credential_reservations()
        ):
            receipt = self._adapter.lookup_credential(reservation_key)
            pending_revoke = self._store.credential_revoke_pending(
                request_id, tenant_id=tenant_id, reservation_key=reservation_key
            )
            if receipt is None and pending_revoke:
                # Once revocation is pending, a none lookup is authoritative absence.
                self._store.clear_credential_revocation(
                    request_id, tenant_id=tenant_id, reservation_key=reservation_key
                )
                continue
            if not isinstance(receipt, FactoryCredentialReceipt):
                # Preserve ambiguous reservations until the adapter can make an
                # authoritative lookup; deleting the key could duplicate a write.
                continue
            if pending_revoke:
                # Validate the typed receipt for audit/binding purposes, but a
                # previously committed revoke decision is terminal: it can only
                # clean up, never complete or link a credential on restart.
                try:
                    self._validate_receipt(
                        receipt,
                        reservation_key=reservation_key,
                        tenant_id=tenant_id,
                        request_id=request_id,
                        expected_subject_version=version,
                        purpose=purpose,
                    )
                except Exception:
                    pass
                try:
                    self._revoke_pending(
                        request_id=request_id, tenant_id=tenant_id,
                        reservation_key=reservation_key,
                        credential_ref=receipt.opaque_reference,
                    )
                except Exception:
                    # Do not retry in the same startup pass: durable state
                    # records the failure for the next reconciliation.
                    continue
                continue
            try:
                self._validate_receipt(
                    receipt,
                    reservation_key=reservation_key,
                    tenant_id=tenant_id,
                    request_id=request_id,
                    expected_subject_version=version,
                    purpose=purpose,
                )
                self._store.complete_credential_reservation(
                    request_id,
                    tenant_id=tenant_id,
                    reservation_key=reservation_key,
                    credential_ref=receipt.opaque_reference,
                    now=self._clock(),
                )
            except Exception:
                try:
                    if isinstance(receipt, FactoryCredentialReceipt):
                        self._revoke_pending(
                            request_id=request_id, tenant_id=tenant_id,
                            reservation_key=reservation_key,
                            credential_ref=receipt.opaque_reference,
                        )
                except Exception:
                    # Leave the durable revoke-pending record for startup retry.
                    continue

    @staticmethod
    def _validate_receipt(
        receipt: FactoryCredentialReceipt,
        *,
        reservation_key: str,
        tenant_id: str,
        request_id: UUID,
        expected_subject_version: int,
        purpose: str,
    ) -> None:
        if not isinstance(receipt, FactoryCredentialReceipt) or (
            receipt.reservation_key != reservation_key
            or receipt.tenant_id != tenant_id
            or receipt.request_id != request_id
            or receipt.request_version != expected_subject_version
            or receipt.purpose != purpose
            or not receipt.opaque_reference.startswith(f"credential://{tenant_id}/")
        ):
            raise ValueError("credential receipt conflicts with reservation")


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
                    ("expected_verification_workflow_sha256", "CHAR(64) NULL"),
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
                        selected_credential_id, expected_verification_workflow_sha256,
                        verification_workflow_sha256,
                        expires_at, used_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, NULL)""",
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
                        current_fence.expected_verification_workflow_sha256,
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
                              expected_verification_workflow_sha256,
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
            expected_verification_workflow_sha256=row[
                "expected_verification_workflow_sha256"
            ],
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
            expected_verification_workflow_sha256=requirement.get(
                "verification_workflow_sha256"
            ),
            verification_workflow_sha256=(
                None if receipt is None else receipt["workflow_content_sha256"]
            ),
        )
    except (KeyError, TypeError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
