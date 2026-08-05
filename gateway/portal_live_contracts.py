"""Secret-free contracts for provider, restart and aggregate portal evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_runtime.contracts import ArtifactRef, IDENTIFIER_PATTERN
from agenten.agent_factory.gitea_template_contracts import GiteaTemplateReleaseV1


class _FrozenContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )


class PortalProviderProbeRequestV1(_FrozenContract):
    probe_request_id: UUID
    run_id: str = Field(pattern=IDENTIFIER_PATTERN)
    job_id: UUID
    correlation_id: UUID
    integration_kind: Literal["bearer", "oauth2"]
    credential_alias: str = Field(pattern=IDENTIFIER_PATTERN)
    credential_id: str = Field(pattern=r"^\S{1,256}$")
    setup_revision: int = Field(ge=1, strict=True)
    setup_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verification_template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PortalProviderProbeCompletionV1(_FrozenContract):
    probe_request_id: UUID
    trace_id: UUID
    run_id: str = Field(pattern=IDENTIFIER_PATTERN)
    job_id: UUID
    correlation_id: UUID
    integration_kind: Literal["bearer", "oauth2"]
    credential_alias: str = Field(pattern=IDENTIFIER_PATTERN)
    credential_id: str = Field(pattern=r"^\S{1,256}$")
    setup_revision: int = Field(ge=1, strict=True)
    setup_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    template_ref: ArtifactRef
    template_release: GiteaTemplateReleaseV1
    deployed_workflow_ref: ArtifactRef
    execution_ref: ArtifactRef
    consent_ref: ArtifactRef | None = None
    callback_ref: ArtifactRef | None = None
    status: Literal["passed"]
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("provider probe timestamp must be UTC")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def require_kind_specific_evidence(self) -> "PortalProviderProbeCompletionV1":
        if self.template_release.sha256 != self.template_ref.sha256:
            raise ValueError("provider probe Gitea release digest mismatch")
        oauth_refs = (self.consent_ref, self.callback_ref)
        if self.integration_kind == "oauth2" and not all(oauth_refs):
            raise ValueError("OAuth completion requires consent and callback evidence")
        if self.integration_kind == "bearer" and any(oauth_refs):
            raise ValueError("Bearer completion cannot contain OAuth evidence")
        return self


class PortalProviderProbeWriteReceiptV1(_FrozenContract):
    probe_request_id: UUID
    trace_id: UUID | None = None
    status: Literal["started", "passed"]
    replayed: bool


class PortalProviderProbeStartedV1(_FrozenContract):
    request: PortalProviderProbeRequestV1
    occurred_at: datetime
    status: Literal["started"] = "started"

    @field_validator("occurred_at")
    @classmethod
    def require_started_at_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("provider probe start timestamp must be UTC")
        return value.astimezone(timezone.utc)


class PortalProviderAuditQueryV1(_FrozenContract):
    run_id: str = Field(pattern=IDENTIFIER_PATTERN)
    job_id: UUID
    correlation_id: UUID


class PortalProviderAuditV1(PortalProviderAuditQueryV1):
    invocation_count: int = Field(ge=0, strict=True)
    completion_count: int = Field(ge=0, strict=True)
    trace_ids: tuple[UUID, ...]
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def require_observed_at_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("provider audit timestamp must be UTC")
        return value.astimezone(timezone.utc)


class PortalRestartReceiptV1(_FrozenContract):
    restart_request_id: UUID
    restart_id: UUID
    run_id: str = Field(pattern=IDENTIFIER_PATTERN)
    job_id: UUID
    correlation_id: UUID
    services: tuple[Literal["gateway", "portal"], ...]
    previous_gateway_boot_id: str = Field(min_length=1, max_length=128)
    new_gateway_boot_id: str = Field(min_length=1, max_length=128)
    portal_deployment_id: str = Field(min_length=1, max_length=256)
    setup_revision: int = Field(ge=1, strict=True)
    setup_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["resumed"]
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def require_restart_at_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("restart receipt timestamp must be UTC")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def require_exact_restart_scope(self) -> "PortalRestartReceiptV1":
        if self.services != ("gateway", "portal"):
            raise ValueError("restart receipt requires exactly gateway and portal")
        if self.previous_gateway_boot_id == self.new_gateway_boot_id:
            raise ValueError("restart boot identity must change")
        return self
