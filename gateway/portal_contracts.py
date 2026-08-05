"""Secret-free, immutable contracts for the integration setup portal."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agenten.agent_runtime.contracts import IDENTIFIER_PATTERN


class _FrozenContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        populate_by_name=True,
    )


class PortalPrincipalV1(_FrozenContract):
    """The portal's authenticated tenant identity."""

    subject_id: str = Field(pattern=IDENTIFIER_PATTERN)
    organization_id: str = Field(pattern=IDENTIFIER_PATTERN)


class PortalTenantBindingV1(_FrozenContract):
    """Captain-provisioned ownership of one integration setup."""

    job_id: UUID
    organization_id: str = Field(pattern=IDENTIFIER_PATTERN)


class PortalSetupTicketRequestV1(_FrozenContract):
    """The tenant-bound request Captain validates before issuing a ticket."""

    job_id: UUID
    organization_id: str = Field(pattern=IDENTIFIER_PATTERN)
    subject_id: str = Field(pattern=IDENTIFIER_PATTERN)
    credential_alias: str = Field(pattern=IDENTIFIER_PATTERN)
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def require_short_lived_expiry(self) -> Self:
        issued_at = _require_utc(self.issued_at)
        expires_at = _require_utc(self.expires_at)
        if not issued_at < expires_at <= issued_at + timedelta(minutes=10):
            raise ValueError("portal ticket expiry must be at most ten minutes")
        return self


class PortalSetupTicketV1(_FrozenContract):
    """Opaque, short-lived ticket returned to the portal after issuance."""

    ticket_id: UUID
    ticket: str = Field(min_length=1)
    job_id: UUID
    credential_alias: str = Field(pattern=IDENTIFIER_PATTERN)
    expires_at: datetime

    @model_validator(mode="after")
    def require_utc_expiry(self) -> Self:
        _require_utc(self.expires_at)
        return self


class PortalSetupActionRequestV1(_FrozenContract):
    """A ticket-bound, explicit lifecycle action for one credential alias."""

    ticket_id: UUID
    ticket: str = Field(min_length=1)
    credential_alias: str = Field(pattern=IDENTIFIER_PATTERN)
    action: Literal["rotation_requested", "revoked"]


PortalTicketAction = Literal[
    "discover",
    "select",
    "rotation_requested",
    "revoked",
]


class PortalSetupTicketIssueV1(_FrozenContract):
    """Secret-free request for one exact portal operation ticket."""

    credential_alias: str = Field(pattern=IDENTIFIER_PATTERN)
    action: PortalTicketAction


class PortalSetupTicketUseV1(_FrozenContract):
    """Opaque ticket proof submitted to its single-purpose route."""

    ticket_id: UUID
    ticket: str = Field(min_length=1, max_length=256)
    credential_alias: str = Field(pattern=IDENTIFIER_PATTERN)


class PortalSetupSelectionRequestV1(PortalSetupTicketUseV1):
    """Explicit secret-free selection of one discovered n8n credential ID."""

    credential_id: str = Field(pattern=r"^\S{1,256}$")


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("portal ticket timestamps must be UTC")
    return value.astimezone(timezone.utc)
