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
    schema_name: Literal["TODO_TOOL.v1"] = Field(default="TODO_TOOL.v1", alias="schema", serialization_alias="schema")
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
    schema_name: Literal["minibook.creation-job.v1"] = Field(default="minibook.creation-job.v1", alias="schema", serialization_alias="schema")
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
    schema_name: Literal["minibook.creation-progress.v1"] = Field(default="minibook.creation-progress.v1", alias="schema", serialization_alias="schema")
    creation_job_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    status: Literal["queued", "running", "blocked", "failed", "cancelled", "succeeded"]
    checkpoint: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    version: int = Field(ge=1, strict=True)


class CreationResultV1(_FrozenContract):
    schema_name: Literal["minibook.creation-result.v1"] = Field(default="minibook.creation-result.v1", alias="schema", serialization_alias="schema")
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


class FactoryEvidenceBlockV1(_FrozenContract):
    """Hermes-authored evidence projected by Minibook, never Captain authority."""

    schema_name: Literal["captain.agent-factory-block.v1"] = Field(
        default="captain.agent-factory-block.v1",
        alias="schema",
        serialization_alias="schema",
    )
    event_id: UUID
    job_id: UUID
    correlation_id: UUID
    causation_id: UUID
    occurred_at: datetime
    producer: Literal["hermes"]
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    phase: Literal[
        "blueprint_created",
        "tool_candidate_tested",
        "agent_code_created",
    ]
    role: Literal["agent_architect", "tool_integrator"]
    status: Literal["succeeded"]
    artifact_refs: tuple[ArtifactRef, ...] = ()
    evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    assertion_ids: tuple[str, ...] = ()
    lease_id: str = Field(pattern=IDENTIFIER_PATTERN)

    @field_validator("occurred_at")
    @classmethod
    def require_evidence_timestamp_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("evidence timestamp must be UTC")
        return value

    @field_validator("assertion_ids")
    @classmethod
    def require_unique_evidence_assertions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item for item in value):
            raise ValueError("evidence assertion ids must be unique and nonblank")
        return value

    @model_validator(mode="after")
    def require_role_and_content_addresses(self) -> "FactoryEvidenceBlockV1":
        expected_role = {
            "blueprint_created": "agent_architect",
            "tool_candidate_tested": "tool_integrator",
            "agent_code_created": "tool_integrator",
        }[self.phase]
        if self.role != expected_role:
            raise ValueError("factory evidence role does not match its phase")
        for reference in (*self.artifact_refs, *self.evidence_refs):
            _require_content_address(reference)
        return self


class CreationPreparationEvidenceV1(_FrozenContract):
    schema_name: Literal["minibook.creation-preparation-evidence.v1"] = Field(
        default="minibook.creation-preparation-evidence.v1",
        alias="schema",
        serialization_alias="schema",
    )
    creation_job: CreationJobV1
    blocks: tuple[FactoryEvidenceBlockV1, FactoryEvidenceBlockV1]

    @model_validator(mode="after")
    def require_exact_preparation_chain(self) -> "CreationPreparationEvidenceV1":
        job = self.creation_job
        if tuple(block.phase for block in self.blocks) != (
            "blueprint_created",
            "tool_candidate_tested",
        ):
            raise ValueError("preparation evidence requires blueprint then tool evidence")
        if self.blocks[0].occurred_at >= self.blocks[1].occurred_at:
            raise ValueError("preparation evidence timestamps must be monotonic")
        if self.blocks[1].occurred_at >= job.deadline_at:
            raise ValueError("preparation evidence must precede the creation deadline")
        if len({block.event_id for block in self.blocks}) != 2:
            raise ValueError("preparation evidence event ids must be unique")
        for block in self.blocks:
            _require_block_job_binding(job, block)
        return self


class CreationCompletionEvidenceV1(_FrozenContract):
    schema_name: Literal["minibook.creation-completion-evidence.v1"] = Field(
        default="minibook.creation-completion-evidence.v1",
        alias="schema",
        serialization_alias="schema",
    )
    result: CreationResultV1
    block: FactoryEvidenceBlockV1

    @model_validator(mode="after")
    def require_success_content_chain(self) -> "CreationCompletionEvidenceV1":
        result = self.result
        block = self.block
        if result.status != "succeeded":
            raise ValueError("completion evidence requires a succeeded result")
        if block.phase != "agent_code_created":
            raise ValueError("completion evidence requires agent code evidence")
        assert result.package_manifest_ref is not None
        assert result.skill_usage_receipt_ref is not None
        references = (
            result.package_manifest_ref,
            result.skill_usage_receipt_ref,
            *result.artifact_refs,
            *result.evidence_refs,
        )
        if result.private_skill_candidate_ref is not None:
            references = (*references, result.private_skill_candidate_ref)
        for reference in references:
            _require_content_address(reference)
        if block.evidence_refs != (
            result.package_manifest_ref,
            result.skill_usage_receipt_ref,
        ):
            raise ValueError("completion block must bind package and skill receipt")
        return self


class CreationEvidenceReceiptV1(_FrozenContract):
    creation_job_id: UUID
    replayed: bool = False


def _require_content_address(reference: ArtifactRef) -> None:
    if (
        "?" in reference.uri
        or "#" in reference.uri
        or reference.uri.rstrip("/").rsplit("/", 1)[-1] != reference.sha256
    ):
        raise ValueError("evidence refs must be content-addressed")


def _require_block_job_binding(
    job: CreationJobV1,
    block: FactoryEvidenceBlockV1,
) -> None:
    if (
        block.job_id != job.factory_job_id
        or block.correlation_id != job.correlation_id
        or block.causation_id != job.causation_id
        or block.subject_version != job.subject_version
        or block.attempt != job.attempt
    ):
        raise ValueError("factory evidence is not bound to the creation job")


class FactoryBuildAssignmentV1(_FrozenContract):
    schema_name: Literal["hermes.factory-build-assignment.v1"] = Field(default="hermes.factory-build-assignment.v1", alias="schema", serialization_alias="schema")
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
