"""Frozen evidence contracts for Captain-controlled Hermes workflow skills."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_runtime.contracts import ArtifactRef, IDENTIFIER_PATTERN, SHA256_PATTERN

from .contracts import FactoryLease, FactoryRole
from .forge_contracts import FactoryBuildAssignmentV1
from .outcome_contracts import AssertionOutcome, ExecutionOutcomeV1
from .skill_evaluation import ReleasedHermesSkill, ToolGapMarker


_PRIVATE_KEY_PATTERN = re.compile(
    r"(?i)(?:^|[_-])(?:api[_-]?key|authorization|credentials?|password|"
    r"private[_-]?key|secrets?|tokens?|raw[_-]?prompt|transcripts?|"
    r"holdouts?[_-]?(?:body|case)|private[_-]?case)(?:$|[_-])"
)
_PRIVATE_VALUE_PATTERN = re.compile(
    r"(?i)(?:\bsk-(?:proj-)?[a-z0-9_-]{8,}|\bbearer\s+\S+|"
    r"\b(?:api[_ -]?key|authorization|credential|password|secret|token)\b\s*[:=]|"
    r"\bfile\s*:)"
)
_LOCAL_PATH_PATTERN = re.compile(
    r"(?i)(?:^|[\s\"'(=])(?:[A-Za-z]:[\\/]|\\\\|"
    r"/(?:root|srv|Users|home|tmp|var|etc|opt|workspace)(?:/|$))"
)
_OPAQUE_URI_PATTERN = re.compile(r"^(?:artifact|holdout|workspace)://")


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class FactorySkillStep(str, Enum):
    DISCOVER = "discover"
    BRIEF_CODEX = "brief_codex"
    EXECUTE_TEAM = "execute_team"
    EVALUATE_TEAM = "evaluate_team"
    IMPROVE_TEAM = "improve_team"
    REPORT_CAPTAIN = "report_captain"


class FactoryFeedbackRecommendation(str, Enum):
    PROMOTE_CANDIDATE = "PROMOTE_CANDIDATE"
    RETRY_BUILD = "RETRY_BUILD"
    BLOCKED_TOOL_REQUIRED = "BLOCKED_TOOL_REQUIRED"
    BLOCKED_CREDENTIAL_REQUIRED = "BLOCKED_CREDENTIAL_REQUIRED"
    BLOCKED_INFRASTRUCTURE = "BLOCKED_INFRASTRUCTURE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    MANUAL_DECISION_REQUIRED = "MANUAL_DECISION_REQUIRED"


class CandidateChangedComponent(str, Enum):
    AGENT_CODE = "agent_code"
    SYSTEM_PROMPT = "system_prompt"
    USER_PROMPT = "user_prompt"
    CONTEXT = "context"
    TOOL_CONTRACT = "tool_contract"
    MODEL_CLIENT = "model_client"
    MEMORY = "memory"
    AUTOGEN_CONVERSATION_PATTERN = "autogen_conversation_pattern"
    HANDOFFS = "handoffs"
    TERMINATION = "termination"
    N8N_WORKFLOW = "n8n_workflow"
    TESTS = "tests"
    DOCUMENTATION = "documentation"


class FactorySkillInvocationV1(_FrozenContract):
    schema_name: Literal["captain.factory-skill-invocation.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    invocation_id: UUID
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    step: FactorySkillStep
    released_skill: ReleasedHermesSkill
    input_ref: ArtifactRef
    input_sha256: str = Field(pattern=SHA256_PATTERN)
    lease: FactoryLease
    idempotency_key: str = Field(pattern=SHA256_PATTERN)
    acceptance_assertion_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def reject_private_content(cls, value: object) -> object:
        _reject_private_content(value, "skill invocation")
        return value

    @field_validator("acceptance_assertion_ids")
    @classmethod
    def require_unique_captain_assertions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique_ids(value, "Captain assertion IDs")

    @model_validator(mode="after")
    def require_exact_bindings(self) -> "FactorySkillInvocationV1":
        if self.input_ref.sha256 != self.input_sha256:
            raise ValueError("skill invocation input digest mismatch")
        if self.lease.job_id != self.job_id or self.lease.correlation_id != self.correlation_id:
            raise ValueError("skill invocation lease mismatch")
        if self.lease.subject_version != self.subject_version or self.lease.attempt != self.attempt:
            raise ValueError("skill invocation version or attempt mismatch")
        expected_role = _STEP_ROLES[self.step]
        if self.lease.role is not expected_role:
            raise ValueError("skill invocation lease role does not match step")
        return self


_STEP_ROLES: dict[FactorySkillStep, FactoryRole] = {
    FactorySkillStep.DISCOVER: FactoryRole.AGENT_ARCHITECT,
    FactorySkillStep.BRIEF_CODEX: FactoryRole.TOOL_INTEGRATOR,
    FactorySkillStep.EXECUTE_TEAM: FactoryRole.REAL_CASE_TESTER,
    FactorySkillStep.EVALUATE_TEAM: FactoryRole.QUALITY_WARDEN,
    FactorySkillStep.IMPROVE_TEAM: FactoryRole.TOOL_INTEGRATOR,
    FactorySkillStep.REPORT_CAPTAIN: FactoryRole.QUALITY_WARDEN,
}


class _WorkflowArtifactBase(_FrozenContract):
    invocation: FactorySkillInvocationV1
    invocation_id: UUID
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    occurred_at: datetime
    producer: Literal["hermes"]
    artifact_ref: ArtifactRef
    evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    acceptance_assertion_ids: tuple[str, ...] = Field(min_length=1)

    _required_step: ClassVar[FactorySkillStep | None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_private_content(cls, value: object) -> object:
        _reject_private_content(value, "workflow artifact")
        return value

    @field_validator("occurred_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("workflow artifact timestamp must be UTC")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def require_unique_evidence_refs(
        cls, value: tuple[ArtifactRef, ...]
    ) -> tuple[ArtifactRef, ...]:
        return _require_unique_refs(value, "workflow artifact evidence refs")

    @field_validator("acceptance_assertion_ids")
    @classmethod
    def require_unique_assertion_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique_ids(value, "Captain assertion IDs")

    @model_validator(mode="after")
    def require_exact_invocation_identity(self) -> "_WorkflowArtifactBase":
        invocation = self.invocation
        if (
            self.invocation_id != invocation.invocation_id
            or self.job_id != invocation.job_id
            or self.correlation_id != invocation.correlation_id
            or self.subject_version != invocation.subject_version
            or self.attempt != invocation.attempt
        ):
            raise ValueError("workflow artifact invocation identity does not match invocation")
        if self._required_step is not None and invocation.step is not self._required_step:
            raise ValueError("workflow artifact invocation step does not match result type")
        if self.acceptance_assertion_ids != invocation.acceptance_assertion_ids:
            raise ValueError("workflow artifact invocation assertion binding does not match")
        if not invocation.lease.issued_at <= self.occurred_at < invocation.lease.expires_at:
            raise ValueError("workflow artifact must occur under an active lease")
        return self


class CodebaseInventoryV1(_WorkflowArtifactBase):
    schema_name: Literal["hermes.factory-codebase-inventory.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    inspected_revision: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    source_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    reusable_component_ids: tuple[str, ...] = ()
    entrypoint_refs: tuple[ArtifactRef, ...] = ()
    test_refs: tuple[ArtifactRef, ...] = ()
    schema_refs: tuple[ArtifactRef, ...] = ()
    autogen_version: str = Field(min_length=1)
    documentation_refs: tuple[ArtifactRef, ...] = ()
    tool_catalog_match_ids: tuple[str, ...] = ()
    gap_refs: tuple[ArtifactRef, ...] = ()

    _required_step: ClassVar[FactorySkillStep] = FactorySkillStep.DISCOVER

    @field_validator(
        "source_refs",
        "entrypoint_refs",
        "test_refs",
        "schema_refs",
        "documentation_refs",
        "gap_refs",
    )
    @classmethod
    def require_unique_refs(cls, value: tuple[ArtifactRef, ...]) -> tuple[ArtifactRef, ...]:
        return _require_unique_refs(value, "inventory artifact refs")

    @field_validator("reusable_component_ids", "tool_catalog_match_ids")
    @classmethod
    def require_unique_component_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique_ids(value, "inventory IDs")


class CodexBuildBriefV1(_WorkflowArtifactBase):
    schema_name: Literal["hermes.factory-codex-build-assignment.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    build_assignment: FactoryBuildAssignmentV1
    prompt_ref: ArtifactRef
    context_refs: tuple[ArtifactRef, ...] = ()
    authorized_path_roots: tuple[str, ...] = Field(min_length=1)
    required_test_command_ids: tuple[str, ...] = Field(min_length=1)
    forbidden_effect_ids: tuple[str, ...] = ()

    _required_step: ClassVar[FactorySkillStep] = FactorySkillStep.BRIEF_CODEX

    @field_validator("context_refs")
    @classmethod
    def require_unique_context_refs(
        cls, value: tuple[ArtifactRef, ...]
    ) -> tuple[ArtifactRef, ...]:
        return _require_unique_refs(value, "brief context refs")

    @field_validator("authorized_path_roots")
    @classmethod
    def require_workspace_roots(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.startswith("workspace://") for item in value):
            raise ValueError("authorized path roots must be unique workspace:// references")
        return value

    @field_validator("required_test_command_ids", "forbidden_effect_ids")
    @classmethod
    def require_unique_identifiers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(IDENTIFIER_PATTERN, item) is None for item in value):
            raise ValueError("brief identifiers must be valid IDs")
        return _require_unique_ids(value, "brief IDs")

    @model_validator(mode="after")
    def require_assignment_bindings(self) -> "CodexBuildBriefV1":
        assignment = self.build_assignment
        if (
            assignment.correlation_id != self.correlation_id
            or assignment.subject_version != self.subject_version
            or assignment.attempt != self.attempt
        ):
            raise ValueError("build assignment does not match brief invocation")
        if set(assignment.public_assertion_ids) != set(self.acceptance_assertion_ids):
            raise ValueError("build assignment must use exactly the Captain assertion IDs")
        released_skill = self.invocation.released_skill
        assigned_skill = assignment.released_skill
        if (
            assigned_skill.skill_id != released_skill.skill_id
            or assigned_skill.version != released_skill.version
            or assigned_skill.content_sha256 != released_skill.content_sha256
            or assigned_skill.content_ref.uri != released_skill.content_ref.uri
            or assigned_skill.content_ref.sha256 != released_skill.content_ref.sha256
            or assigned_skill.content_ref.media_type != released_skill.content_ref.media_type
        ):
            raise ValueError("build assignment released skill does not match invocation")
        if assignment.idempotency_key != self.invocation.idempotency_key:
            raise ValueError("build assignment idempotency key does not match invocation")
        if assignment.workspace_ref != self.invocation.lease.workspace_ref:
            raise ValueError("build assignment workspace does not match invocation lease")
        return self


class TeamExecutionEvidenceV1(_WorkflowArtifactBase):
    schema_name: Literal["hermes.factory-team-execution-evidence.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    run_number: int = Field(ge=1, strict=True)
    candidate_ref: ArtifactRef
    execution_outcome: ExecutionOutcomeV1
    usage_receipt_refs: tuple[ArtifactRef, ...] = ()
    handoff_evidence_refs: tuple[ArtifactRef, ...] = ()
    tool_evidence_refs: tuple[ArtifactRef, ...] = ()
    workflow_evidence_refs: tuple[ArtifactRef, ...] = ()
    termination_reason: str = Field(min_length=1, max_length=200)
    status: Literal["succeeded", "failed", "unresolved"]

    _required_step: ClassVar[FactorySkillStep] = FactorySkillStep.EXECUTE_TEAM

    @field_validator(
        "usage_receipt_refs",
        "handoff_evidence_refs",
        "tool_evidence_refs",
        "workflow_evidence_refs",
    )
    @classmethod
    def require_unique_refs(cls, value: tuple[ArtifactRef, ...]) -> tuple[ArtifactRef, ...]:
        return _require_unique_refs(value, "execution evidence refs")

    @model_validator(mode="after")
    def require_successful_runtime_outcome(self) -> "TeamExecutionEvidenceV1":
        if self.execution_outcome.correlation_id != self.correlation_id:
            raise ValueError("execution outcome correlation does not match invocation")
        outcome_ids = tuple(item.assertion_id for item in self.execution_outcome.assertion_outcomes)
        _require_unique_ids(outcome_ids, "execution assertion IDs")
        if set(outcome_ids) - set(self.acceptance_assertion_ids):
            raise ValueError("execution outcome contains non-Captain assertion IDs")
        if self.status == "succeeded":
            if self.execution_outcome.status != "succeeded" or any(
                item.status != "passed" for item in self.execution_outcome.assertion_outcomes
            ):
                raise ValueError("successful execution requires a passed runtime outcome")
        return self


class TeamEvaluationV1(_WorkflowArtifactBase):
    schema_name: Literal["hermes.factory-team-evaluation.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    assertion_outcomes: tuple[AssertionOutcome, ...] = Field(min_length=1)
    holdout_receipt_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    deterministic_check_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    judge_ref: ArtifactRef | None = None
    prior_green_regression_ids: tuple[str, ...] = ()
    cost_summary_ref: ArtifactRef
    latency_summary_ref: ArtifactRef
    failure_class: Literal[
        "behavioral_failure",
        "test_regression",
        "tool_required",
        "credential_required",
        "infrastructure_failure",
        "budget_exhausted",
        "unresolved",
    ] | None = None
    recommendation: FactoryFeedbackRecommendation

    _required_step: ClassVar[FactorySkillStep] = FactorySkillStep.EVALUATE_TEAM

    @field_validator("holdout_receipt_refs", "deterministic_check_refs")
    @classmethod
    def require_unique_refs(cls, value: tuple[ArtifactRef, ...]) -> tuple[ArtifactRef, ...]:
        return _require_unique_refs(value, "evaluation evidence refs")

    @field_validator("prior_green_regression_ids")
    @classmethod
    def require_unique_regression_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique_ids(value, "prior-green regression IDs")

    @model_validator(mode="after")
    def require_captain_assertion_outcomes(self) -> "TeamEvaluationV1":
        outcome_ids = tuple(item.assertion_id for item in self.assertion_outcomes)
        _require_unique_ids(outcome_ids, "evaluation assertion IDs")
        accepted = set(self.acceptance_assertion_ids)
        if set(outcome_ids) != accepted:
            raise ValueError("evaluation must contain exactly the Captain assertion IDs")
        if set(self.prior_green_regression_ids) - accepted:
            raise ValueError("prior-green regression IDs must be Captain assertion IDs")
        return self


class CandidateRevisionV1(_WorkflowArtifactBase):
    schema_name: Literal["hermes.factory-candidate-revision.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    parent_candidate_ref: ArtifactRef
    candidate_ref: ArtifactRef
    failed_assertion_ids: tuple[str, ...] = Field(min_length=1)
    changed_components: tuple[CandidateChangedComponent, ...] = Field(min_length=1)
    regression_assertion_ids: tuple[str, ...] = ()
    codex_session_ref: ArtifactRef

    _required_step: ClassVar[FactorySkillStep] = FactorySkillStep.IMPROVE_TEAM

    @field_validator("failed_assertion_ids", "regression_assertion_ids")
    @classmethod
    def require_unique_assertions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique_ids(value, "revision assertion IDs")

    @field_validator("changed_components")
    @classmethod
    def require_unique_components(
        cls, value: tuple[CandidateChangedComponent, ...]
    ) -> tuple[CandidateChangedComponent, ...]:
        if len(value) != len(set(value)):
            raise ValueError("changed components must not contain duplicates")
        return value

    @model_validator(mode="after")
    def require_bounded_revision(self) -> "CandidateRevisionV1":
        if self.parent_candidate_ref == self.candidate_ref:
            raise ValueError("candidate revision must produce a new candidate")
        accepted = set(self.acceptance_assertion_ids)
        if set(self.failed_assertion_ids) - accepted or set(self.regression_assertion_ids) - accepted:
            raise ValueError("revision assertion IDs must be Captain assertion IDs")
        if set(self.failed_assertion_ids) & set(self.regression_assertion_ids):
            raise ValueError("failed and regression assertion IDs must not overlap")
        return self


class FactoryFeedbackV1(_WorkflowArtifactBase):
    schema_name: Literal["hermes.factory-feedback.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    recommendation: FactoryFeedbackRecommendation
    assertion_ids: tuple[str, ...] = Field(min_length=1)
    tool_gaps: tuple[ToolGapMarker, ...] = ()
    tool_gap_refs: tuple[ArtifactRef, ...] = ()
    reason_codes: tuple[str, ...] = Field(min_length=1)

    _required_step: ClassVar[FactorySkillStep] = FactorySkillStep.REPORT_CAPTAIN

    @field_validator("assertion_ids", "reason_codes")
    @classmethod
    def require_unique_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique_ids(value, "feedback IDs")

    @field_validator("tool_gap_refs")
    @classmethod
    def require_unique_gap_refs(cls, value: tuple[ArtifactRef, ...]) -> tuple[ArtifactRef, ...]:
        return _require_unique_refs(value, "tool gap refs")

    @model_validator(mode="after")
    def require_captain_assertions_and_safe_promotion(self) -> "FactoryFeedbackV1":
        accepted = set(self.acceptance_assertion_ids)
        if set(self.assertion_ids) - accepted:
            raise ValueError("feedback assertion IDs must be Captain assertion IDs")
        gap_ids = tuple(gap.gap_id for gap in self.tool_gaps)
        _require_unique_ids(gap_ids, "tool gap IDs")
        for gap in self.tool_gaps:
            if set(gap.acceptance_assertion_ids) - accepted:
                raise ValueError("tool gap contains non-Captain assertion IDs")
        referenced = {
            (ref.uri, ref.sha256, ref.media_type) for ref in self.tool_gap_refs
        }
        embedded = {
            (gap.evidence_ref.uri, gap.evidence_ref.sha256, gap.evidence_ref.media_type)
            for gap in self.tool_gaps
        }
        if referenced != embedded:
            raise ValueError("tool gap refs must bind exactly to embedded TODO_TOOL.v1 markers")
        if self.recommendation is FactoryFeedbackRecommendation.PROMOTE_CANDIDATE:
            if set(self.assertion_ids) != accepted:
                raise ValueError("PROMOTE_CANDIDATE requires all Captain assertions")
            if any(
                gap.severity == "required" and gap.status == "unresolved"
                for gap in self.tool_gaps
            ):
                raise ValueError(
                    "PROMOTE_CANDIDATE cannot include a required unresolved TODO_TOOL.v1"
                )
        return self


def _require_unique_ids(value: tuple[str, ...], label: str) -> tuple[str, ...]:
    if len(value) != len(set(value)) or any(not item for item in value):
        raise ValueError(f"{label} must be unique and nonblank")
    return value


def _require_unique_refs(value: tuple[ArtifactRef, ...], label: str) -> tuple[ArtifactRef, ...]:
    identities = tuple((item.uri, item.sha256, item.media_type) for item in value)
    if len(identities) != len(set(identities)):
        raise ValueError(f"{label} must not contain duplicates")
    return value


def _reject_private_content(value: object, context: str) -> None:
    for key, item in _walk(value):
        if key is not None and _PRIVATE_KEY_PATTERN.search(key):
            raise ValueError(f"{context} contains private field {key}")
        if isinstance(item, str) and _PRIVATE_VALUE_PATTERN.search(item):
            raise ValueError(f"{context} contains private content")
        if (
            isinstance(item, str)
            and _OPAQUE_URI_PATTERN.match(item) is None
            and _LOCAL_PATH_PATTERN.search(item)
        ):
            raise ValueError(f"{context} contains local-path content")


def _walk(value: object, key: str | None = None) -> Sequence[tuple[str | None, object]]:
    if isinstance(value, BaseModel):
        return _walk(value.model_dump(mode="python", by_alias=True), key)
    if isinstance(value, Mapping):
        items: list[tuple[str | None, object]] = [(key, value)]
        for child_key, child_value in value.items():
            child_name = str(child_key)
            items.extend(_walk(child_value, child_name))
        return items
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = [(key, value)]
        for child_value in value:
            items.extend(_walk(child_value, key))
        return items
    return [(key, value)]
