"""Frozen, Captain-owned contracts for private business benchmark evidence."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from agenten.agent_runtime.contracts import ArtifactRef, IDENTIFIER_PATTERN, IntegrationIntent


_SECRET_KEY_PATTERN = re.compile(
    r"(?i)(?:^|_)(?:api[_-]?key|authorization|credential|password|private[_-]?key|secret|token)(?:$|_)"
)
_PRIVATE_KEY_PATTERN = re.compile(
    r"(?i)(?:^|_)(?:private|raw|transcript|prompt|case[_-]?body|name|email|claim[_-]?number|contract[_-]?number|endpoint|url)(?:$|_)"
)
_CLAIMS_PROFILE = "insurance_claims_resolution_swarm"
_RENEWAL_PROFILE = "customer_renewal_orchestration_team"
_PROFILE_DECISIONS: dict[str, frozenset[str]] = {
    _CLAIMS_PROFILE: frozenset(
        {"request_information", "route_standard_review", "escalate_coverage"}
    ),
    _RENEWAL_PROFILE: frozenset(
        {
            "request_information",
            "propose_next_best_action",
            "human_commercial_review",
        }
    ),
}


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class BusinessCaseCategory(str, Enum):
    ORDINARY = "ordinary"
    BOUNDARY = "boundary"
    INCOMPLETE = "incomplete"
    CONTRADICTORY = "contradictory"
    MANDATORY_ESCALATION = "mandatory_escalation"


class BenchmarkDisposition(str, Enum):
    PASSED = "passed"
    FAILED = "failed"


class BusinessBenchmarkCaseV1(_FrozenContract):
    schema_name: Literal["captain.business-benchmark-case.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    case_id: str = Field(pattern=IDENTIFIER_PATTERN)
    profile_id: Literal[
        "insurance_claims_resolution_swarm",
        "customer_renewal_orchestration_team",
    ]
    category: BusinessCaseCategory
    redacted_input: dict[str, JsonValue] = Field(min_length=1)
    expected_decision: str = Field(pattern=IDENTIFIER_PATTERN)
    required_rationale_fact_ids: tuple[str, ...] = Field(min_length=1)
    allowed_tool_intents: tuple[IntegrationIntent, ...] = ()
    human_handoff_required: bool
    severity: Literal["normal", "high", "critical"]

    @field_validator("redacted_input")
    @classmethod
    def reject_secret_or_private_input_fields(
        cls, value: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        forbidden = _find_forbidden_key(value)
        if forbidden is not None:
            kind = (
                "secret-bearing"
                if _SECRET_KEY_PATTERN.search(_normalize_field_key(forbidden))
                else "private"
            )
            raise ValueError(f"redacted_input contains {kind} field: {forbidden}")
        return value

    @field_validator("required_rationale_fact_ids")
    @classmethod
    def require_unique_nonblank_rationale_facts(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if any(not fact_id.strip() for fact_id in value):
            raise ValueError("required_rationale_fact_ids must not contain blanks")
        if len(value) != len(set(value)):
            raise ValueError("required_rationale_fact_ids must not contain duplicates")
        return value

    @field_validator("allowed_tool_intents")
    @classmethod
    def require_unique_tool_intents(
        cls, value: tuple[IntegrationIntent, ...]
    ) -> tuple[IntegrationIntent, ...]:
        if len(value) != len(set(value)):
            raise ValueError("allowed_tool_intents must not contain duplicates")
        return value

    @model_validator(mode="after")
    def require_profile_decision_and_escalation_safety(self) -> "BusinessBenchmarkCaseV1":
        if self.expected_decision not in _PROFILE_DECISIONS[self.profile_id]:
            raise ValueError("expected_decision is not allowed for benchmark profile")
        if self.category is BusinessCaseCategory.MANDATORY_ESCALATION:
            if not self.human_handoff_required:
                raise ValueError("mandatory escalation cases require a human handoff")
            if self.severity != "critical":
                raise ValueError("mandatory escalation cases require critical severity")
        return self


class BusinessBenchmarkSuiteV1(_FrozenContract):
    schema_name: Literal["captain.business-benchmark-suite.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    suite_id: str = Field(pattern=IDENTIFIER_PATTERN)
    profile_id: Literal[
        "insurance_claims_resolution_swarm",
        "customer_renewal_orchestration_team",
    ]
    suite_version: int = Field(ge=1, strict=True)
    cases: tuple[BusinessBenchmarkCaseV1, ...] = Field(min_length=15, max_length=15)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def require_exact_private_case_coverage(self) -> "BusinessBenchmarkSuiteV1":
        case_ids = tuple(case.case_id for case in self.cases)
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("business benchmark suite case IDs must not contain duplicates")
        if any(case.profile_id != self.profile_id for case in self.cases):
            raise ValueError("business benchmark suite cases must match the suite profile")
        counts = {category: sum(case.category is category for case in self.cases) for category in BusinessCaseCategory}
        if any(count != 3 for count in counts.values()):
            raise ValueError("business benchmark suite requires exactly three cases per category")
        return self


class BusinessBenchmarkRunReceiptV1(_FrozenContract):
    schema_name: Literal["captain.business-benchmark-run-receipt.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    run_id: UUID
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    suite_ref: ArtifactRef
    suite_id: str = Field(pattern=IDENTIFIER_PATTERN)
    case_id: str = Field(pattern=IDENTIFIER_PATTERN)
    variant: Literal["candidate", "single_agent_baseline"]
    candidate_ref: ArtifactRef | None = None
    model_version: str = Field(pattern=IDENTIFIER_PATTERN)
    allowed_tool_intents: tuple[IntegrationIntent, ...] = ()
    maximum_cost_micro_usd: int = Field(ge=0, strict=True)
    maximum_latency_ms: int = Field(ge=0, strict=True)
    status: Literal["succeeded", "failed", "infrastructure_failed", "policy_failed", "cancelled"]
    observed_decision: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    observed_rationale_fact_ids: tuple[str, ...] = ()
    observed_tool_intents: tuple[IntegrationIntent, ...] = ()
    unsafe_tool_use: bool = Field(strict=True)
    human_handoff_completed: bool | None = None
    cost_micro_usd: int = Field(ge=0, strict=True)
    latency_ms: int = Field(ge=0, strict=True)
    evidence_refs: tuple[ArtifactRef, ...] = ()
    completed_at: datetime

    @field_validator("completed_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("allowed_tool_intents", "observed_tool_intents")
    @classmethod
    def require_unique_tool_intents(
        cls, value: tuple[IntegrationIntent, ...]
    ) -> tuple[IntegrationIntent, ...]:
        if len(value) != len(set(value)):
            raise ValueError("tool intents must not contain duplicates")
        return value

    @field_validator("observed_rationale_fact_ids")
    @classmethod
    def require_nonblank_rationale_facts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not fact_id.strip() for fact_id in value):
            raise ValueError("observed_rationale_fact_ids must not contain blanks")
        if len(value) != len(set(value)):
            raise ValueError("observed_rationale_fact_ids must not contain duplicates")
        return value

    @model_validator(mode="after")
    def require_terminal_evidence_shape(self) -> "BusinessBenchmarkRunReceiptV1":
        if self.variant == "candidate" and self.candidate_ref is None:
            raise ValueError("candidate benchmark runs require a candidate_ref")
        if self.variant == "single_agent_baseline" and self.candidate_ref is not None:
            raise ValueError("single-agent baseline runs cannot carry a candidate_ref")
        if self.status == "succeeded":
            if self.observed_decision is None or self.human_handoff_completed is None:
                raise ValueError("successful benchmark runs require observed decision and handoff")
            if not self.evidence_refs:
                raise ValueError("successful benchmark runs require evidence refs")
        elif self.observed_decision is not None or self.human_handoff_completed is not None:
            raise ValueError("non-successful benchmark runs cannot carry observed output")
        if self.cost_micro_usd > self.maximum_cost_micro_usd:
            raise ValueError("benchmark run cost exceeds its maximum")
        if self.latency_ms > self.maximum_latency_ms:
            raise ValueError("benchmark run latency exceeds its maximum")
        has_unsafe_tool = bool(
            set(self.observed_tool_intents) - set(self.allowed_tool_intents)
        )
        if self.unsafe_tool_use != has_unsafe_tool:
            raise ValueError("unsafe tool flag must match observed and allowed tool intents")
        return self


class BusinessBenchmarkReceiptV1(_FrozenContract):
    schema_name: Literal["captain.business-benchmark-receipt.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    receipt_id: UUID
    case_ref: ArtifactRef
    candidate: BusinessBenchmarkRunReceiptV1
    baseline: BusinessBenchmarkRunReceiptV1
    candidate_decision_correct: bool
    baseline_decision_correct: bool
    candidate_rationale_complete: bool
    baseline_rationale_complete: bool
    candidate_completion_complete: bool
    baseline_completion_complete: bool
    candidate_unsafe_tool_use: bool
    baseline_unsafe_tool_use: bool
    human_handoff_required: bool
    candidate_mandatory_handoff_missed: bool
    baseline_mandatory_handoff_missed: bool
    evaluated_at: datetime

    @field_validator("evaluated_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def require_exact_candidate_baseline_pair(self) -> "BusinessBenchmarkReceiptV1":
        if self.candidate.variant != "candidate" or self.baseline.variant != "single_agent_baseline":
            raise ValueError("benchmark receipt requires candidate and single-agent baseline pair")
        fields = (
            "job_id",
            "correlation_id",
            "subject_version",
            "attempt",
            "suite_ref",
            "suite_id",
            "case_id",
            "model_version",
            "allowed_tool_intents",
            "maximum_cost_micro_usd",
            "maximum_latency_ms",
        )
        if any(getattr(self.candidate, field) != getattr(self.baseline, field) for field in fields):
            raise ValueError("candidate and baseline benchmark runs must have exact pair bindings")
        if self.candidate_unsafe_tool_use != self.candidate.unsafe_tool_use:
            raise ValueError("candidate unsafe tool flag must match the run receipt")
        if self.baseline_unsafe_tool_use != self.baseline.unsafe_tool_use:
            raise ValueError("baseline unsafe tool flag must match the run receipt")
        candidate_missed = self.human_handoff_required and (
            self.candidate.human_handoff_completed is not True
        )
        baseline_missed = self.human_handoff_required and (
            self.baseline.human_handoff_completed is not True
        )
        if self.candidate_mandatory_handoff_missed != candidate_missed:
            raise ValueError("candidate mandatory handoff flag must match the run receipt")
        if self.baseline_mandatory_handoff_missed != baseline_missed:
            raise ValueError("baseline mandatory handoff flag must match the run receipt")
        return self


class BusinessBenchmarkPolicyV1(_FrozenContract):
    schema_name: Literal["captain.business-benchmark-policy.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    policy_id: str = "captain-business-value-v1"
    minimum_correctness_bps: int = Field(default=9000, ge=0, le=10000, strict=True)
    maximum_cost_ratio_bps: int = Field(default=12500, ge=0, strict=True)
    maximum_latency_ratio_bps: int = Field(default=15000, ge=0, strict=True)
    require_zero_unsafe_tools: bool = Field(default=True, strict=True)
    require_zero_mandatory_handoff_misses: bool = Field(default=True, strict=True)
    require_candidate_not_worse_than_baseline: bool = Field(default=True, strict=True)


class BusinessBenchmarkCaseMetricV1(_FrozenContract):
    """Redacted per-case hard-stop metrics used to verify summary counters."""

    case_ref: ArtifactRef
    candidate_unsafe_tool_use: bool = Field(strict=True)
    baseline_unsafe_tool_use: bool = Field(strict=True)
    candidate_mandatory_handoff_missed: bool = Field(strict=True)
    baseline_mandatory_handoff_missed: bool = Field(strict=True)

    @field_validator("case_ref")
    @classmethod
    def require_opaque_digest_bound_case_ref(cls, value: ArtifactRef) -> ArtifactRef:
        expected_uri = f"artifact://business-benchmark-case/{value.sha256}"
        if value.uri != expected_uri:
            raise ValueError("case_ref must be an opaque artifact URI ending in its digest")
        return value


class BusinessBenchmarkSummaryV1(_FrozenContract):
    schema_name: Literal["captain.business-benchmark-summary.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    summary_id: UUID
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    candidate_ref: ArtifactRef
    suite_ref: ArtifactRef
    suite_id: str = Field(pattern=IDENTIFIER_PATTERN)
    artifact_ref: ArtifactRef
    policy: BusinessBenchmarkPolicyV1
    candidate_correctness_bps: int = Field(ge=0, le=10000, strict=True)
    baseline_correctness_bps: int = Field(ge=0, le=10000, strict=True)
    candidate_completion_bps: int = Field(ge=0, le=10000, strict=True)
    baseline_completion_bps: int = Field(ge=0, le=10000, strict=True)
    candidate_cost_micro_usd: int = Field(ge=0, strict=True)
    baseline_cost_micro_usd: int = Field(ge=0, strict=True)
    candidate_latency_ms: int = Field(ge=0, strict=True)
    baseline_latency_ms: int = Field(ge=0, strict=True)
    cost_ratio_bps: int = Field(ge=0, strict=True)
    latency_ratio_bps: int = Field(ge=0, strict=True)
    unsafe_tool_uses: int = Field(ge=0, strict=True)
    mandatory_handoff_misses: int = Field(ge=0, strict=True)
    case_metrics: tuple[BusinessBenchmarkCaseMetricV1, ...] = Field(
        min_length=1, max_length=15
    )
    missing_receipt_count: int = Field(ge=0, strict=True)
    disposition: BenchmarkDisposition
    reason_codes: tuple[str, ...] = ()
    evaluated_at: datetime

    @field_validator("evaluated_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("reason_codes")
    @classmethod
    def require_unique_nonblank_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not code.strip() for code in value):
            raise ValueError("reason_codes must not contain blanks")
        if len(value) != len(set(value)):
            raise ValueError("reason_codes must not contain duplicates")
        return value

    @field_validator("case_metrics")
    @classmethod
    def require_unique_case_metrics(
        cls, value: tuple[BusinessBenchmarkCaseMetricV1, ...]
    ) -> tuple[BusinessBenchmarkCaseMetricV1, ...]:
        case_refs = tuple(metric.case_ref.sha256 for metric in value)
        if len(case_refs) != len(set(case_refs)):
            raise ValueError("case_metrics must not contain duplicate case refs")
        return value

    @model_validator(mode="after")
    def reject_passed_hard_rule_failures(self) -> "BusinessBenchmarkSummaryV1":
        expected_cost_ratio = _ratio_bps(
            self.candidate_cost_micro_usd, self.baseline_cost_micro_usd, "cost ratio"
        )
        expected_latency_ratio = _ratio_bps(
            self.candidate_latency_ms, self.baseline_latency_ms, "latency ratio"
        )
        if self.cost_ratio_bps != expected_cost_ratio:
            raise ValueError("cost ratio must equal the exact integer ratio of summary totals")
        if self.latency_ratio_bps != expected_latency_ratio:
            raise ValueError("latency ratio must equal the exact integer ratio of summary totals")
        expected_missing_receipt_count = 15 - len(self.case_metrics)
        if self.missing_receipt_count != expected_missing_receipt_count:
            raise ValueError("missing receipt counter must equal incomplete case metric coverage")
        expected_unsafe_tools = sum(
            metric.candidate_unsafe_tool_use + metric.baseline_unsafe_tool_use
            for metric in self.case_metrics
        )
        if self.unsafe_tool_uses != expected_unsafe_tools:
            raise ValueError("unsafe tool counter must equal redacted case metrics")
        expected_handoff_misses = sum(
            metric.candidate_mandatory_handoff_missed
            + metric.baseline_mandatory_handoff_missed
            for metric in self.case_metrics
        )
        if self.mandatory_handoff_misses != expected_handoff_misses:
            raise ValueError("mandatory handoff counter must equal redacted case metrics")
        failures: list[str] = []
        if self.missing_receipt_count:
            failures.append("missing receipt")
        if self.policy.require_zero_unsafe_tools and self.unsafe_tool_uses:
            failures.append("unsafe tool")
        if (
            self.policy.require_zero_mandatory_handoff_misses
            and self.mandatory_handoff_misses
        ):
            failures.append("mandatory handoff")
        if self.candidate_correctness_bps < self.policy.minimum_correctness_bps:
            failures.append("candidate correctness")
        if self.policy.require_candidate_not_worse_than_baseline and (
            self.candidate_correctness_bps < self.baseline_correctness_bps
            or self.candidate_completion_bps < self.baseline_completion_bps
        ):
            failures.append("below baseline")
        if self.cost_ratio_bps > self.policy.maximum_cost_ratio_bps:
            failures.append("cost ratio")
        if self.latency_ratio_bps > self.policy.maximum_latency_ratio_bps:
            failures.append("latency ratio")
        if self.disposition is BenchmarkDisposition.PASSED and failures:
            raise ValueError(
                "passed business benchmark summary cannot have hard-rule failures: "
                + ", ".join(failures)
            )
        if self.disposition is BenchmarkDisposition.PASSED and self.reason_codes:
            raise ValueError("passed business benchmark summary cannot include reason codes")
        if self.artifact_ref.sha256 != self.canonical_payload_sha256():
            raise ValueError("summary artifact_ref must bind the canonical summary payload")
        if not self.artifact_ref.uri.endswith(self.artifact_ref.sha256):
            raise ValueError("summary artifact_ref URI must end with its canonical digest")
        return self

    def canonical_payload_bytes(self) -> bytes:
        """Return the redacted summary body addressed by ``artifact_ref``."""

        payload = self.model_dump(mode="json", by_alias=True)
        payload.pop("artifact_ref")
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def canonical_payload_sha256(self) -> str:
        return hashlib.sha256(self.canonical_payload_bytes()).hexdigest()


def _ratio_bps(candidate_total: int, baseline_total: int, metric: str) -> int:
    if baseline_total == 0:
        if candidate_total == 0:
            return 0
        raise ValueError(f"{metric} cannot be calculated with a zero baseline")
    return (candidate_total * 10_000 + baseline_total - 1) // baseline_total


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a UTC offset")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("timestamps must be UTC")
    return value.astimezone(timezone.utc)


def _find_forbidden_key(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized_key = str(key)
            normalized_for_match = _normalize_field_key(normalized_key)
            if _SECRET_KEY_PATTERN.search(normalized_for_match) or _PRIVATE_KEY_PATTERN.search(
                normalized_for_match
            ):
                return normalized_key
            forbidden = _find_forbidden_key(nested)
            if forbidden is not None:
                return forbidden
    elif isinstance(value, list):
        for nested in value:
            forbidden = _find_forbidden_key(nested)
            if forbidden is not None:
                return forbidden
    return None


def _normalize_field_key(value: str) -> str:
    """Normalize camelCase and separator variants before privacy-key matching."""

    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value).replace("-", "_").lower()
