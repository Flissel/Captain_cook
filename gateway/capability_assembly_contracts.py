"""Typed, redacted contracts for the server-side capability assembly facade.

The contracts carry caller-selected identities and authority receipts only.
They never mint a provider, Gateway, Runtime, or Minibook receipt.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import PurePosixPath
from typing import Literal, Protocol
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from agenten.agent_factory.capability_live_runner import (
    CAPABILITY_LIVE_STAGES,
    CapabilityLiveCapabilityEvidence,
    CapabilityLiveReservation,
    CapabilityLiveStage,
)
from agenten.agent_factory.input_compiler import CompiledFactorySpecification
from agenten.agent_factory.input_contracts import FactoryInputDocumentV2
from agenten.agent_factory.skill_evaluation import ToolGapMarker
from agenten.agent_runtime.contracts import ArtifactRef, IDENTIFIER_PATTERN, SHA256_PATTERN
from gateway.capability_live_pipeline import (
    CapabilityBusinessBenchmarkAuthority,
    CapabilityExecutionAuthority,
    CapabilityFactoryAuthority,
    CapabilityProjectionAuthority,
    CapabilityResumeAuthorization,
)


class CapabilityAssemblyStatus(str, Enum):
    ADMITTED = "admitted"
    ADVANCING = "advancing"
    READY_TO_USE = "ready_to_use"


class CapabilityAssemblySliceKind(str, Enum):
    SCAFFOLD_AND_TEST_HARNESS = "scaffold_and_test_harness"
    CODEX_IMPLEMENTATION = "codex_implementation"
    SEAL = "seal"
    EVALUATE = "evaluate"
    PROMOTE = "promote"
    EXECUTE = "execute"
    PROJECT = "project"
    RESUME_AUTHORIZE = "resume_authorize"
    RESUME_DISPATCH = "resume_dispatch"
    RESUME_READBACK = "resume_readback"


ASSEMBLY_INPUT_STAGES = tuple(CapabilityLiveStage)
ASSEMBLY_TOTAL_SLICES = len(ASSEMBLY_INPUT_STAGES) * 3 + 3


class CapabilityAssemblyWriteConflict(RuntimeError):
    """The sole-writer repository rejected a stale or conflicting append."""


class _FrozenContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )


class CapabilityAssemblyInputRequest(_FrozenContract):
    """One caller-bound repository path and its immutable run authority."""

    input_id: str = Field(pattern=IDENTIFIER_PATTERN)
    repository_path: str = Field(min_length=1, max_length=512)
    correlation_id: UUID
    reservation: CapabilityLiveReservation

    @field_validator("repository_path")
    @classmethod
    def require_repository_relative_input(cls, value: str) -> str:
        if "\\" in value or re.match(r"^[A-Za-z]:", value):
            raise ValueError("repository-relative TO_BE_BUILT.md path is required")
        path = PurePosixPath(value)
        if path.is_absolute() or path.name != "TO_BE_BUILT.md":
            raise ValueError("repository-relative TO_BE_BUILT.md path is required")
        if any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("repository-relative TO_BE_BUILT.md path is required")
        return path.as_posix()

    @model_validator(mode="after")
    def require_reservation_binding(self) -> "CapabilityAssemblyInputRequest":
        if self.reservation.input_id != self.input_id:
            raise ValueError("input reservation changed the input identity")
        return self


class CapabilityAssemblySubmitRequest(_FrozenContract):
    schema_name: Literal["captain.capability-assembly-submit.v1"] = Field(
        default="captain.capability-assembly-submit.v1",
        alias="schema",
        serialization_alias="schema",
    )
    assembly_id: UUID
    idempotency_key: str = Field(pattern=SHA256_PATTERN)
    maximum_cost_usd: Decimal
    inputs: tuple[CapabilityAssemblyInputRequest, ...] = Field(
        min_length=3,
        max_length=3,
    )

    @field_validator("maximum_cost_usd", mode="before")
    @classmethod
    def require_positive_cost(cls, value: object) -> Decimal:
        return _positive_decimal(value, "maximum_cost_usd")

    @model_validator(mode="after")
    def require_exact_three_input_envelope(self) -> "CapabilityAssemblySubmitRequest":
        if len({item.input_id for item in self.inputs}) != 3:
            raise ValueError("assembly requires three distinct input IDs")
        if len({item.repository_path for item in self.inputs}) != 3:
            raise ValueError("assembly requires three distinct repository inputs")
        if len({item.correlation_id for item in self.inputs}) != 3:
            raise ValueError("assembly requires three distinct correlations")
        if len({item.reservation.reservation_id for item in self.inputs}) != 3:
            raise ValueError("assembly requires three distinct reservations")
        if any(item.reservation.run_id != self.assembly_id for item in self.inputs):
            raise ValueError("input reservation changed the assembly identity")
        reserved = sum(
            (item.reservation.maximum_cost_usd for item in self.inputs),
            Decimal("0"),
        )
        if reserved != self.maximum_cost_usd:
            raise ValueError("three reservations must equal the assembly cost ceiling")
        return self


class CapabilityAssemblyCanonicalInput(_FrozenContract):
    """Exact canonical input and compiler result admitted by production Captain."""

    schema_name: Literal["captain.capability-assembly-canonical-input.v1"] = Field(
        default="captain.capability-assembly-canonical-input.v1",
        alias="schema",
        serialization_alias="schema",
    )
    input_id: str = Field(pattern=IDENTIFIER_PATTERN)
    conversation_pattern: Literal["sequential", "selector_group_chat", "swarm"]
    document: FactoryInputDocumentV2
    compiled_specification: CompiledFactorySpecification
    tool_gaps: tuple[ToolGapMarker, ...] = ()

    @property
    def source_ref(self) -> ArtifactRef:
        return self.document.input_ref

    @property
    def n8n_required(self) -> bool:
        return any(integration.required for integration in self.document.integrations)

    @property
    def required_tool_gaps(self) -> tuple[ToolGapMarker, ...]:
        return tuple(
            marker
            for marker in self.tool_gaps
            if marker.severity == "required" and marker.status == "unresolved"
        )

    @field_serializer("compiled_specification")
    def serialize_compiled_specification(
        self,
        value: CompiledFactorySpecification,
    ) -> dict[str, object]:
        return value.model_dump(
            mode="json",
            by_alias=True,
            exclude_computed_fields=True,
        )

    @model_validator(mode="after")
    def require_compiler_source_binding(self) -> "CapabilityAssemblyCanonicalInput":
        if self.compiled_specification.source_ref != self.document.input_ref:
            raise ValueError("compiled specification changed canonical source authority")
        gap_ids = tuple(marker.gap_id for marker in self.tool_gaps)
        if len(gap_ids) != len(set(gap_ids)):
            raise ValueError("canonical input contains duplicate tool gap identities")
        assertion_ids = set(self.compiled_specification.assertion_ids)
        for marker in self.tool_gaps:
            if not set(marker.acceptance_assertion_ids).issubset(assertion_ids):
                raise ValueError("tool gap changed compiled acceptance authority")
        return self


class CapabilityAssemblyAdmittedInput(_FrozenContract):
    """Read-only admission result for exact repository-owned source bytes."""

    repository_path: str = Field(min_length=1, max_length=512)
    input: CapabilityAssemblyCanonicalInput

    @field_validator("repository_path")
    @classmethod
    def require_canonical_path(cls, value: str) -> str:
        return CapabilityAssemblyInputRequest.require_repository_relative_input(value)


class CapabilityAssemblyStageRequest(_FrozenContract):
    """One compiler-bound assembly stage request, separate from the legacy demo."""

    run_id: UUID
    input: CapabilityAssemblyCanonicalInput
    correlation_id: UUID
    reservation: CapabilityLiveReservation
    stage: CapabilityLiveStage
    controlled_recovery_required: bool
    suite_ref: ArtifactRef | None = None
    hermes_result_ref: ArtifactRef | None = None
    retry_number: int = Field(default=0, ge=0, le=5, strict=True)

    @model_validator(mode="after")
    def require_plan_binding(self) -> "CapabilityAssemblyStageRequest":
        if (
            self.reservation.run_id != self.run_id
            or self.reservation.input_id != self.input.input_id
        ):
            raise ValueError("assembly stage request does not match its budget reservation")
        return self


class CapabilityAssemblyItemState(_FrozenContract):
    request: CapabilityAssemblyInputRequest
    admitted: CapabilityAssemblyAdmittedInput
    completed_stages: tuple[CapabilityLiveStage, ...] = ()
    stage_authority: CapabilityFactoryAuthority | None = None
    benchmark_authority: CapabilityBusinessBenchmarkAuthority | None = None
    release_authority: CapabilityFactoryAuthority | None = None
    execution_authority: CapabilityExecutionAuthority | None = None
    projection_authority: CapabilityProjectionAuthority | None = None
    capability_evidence: CapabilityLiveCapabilityEvidence | None = None

    @model_validator(mode="after")
    def require_monotone_stage_prefix(self) -> "CapabilityAssemblyItemState":
        expected = ASSEMBLY_INPUT_STAGES[: len(self.completed_stages)]
        if self.completed_stages != expected:
            raise ValueError("assembly item stages must form a monotone prefix")
        count = len(self.completed_stages)
        if count >= 1 and self.stage_authority is None:
            raise ValueError("Factory stage authority is unavailable")
        if count >= 4 and self.benchmark_authority is None:
            raise ValueError("benchmark authority is unavailable")
        if count >= 5 and self.release_authority is None:
            raise ValueError("release authority is unavailable")
        if count >= 6 and self.execution_authority is None:
            raise ValueError("execution authority is unavailable")
        if count >= 7 and (
            self.projection_authority is None or self.capability_evidence is None
        ):
            raise ValueError("projection authority is unavailable")
        return self


class CapabilityAssemblySlice(_FrozenContract):
    ordinal: int = Field(ge=0, lt=ASSEMBLY_TOTAL_SLICES, strict=True)
    kind: CapabilityAssemblySliceKind
    input_id: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)


class CapabilityAssemblyNoEffectRetryAuthorization(_FrozenContract):
    """Captain receipt proving a started slice produced no external effect."""

    schema_name: Literal["captain.capability-assembly-no-effect-retry.v1"] = Field(
        default="captain.capability-assembly-no-effect-retry.v1",
        alias="schema",
        serialization_alias="schema",
    )
    assembly_id: UUID
    operation_id: UUID
    request_sha256: str = Field(pattern=SHA256_PATTERN)
    started_slice: CapabilityAssemblySlice
    started_checkpoint_ref: ArtifactRef
    authorization_receipt_id: UUID
    evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    no_effect_observed: Literal[True]


class CapabilityAssemblyReconciliationOutcome(_FrozenContract):
    """External readback or explicit Captain no-effect retry authorization."""

    schema_name: Literal["captain.capability-assembly-reconciliation.v1"] = Field(
        default="captain.capability-assembly-reconciliation.v1",
        alias="schema",
        serialization_alias="schema",
    )
    assembly_id: UUID
    operation_id: UUID
    request_sha256: str = Field(pattern=SHA256_PATTERN)
    started_slice: CapabilityAssemblySlice
    started_checkpoint_ref: ArtifactRef
    disposition: Literal["terminal_readback", "retry_authorized_no_effect"]
    retry_authorization: CapabilityAssemblyNoEffectRetryAuthorization | None = None
    factory_authority: CapabilityFactoryAuthority | None = None
    benchmark_authority: CapabilityBusinessBenchmarkAuthority | None = None
    execution_authority: CapabilityExecutionAuthority | None = None
    projection_authority: CapabilityProjectionAuthority | None = None
    resume_authorization: CapabilityResumeAuthorization | None = None

    @model_validator(mode="after")
    def require_typed_slice_result(self) -> "CapabilityAssemblyReconciliationOutcome":
        authorities = (
            self.factory_authority,
            self.benchmark_authority,
            self.execution_authority,
            self.projection_authority,
            self.resume_authorization,
        )
        if self.disposition == "retry_authorized_no_effect":
            if self.retry_authorization is None or any(
                authority is not None for authority in authorities
            ):
                raise ValueError("no-effect retry requires only Captain authorization")
            authorization = self.retry_authorization
            if (
                authorization.assembly_id != self.assembly_id
                or authorization.operation_id != self.operation_id
                or authorization.request_sha256 != self.request_sha256
                or authorization.started_slice != self.started_slice
                or authorization.started_checkpoint_ref != self.started_checkpoint_ref
            ):
                raise ValueError("no-effect retry authorization changed reconciliation scope")
            return self
        if self.retry_authorization is not None:
            raise ValueError("terminal readback cannot contain retry authorization")
        expected_field = {
            CapabilityAssemblySliceKind.SCAFFOLD_AND_TEST_HARNESS: "factory_authority",
            CapabilityAssemblySliceKind.CODEX_IMPLEMENTATION: "factory_authority",
            CapabilityAssemblySliceKind.SEAL: "factory_authority",
            CapabilityAssemblySliceKind.EVALUATE: "benchmark_authority",
            CapabilityAssemblySliceKind.PROMOTE: "factory_authority",
            CapabilityAssemblySliceKind.EXECUTE: "execution_authority",
            CapabilityAssemblySliceKind.PROJECT: "projection_authority",
            CapabilityAssemblySliceKind.RESUME_AUTHORIZE: "resume_authorization",
            CapabilityAssemblySliceKind.RESUME_DISPATCH: "execution_authority",
            CapabilityAssemblySliceKind.RESUME_READBACK: "execution_authority",
        }[self.started_slice.kind]
        if sum(authority is not None for authority in authorities) != 1:
            raise ValueError("terminal reconciliation requires one typed authority")
        if getattr(self, expected_field) is None:
            raise ValueError("terminal reconciliation authority does not match its slice")
        return self


class CapabilityAssemblyReconciliationRequest(_FrozenContract):
    schema_name: Literal["captain.capability-assembly-reconcile.v1"] = Field(
        default="captain.capability-assembly-reconcile.v1",
        alias="schema",
        serialization_alias="schema",
    )
    assembly_id: UUID
    operation_id: UUID
    request_sha256: str = Field(pattern=SHA256_PATTERN)
    started_slice: CapabilityAssemblySlice
    started_checkpoint_ref: ArtifactRef


class CapabilityAssemblyActiveAdvance(_FrozenContract):
    operation_id: UUID
    request_sha256: str = Field(pattern=SHA256_PATTERN)
    maximum_slices: int = Field(ge=1, le=8, strict=True)
    start_completed_slices: int = Field(ge=0, le=ASSEMBLY_TOTAL_SLICES, strict=True)
    target_completed_slices: int = Field(ge=0, le=ASSEMBLY_TOTAL_SLICES, strict=True)

    @model_validator(mode="after")
    def require_bounded_target(self) -> "CapabilityAssemblyActiveAdvance":
        if not (
            self.start_completed_slices
            <= self.target_completed_slices
            <= self.start_completed_slices + self.maximum_slices
        ):
            raise ValueError("advance target exceeds its bounded slice request")
        return self


class CapabilityAssemblyAdvanceReceipt(_FrozenContract):
    schema_name: Literal["captain.capability-assembly-advance-receipt.v1"] = Field(
        default="captain.capability-assembly-advance-receipt.v1",
        alias="schema",
        serialization_alias="schema",
    )
    assembly_id: UUID
    operation_id: UUID
    request_sha256: str = Field(pattern=SHA256_PATTERN)
    maximum_slices: int = Field(ge=1, le=8, strict=True)
    start_completed_slices: int = Field(ge=0, le=ASSEMBLY_TOTAL_SLICES, strict=True)
    end_completed_slices: int = Field(ge=0, le=ASSEMBLY_TOTAL_SLICES, strict=True)
    executed_slices: int = Field(ge=0, le=8, strict=True)
    status: CapabilityAssemblyStatus

    @model_validator(mode="after")
    def require_exact_delta(self) -> "CapabilityAssemblyAdvanceReceipt":
        if self.end_completed_slices - self.start_completed_slices != self.executed_slices:
            raise ValueError("advance receipt slice delta is invalid")
        if self.executed_slices > self.maximum_slices:
            raise ValueError("advance receipt exceeded its slice bound")
        return self


class CapabilityAssemblyCheckpoint(_FrozenContract):
    schema_name: Literal["captain.capability-assembly-checkpoint.v1"] = Field(
        default="captain.capability-assembly-checkpoint.v1",
        alias="schema",
        serialization_alias="schema",
    )
    assembly_id: UUID
    request: CapabilityAssemblySubmitRequest
    request_sha256: str = Field(pattern=SHA256_PATTERN)
    revision: int = Field(ge=1, strict=True)
    status: CapabilityAssemblyStatus
    completed_slices: int = Field(ge=0, le=ASSEMBLY_TOTAL_SLICES, strict=True)
    items: tuple[CapabilityAssemblyItemState, ...] = Field(min_length=3, max_length=3)
    active_advance: CapabilityAssemblyActiveAdvance | None = None
    advance_receipts: tuple[CapabilityAssemblyAdvanceReceipt, ...] = ()
    resume_authorization: CapabilityResumeAuthorization | None = None
    resume_dispatch: CapabilityExecutionAuthority | None = None
    resume_readback: CapabilityExecutionAuthority | None = None
    last_slice: CapabilityAssemblySlice | None = None
    started_slice: CapabilityAssemblySlice | None = None
    reconciliation_outcomes: tuple[CapabilityAssemblyReconciliationOutcome, ...] = ()

    @property
    def idempotency_key(self) -> str:
        return self.request.idempotency_key

    @property
    def inputs(self) -> tuple[CapabilityAssemblyAdmittedInput, ...]:
        return tuple(item.admitted for item in self.items)

    @model_validator(mode="after")
    def require_internal_binding(self) -> "CapabilityAssemblyCheckpoint":
        if self.request.assembly_id != self.assembly_id:
            raise ValueError("checkpoint changed the assembly identity")
        if tuple(item.request for item in self.items) != self.request.inputs:
            raise ValueError("checkpoint changed the submitted inputs")
        resume_count = 0
        if self.resume_authorization is not None:
            resume_count = 1
        if self.resume_dispatch is not None:
            if self.resume_authorization is None:
                raise ValueError("resume dispatch lacks authorization")
            resume_count = 2
        if self.resume_readback is not None:
            if self.resume_dispatch is None:
                raise ValueError("resume read-back lacks dispatch")
            resume_count = 3
        observed_slices = sum(len(item.completed_stages) for item in self.items) + resume_count
        if observed_slices != self.completed_slices:
            raise ValueError("checkpoint slice count does not match authority progress")
        if self.active_advance is not None and not (
            self.active_advance.start_completed_slices
            <= self.completed_slices
            <= self.active_advance.target_completed_slices
        ):
            raise ValueError("checkpoint progress escaped its active advance bound")
        if self.started_slice is not None:
            if (
                self.active_advance is None
                or self.completed_slices >= self.active_advance.target_completed_slices
                or self.started_slice.ordinal != self.completed_slices
            ):
                raise ValueError("started slice escaped its active advance bound")
        if len({receipt.operation_id for receipt in self.advance_receipts}) != len(
            self.advance_receipts
        ):
            raise ValueError("checkpoint contains duplicate advance receipts")
        if any(
            receipt.assembly_id != self.assembly_id
            or receipt.request_sha256 != self.request_sha256
            for receipt in self.advance_receipts
        ):
            raise ValueError("advance receipt changed assembly authority")
        if any(
            outcome.assembly_id != self.assembly_id
            or outcome.request_sha256 != self.request_sha256
            for outcome in self.reconciliation_outcomes
        ):
            raise ValueError("reconciliation outcome changed assembly authority")
        outcome_keys = tuple(
            (
                outcome.operation_id,
                outcome.started_checkpoint_ref.sha256,
                outcome.disposition,
            )
            for outcome in self.reconciliation_outcomes
        )
        if len(set(outcome_keys)) != len(outcome_keys):
            raise ValueError("checkpoint contains duplicate reconciliation outcomes")
        retry_authorizations: dict[UUID, CapabilityAssemblyNoEffectRetryAuthorization] = {}
        for outcome in self.reconciliation_outcomes:
            authorization = outcome.retry_authorization
            if authorization is None:
                continue
            prior = retry_authorizations.get(authorization.authorization_receipt_id)
            if prior is not None:
                if canonical_assembly_model_bytes(prior) != canonical_assembly_model_bytes(
                    authorization
                ):
                    raise ValueError(
                        "authorization receipt changed its binding or evidence"
                    )
                raise ValueError("authorization receipt was consumed more than once")
            retry_authorizations[authorization.authorization_receipt_id] = authorization
        if self.status is CapabilityAssemblyStatus.READY_TO_USE:
            if self.completed_slices != ASSEMBLY_TOTAL_SLICES:
                raise ValueError("ready assembly has incomplete slices")
            if self.resume_readback is None:
                raise ValueError("ready assembly lacks resume read-back")
        elif self.completed_slices == ASSEMBLY_TOTAL_SLICES:
            raise ValueError("complete assembly must be ready_to_use")
        return self


class CapabilityAssemblyAdvanceRequest(_FrozenContract):
    schema_name: Literal["captain.capability-assembly-advance.v1"] = Field(
        default="captain.capability-assembly-advance.v1",
        alias="schema",
        serialization_alias="schema",
    )
    assembly_id: UUID
    operation_id: UUID
    request_sha256: str = Field(pattern=SHA256_PATTERN)
    maximum_slices: int = Field(ge=1, le=8, strict=True)


class CapabilityAssemblyInputAdmissionPort(Protocol):
    """Compile exact repository bytes into a content-addressed, classified input."""

    def admit(
        self,
        request: CapabilityAssemblyInputRequest,
    ) -> CapabilityAssemblyAdmittedInput: ...


class CapabilityAssemblyFactoryPort(Protocol):
    async def scaffold(
        self, request: CapabilityAssemblyStageRequest
    ) -> CapabilityFactoryAuthority | None: ...

    async def implement(
        self, request: CapabilityAssemblyStageRequest
    ) -> CapabilityFactoryAuthority | None: ...

    async def seal(
        self, request: CapabilityAssemblyStageRequest
    ) -> CapabilityFactoryAuthority | None: ...

    async def promote(
        self, request: CapabilityAssemblyStageRequest
    ) -> CapabilityFactoryAuthority | None: ...

    async def released(
        self, request: CapabilityAssemblyStageRequest
    ) -> CapabilityFactoryAuthority | None: ...


class CapabilityAssemblyBusinessBenchmarkPort(Protocol):
    async def evaluate(
        self, request: CapabilityAssemblyStageRequest
    ) -> CapabilityBusinessBenchmarkAuthority | None: ...


class CapabilityAssemblyExecutionPort(Protocol):
    async def execute(
        self,
        request: CapabilityAssemblyStageRequest,
        release: CapabilityFactoryAuthority,
    ) -> CapabilityExecutionAuthority | None: ...


class CapabilityAssemblyProjectionPort(Protocol):
    async def project_and_readback(
        self,
        request: CapabilityAssemblyStageRequest,
        release: CapabilityFactoryAuthority,
        execution: CapabilityExecutionAuthority,
    ) -> CapabilityProjectionAuthority | None: ...


class CapabilityAssemblyReconciliationPort(Protocol):
    """Read external truth for a durably started slice without re-executing it."""

    async def reconcile(
        self,
        request: CapabilityAssemblyReconciliationRequest,
    ) -> CapabilityAssemblyReconciliationOutcome | None: ...


class CapabilityAssemblyRecordPort(Protocol):
    checkpoint: CapabilityAssemblyCheckpoint
    artifact_ref: ArtifactRef


class CapabilityAssemblyRepositoryPort(Protocol):
    """Append-only, atomic sole-writer persistence for assembly checkpoints."""

    def history(self, assembly_id: UUID) -> tuple[CapabilityAssemblyRecordPort, ...]: ...

    def find_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> CapabilityAssemblyRecordPort | None: ...

    def append(
        self,
        checkpoint: CapabilityAssemblyCheckpoint,
        *,
        expected_revision: int,
    ) -> CapabilityAssemblyRecordPort: ...

    def consume_retry_authorization_and_append(
        self,
        checkpoint: CapabilityAssemblyCheckpoint,
        authorization: CapabilityAssemblyNoEffectRetryAuthorization,
        *,
        expected_revision: int,
    ) -> CapabilityAssemblyRecordPort:
        """Atomically consume one globally unique receipt and append its checkpoint."""
        ...


def canonical_assembly_model_bytes(value: BaseModel) -> bytes:
    import json

    return (
        json.dumps(
            value.model_dump(
                mode="json",
                by_alias=True,
                exclude_computed_fields=True,
            ),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def assembly_request_sha256(request: CapabilityAssemblySubmitRequest) -> str:
    import hashlib

    return hashlib.sha256(canonical_assembly_model_bytes(request)).hexdigest()


def _positive_decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite decimal")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label} must be a finite decimal") from None
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{label} must be positive and finite")
    return parsed


__all__ = [
    "ASSEMBLY_INPUT_STAGES",
    "ASSEMBLY_TOTAL_SLICES",
    "CapabilityAssemblyActiveAdvance",
    "CapabilityAssemblyAdmittedInput",
    "CapabilityAssemblyCanonicalInput",
    "CapabilityAssemblyAdvanceReceipt",
    "CapabilityAssemblyAdvanceRequest",
    "CapabilityAssemblyBusinessBenchmarkPort",
    "CapabilityAssemblyCheckpoint",
    "CapabilityAssemblyExecutionPort",
    "CapabilityAssemblyFactoryPort",
    "CapabilityAssemblyInputAdmissionPort",
    "CapabilityAssemblyInputRequest",
    "CapabilityAssemblyItemState",
    "CapabilityAssemblyRecordPort",
    "CapabilityAssemblyReconciliationOutcome",
    "CapabilityAssemblyReconciliationPort",
    "CapabilityAssemblyReconciliationRequest",
    "CapabilityAssemblyRepositoryPort",
    "CapabilityAssemblyProjectionPort",
    "CapabilityAssemblyNoEffectRetryAuthorization",
    "CapabilityAssemblySlice",
    "CapabilityAssemblySliceKind",
    "CapabilityAssemblyStatus",
    "CapabilityAssemblyStageRequest",
    "CapabilityAssemblySubmitRequest",
    "CapabilityAssemblyWriteConflict",
    "assembly_request_sha256",
    "canonical_assembly_model_bytes",
]
