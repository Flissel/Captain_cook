"""Frozen contracts for Captain-scoped Hermes skill evaluation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_runtime.contracts import ArtifactRef, IDENTIFIER_PATTERN, SHA256_PATTERN

from .contracts import FactoryLease, FactoryRole


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class BoundedEvaluationCommand(_FrozenContract):
    """An allowlisted command identity with a finite execution window."""

    command_id: str = Field(pattern=IDENTIFIER_PATTERN)
    max_seconds: int = Field(ge=1, le=3600, strict=True)


class ReleasedHermesSkill(_FrozenContract):
    schema_name: Literal["captain.released-hermes-skill.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    skill_id: str = Field(pattern=IDENTIFIER_PATTERN)
    version: int = Field(ge=1, strict=True)
    capability: str = Field(pattern=IDENTIFIER_PATTERN)
    content_ref: ArtifactRef
    content_sha256: str = Field(pattern=SHA256_PATTERN)
    status: Literal["released"]
    released_at: datetime
    producer: Literal["captain"]

    @field_validator("released_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def require_matching_content_digest(self) -> "ReleasedHermesSkill":
        if self.content_sha256 != self.content_ref.sha256:
            raise ValueError("released skill content digest must match content_ref")
        return self


class HermesSkillEvaluationRequest(_FrozenContract):
    schema_name: Literal["captain.hermes-skill-evaluation-request.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    request_id: UUID
    job_id: UUID
    correlation_id: UUID
    subject_id: str = Field(pattern=IDENTIFIER_PATTERN)
    subject_version: int = Field(ge=1, strict=True)
    occurred_at: datetime
    producer: Literal["captain"]
    lease: FactoryLease
    released_skill: ReleasedHermesSkill
    candidate_source_ref: ArtifactRef
    acceptance_assertion_ids: tuple[str, ...] = Field(min_length=1)
    max_iterations: int = Field(ge=1, le=5, strict=True)

    @field_validator("occurred_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("acceptance_assertion_ids")
    @classmethod
    def require_unique_assertions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique_nonblank(value, "acceptance_assertion_ids")

    @model_validator(mode="after")
    def require_matching_active_lease(self) -> "HermesSkillEvaluationRequest":
        if self.lease.job_id != self.job_id:
            raise ValueError("evaluation request lease job does not match request job")
        if self.lease.correlation_id != self.correlation_id:
            raise ValueError("evaluation request lease correlation does not match request correlation")
        if self.lease.subject_version != self.subject_version:
            raise ValueError("evaluation request lease subject version does not match request")
        if self.lease.role is not FactoryRole.TOOL_INTEGRATOR:
            raise ValueError("skill evaluation requires a tool integrator lease")
        return self


class HermesSkillUsageReceipt(_FrozenContract):
    schema_name: Literal["hermes.skill-usage-receipt.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    receipt_id: UUID
    request_id: UUID
    job_id: UUID
    correlation_id: UUID
    lease_id: str = Field(pattern=IDENTIFIER_PATTERN)
    occurred_at: datetime
    producer: Literal["hermes"]
    released_skill: ReleasedHermesSkill
    used_skill_id: str = Field(pattern=IDENTIFIER_PATTERN)
    used_skill_version: int = Field(ge=1, strict=True)
    used_skill_sha256: str = Field(pattern=SHA256_PATTERN)
    commands: tuple[BoundedEvaluationCommand, ...] = Field(min_length=1)
    evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    assertion_ids: tuple[str, ...] = Field(min_length=1)
    outcome: Literal["passed", "redo", "blocked_tool_gap", "unresolved", "failed"]

    @field_validator("occurred_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("commands")
    @classmethod
    def require_unique_commands(
        cls, value: tuple[BoundedEvaluationCommand, ...]
    ) -> tuple[BoundedEvaluationCommand, ...]:
        command_ids = tuple(command.command_id for command in value)
        _require_unique_nonblank(command_ids, "commands")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def require_unique_evidence_refs(
        cls, value: tuple[ArtifactRef, ...]
    ) -> tuple[ArtifactRef, ...]:
        _require_unique_artifact_refs(value, "evidence_refs")
        return value

    @field_validator("assertion_ids")
    @classmethod
    def require_unique_assertions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique_nonblank(value, "assertion_ids")

    @model_validator(mode="after")
    def require_matching_released_skill(self) -> "HermesSkillUsageReceipt":
        skill = self.released_skill
        if self.used_skill_id != skill.skill_id or self.used_skill_version != skill.version:
            raise ValueError("usage receipt skill identity must match released skill")
        if self.used_skill_sha256 != skill.content_sha256:
            raise ValueError("usage receipt skill digest must match released skill")
        return self


class HermesSkillCandidate(_FrozenContract):
    schema_name: Literal["hermes.skill-candidate.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    candidate_id: str = Field(pattern=IDENTIFIER_PATTERN)
    request_id: UUID
    created_at: datetime
    producer: Literal["hermes"]
    content_ref: ArtifactRef
    content_sha256: str = Field(pattern=SHA256_PATTERN)
    parent_released_skill: ReleasedHermesSkill
    creation_reason: str = Field(min_length=1)
    status: Literal["private_candidate"]

    @field_validator("created_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def require_matching_content_digest(self) -> "HermesSkillCandidate":
        if self.content_sha256 != self.content_ref.sha256:
            raise ValueError("candidate skill content digest must match content_ref")
        return self


class ToolImplementationOption(_FrozenContract):
    option_id: str = Field(pattern=IDENTIFIER_PATTERN)
    description: str = Field(min_length=1)
    acceptance_assertion_id: str = Field(min_length=1)


class ToolGapMarker(_FrozenContract):
    schema_name: Literal["TODO_TOOL.v1"] = Field(alias="schema", serialization_alias="schema")
    gap_id: str = Field(pattern=IDENTIFIER_PATTERN)
    severity: Literal["required", "optional"]
    input_contract_ref: ArtifactRef
    output_contract_ref: ArtifactRef
    least_privilege_capability: str = Field(pattern=IDENTIFIER_PATTERN)
    implementation_options: tuple[ToolImplementationOption, ...] = Field(max_length=3)
    acceptance_assertion_ids: tuple[str, ...] = Field(min_length=1)
    evidence_ref: ArtifactRef
    status: Literal["unresolved", "resolved"]

    @field_validator("implementation_options")
    @classmethod
    def require_unique_options(
        cls, value: tuple[ToolImplementationOption, ...]
    ) -> tuple[ToolImplementationOption, ...]:
        option_ids = tuple(option.option_id for option in value)
        _require_unique_nonblank(option_ids, "implementation_options")
        return value

    @field_validator("acceptance_assertion_ids")
    @classmethod
    def require_unique_assertions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique_nonblank(value, "acceptance_assertion_ids")


class SkillEvaluationCheck(_FrozenContract):
    check_id: str = Field(pattern=IDENTIFIER_PATTERN)
    kind: Literal["build", "test"]
    command: BoundedEvaluationCommand
    status: Literal["passed", "failed", "skipped"]
    evidence_ref: ArtifactRef
    assertion_ids: tuple[str, ...] = ()

    @field_validator("assertion_ids")
    @classmethod
    def require_unique_assertions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique_nonblank(value, "assertion_ids")


class HermesSkillEvaluationEvidence(_FrozenContract):
    schema_name: Literal["hermes.skill-evaluation-evidence.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    evidence_id: UUID
    request_id: UUID
    job_id: UUID
    correlation_id: UUID
    subject_id: str = Field(pattern=IDENTIFIER_PATTERN)
    subject_version: int = Field(ge=1, strict=True)
    occurred_at: datetime
    producer: Literal["hermes"]
    receipt: HermesSkillUsageReceipt
    candidate: HermesSkillCandidate | None = None
    tool_gaps: tuple[ToolGapMarker, ...] = ()
    checks: tuple[SkillEvaluationCheck, ...] = Field(min_length=1)
    assertion_ids: tuple[str, ...] = Field(min_length=1)
    outcome: Literal["passed", "redo", "blocked_tool_gap", "unresolved", "failed"]

    @field_validator("occurred_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("tool_gaps")
    @classmethod
    def require_unique_tool_gaps(
        cls, value: tuple[ToolGapMarker, ...]
    ) -> tuple[ToolGapMarker, ...]:
        gap_ids = tuple(marker.gap_id for marker in value)
        _require_unique_nonblank(gap_ids, "tool_gaps")
        return value

    @field_validator("checks")
    @classmethod
    def require_unique_checks(
        cls, value: tuple[SkillEvaluationCheck, ...]
    ) -> tuple[SkillEvaluationCheck, ...]:
        check_ids = tuple(check.check_id for check in value)
        _require_unique_nonblank(check_ids, "checks")
        return value

    @field_validator("assertion_ids")
    @classmethod
    def require_unique_assertions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique_nonblank(value, "assertion_ids")

    @model_validator(mode="after")
    def require_linked_evaluation_records(self) -> "HermesSkillEvaluationEvidence":
        receipt = self.receipt
        if receipt.request_id != self.request_id:
            raise ValueError("evaluation evidence receipt request does not match evidence")
        if receipt.job_id != self.job_id:
            raise ValueError("evaluation evidence receipt job does not match evidence")
        if receipt.correlation_id != self.correlation_id:
            raise ValueError("evaluation evidence receipt correlation does not match evidence")
        if self.candidate is not None:
            if self.candidate.request_id != self.request_id:
                raise ValueError("evaluation evidence candidate request does not match evidence")
            if self.candidate.parent_released_skill != receipt.released_skill:
                raise ValueError("evaluation evidence candidate parent skill does not match receipt")
        known_assertions = set(self.assertion_ids)
        for check in self.checks:
            if not set(check.assertion_ids).issubset(known_assertions):
                raise ValueError("evaluation check assertions must belong to evidence")
        return self


def required_tool_gaps(evidence: HermesSkillEvaluationEvidence) -> tuple[ToolGapMarker, ...]:
    """Return unresolved required gaps without making a release decision."""

    return tuple(
        marker
        for marker in evidence.tool_gaps
        if marker.severity == "required" and marker.status == "unresolved"
    )


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a UTC offset")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("timestamps must be UTC")
    return value.astimezone(timezone.utc)


def _require_unique_nonblank(value: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if len(value) != len(set(value)):
        raise ValueError(f"{field_name} must not contain duplicates")
    if any(not item for item in value):
        raise ValueError(f"{field_name} must not contain blanks")
    return value


def _require_unique_artifact_refs(
    value: tuple[ArtifactRef, ...], field_name: str
) -> tuple[ArtifactRef, ...]:
    identities = tuple((item.uri, item.sha256, item.media_type) for item in value)
    if len(identities) != len(set(identities)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return value
