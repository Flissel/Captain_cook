"""Secret-free contracts for provider, restart and aggregate portal evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_runtime.contracts import ArtifactRef, IDENTIFIER_PATTERN
from agenten.agent_factory.gitea_template_contracts import GiteaTemplateReleaseV1
from agenten.delivery.minibook_events import MinibookProjectionRebuildReceiptV1


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
    provider_proof_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_probe_id: str = Field(min_length=1, max_length=128)
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


class PortalLiveRunFinalizationV1(_FrozenContract):
    decision_request_id: UUID
    run_id: str = Field(pattern=IDENTIFIER_PATTERN)
    job_id: UUID
    correlation_id: UUID
    provider_trace_ids: tuple[UUID, ...]
    restart_id: UUID
    minibook_rebuild_id: UUID
    policy_version: str = Field(pattern=IDENTIFIER_PATTERN)
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def require_decision_at_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("live decision timestamp must be UTC")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def require_three_unique_traces(self) -> "PortalLiveRunFinalizationV1":
        if len(self.provider_trace_ids) != 3:
            raise ValueError("live finalization requires exactly three provider traces")
        if len(set(self.provider_trace_ids)) != 3:
            raise ValueError("live finalization provider traces must be unique")
        return self


class PortalLiveRunDecisionV1(_FrozenContract):
    decision_id: UUID
    decision_request_id: UUID
    run_id: str = Field(pattern=IDENTIFIER_PATTERN)
    job_id: UUID
    correlation_id: UUID
    setup_revision: int = Field(ge=1, strict=True)
    setup_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_traces: tuple[PortalProviderProbeCompletionV1, ...]
    restart_receipt: PortalRestartReceiptV1
    minibook_rebuild_receipt: MinibookProjectionRebuildReceiptV1
    gateway_execution_ref: ArtifactRef
    policy_version: str = Field(pattern=IDENTIFIER_PATTERN)
    status: Literal["accepted"]
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def require_accepted_at_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("live decision timestamp must be UTC")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def require_complete_evidence_fences(self) -> "PortalLiveRunDecisionV1":
        if len(self.provider_traces) != 3 or len(
            {trace.trace_id for trace in self.provider_traces}
        ) != 3:
            raise ValueError("accepted live decision requires three unique provider traces")
        kinds = {trace.integration_kind for trace in self.provider_traces}
        if kinds != {"bearer", "oauth2"}:
            raise ValueError("accepted live decision requires Bearer and OAuth evidence")
        bound = (
            self.run_id,
            self.job_id,
            self.correlation_id,
            self.setup_revision,
            self.setup_content_sha256,
        )
        if any(
            (
                trace.run_id,
                trace.job_id,
                trace.correlation_id,
                trace.setup_revision,
                trace.setup_content_sha256,
            )
            != bound
            for trace in self.provider_traces
        ):
            raise ValueError("provider trace does not match accepted live run")
        restart = self.restart_receipt
        rebuild = self.minibook_rebuild_receipt
        if (
            (restart.run_id, restart.job_id, restart.correlation_id,
             restart.setup_revision, restart.setup_content_sha256) != bound
            or (rebuild.run_id, rebuild.job_id, rebuild.correlation_id,
                rebuild.setup_revision, rebuild.setup_content_sha256) != bound
        ):
            raise ValueError("restart or Minibook evidence does not match accepted live run")
        evidence_times = (
            *(trace.occurred_at for trace in self.provider_traces),
            restart.occurred_at,
            rebuild.occurred_at,
        )
        if any(value >= self.occurred_at for value in evidence_times):
            raise ValueError("accepted live decision must follow all evidence")
        return self


class PortalLiveEvidenceQueryV1(_FrozenContract):
    run_id: str = Field(pattern=IDENTIFIER_PATTERN)
    job_id: UUID
    correlation_id: UUID


class PortalLiveEvidenceV1(PortalLiveEvidenceQueryV1):
    provider_traces: tuple[PortalProviderProbeCompletionV1, ...]
    gitea_release_sha256: tuple[str, ...]
    gateway_decision_ref: ArtifactRef
    gateway_execution_ref: ArtifactRef
    restart_ref: ArtifactRef
    minibook_projection_ref: ArtifactRef
    minibook_rebuild_ref: ArtifactRef
    status: Literal["accepted"]
