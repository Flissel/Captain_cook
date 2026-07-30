"""Frozen evidence contracts for Captain-controlled Hermes workflow skills."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_runtime.contracts import ArtifactRef, IDENTIFIER_PATTERN, SHA256_PATTERN

from .business_benchmark_contracts import (
    BusinessBenchmarkMetricId,
    BusinessBenchmarkReasonCode,
)
from .contracts import FactoryLease, FactoryRole
from .forge_contracts import (
    CodexBuildReceiptV1,
    FactoryBuildAssignmentV1,
    codex_build_receipt_sha256,
)
from .holdout_contracts import PrivateHoldoutRef
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
    r"(?i)(?:^|[\s\"'(=])(?:/|[A-Za-z]:[\\/]|\\\\)"
)
_OPAQUE_URI_PATTERN = re.compile(r"^(?:artifact|holdout|workspace)://")


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class FactorySkillStep(str, Enum):
    DISCOVER = "discover"
    BRIEF_CODEX = "brief_codex"
    SEAL_CODEX_BUILD = "seal_codex_build"
    EXECUTE_TEAM = "execute_team"
    EVALUATE_TEAM = "evaluate_team"
    IMPROVE_TEAM = "improve_team"
    REPORT_CAPTAIN = "report_captain"


FACTORY_SKILL_ID_BY_STEP: dict[FactorySkillStep, str] = {
    FactorySkillStep.DISCOVER: "captain-factory-discover",
    FactorySkillStep.BRIEF_CODEX: "captain-factory-brief-codex",
    FactorySkillStep.SEAL_CODEX_BUILD: "captain-factory-seal-codex-build",
    FactorySkillStep.EXECUTE_TEAM: "captain-factory-execute-team",
    FactorySkillStep.EVALUATE_TEAM: "captain-factory-evaluate-team",
    FactorySkillStep.IMPROVE_TEAM: "captain-factory-improve-team",
    FactorySkillStep.REPORT_CAPTAIN: "captain-factory-report-captain",
}


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
    execution_scope_ref: PrivateHoldoutRef | None = None

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
        if self.released_skill.skill_id != FACTORY_SKILL_ID_BY_STEP[self.step]:
            raise ValueError("skill invocation released skill ID does not match step")
        return self


_STEP_ROLES: dict[FactorySkillStep, FactoryRole] = {
    FactorySkillStep.DISCOVER: FactoryRole.AGENT_ARCHITECT,
    FactorySkillStep.BRIEF_CODEX: FactoryRole.TOOL_INTEGRATOR,
    FactorySkillStep.SEAL_CODEX_BUILD: FactoryRole.TOOL_INTEGRATOR,
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
        original_lease_active = (
            invocation.lease.issued_at
            <= self.occurred_at
            < invocation.lease.expires_at
        )
        recovery_binding = getattr(self, "runtime_retry_binding", None)
        recovery_authority_active = (
            self._required_step is FactorySkillStep.SEAL_CODEX_BUILD
            and recovery_binding is not None
            and recovery_binding.issued_at
            <= self.occurred_at
            < recovery_binding.expires_at
        )
        if not original_lease_active and not recovery_authority_active:
            raise ValueError(
                "workflow artifact must occur under an active lease or Captain recovery authority"
            )
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
    failed_benchmark_metric_ids: tuple[BusinessBenchmarkMetricId, ...] = ()
    regression_benchmark_metric_ids: tuple[BusinessBenchmarkMetricId, ...] = ()

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

    @field_validator(
        "failed_benchmark_metric_ids",
        "regression_benchmark_metric_ids",
    )
    @classmethod
    def require_unique_benchmark_metric_ids(
        cls,
        value: tuple[BusinessBenchmarkMetricId, ...],
    ) -> tuple[BusinessBenchmarkMetricId, ...]:
        if len(value) != len(set(value)):
            raise ValueError("brief benchmark metric IDs must not contain duplicates")
        return value

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
        if set(self.failed_benchmark_metric_ids) & set(
            self.regression_benchmark_metric_ids
        ):
            raise ValueError(
                "failed and regression benchmark metric IDs must be disjoint"
            )
        return self


class FactoryRuntimeRetryEvidenceBindingV1(_FrozenContract):
    """Public exact binding of one Captain-issued Codex recovery authority."""

    schema_name: Literal["captain.factory-runtime-retry-evidence-binding.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    producer: Literal["captain"]
    status: Literal["succeeded"]
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    invocation_id: UUID
    idempotency_key: str = Field(pattern=SHA256_PATTERN)
    lease_id: str = Field(pattern=IDENTIFIER_PATTERN)
    checkpoint_ref: ArtifactRef
    terminal_receipt_ref: ArtifactRef
    workspace_ref: str = Field(pattern=r"^workspace://")
    base_revision: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    scaffold_manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    brief_sha256: str = Field(pattern=SHA256_PATTERN)
    resume_ordinal: int = Field(ge=1, le=2, strict=True)
    maximum_runtime_seconds: int = Field(ge=1, le=900, strict=True)
    issued_at: datetime
    expires_at: datetime

    @field_validator("issued_at", "expires_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("runtime retry evidence timestamp must be UTC")
        return value

    @model_validator(mode="after")
    def require_bounded_window(self) -> "FactoryRuntimeRetryEvidenceBindingV1":
        if self.expires_at <= self.issued_at:
            raise ValueError("runtime retry evidence expiry must follow issuance")
        if self.maximum_runtime_seconds > int(
            (self.expires_at - self.issued_at).total_seconds()
        ):
            raise ValueError("runtime retry evidence runtime exceeds its window")
        return self


def factory_runtime_retry_evidence_binding(
    authorization: BaseModel,
) -> FactoryRuntimeRetryEvidenceBindingV1:
    payload = authorization.model_dump(mode="json", by_alias=True)
    payload.pop("authorization_ref", None)
    payload["schema"] = "captain.factory-runtime-retry-evidence-binding.v1"
    return FactoryRuntimeRetryEvidenceBindingV1.model_validate(payload)


def factory_runtime_retry_evidence_binding_sha256(
    binding: FactoryRuntimeRetryEvidenceBindingV1,
) -> str:
    content = json.dumps(
        binding.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


class CodexBuildEvidenceV1(_WorkflowArtifactBase):
    """Hermes evidence for the Captain-sealed result of one Codex assignment."""

    schema_name: Literal["hermes.factory-codex-build-evidence.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    build_receipt_ref: ArtifactRef
    build_receipt: CodexBuildReceiptV1
    runtime_retry_ref: ArtifactRef | None = None
    runtime_retry_binding: FactoryRuntimeRetryEvidenceBindingV1 | None = None
    status: Literal["sealed"]

    _required_step: ClassVar[FactorySkillStep] = FactorySkillStep.SEAL_CODEX_BUILD

    @model_validator(mode="after")
    def require_captain_receipt_bindings(self) -> "CodexBuildEvidenceV1":
        receipt = self.build_receipt
        invocation = self.invocation
        if receipt.factory_job_id != self.job_id:
            raise ValueError("Codex build receipt job mismatch")
        if receipt.correlation_id != self.correlation_id:
            raise ValueError("Codex build receipt correlation mismatch")
        if receipt.subject_version != self.subject_version:
            raise ValueError("Codex build receipt version mismatch")
        if receipt.attempt != self.attempt:
            raise ValueError("Codex build receipt attempt mismatch")
        if receipt.seal_idempotency_key != invocation.idempotency_key:
            raise ValueError("Codex build receipt idempotency mismatch")
        if receipt.workspace_ref != invocation.lease.workspace_ref:
            raise ValueError("Codex build receipt workspace mismatch")
        if not _same_ref(receipt.build_brief_ref, invocation.input_ref):
            raise ValueError("Codex build brief does not match invocation input")
        if receipt.acceptance_assertion_ids != self.acceptance_assertion_ids:
            raise ValueError("Codex build receipt assertion mismatch")
        if receipt.completed_at > self.occurred_at:
            raise ValueError("Codex build receipt was issued after workflow evidence")
        if self.build_receipt_ref.media_type != "application/json":
            raise ValueError("Codex build receipt ref must be application/json")
        if self.build_receipt_ref.sha256 != codex_build_receipt_sha256(receipt):
            raise ValueError("Codex build receipt digest mismatch")
        if self.runtime_retry_ref is None and self.runtime_retry_binding is None:
            if self.evidence_refs != (self.build_receipt_ref,):
                raise ValueError(
                    "Codex build evidence may reference only its Captain receipt"
                )
            return self
        if self.runtime_retry_ref is None or self.runtime_retry_binding is None:
            raise ValueError("Codex build recovery authority is incomplete")
        if self.evidence_refs != (self.build_receipt_ref, self.runtime_retry_ref):
            raise ValueError(
                "Codex build recovery authority ref must be retained in evidence refs"
            )
        binding = self.runtime_retry_binding
        binding_sha256 = factory_runtime_retry_evidence_binding_sha256(binding)
        if (
            self.runtime_retry_ref.sha256 != binding_sha256
            or not self.runtime_retry_ref.uri.endswith(f"/{binding_sha256}")
        ):
            raise ValueError("Codex build recovery binding digest does not match authority ref")
        if (
            binding.job_id != invocation.job_id
            or binding.correlation_id != invocation.correlation_id
            or binding.subject_version != invocation.subject_version
            or binding.attempt != invocation.attempt
            or binding.invocation_id != invocation.invocation_id
            or binding.idempotency_key != invocation.idempotency_key
            or binding.lease_id != invocation.lease.lease_id
            or binding.workspace_ref != invocation.lease.workspace_ref
        ):
            raise ValueError("Codex build recovery binding does not match invocation")
        if not binding.issued_at <= self.occurred_at < binding.expires_at:
            raise ValueError("Codex build occurred outside recovery authority")
        return self


class TeamExecutionEvidenceV1(_WorkflowArtifactBase):
    schema_name: Literal["hermes.factory-team-execution-evidence.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    run_number: int = Field(ge=1, strict=True)
    candidate_ref: ArtifactRef
    holdout_ref: PrivateHoldoutRef
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
        if self.invocation.execution_scope_ref != self.holdout_ref:
            raise ValueError("execution holdout does not match invocation scope")
        if self.execution_outcome.correlation_id != self.correlation_id:
            raise ValueError("execution outcome correlation does not match invocation")
        outcome_ids = tuple(item.assertion_id for item in self.execution_outcome.assertion_outcomes)
        _require_unique_ids(outcome_ids, "execution assertion IDs")
        if outcome_ids != self.acceptance_assertion_ids:
            raise ValueError("execution outcome must exactly match Captain assertion IDs")
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
    benchmark_summary_ref: ArtifactRef | None = None
    benchmark_policy_id: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    benchmark_disposition: Literal["passed", "failed"] | None = None
    benchmark_reason_codes: tuple[BusinessBenchmarkReasonCode, ...] = ()
    failed_benchmark_metric_ids: tuple[BusinessBenchmarkMetricId, ...] = ()
    prior_green_benchmark_metric_ids: tuple[BusinessBenchmarkMetricId, ...] = ()
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

    @field_validator(
        "benchmark_reason_codes",
        "failed_benchmark_metric_ids",
        "prior_green_benchmark_metric_ids",
    )
    @classmethod
    def require_unique_benchmark_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique_ids(value, "business benchmark IDs")

    @field_validator("benchmark_summary_ref")
    @classmethod
    def require_canonical_benchmark_summary_ref(
        cls, value: ArtifactRef | None
    ) -> ArtifactRef | None:
        if value is not None and value.uri != (
            f"artifact://business-benchmark-summary/{value.sha256}"
        ):
            raise ValueError("business benchmark summary ref must be canonical")
        return value

    @model_validator(mode="after")
    def require_captain_assertion_outcomes(self) -> "TeamEvaluationV1":
        outcome_ids = tuple(item.assertion_id for item in self.assertion_outcomes)
        _require_unique_ids(outcome_ids, "evaluation assertion IDs")
        accepted = set(self.acceptance_assertion_ids)
        if set(outcome_ids) != accepted:
            raise ValueError("evaluation must contain exactly the Captain assertion IDs")
        if set(self.prior_green_regression_ids) - accepted:
            raise ValueError("prior-green regression IDs must be Captain assertion IDs")
        binding = (
            self.benchmark_summary_ref,
            self.benchmark_policy_id,
            self.benchmark_disposition,
        )
        if any(item is not None for item in binding) and any(
            item is None for item in binding
        ):
            raise ValueError("business benchmark binding must be complete")
        if self.benchmark_summary_ref is None and (
            self.benchmark_reason_codes
            or self.failed_benchmark_metric_ids
            or self.prior_green_benchmark_metric_ids
        ):
            raise ValueError("legacy evaluation cannot contain unbound benchmark results")
        if (
            self.benchmark_summary_ref is not None
            and self.benchmark_summary_ref not in self.evidence_refs
        ):
            raise ValueError("business benchmark summary ref must be evaluation evidence")
        if self.benchmark_disposition == "passed" and (
            self.benchmark_reason_codes or self.failed_benchmark_metric_ids
        ):
            raise ValueError("passed business benchmark cannot contain failures")
        if (
            self.benchmark_disposition == "failed"
            and not self.failed_benchmark_metric_ids
        ):
            raise ValueError("failed business benchmark requires failed metric IDs")
        if set(self.failed_benchmark_metric_ids) & set(
            self.prior_green_benchmark_metric_ids
        ):
            raise ValueError(
                "failed and prior-green benchmark metric IDs must not overlap"
            )
        return self


class CandidateRevisionV1(_WorkflowArtifactBase):
    schema_name: Literal["hermes.factory-candidate-revision.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    parent_candidate_ref: ArtifactRef
    candidate_ref: ArtifactRef
    failed_assertion_ids: tuple[str, ...] = ()
    failed_benchmark_metric_ids: tuple[BusinessBenchmarkMetricId, ...] = ()
    changed_components: tuple[CandidateChangedComponent, ...] = Field(min_length=1)
    regression_assertion_ids: tuple[str, ...] = ()
    regression_benchmark_metric_ids: tuple[BusinessBenchmarkMetricId, ...] = ()
    codex_session_ref: ArtifactRef

    _required_step: ClassVar[FactorySkillStep] = FactorySkillStep.IMPROVE_TEAM

    @field_validator("failed_assertion_ids", "regression_assertion_ids")
    @classmethod
    def require_unique_assertions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique_ids(value, "revision assertion IDs")

    @field_validator(
        "failed_benchmark_metric_ids", "regression_benchmark_metric_ids"
    )
    @classmethod
    def require_unique_benchmark_metrics(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        return _require_unique_ids(value, "revision benchmark metric IDs")

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
        if not self.failed_assertion_ids and not self.failed_benchmark_metric_ids:
            raise ValueError(
                "candidate revision requires a failed assertion or benchmark metric"
            )
        if set(self.failed_benchmark_metric_ids) & set(
            self.regression_benchmark_metric_ids
        ):
            raise ValueError("failed and regression benchmark metric IDs must not overlap")
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
    identities = tuple(_ref_identity(item) for item in value)
    if len(identities) != len(set(identities)):
        raise ValueError(f"{label} must not contain duplicates")
    return value


def _ref_identity(value: object) -> tuple[str, str, str]:
    return (
        str(getattr(value, "uri")),
        str(getattr(value, "sha256")),
        str(getattr(value, "media_type")),
    )


def _same_ref(left: object, right: object) -> bool:
    return _ref_identity(left) == _ref_identity(right)


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
