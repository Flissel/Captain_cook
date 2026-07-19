"""Strict, transport-neutral contracts for the Captain agent factory."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_runtime.contracts import ArtifactRef, IDENTIFIER_PATTERN


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class FactoryRole(str, Enum):
    AGENT_ARCHITECT = "agent_architect"
    TOOL_INTEGRATOR = "tool_integrator"
    REAL_CASE_TESTER = "real_case_tester"
    QUALITY_WARDEN = "quality_warden"


class FactoryPhase(str, Enum):
    FORGE_REQUESTED = "forge_requested"
    BLUEPRINT_CREATED = "blueprint_created"
    TOOL_CANDIDATE_TESTED = "tool_candidate_tested"
    AGENT_CODE_CREATED = "agent_code_created"
    BUILD_PASSED = "build_passed"
    BUILD_FAILED = "build_failed"
    REAL_CASE_EVIDENCE = "real_case_evidence"
    QUALITY_REVIEWED = "quality_reviewed"
    IMPROVEMENT_REQUESTED = "improvement_requested"
    CAPABILITY_PROMOTED = "capability_promoted"
    ESCALATED = "escalated"


class FactoryBlockStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INFRASTRUCTURE_FAILED = "infrastructure_failed"
    RECOMMENDED = "recommended"


class AgentFactoryJob(_FrozenContract):
    schema_name: Literal["captain.agent-factory-job.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    event_id: UUID
    correlation_id: UUID
    causation_id: UUID | None = None
    occurred_at: datetime
    producer: Literal["captain"]
    job_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    input_ref: ArtifactRef
    required_capability: str = Field(pattern=IDENTIFIER_PATTERN)
    acceptance_assertion_ids: tuple[str, ...] = Field(min_length=1)
    max_behavioral_iterations: Literal[5] = 5

    @field_validator("occurred_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("acceptance_assertion_ids")
    @classmethod
    def require_unique_assertions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("acceptance_assertion_ids must not contain duplicates")
        if any(not assertion for assertion in value):
            raise ValueError("acceptance_assertion_ids must not contain blanks")
        return value


class FactoryEvidenceBlock(_FrozenContract):
    schema_name: Literal["captain.agent-factory-block.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    event_id: UUID
    job_id: UUID
    correlation_id: UUID
    causation_id: UUID | None = None
    occurred_at: datetime
    producer: Literal["captain", "hermes"]
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    phase: FactoryPhase
    role: FactoryRole | None = None
    status: FactoryBlockStatus
    artifact_refs: tuple[ArtifactRef, ...] = ()
    evidence_refs: tuple[ArtifactRef, ...] = ()
    assertion_ids: tuple[str, ...] = ()
    lease_id: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)

    @field_validator("occurred_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("assertion_ids")
    @classmethod
    def require_unique_assertions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("assertion_ids must not contain duplicates")
        if any(not assertion for assertion in value):
            raise ValueError("assertion_ids must not contain blanks")
        return value

    @model_validator(mode="after")
    def require_phase_authority(self) -> "FactoryEvidenceBlock":
        expected_role = _ROLE_PHASES.get(self.phase)
        if expected_role is not None:
            if self.producer != "hermes":
                raise ValueError(f"{self.phase.value} must be emitted by Hermes")
            if self.role is not expected_role:
                raise ValueError(f"{self.phase.value} requires {expected_role.value.title().replace('_', '')}")
            if self.lease_id is None:
                raise ValueError(f"{self.phase.value} requires a lease")
        elif self.phase in _CAPTAIN_PHASES:
            if self.producer != "captain":
                raise ValueError(f"{self.phase.value} requires Captain authority")
            if self.role is not None or self.lease_id is not None:
                raise ValueError(f"{self.phase.value} cannot carry a role or lease")

        if self.phase is FactoryPhase.CAPABILITY_PROMOTED:
            if self.status is not FactoryBlockStatus.SUCCEEDED:
                raise ValueError("capability_promoted requires succeeded status")
            if not self.assertion_ids:
                raise ValueError("capability_promoted requires assertion evidence")
            if not self.evidence_refs:
                raise ValueError("capability_promoted requires evidence refs")
        return self


class PromotedCapability(_FrozenContract):
    capability_id: str = Field(pattern=IDENTIFIER_PATTERN)
    version: int = Field(ge=1, strict=True)
    status: Literal["ready_to_use"]
    blueprint_ref: ArtifactRef
    code_ref: ArtifactRef
    tool_refs: tuple[ArtifactRef, ...] = ()
    promotion_block_ref: ArtifactRef | None = None

    @model_validator(mode="after")
    def require_promotion_reference(self) -> "PromotedCapability":
        if self.promotion_block_ref is None:
            raise ValueError("ready_to_use capability requires a promotion block reference")
        return self


_ROLE_PHASES: dict[FactoryPhase, FactoryRole] = {
    FactoryPhase.BLUEPRINT_CREATED: FactoryRole.AGENT_ARCHITECT,
    FactoryPhase.TOOL_CANDIDATE_TESTED: FactoryRole.TOOL_INTEGRATOR,
    FactoryPhase.AGENT_CODE_CREATED: FactoryRole.TOOL_INTEGRATOR,
    FactoryPhase.BUILD_PASSED: FactoryRole.TOOL_INTEGRATOR,
    FactoryPhase.BUILD_FAILED: FactoryRole.TOOL_INTEGRATOR,
    FactoryPhase.REAL_CASE_EVIDENCE: FactoryRole.REAL_CASE_TESTER,
    FactoryPhase.QUALITY_REVIEWED: FactoryRole.QUALITY_WARDEN,
}

_CAPTAIN_PHASES = frozenset(
    {
        FactoryPhase.FORGE_REQUESTED,
        FactoryPhase.IMPROVEMENT_REQUESTED,
        FactoryPhase.CAPABILITY_PROMOTED,
        FactoryPhase.ESCALATED,
    }
)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a UTC offset")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("timestamps must be UTC")
    return value.astimezone(timezone.utc)

