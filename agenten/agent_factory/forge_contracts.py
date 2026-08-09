"""Strict JSON contracts for the Minibook creation boundary."""
from __future__ import annotations

import hashlib
import json
from math import log2
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


class ForgeBuildSkillUsageReceiptV1(_FrozenContract):
    """Hermes evidence that the exact released build skill fulfilled one Forge job."""

    schema_name: Literal["hermes.forge-build-skill-usage-receipt.v1"] = Field(
        default="hermes.forge-build-skill-usage-receipt.v1",
        alias="schema",
        serialization_alias="schema",
    )
    producer: Literal["hermes"]
    outcome: Literal["fulfilled"]
    creation_job_id: UUID
    factory_job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    idempotency_key: str = Field(pattern=SHA256_PATTERN)
    released_skill: ReleasedSkillRefV1
    public_assertion_ids: tuple[str, ...] = Field(min_length=1)
    evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)

    @field_validator("public_assertion_ids")
    @classmethod
    def require_unique_assertions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item for item in value):
            raise ValueError("public assertion ids must be unique and nonblank")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def require_unique_evidence_refs(
        cls,
        value: tuple[ArtifactRef, ...],
    ) -> tuple[ArtifactRef, ...]:
        identities = tuple((ref.uri, ref.sha256, ref.media_type) for ref in value)
        if len(identities) != len(set(identities)):
            raise ValueError("skill usage evidence refs must be unique")
        return value


class CreationPackageManifestV1(_FrozenContract):
    """Content-addressed binding from one Forge result to executable bytes."""

    schema_name: Literal["minibook.creation-package-manifest.v1"] = Field(
        default="minibook.creation-package-manifest.v1",
        alias="schema",
        serialization_alias="schema",
    )
    creation_job_id: UUID
    factory_job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    candidate_manifest_ref: ArtifactRef
    source_archive_ref: ArtifactRef
    skill_usage_receipt_ref: ArtifactRef

    @model_validator(mode="after")
    def require_executable_media_types(self) -> "CreationPackageManifestV1":
        if self.candidate_manifest_ref.media_type != "application/json":
            raise ValueError("candidate manifest must be application/json")
        if self.source_archive_ref.media_type != "application/zip":
            raise ValueError("candidate source must be application/zip")
        if self.skill_usage_receipt_ref.media_type != "application/json":
            raise ValueError("skill usage receipt must be application/json")
        return self


class CreationPackageManifestV2(CreationPackageManifestV1):
    """Minibook package retaining the exact Captain Codex receipt edge."""

    schema_name: Literal["minibook.creation-package-manifest.v2"] = Field(
        default="minibook.creation-package-manifest.v2",
        alias="schema",
        serialization_alias="schema",
    )
    codex_build_receipt_ref: ArtifactRef

    @field_validator("codex_build_receipt_ref")
    @classmethod
    def require_codex_receipt_json(cls, value: ArtifactRef) -> ArtifactRef:
        if value.media_type != "application/json":
            raise ValueError("Codex build receipt must be application/json")
        return value


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


class CodexBuildReceiptV1(_FrozenContract):
    """Captain-issued seal over one concrete Codex build and its evidence."""

    schema_name: Literal["captain.codex-build-receipt.v1"] = Field(
        default="captain.codex-build-receipt.v1",
        alias="schema",
        serialization_alias="schema",
    )
    receipt_id: UUID
    producer: Literal["captain"]
    outcome: Literal["sealed"]
    factory_job_id: UUID
    creation_job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    assignment_id: UUID
    idempotency_key: str = Field(pattern=SHA256_PATTERN)
    seal_idempotency_key: str = Field(pattern=SHA256_PATTERN)
    build_brief_ref: ArtifactRef
    workspace_ref: str = Field(pattern=r"^workspace://[A-Za-z0-9._:/-]+$")
    codex_session_ref: ArtifactRef
    workspace_snapshot_ref: ArtifactRef
    candidate_manifest_ref: ArtifactRef
    source_archive_ref: ArtifactRef
    test_evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    acceptance_assertion_ids: tuple[str, ...] = Field(min_length=1)
    completed_at: datetime

    @field_validator("completed_at")
    @classmethod
    def require_completed_at_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("completed_at must be UTC")
        return value

    @field_validator("test_evidence_refs")
    @classmethod
    def require_unique_test_evidence_refs(
        cls,
        value: tuple[ArtifactRef, ...],
    ) -> tuple[ArtifactRef, ...]:
        identities = tuple((ref.uri, ref.sha256, ref.media_type) for ref in value)
        if len(identities) != len(set(identities)):
            raise ValueError("test evidence refs must be unique")
        return value

    @field_validator("acceptance_assertion_ids")
    @classmethod
    def require_unique_acceptance_assertions(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item for item in value):
            raise ValueError("acceptance assertion IDs must be unique and nonblank")
        return value

    @model_validator(mode="after")
    def require_sealed_media_and_distinct_refs(self) -> "CodexBuildReceiptV1":
        if self.candidate_manifest_ref.media_type != "application/json":
            raise ValueError("candidate manifest must be application/json")
        if self.source_archive_ref.media_type != "application/zip":
            raise ValueError("source archive must be application/zip")
        if self.workspace_snapshot_ref.media_type != "application/zip":
            raise ValueError("workspace snapshot must be application/zip")
        sealed_refs = (
            self.build_brief_ref,
            self.codex_session_ref,
            self.workspace_snapshot_ref,
            self.candidate_manifest_ref,
            self.source_archive_ref,
            *self.test_evidence_refs,
        )
        identities = tuple(
            (ref.uri, ref.sha256, ref.media_type) for ref in sealed_refs
        )
        if len(identities) != len(set(identities)):
            raise ValueError("sealed Codex build refs must be distinct")
        return self


def codex_build_receipt_sha256(receipt: CodexBuildReceiptV1) -> str:
    """Return the canonical digest used by a Captain receipt ArtifactRef."""

    canonical = json.dumps(
        receipt.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class CreationJobV2(CreationJobV1):
    """Minibook request bound to one Captain-sealed Codex build."""

    schema_name: Literal["minibook.creation-job.v2"] = Field(
        default="minibook.creation-job.v2",
        alias="schema",
        serialization_alias="schema",
    )
    source_archive_ref: ArtifactRef
    codex_build_receipt_ref: ArtifactRef
    codex_build_receipt: CodexBuildReceiptV1

    @model_validator(mode="after")
    def require_exact_codex_build_binding(self) -> "CreationJobV2":
        receipt = self.codex_build_receipt
        if (
            self.source_archive_ref.media_type != "application/zip"
            or self.codex_build_receipt_ref.media_type != "application/json"
            or self.codex_build_receipt_ref.sha256
            != codex_build_receipt_sha256(receipt)
            or receipt.creation_job_id != self.creation_job_id
            or receipt.factory_job_id != self.factory_job_id
            or receipt.correlation_id != self.correlation_id
            or receipt.subject_version != self.subject_version
            or receipt.attempt != self.attempt
            or receipt.idempotency_key != self.idempotency_key
            or receipt.acceptance_assertion_ids != self.public_assertion_ids
            or receipt.source_archive_ref.sha256 != self.source_archive_ref.sha256
            or receipt.source_archive_ref.media_type
            != self.source_archive_ref.media_type
        ):
            raise ValueError("Codex build receipt does not match creation job")
        return self


class PackageBuildEvidenceV1(_FrozenContract):
    schema_name: Literal["captain.package-build-evidence.v1"] = Field(default="captain.package-build-evidence.v1", alias="schema", serialization_alias="schema")
    producer: Literal["captain"]
    outcome: Literal["passed"]
    assignment_id: UUID; creation_job_id: UUID; factory_job_id: UUID; correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True); checkpoint_version: int = Field(ge=1, strict=True); attempt: int = Field(ge=1, le=5, strict=True)
    effect_key: str = Field(pattern=SHA256_PATTERN)
    source_archive_ref: ArtifactRef; codex_session_ref: ArtifactRef; workspace_snapshot_ref: ArtifactRef
    completed_at: datetime
    @model_validator(mode="after")
    def safe(self) -> "PackageBuildEvidenceV1":
        if self.source_archive_ref.media_type != "application/zip" or self.workspace_snapshot_ref.media_type != "application/zip": raise ValueError("package build archives must be ZIP")
        if self.completed_at.tzinfo is None or self.completed_at.utcoffset() != timezone.utc.utcoffset(self.completed_at): raise ValueError("completed_at must be UTC")
        return self


class PackageRealCaseEvidenceV1(_FrozenContract):
    schema_name: Literal["captain.package-real-case-evidence.v1"] = Field(default="captain.package-real-case-evidence.v1", alias="schema", serialization_alias="schema")
    producer: Literal["captain"]
    outcome: Literal["passed"]
    assignment_id: UUID; creation_job_id: UUID; factory_job_id: UUID; correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True); checkpoint_version: int = Field(ge=1, strict=True); attempt: int = Field(ge=1, le=5, strict=True)
    effect_key: str = Field(pattern=SHA256_PATTERN)
    source_archive_ref: ArtifactRef; codex_session_ref: ArtifactRef
    assertion_ids: tuple[str, ...] = Field(min_length=1)
    declared_test_paths: tuple[str, ...] = Field(min_length=1)
    evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    completed_at: datetime
    @model_validator(mode="after")
    def safe(self) -> "PackageRealCaseEvidenceV1":
        if self.source_archive_ref.media_type != "application/zip": raise ValueError("real-case source must be ZIP")
        if len(self.assertion_ids) != len(set(self.assertion_ids)) or len(self.declared_test_paths) != len(set(self.declared_test_paths)): raise ValueError("real-case assertions and test paths must be unique")
        if self.completed_at.tzinfo is None or self.completed_at.utcoffset() != timezone.utc.utcoffset(self.completed_at): raise ValueError("completed_at must be UTC")
        return self


class AgentHandoffV1(_FrozenContract):
    from_agent_id: str = Field(pattern=IDENTIFIER_PATTERN)
    to_agent_id: str = Field(pattern=IDENTIFIER_PATTERN)

    @model_validator(mode="after")
    def require_distinct_agents(self) -> "AgentHandoffV1":
        if self.from_agent_id == self.to_agent_id:
            raise ValueError("handoff agents must be distinct")
        return self


class FactoryToolRequirementV1(_FrozenContract):
    tool_id: str = Field(pattern=IDENTIFIER_PATTERN)
    required: bool


class ClarificationPolicyV1(_FrozenContract):
    allowed_request_types: tuple[Literal["clarification", "credential_required", "external_api_approval", "n8n_approval"], ...] = Field(min_length=1)
    max_requests: int = Field(ge=0, le=10, strict=True)


class FactoryBuildLimitsV1(_FrozenContract):
    max_wall_clock_seconds: int = Field(ge=1, strict=True)
    max_provider_cost_usd: float = Field(gt=0, strict=True)
    max_retries: int = Field(ge=0, le=5, strict=True)
    max_files: int = Field(ge=1, strict=True)


class CreationJobV3(CreationJobV1):
    schema_name: Literal["minibook.creation-job.v3"] = Field(default="minibook.creation-job.v3", alias="schema", serialization_alias="schema")
    command_id: UUID
    lease_ref: ArtifactRef
    lease_sha256: str = Field(pattern=SHA256_PATTERN)
    blueprint_ref: ArtifactRef
    tool_plan_ref: ArtifactRef
    required_agent_ids: tuple[str, ...] = Field(min_length=1)
    ordered_handoffs: tuple[AgentHandoffV1, ...]
    tool_requirements: tuple[FactoryToolRequirementV1, ...] = Field(min_length=1)
    workspace_policy_ref: ArtifactRef
    clarification_policy: ClarificationPolicyV1
    required_output_contract: Literal["minibook.creation-result.v2"]

    @field_validator("required_agent_ids")
    @classmethod
    def require_unique_agent_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not re.fullmatch(IDENTIFIER_PATTERN, agent_id) for agent_id in value):
            raise ValueError("required agent ids must be unique and valid")
        return value

    @model_validator(mode="after")
    def require_v3_bindings(self) -> "CreationJobV3":
        if self.lease_sha256 != self.lease_ref.sha256:
            raise ValueError("lease digest must match lease_ref")
        agents = set(self.required_agent_ids)
        if any(handoff.from_agent_id not in agents or handoff.to_agent_id not in agents for handoff in self.ordered_handoffs):
            raise ValueError("handoff agents must be required agents")
        if len({tool.tool_id for tool in self.tool_requirements}) != len(self.tool_requirements):
            raise ValueError("tool requirements must be unique")
        for ref in (self.lease_ref, self.blueprint_ref, self.tool_plan_ref, self.workspace_policy_ref):
            if ref.media_type != "application/json":
                raise ValueError("V3 binding refs must be application/json")
        return self


class FactoryBuildAssignmentV2(_FrozenContract):
    schema_name: Literal["hermes.factory-build-assignment.v2"] = Field(default="hermes.factory-build-assignment.v2", alias="schema", serialization_alias="schema")
    assignment_id: UUID
    creation_job_id: UUID
    factory_job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    checkpoint_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    effect_key: str = Field(pattern=SHA256_PATTERN)
    workspace_ref: str = Field(pattern=r"^workspace://[A-Za-z0-9._:/-]+$")
    workspace_policy_sha256: str = Field(pattern=SHA256_PATTERN)
    required_agent_ids: tuple[str, ...] = Field(min_length=1)
    ordered_handoffs: tuple[AgentHandoffV1, ...]
    tool_requirements: tuple[FactoryToolRequirementV1, ...] = Field(min_length=1)
    allowed_output_roots: tuple[str, ...] = Field(min_length=1)
    allowed_commands: tuple[tuple[str, ...], ...] = Field(min_length=1)
    limits: FactoryBuildLimitsV1
    lease_ref: ArtifactRef
    deadline_at: datetime

    @model_validator(mode="after")
    def require_assignment_safety(self) -> "FactoryBuildAssignmentV2":
        if len(self.required_agent_ids) != len(set(self.required_agent_ids)) or any(not re.fullmatch(IDENTIFIER_PATTERN, agent_id) for agent_id in self.required_agent_ids):
            raise ValueError("required agent ids must be unique and valid")
        agents = set(self.required_agent_ids)
        if any(h.from_agent_id not in agents or h.to_agent_id not in agents for h in self.ordered_handoffs):
            raise ValueError("handoff agents must be required agents")
        if any(not root or root == "." or root.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", root) or any(part in {".", ".."} for part in root.replace("\\", "/").split("/")) for root in self.allowed_output_roots):
            raise ValueError("output roots must be relative")
        for command in self.allowed_commands:
            if not command or any(not arg.strip() for arg in command):
                raise ValueError("allowed command must contain nonblank argv")
            executable = command[0]
            python_pytest = (
                executable in {"python", "python.exe", "python3", "python3.exe"}
                and command[1:] == ("-m", "pytest")
            )
            python_compileall = (
                executable in {"python", "python.exe", "python3", "python3.exe"}
                and len(command) == 4
                and command[1:3] == ("-m", "compileall")
                and command[3] in self.allowed_output_roots
            )
            allowed = python_pytest or python_compileall or (executable in {"pytest", "pytest.exe"} and len(command) == 1)
            if not allowed or any(
                re.search(r"[|&;*?`$<>]", arg) or arg.startswith(("/", "\\", ".")) or re.match(r"^[A-Za-z]:", arg) or any(part in {".", ".."} for part in arg.replace("\\", "/").split("/"))
                for arg in command
            ):
                raise ValueError("allowed command has unsafe argv")
        if self.lease_ref.media_type != "application/json":
            raise ValueError("lease ref must be application/json")
        if self.deadline_at.tzinfo is None or self.deadline_at.utcoffset() != timezone.utc.utcoffset(self.deadline_at):
            raise ValueError("deadline_at must be UTC")
        return self


class FactoryBuildEffectV2(_FrozenContract):
    schema_name: Literal["hermes.factory-build-effect.v2"] = Field(default="hermes.factory-build-effect.v2", alias="schema", serialization_alias="schema")
    assignment_id: UUID
    creation_job_id: UUID
    factory_job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    checkpoint_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    effect_key: str = Field(pattern=SHA256_PATTERN)
    hermes_usage_receipt_ref: ArtifactRef
    codex_build_receipt_ref: ArtifactRef
    codex_session_ref: ArtifactRef
    workspace_snapshot_ref: ArtifactRef
    source_archive_ref: ArtifactRef
    preliminary_team_manifest_ref: ArtifactRef
    build_evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    test_evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    tool_gaps: tuple[ToolGapMarkerV1, ...] = ()

    @model_validator(mode="after")
    def require_effect_media_types(self) -> "FactoryBuildEffectV2":
        if self.workspace_snapshot_ref.media_type != "application/zip" or self.source_archive_ref.media_type != "application/zip":
            raise ValueError("workspace snapshot and source archive must be application/zip")
        if self.preliminary_team_manifest_ref.media_type != "application/json":
            raise ValueError("preliminary team manifest must be application/json")
        return self


class CreationResultV2(_FrozenContract):
    schema_name: Literal["minibook.creation-result.v2"] = Field(default="minibook.creation-result.v2", alias="schema", serialization_alias="schema")
    creation_job_id: UUID
    factory_job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    status: Literal["succeeded", "failed", "blocked", "cancelled"]
    team_manifest_ref: ArtifactRef | None = None
    candidate_manifest_ref: ArtifactRef | None = None
    source_archive_ref: ArtifactRef | None = None
    package_manifest_ref: ArtifactRef | None = None
    hermes_usage_receipt_ref: ArtifactRef | None = None
    codex_build_receipt_ref: ArtifactRef | None = None
    build_evidence_refs: tuple[ArtifactRef, ...] = ()
    real_case_evidence_refs: tuple[ArtifactRef, ...] = ()
    cas_readback_receipt_ref: ArtifactRef | None = None
    tool_gaps: tuple[ToolGapMarkerV1, ...] = ()
    failure: CreationFailure | None = None

    @model_validator(mode="after")
    def require_v2_terminal_payload(self) -> "CreationResultV2":
        if self.status == "succeeded":
            required = (self.team_manifest_ref, self.candidate_manifest_ref, self.source_archive_ref, self.package_manifest_ref, self.hermes_usage_receipt_ref, self.codex_build_receipt_ref, self.cas_readback_receipt_ref)
            if any(ref is None for ref in required) or not self.build_evidence_refs or not self.real_case_evidence_refs:
                raise ValueError("successful V2 result requires all output contracts")
            if self.failure is not None:
                raise ValueError("successful V2 result cannot include failure")
            refs = (*required, *self.build_evidence_refs, *self.real_case_evidence_refs)
            identities = tuple((ref.uri, ref.sha256, ref.media_type) for ref in refs if ref is not None)
            if len(identities) != len(set(identities)):
                raise ValueError("successful V2 result refs must be distinct")
        elif self.failure is None:
            raise ValueError("non-success result requires failure")
        return self


def _is_explicit_digest_key(key: str | None) -> bool:
    return key is not None and bool(re.fullmatch(r"(?:[a-z0-9]+_)*(?:sha256|digest)(?:_[a-z0-9]+)*", key.casefold()))


def _is_opaque_secret_token(value: str, key: str | None) -> bool:
    if re.fullmatch(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", value):
        return False
    if key is not None and (key.casefold() == "id" or key.casefold().endswith("_id")) and re.fullmatch(IDENTIFIER_PATTERN, value):
        return False
    if _is_explicit_digest_key(key):
        return not bool(re.fullmatch(SHA256_PATTERN, value))
    if re.fullmatch(r"[0-9a-fA-F]{32,}", value):
        return not _is_explicit_digest_key(key)
    if not re.fullmatch(r"[A-Za-z0-9_+/-]{32,}={0,2}", value):
        return False
    entropy = -sum((value.count(character) / len(value)) * log2(value.count(character) / len(value)) for character in set(value))
    return entropy >= 3.5


def _reject_secret_like_content(value: object, *, key: str | None = None) -> object:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) != "credential_alias" and re.search(r"(?i)(api.?key|authorization|password|secret|token|prompt|holdout|provider.?response|credential|private.?key)", str(key)):
                raise ValueError("secret-like metadata is forbidden")
            _reject_secret_like_content(item, key=str(key))
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_secret_like_content(item, key=key)
    elif isinstance(value, str):
        if value.startswith("credential://") or re.search(r"(?i)(bearer\s+|basic\s+[A-Za-z0-9+/=]+|(?:api[_ -]?key|password|secret|token|credential)\s*=|private.?key|system.?prompt|provider.?response|holdout|\bsk_(?:live|test)_[A-Za-z0-9_-]+|\bsk-proj-[A-Za-z0-9_-]+|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bgh[pours]_[A-Za-z0-9_-]+|\bxox[a-z]-[A-Za-z0-9-]+|\bAIza[A-Za-z0-9_-]+)", value) or re.fullmatch(r"[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}", value):
            raise ValueError("secret-like metadata is forbidden")
        if _is_opaque_secret_token(value, key):
            raise ValueError("secret-like metadata is forbidden")
    return value


class FactoryActionRequestV1(_FrozenContract):
    schema_name: Literal["captain.factory-action-request.v1"] = Field(default="captain.factory-action-request.v1", alias="schema", serialization_alias="schema")
    request_id: UUID
    factory_job_id: UUID
    creation_job_id: UUID
    correlation_id: UUID
    expected_subject_version: int = Field(ge=1, strict=True)
    request_type: Literal["clarification", "credential_required", "external_api_approval", "n8n_approval"]
    safe_checkpoint_ref: ArtifactRef
    safe_checkpoint_sha256: str = Field(pattern=SHA256_PATTERN)
    required_scope: str = Field(min_length=1, max_length=200)
    allowed_decisions: tuple[Literal["approve", "deny"], ...] = Field(min_length=1)
    message: str = Field(min_length=1, max_length=500)
    deadline_at: datetime
    status: Literal["pending", "answered", "expired", "consumed"]

    @field_validator("required_scope", "message")
    @classmethod
    def require_display_safe_text(cls, value: str) -> str:
        try:
            _reject_secret_like_content(value)
        except ValueError as error:
            raise ValueError("display-safe request text is required") from error
        return value

    @field_validator("deadline_at")
    @classmethod
    def require_deadline_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("deadline_at must be UTC")
        return value

    @model_validator(mode="after")
    def require_checkpoint_digest(self) -> "FactoryActionRequestV1":
        if self.safe_checkpoint_sha256 != self.safe_checkpoint_ref.sha256:
            raise ValueError("safe checkpoint digest must match ref")
        return self


class FactoryActionResponseV1(_FrozenContract):
    schema_name: Literal["captain.factory-action-response.v1"] = Field(default="captain.factory-action-response.v1", alias="schema", serialization_alias="schema")
    request_id: UUID
    expected_subject_version: int = Field(ge=1, strict=True)
    decision: Literal["approve", "deny"]
    approved_scope: str = Field(min_length=1, max_length=200)
    credential_ref: str | None = None
    answer_metadata: dict[str, object] = Field(default_factory=dict)
    answered_at: datetime
    idempotency_key: str = Field(pattern=SHA256_PATTERN)

    @field_validator("approved_scope")
    @classmethod
    def require_display_safe_scope(cls, value: str) -> str:
        try:
            _reject_secret_like_content(value)
        except ValueError as error:
            raise ValueError("display-safe response text is required") from error
        return value

    @field_validator("credential_ref")
    @classmethod
    def require_credential_reference(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"credential://[A-Za-z0-9._/-]+", value):
            raise ValueError("credential_ref must use credential:// URI")
        return value

    @field_validator("answered_at")
    @classmethod
    def require_answered_at_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("answered_at must be UTC")
        return value

    @field_validator("answer_metadata")
    @classmethod
    def reject_secret_like_metadata(cls, value: dict[str, object]) -> dict[str, object]:
        _reject_secret_like_content(value)
        return value


def validate_build_effect_binding(assignment: FactoryBuildAssignmentV2, effect: FactoryBuildEffectV2) -> None:
    fields = ("assignment_id", "creation_job_id", "factory_job_id", "correlation_id", "subject_version", "checkpoint_version", "attempt", "effect_key")
    if any(getattr(assignment, field) != getattr(effect, field) for field in fields):
        raise ValueError("build effect identity drift")


def validate_action_response_binding(request: FactoryActionRequestV1, response: FactoryActionResponseV1) -> None:
    if request.request_id != response.request_id or request.expected_subject_version != response.expected_subject_version:
        raise ValueError("action response identity drift")
    if response.decision not in request.allowed_decisions:
        raise ValueError("action response decision is not allowed")
    if response.approved_scope != request.required_scope:
        raise ValueError("action response scope does not match request scope")
