"""Frozen contracts for deterministic evaluation source inventorying."""

from __future__ import annotations

import hashlib
import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_SECRET_ASSIGNMENT = re.compile(
    r"(?m)^(?P<indent>[ \t]*)(?P<name>(?:[A-Za-z][A-Za-z0-9_]*_)?(?:API_KEY|TOKEN)|password)=(?P<value>[^\r\n]*)$"
)


class EvaluationStatus(str, Enum):
    CREATED = "created"
    INVENTORYING = "inventorying"
    PLANNING = "planning"
    ACCEPTED = "accepted"
    PARTIAL = "partial"
    FAILED = "failed"


class EvaluationOutcome(str, Enum):
    """Captain's final per-component disposition, distinct from run status."""

    ACCEPTED = "accepted"
    NEEDS_REVISION = "needs_revision"
    UNRESOLVED = "unresolved"
    FAILED = "failed"


class SourceBlock(BaseModel):
    """One immutable, redacted Markdown fragment safe for later model use."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    block_id: str = Field(pattern=r"^block-[0-9]{4}$")
    heading_path: tuple[str, ...] = Field(min_length=1)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def has_ordered_lines_and_matching_digest(self) -> "SourceBlock":
        if self.line_end < self.line_start:
            raise ValueError("line_end must not precede line_start")
        if any(not heading.strip() for heading in self.heading_path):
            raise ValueError("heading_path entries must be non-empty")
        if self.sha256 != hashlib.sha256(self.text.encode("utf-8")).hexdigest():
            raise ValueError("sha256 does not match redacted block text")
        if any(match.group("value") != "[REDACTED]" for match in _SECRET_ASSIGNMENT.finditer(self.text)):
            raise ValueError("source block credential assignments must be redacted")
        return self


class EvaluationSource(BaseModel):
    """Redacted blocks and original-byte provenance, without a filesystem path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_reference: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(ge=1)
    blocks: tuple[SourceBlock, ...] = Field(min_length=1)

    @field_validator("source_reference")
    @classmethod
    def source_reference_is_logical_and_relative(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        segments = normalized.split("/")
        if (
            normalized.startswith("/")
            or re.match(r"^[A-Za-z]:", normalized)
            or any(segment in {"", ".", ".."} for segment in segments)
            or any(ord(character) < 32 for character in normalized)
        ):
            raise ValueError("source_reference must be a safe logical relative path")
        return normalized


class EvaluationRun(BaseModel):
    """Frozen later-task execution envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    source: EvaluationSource
    status: EvaluationStatus
    max_rounds: int = Field(ge=1)
    max_calls: int = Field(ge=1)


class AcceptanceTestPlan(BaseModel):
    """A proposed deterministic test with a reviewable oracle."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    test_id: str = Field(min_length=1)
    test_type: Literal["unit", "integration", "contract", "live"]
    setup: str
    action: str
    expected: str
    command: str


class QaReview(BaseModel):
    """A bounded QA assessment of one component-plan revision."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    component_key: str = Field(min_length=1)
    revision: int = Field(ge=1, le=3)
    decision: Literal["approved", "revision_required"]
    score: int = Field(ge=0, le=7)
    defect_codes: tuple[str, ...]
    revision_requests: tuple[str, ...]


class ComponentPlanCandidate(BaseModel):
    """A non-authoritative component implementation proposal."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    component_key: str = Field(min_length=1)
    revision: int = Field(default=1, ge=1, le=3)
    scope: tuple[str, ...]
    non_goals: tuple[str, ...]
    team_roles: tuple[str, ...]
    implementation_steps: tuple[str, ...]
    interfaces: tuple[str, ...]
    acceptance_tests: tuple[AcceptanceTestPlan, ...]
    definition_of_done: tuple[str, ...]
    risks: tuple[str, ...]
    dependencies: tuple[str, ...]
    source_citations: tuple[str, ...]
    claims: tuple[str, ...] = ()
    qa_reviews: tuple[QaReview, ...] = ()


class ComponentInventoryCandidate(BaseModel):
    """A source-bound inventory of non-authoritative component candidates."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    inventory_id: str = Field(min_length=1)
    source: EvaluationSource
    source_citations: tuple[str, ...]
    components: tuple[ComponentPlanCandidate, ...]


class ValidationIssue(BaseModel):
    """One deterministic validation finding; never a lifecycle decision."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    component_key: str | None = None


class ComponentOutcome(BaseModel):
    """Stored non-authoritative plan and review evidence for one component."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    component_key: str = Field(min_length=1)
    outcome: EvaluationOutcome
    revision: int = Field(ge=1, le=3)
    candidate: ComponentPlanCandidate | None = None
    review: QaReview | None = None


class InventoryReceipt(BaseModel):
    """Receipt for a Captain-owned append-only inventory write."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: str = Field(min_length=1)
    inventory_id: str = Field(min_length=1)
    artifact_reference: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CandidateReceipt(BaseModel):
    """Receipt for one immutable candidate revision."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: str = Field(min_length=1)
    component_key: str = Field(min_length=1)
    revision: int = Field(ge=1, le=3)
    artifact_reference: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewReceipt(BaseModel):
    """Receipt for one immutable QA review revision."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: str = Field(min_length=1)
    component_key: str = Field(min_length=1)
    revision: int = Field(ge=1, le=3)
    artifact_reference: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class EvaluationManifest(BaseModel):
    """Final redacted evidence projection, never a lifecycle authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    status: EvaluationStatus
    source: EvaluationSource
    component_outcomes: tuple[ComponentOutcome, ...]
    model_identifier: str = Field(default="not-configured", min_length=1)
    prompt_version: str = Field(default="not-configured", min_length=1)
    call_count: int = Field(default=0, ge=0)
    token_total: int = Field(default=0, ge=0)
    cost_total: float = Field(default=0.0, ge=0)
    artifact_digests: tuple[str, ...]
    planning_disclaimer: str = "Acceptance tests are planned, not executed by this evaluation."
