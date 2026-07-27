"""Deterministic, paired execution envelopes for private business benchmarks."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkCaseV1,
    BusinessBenchmarkRunReceiptV1,
    canonical_business_benchmark_model_bytes,
)
from agenten.agent_factory.business_benchmark_replay import (
    BenchmarkRecoveryUncertainError,
    BusinessBenchmarkEffectClaimV1,
    BusinessBenchmarkEffectIdentityV1,
    BusinessBenchmarkFenceReceiptV1,
    BusinessBenchmarkPreparedEffectV1,
    BusinessBenchmarkRecoveryObservationV1,
    BusinessBenchmarkReplayStore,
    BusinessBenchmarkRuntimePreparationV1,
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
    """Provider boundary that registers and enforces the greatest seen fence.

    Implementations must durably register each fence before recovery/execution
    and reject either operation when its claim or fence is older than the
    provider-side maximum for the prepared runtime identity.
    """

    async def prepare(
        self, envelope: BusinessBenchmarkExecutionEnvelopeV1
    ) -> BusinessBenchmarkRuntimePreparationV1: ...

    async def execute(
        self,
        envelope: BusinessBenchmarkExecutionEnvelopeV1,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
    ) -> BusinessBenchmarkRunReceiptV1: ...

    async def register_fence(
        self,
        prepared: BusinessBenchmarkPreparedEffectV1,
        claim: BusinessBenchmarkEffectClaimV1,
    ) -> BusinessBenchmarkFenceReceiptV1: ...

    async def recover(
        self,
        prepared: BusinessBenchmarkPreparedEffectV1,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
    ) -> BusinessBenchmarkRecoveryObservationV1: ...


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
        replay_store: BusinessBenchmarkReplayStore,
        clock: Callable[[], datetime] | None = None,
        claim_ttl: timedelta = timedelta(minutes=5),
        claim_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if subject_version < 1:
            raise ValueError("subject_version must be positive")
        if attempt < 1 or attempt > 5:
            raise ValueError("attempt must be between 1 and 5")
        if not suite_id:
            raise ValueError("suite_id must not be blank")
        if claim_ttl <= timedelta(0):
            raise ValueError("claim_ttl must be positive")
        self._job_id = job_id
        self._correlation_id = correlation_id
        self._subject_version = subject_version
        self._attempt = attempt
        self._suite_id = suite_id
        self._executor = executor
        self._replay_store = replay_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._claim_ttl = claim_ttl
        self._claim_id_factory = claim_id_factory

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
        candidate_receipt = await self._run_variant(candidate)
        baseline_receipt = await self._run_variant(baseline)
        self._validate_pair(candidate_receipt, baseline_receipt)
        return candidate_receipt, baseline_receipt

    async def _run_variant(
        self, envelope: BusinessBenchmarkExecutionEnvelopeV1
    ) -> BusinessBenchmarkRunReceiptV1:
        identity = BusinessBenchmarkEffectIdentityV1.create(
            request_id=envelope.request_id,
            job_id=envelope.job_id,
            correlation_id=envelope.correlation_id,
            subject_version=envelope.subject_version,
            attempt=envelope.attempt,
            suite_ref=envelope.suite_ref,
            suite_id=envelope.suite_id,
            case_id=envelope.case.case_id,
            variant=envelope.variant,
            execution_policy_sha256=envelope.execution_policy_sha256,
            variant_policy_sha256=envelope.variant_policy_sha256,
        )
        snapshot = self._replay_store.snapshot(identity)
        if snapshot.receipt is not None:
            self._validate_receipt(snapshot.receipt, envelope)
            return snapshot.receipt
        prepared = snapshot.prepared_effect
        if prepared is None:
            preparation = await self._executor.prepare(envelope)
            if preparation.runtime_session_id != envelope.runtime_session_id:
                raise BusinessBenchmarkExecutionError(
                    "prepared runtime session does not match envelope"
                )
            prepared = BusinessBenchmarkPreparedEffectV1(
                schema="captain.business-benchmark-prepared-effect.v1",
                identity=identity,
                runtime_session_id=preparation.runtime_session_id,
            )
        now = self._clock()
        result = self._replay_store.claim(
            prepared,
            claim_id=self._claim_id_factory(),
            acquired_at=now,
            expires_at=now + self._claim_ttl,
        )
        if result.receipt is not None:
            self._validate_receipt(result.receipt, envelope)
            return result.receipt
        claim = result.claim
        if not result.acquired or claim is None:
            raise BusinessBenchmarkExecutionError(
                "benchmark replay store returned no effect claim"
            )
        fence_receipt = await self._executor.register_fence(
            claim.prepared_effect, claim
        )
        self._validate_fence_receipt(fence_receipt, claim)
        fence_receipt = self._replay_store.record_fence(claim, fence_receipt)
        if result.recovery_required:
            recovery = await self._executor.recover(
                claim.prepared_effect, claim, fence_receipt
            )
            self._validate_recovery(recovery, claim, fence_receipt)
            recovery = self._replay_store.record_recovery(
                claim, fence_receipt, recovery
            )
            if recovery.outcome == "terminal":
                recovered_receipt = recovery.receipt
                if recovered_receipt is None:
                    raise BusinessBenchmarkExecutionError(
                        "terminal recovery did not include a receipt"
                    )
                self._validate_receipt(recovered_receipt, envelope)
                return self._replay_store.complete(
                    claim, fence_receipt, recovered_receipt
                )
            if recovery.outcome == "uncertain":
                raise BenchmarkRecoveryUncertainError(
                    "benchmark effect recovery is uncertain; execution remains fenced"
                )
        receipt = await self._executor.execute(envelope, claim, fence_receipt)
        self._validate_receipt(receipt, envelope)
        return self._replay_store.complete(claim, fence_receipt, receipt)

    @staticmethod
    def _validate_fence_receipt(
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
        claim: BusinessBenchmarkEffectClaimV1,
    ) -> None:
        expected: dict[str, object] = {
            "effect_id": claim.identity.effect_id,
            "runtime_session_id": claim.prepared_effect.runtime_session_id,
            "claim_id": claim.claim_id,
            "fence": claim.fence,
        }
        for field_name, value in expected.items():
            if getattr(fence_receipt, field_name) != value:
                raise BusinessBenchmarkExecutionError(
                    f"provider fence receipt {field_name} does not match claim"
                )

    @staticmethod
    def _validate_recovery(
        recovery: BusinessBenchmarkRecoveryObservationV1,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
    ) -> None:
        if recovery.effect_id != claim.identity.effect_id:
            raise BusinessBenchmarkExecutionError(
                "recovery effect identity does not match prepared effect"
            )
        if recovery.runtime_session_id != claim.prepared_effect.runtime_session_id:
            raise BusinessBenchmarkExecutionError(
                "recovery runtime session does not match prepared effect"
            )
        if recovery.claim_id != claim.claim_id or recovery.fence != claim.fence:
            raise BusinessBenchmarkExecutionError(
                "recovery claim or fence does not match current claim"
            )
        if recovery.fence_receipt != fence_receipt:
            raise BusinessBenchmarkExecutionError(
                "recovery proof does not match provider fence receipt"
            )

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
            "subject_version": self._subject_version,
            "attempt": self._attempt,
            "suite_ref": suite_ref.model_dump(mode="json", by_alias=True),
            "suite_id": self._suite_id,
            "case_id": case.case_id,
            "case_sha256": _digest_model(case),
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
            runtime_session_id=f"benchmark-session-{variant}-{idempotency_key}",
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
            "case_sha256",
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
            "case_sha256",
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
    return hashlib.sha256(canonical_business_benchmark_model_bytes(model)).hexdigest()


def _digest_value(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
