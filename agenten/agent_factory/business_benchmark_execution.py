"""Deterministic, paired execution envelopes for private business benchmarks."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkCaseV1,
    BusinessBenchmarkRunReceiptV1,
)
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_runtime.contracts import ArtifactRef, IDENTIFIER_PATTERN, IntegrationIntent


class _FrozenExecutionContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class BenchmarkExecutionPolicyV1(_FrozenExecutionContract):
    """The shared, versioned controls for one candidate/baseline pair."""

    schema_name: Literal["captain.business-benchmark-execution-policy.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    model_version: str = Field(pattern=IDENTIFIER_PATTERN)
    allowed_tool_intents: tuple[IntegrationIntent, ...] = ()
    maximum_cost_micro_usd: int = Field(ge=0, strict=True)
    maximum_latency_ms: int = Field(ge=0, strict=True)
    redaction_policy_version: str = Field(pattern=IDENTIFIER_PATTERN)
    baseline_system_policy_version: str = Field(pattern=IDENTIFIER_PATTERN)

    @field_validator("allowed_tool_intents")
    @classmethod
    def require_unique_tool_intents(
        cls, value: tuple[IntegrationIntent, ...]
    ) -> tuple[IntegrationIntent, ...]:
        if len(value) != len(set(value)):
            raise ValueError("tool intents must not contain duplicates")
        return value


class BusinessBenchmarkExecutionEnvelopeV1(_FrozenExecutionContract):
    """One bounded executor request; this is not a release or routing command."""

    schema_name: Literal["captain.business-benchmark-execution-envelope.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    request_id: UUID
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    suite_ref: PrivateHoldoutRef
    suite_id: str = Field(pattern=IDENTIFIER_PATTERN)
    case: BusinessBenchmarkCaseV1
    case_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    variant: Literal["candidate", "single_agent_baseline"]
    candidate_ref: ArtifactRef | None = None
    model_version: str = Field(pattern=IDENTIFIER_PATTERN)
    allowed_tool_intents: tuple[IntegrationIntent, ...] = ()
    maximum_cost_micro_usd: int = Field(ge=0, strict=True)
    maximum_latency_ms: int = Field(ge=0, strict=True)
    redaction_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    variant_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_session_id: str = Field(min_length=1)
    evaluation_only: bool

    @field_validator("runtime_session_id")
    @classmethod
    def require_nonblank_runtime_session_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("runtime_session_id must not be blank")
        return value

    @field_validator("allowed_tool_intents")
    @classmethod
    def require_unique_tool_intents(
        cls, value: tuple[IntegrationIntent, ...]
    ) -> tuple[IntegrationIntent, ...]:
        if len(value) != len(set(value)):
            raise ValueError("tool intents must not contain duplicates")
        return value

    @model_validator(mode="after")
    def require_variant_authority_boundary(self) -> "BusinessBenchmarkExecutionEnvelopeV1":
        if self.case_sha256 != _digest_model(self.case):
            raise ValueError("case_sha256 must match the immutable case")
        if self.allowed_tool_intents != self.case.allowed_tool_intents:
            raise ValueError("execution envelope tool intents must match the case")
        if self.variant == "candidate":
            if self.candidate_ref is None:
                raise ValueError("candidate benchmark execution requires candidate_ref")
            if self.evaluation_only:
                raise ValueError("candidate benchmark execution is not evaluation-only")
        elif self.candidate_ref is not None:
            raise ValueError("evaluation-only baseline cannot carry candidate_ref")
        elif not self.evaluation_only:
            raise ValueError("single-agent baseline must be evaluation-only")
        return self


class BusinessBenchmarkExecutorPort(Protocol):
    """Injected boundary for a bounded candidate or baseline execution."""

    async def execute(
        self, envelope: BusinessBenchmarkExecutionEnvelopeV1
    ) -> BusinessBenchmarkRunReceiptV1: ...


class BusinessBenchmarkExecutionError(ValueError):
    """An executor returned evidence that is not bound to its request."""


class PairedBusinessBenchmarkCoordinator:
    """Creates and validates exactly one deterministic candidate/baseline pair."""

    def __init__(
        self,
        *,
        job_id: UUID,
        correlation_id: UUID,
        subject_version: int,
        attempt: int,
        suite_id: str,
        executor: BusinessBenchmarkExecutorPort,
    ) -> None:
        if subject_version < 1:
            raise ValueError("subject_version must be positive")
        if attempt < 1 or attempt > 5:
            raise ValueError("attempt must be between 1 and 5")
        if not suite_id:
            raise ValueError("suite_id must not be blank")
        self._job_id = job_id
        self._correlation_id = correlation_id
        self._subject_version = subject_version
        self._attempt = attempt
        self._suite_id = suite_id
        self._executor = executor

    async def run_case_pair(
        self,
        *,
        case: BusinessBenchmarkCaseV1,
        suite_ref: PrivateHoldoutRef,
        candidate_ref: ArtifactRef,
        execution_policy: BenchmarkExecutionPolicyV1,
    ) -> tuple[BusinessBenchmarkRunReceiptV1, BusinessBenchmarkRunReceiptV1]:
        if execution_policy.allowed_tool_intents != case.allowed_tool_intents:
            raise BusinessBenchmarkExecutionError(
                "execution policy tool intents must exactly match case tool intents"
            )
        candidate = self._envelope(
            variant="candidate",
            case=case,
            suite_ref=suite_ref,
            candidate_ref=candidate_ref,
            execution_policy=execution_policy,
        )
        baseline = self._envelope(
            variant="single_agent_baseline",
            case=case,
            suite_ref=suite_ref,
            candidate_ref=None,
            execution_policy=execution_policy,
        )
        candidate_receipt = await self._executor.execute(candidate)
        baseline_receipt = await self._executor.execute(baseline)
        self._validate_receipt(candidate_receipt, candidate)
        self._validate_receipt(baseline_receipt, baseline)
        self._validate_pair(candidate_receipt, baseline_receipt)
        return candidate_receipt, baseline_receipt

    def _envelope(
        self,
        *,
        variant: Literal["candidate", "single_agent_baseline"],
        case: BusinessBenchmarkCaseV1,
        suite_ref: PrivateHoldoutRef,
        candidate_ref: ArtifactRef | None,
        execution_policy: BenchmarkExecutionPolicyV1,
    ) -> BusinessBenchmarkExecutionEnvelopeV1:
        execution_policy_sha256 = _digest_model(execution_policy)
        redaction_policy_sha256 = _digest_value(
            {"redaction_policy_version": execution_policy.redaction_policy_version}
        )
        variant_policy_sha256 = _digest_value(
            {
                "candidate_ref_sha256": candidate_ref.sha256
                if variant == "candidate" and candidate_ref is not None
                else None,
                "baseline_system_policy_version": execution_policy.baseline_system_policy_version
                if variant == "single_agent_baseline"
                else None,
                "variant": variant,
            }
        )
        binding = {
            "job_id": str(self._job_id),
            "correlation_id": str(self._correlation_id),
            "attempt": self._attempt,
            "suite_ref": suite_ref.model_dump(mode="json", by_alias=True),
            "case_id": case.case_id,
            "variant": variant,
            "execution_policy_sha256": execution_policy_sha256,
            "variant_policy_sha256": variant_policy_sha256,
        }
        idempotency_key = _digest_value(binding)
        return BusinessBenchmarkExecutionEnvelopeV1(
            schema="captain.business-benchmark-execution-envelope.v1",
            request_id=uuid5(
                NAMESPACE_URL,
                f"captain.business-benchmark-execution:{idempotency_key}",
            ),
            idempotency_key=idempotency_key,
            job_id=self._job_id,
            correlation_id=self._correlation_id,
            subject_version=self._subject_version,
            attempt=self._attempt,
            suite_ref=suite_ref,
            suite_id=self._suite_id,
            case=case,
            case_sha256=_digest_model(case),
            variant=variant,
            candidate_ref=candidate_ref,
            model_version=execution_policy.model_version,
            allowed_tool_intents=execution_policy.allowed_tool_intents,
            maximum_cost_micro_usd=execution_policy.maximum_cost_micro_usd,
            maximum_latency_ms=execution_policy.maximum_latency_ms,
            redaction_policy_sha256=redaction_policy_sha256,
            execution_policy_sha256=execution_policy_sha256,
            variant_policy_sha256=variant_policy_sha256,
            runtime_session_id=f"benchmark-session-{variant}-{idempotency_key[:16]}",
            evaluation_only=variant == "single_agent_baseline",
        )

    @staticmethod
    def _validate_receipt(
        receipt: BusinessBenchmarkRunReceiptV1,
        envelope: BusinessBenchmarkExecutionEnvelopeV1,
    ) -> None:
        if receipt.request_id != envelope.request_id:
            raise BusinessBenchmarkExecutionError("receipt request identity does not match envelope")
        if receipt.execution_policy_sha256 != envelope.execution_policy_sha256:
            raise BusinessBenchmarkExecutionError("receipt execution policy does not match envelope")
        if receipt.runtime_session_id != envelope.runtime_session_id:
            raise BusinessBenchmarkExecutionError("receipt runtime session does not match envelope")
        fields = (
            "job_id",
            "correlation_id",
            "subject_version",
            "attempt",
            "suite_ref",
            "suite_id",
            "case_id",
            "variant",
            "candidate_ref",
            "model_version",
            "allowed_tool_intents",
            "maximum_cost_micro_usd",
            "maximum_latency_ms",
        )
        for field in fields:
            expected = envelope.case.case_id if field == "case_id" else getattr(envelope, field)
            if getattr(receipt, field) != expected:
                raise BusinessBenchmarkExecutionError(
                    f"receipt {field} does not match envelope"
                )

    @staticmethod
    def _validate_pair(
        candidate: BusinessBenchmarkRunReceiptV1,
        baseline: BusinessBenchmarkRunReceiptV1,
    ) -> None:
        if candidate.variant != "candidate" or baseline.variant != "single_agent_baseline":
            raise BusinessBenchmarkExecutionError("receipt variants are not a candidate/baseline pair")
        shared_fields = (
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
            "execution_policy_sha256",
        )
        if any(
            getattr(candidate, field) != getattr(baseline, field)
            for field in shared_fields
        ):
            raise BusinessBenchmarkExecutionError("receipts must have exact pair bindings")


def _digest_model(model: BaseModel) -> str:
    return _digest_value(model.model_dump(mode="json", by_alias=True))


def _digest_value(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
