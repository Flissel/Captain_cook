"""Strict JSON contracts for the Minibook creation boundary."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SHA256_PATTERN = r"^[0-9a-f]{64}$"
IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_SECRET_PATTERN = re.compile(r"(?i)(api.?key|authorization|credential|password|secret|token)")


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class ArtifactRef(_FrozenContract):
    uri: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)
    media_type: str = Field(pattern=r"^[a-z0-9.+-]+/[a-z0-9.+-]+$")

    @field_validator("uri")
    @classmethod
    def require_opaque_uri(cls, value: str) -> str:
        if not value.startswith("artifact://"):
            raise ValueError("artifact refs must use artifact:// URIs")
        return value


class ReleasedSkillRefV1(_FrozenContract):
    skill_id: str = Field(pattern=IDENTIFIER_PATTERN)
    version: int = Field(ge=1, strict=True)
    content_ref: ArtifactRef
    content_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def require_digest_binding(self) -> "ReleasedSkillRefV1":
        if self.content_sha256 != self.content_ref.sha256:
            raise ValueError("released skill digest must match content_ref")
        return self


class DocumentationQuery(_FrozenContract):
    ecosystem: Literal["autogen", "n8n"]
    package_id: str = Field(pattern=IDENTIFIER_PATTERN)
    installed_version: str = Field(min_length=1)
    query: str = Field(min_length=1, max_length=500)
    required: bool


class DocumentationEvidence(_FrozenContract):
    query: DocumentationQuery
    query_sha256: str = Field(pattern=SHA256_PATTERN)
    retrieved_version: str = Field(min_length=1)
    retrieved_at: datetime
    source_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    content_sha256: str = Field(pattern=SHA256_PATTERN)

    @field_validator("retrieved_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("timestamp must be UTC")
        return value


class IntegrationRequirementV1(_FrozenContract):
    integration_id: str = Field(pattern=IDENTIFIER_PATTERN)
    kind: str = Field(pattern=IDENTIFIER_PATTERN)
    severity: Literal["required", "optional"]
    input_contract_ref: ArtifactRef
    output_contract_ref: ArtifactRef


class ToolGapMarkerV1(_FrozenContract):
    schema_name: Literal["TODO_TOOL.v1"] = Field(alias="schema", serialization_alias="schema")
    gap_id: str = Field(pattern=IDENTIFIER_PATTERN)
    severity: Literal["required", "optional"]
    evidence_ref: ArtifactRef
    status: Literal["unresolved", "resolved"]


class CreationArtifact(_FrozenContract):
    artifact_id: str = Field(pattern=IDENTIFIER_PATTERN)
    kind: str = Field(pattern=IDENTIFIER_PATTERN)
    ref: ArtifactRef
    relative_path: str = Field(min_length=1)

    @field_validator("relative_path")
    @classmethod
    def require_safe_relative_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
            raise ValueError("artifact path must be relative")
        if ".." in normalized.split("/"):
            raise ValueError("artifact path must not traverse")
        return normalized


class CreationFailure(_FrozenContract):
    code: Literal[
        "documentation_unavailable",
        "tool_unresolved",
        "codex_failed",
        "n8n_failed",
        "build_failed",
        "validation_failed",
        "deadline_expired",
        "cancelled",
        "internal_error",
    ]
    summary: str = Field(min_length=1, max_length=300)
    exception_type: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
    evidence_refs: tuple[ArtifactRef, ...] = ()

    @field_validator("summary")
    @classmethod
    def reject_secret_like_summary(cls, value: str) -> str:
        if _SECRET_PATTERN.search(value):
            raise ValueError("failure summary contains secret-like material")
        return value


class CreationJobV1(_FrozenContract):
    schema_name: Literal["minibook.creation-job.v1"] = Field(alias="schema", serialization_alias="schema")
    creation_job_id: UUID
    factory_job_id: UUID
    correlation_id: UUID
    causation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    idempotency_key: str = Field(pattern=SHA256_PATTERN)
    input_ref: ArtifactRef
    compiled_spec_ref: ArtifactRef
    dependency_graph_ref: ArtifactRef
    released_skill: ReleasedSkillRefV1
    public_assertion_ids: tuple[str, ...] = Field(min_length=1)
    deadline_at: datetime

    @field_validator("deadline_at")
    @classmethod
    def require_deadline_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("deadline_at must be UTC")
        return value

    @field_validator("public_assertion_ids")
    @classmethod
    def unique_assertions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item for item in value):
            raise ValueError("public assertion ids must be unique and nonblank")
        return value


class CreationSubmissionReceipt(_FrozenContract):
    creation_job_id: UUID
    status: Literal["queued"]
    subject_version: int = Field(ge=1, strict=True)
    replayed: bool = False


class CreationProgressV1(_FrozenContract):
    schema_name: Literal["minibook.creation-progress.v1"] = Field(alias="schema", serialization_alias="schema")
    creation_job_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    status: Literal["queued", "running", "blocked", "failed", "cancelled", "succeeded"]
    checkpoint: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    version: int = Field(ge=1, strict=True)


class CreationResultV1(_FrozenContract):
    schema_name: Literal["minibook.creation-result.v1"] = Field(alias="schema", serialization_alias="schema")
    creation_job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    status: Literal["succeeded", "failed", "blocked", "cancelled"]
    package_manifest_ref: ArtifactRef | None = None
    artifact_refs: tuple[ArtifactRef, ...] = ()
    evidence_refs: tuple[ArtifactRef, ...] = ()
    tool_gaps: tuple[ToolGapMarkerV1, ...] = ()
    skill_usage_receipt_ref: ArtifactRef | None = None
    private_skill_candidate_ref: ArtifactRef | None = None
    failure: CreationFailure | None = None

    @model_validator(mode="after")
    def require_status_payload(self) -> "CreationResultV1":
        if self.status == "succeeded":
            if self.package_manifest_ref is None:
                raise ValueError("succeeded result requires package manifest")
            if self.skill_usage_receipt_ref is None:
                raise ValueError("succeeded result requires skill receipt")
            if self.failure is not None:
                raise ValueError("succeeded result cannot include failure")
        elif self.failure is None:
            raise ValueError("non-success result requires failure")
        return self


class FactoryBuildAssignmentV1(_FrozenContract):
    schema_name: Literal["hermes.factory-build-assignment.v1"] = Field(alias="schema", serialization_alias="schema")
    assignment_id: UUID
    creation_job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    idempotency_key: str = Field(pattern=SHA256_PATTERN)
    released_skill: ReleasedSkillRefV1
    compiled_spec_ref: ArtifactRef
    dependency_graph_ref: ArtifactRef
    workspace_ref: str = Field(pattern=r"^workspace://[A-Za-z0-9._:/-]+$")
    documentation_queries: tuple[DocumentationQuery, ...] = Field(min_length=1)
    integrations: tuple[IntegrationRequirementV1, ...] = ()
    public_assertion_ids: tuple[str, ...] = Field(min_length=1)
    deadline_at: datetime

    @field_validator("deadline_at")
    @classmethod
    def require_assignment_deadline_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("deadline_at must be UTC")
        return value
