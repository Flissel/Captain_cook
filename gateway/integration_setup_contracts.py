"""Secret-free, versioned integration setup contracts owned by Captain."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urljoin, urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_factory.integration_setup import (
    IntegrationSetupPlanV1,
    IntegrationSetupStatus,
    N8nCredentialMetadataV1,
)


class _FrozenContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class IntegrationSetupSubmissionV1(_FrozenContract):
    """One immutable, digest-fenced setup projection submitted by Captain."""

    schema_name: Literal["captain.integration-setup-submission.v1"] = Field(
        default="captain.integration-setup-submission.v1",
        alias="schema",
        serialization_alias="schema",
    )
    event_id: UUID
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    revision: int = Field(ge=1, strict=True)
    previous_content_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    occurred_at: datetime
    change_kind: Literal["observed", "rotation_requested", "revoked"] = "observed"
    plan: IntegrationSetupPlanV1

    @field_validator("occurred_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("integration setup timestamp must be UTC")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def require_revision_fence(self) -> "IntegrationSetupSubmissionV1":
        if self.revision == 1 and self.previous_content_sha256 is not None:
            raise ValueError("first integration setup revision cannot name a previous digest")
        if self.revision > 1 and self.previous_content_sha256 is None:
            raise ValueError("later integration setup revisions require a previous digest")
        for connection in self.plan.connections:
            receipt = connection.verification_receipt
            if (
                connection.status is IntegrationSetupStatus.READY
                and receipt is not None
                and receipt.valid_until is not None
                and receipt.valid_until <= self.occurred_at
            ):
                raise ValueError("ready integration verification is expired")
            if (
                connection.status is IntegrationSetupStatus.EXPIRED
                and receipt is not None
                and receipt.valid_until is not None
                and receipt.valid_until > self.occurred_at
            ):
                raise ValueError("integration cannot be expired before its validity ends")
        return self


class IntegrationSetupWriteReceiptV1(_FrozenContract):
    event_id: UUID
    job_id: UUID
    revision: int = Field(ge=1, strict=True)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    replayed: bool


class PersistedIntegrationSetupV1(_FrozenContract):
    submission: IntegrationSetupSubmissionV1
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class IntegrationSetupMutationV1(_FrozenContract):
    schema_name: Literal["captain.integration-setup-mutation.v1"] = Field(
        default="captain.integration-setup-mutation.v1",
        alias="schema",
        serialization_alias="schema",
    )
    event_id: UUID
    credential_alias: str
    expected_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    occurred_at: datetime
    action: Literal["rotation_requested", "revoked"]

    @field_validator("occurred_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("integration setup mutation timestamp must be UTC")
        return value.astimezone(timezone.utc)


def apply_integration_setup_mutation(
    persisted: PersistedIntegrationSetupV1,
    mutation: IntegrationSetupMutationV1,
) -> IntegrationSetupSubmissionV1:
    """Create the next full snapshot for one controlled rotation or revoke."""

    if mutation.expected_content_sha256 != persisted.content_sha256:
        raise ValueError("integration setup mutation digest fence mismatch")
    connections = persisted.submission.plan.connections
    matches = tuple(
        (index, connection)
        for index, connection in enumerate(connections)
        if connection.requirement.credential_alias == mutation.credential_alias
    )
    if len(matches) != 1:
        raise ValueError("integration setup mutation credential alias is unavailable")
    target_index, target = matches[0]
    if target.selected_credential is None:
        raise ValueError("integration setup mutation requires a selected credential")
    if target.status is IntegrationSetupStatus.REVOKED:
        raise ValueError("integration setup credential is already revoked")
    if (
        mutation.action == "rotation_requested"
        and target.status is IntegrationSetupStatus.VERIFICATION_REQUIRED
    ):
        raise ValueError("integration setup credential already requires verification")

    status = (
        IntegrationSetupStatus.VERIFICATION_REQUIRED
        if mutation.action == "rotation_requested"
        else IntegrationSetupStatus.REVOKED
    )
    changed = target.model_copy(
        update={
            "status": status,
            "verification_receipt": None,
        }
    )
    updated_connections = tuple(
        changed if index == target_index else connection
        for index, connection in enumerate(connections)
    )
    current = persisted.submission
    return IntegrationSetupSubmissionV1(
        event_id=mutation.event_id,
        job_id=current.job_id,
        correlation_id=current.correlation_id,
        subject_version=current.subject_version,
        revision=current.revision + 1,
        previous_content_sha256=persisted.content_sha256,
        occurred_at=mutation.occurred_at,
        change_kind=mutation.action,
        plan=current.plan.model_copy(update={"connections": updated_connections}),
    )


def validate_integration_setup_transition(
    previous: IntegrationSetupSubmissionV1,
    current: IntegrationSetupSubmissionV1,
) -> None:
    """Validate immutable requirements and fresh evidence after invalidation."""

    if current.occurred_at <= previous.occurred_at:
        raise ValueError("integration setup transition time must increase")
    prior_requirements = tuple(
        connection.requirement for connection in previous.plan.connections
    )
    current_requirements = tuple(
        connection.requirement for connection in current.plan.connections
    )
    if current_requirements != prior_requirements:
        raise ValueError("integration setup requirements are immutable")

    for prior, next_connection in zip(
        previous.plan.connections,
        current.plan.connections,
        strict=True,
    ):
        was_invalidated = (
            previous.change_kind == "rotation_requested"
            and prior.status is IntegrationSetupStatus.VERIFICATION_REQUIRED
        ) or prior.status in {
            IntegrationSetupStatus.REVOKED,
            IntegrationSetupStatus.EXPIRED,
        }
        if was_invalidated and next_connection.status is IntegrationSetupStatus.READY:
            receipt = next_connection.verification_receipt
            if receipt is None or receipt.occurred_at <= previous.occurred_at:
                raise ValueError(
                    "invalidated integration requires fresh provider verification"
                )


class IntegrationSetupActionV1(_FrozenContract):
    integration_key: str
    credential_alias: str
    credential_type: str
    setup_label: str
    required: bool
    status: IntegrationSetupStatus
    candidate_credentials: tuple[N8nCredentialMetadataV1, ...]
    selected_credential: N8nCredentialMetadataV1 | None = None


class IntegrationSetupSurfaceV1(_FrozenContract):
    job_id: UUID
    revision: int = Field(ge=1, strict=True)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    overall_status: IntegrationSetupStatus
    n8n_credentials_url: str
    actions: tuple[IntegrationSetupActionV1, ...]


def build_integration_setup_surface(
    persisted: PersistedIntegrationSetupV1,
    *,
    n8n_ui_base_url: str,
) -> IntegrationSetupSurfaceV1:
    """Build the authenticated UI model; secret collection remains in n8n."""

    parsed = urlsplit(n8n_ui_base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("n8n UI base URL must be a safe HTTP URL")
    credential_url = urljoin(n8n_ui_base_url.rstrip("/") + "/", "home/credentials")
    connections = persisted.submission.plan.connections
    priority = {
        IntegrationSetupStatus.REVOKED: 0,
        IntegrationSetupStatus.EXPIRED: 1,
        IntegrationSetupStatus.VERIFICATION_FAILED: 2,
        IntegrationSetupStatus.MISSING: 3,
        IntegrationSetupStatus.SELECTION_REQUIRED: 4,
        IntegrationSetupStatus.VERIFICATION_REQUIRED: 5,
        IntegrationSetupStatus.READY: 6,
    }
    required_statuses = tuple(
        connection.status
        for connection in connections
        if connection.requirement.required
    )
    overall_status = (
        min(required_statuses, key=priority.__getitem__)
        if required_statuses
        else IntegrationSetupStatus.READY
    )
    actions = tuple(
        IntegrationSetupActionV1(
            integration_key=connection.requirement.integration_key,
            credential_alias=connection.requirement.credential_alias,
            credential_type=connection.requirement.credential_type,
            setup_label=connection.requirement.setup_label,
            required=connection.requirement.required,
            status=connection.status,
            candidate_credentials=connection.candidate_credentials,
            selected_credential=connection.selected_credential,
        )
        for connection in connections
    )
    return IntegrationSetupSurfaceV1(
        job_id=persisted.submission.job_id,
        revision=persisted.submission.revision,
        content_sha256=persisted.content_sha256,
        overall_status=overall_status,
        n8n_credentials_url=credential_url,
        actions=actions,
    )
