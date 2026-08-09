"""Three-input provider-live capability orchestration.

The runner owns only suite shape, bounded reservations, resumable stage
checkpoints, and final evidence validation.  Factory, benchmark, Gateway,
runtime, and Minibook decisions stay behind ``CapabilityLivePipelinePort``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol, TypeVar
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_runtime.confined_files import ConfinedFileError, ConfinedFileStore
from agenten.agent_runtime.contracts import ArtifactRef, IDENTIFIER_PATTERN


_LIVE_NAMESPACE = UUID("68dfbb91-acde-5d85-ae91-f36230135807")
_CENT = Decimal("0.01")
_ModelT = TypeVar("_ModelT", bound=BaseModel)


class CapabilityLiveEvidenceError(RuntimeError):
    """A final provider-live claim is incomplete or crosses an authority."""


class CapabilityLiveRuntimeRetry(RuntimeError):
    """The current runtime stage may be retried with its exact prior bindings."""


class CapabilityLiveStage(str, Enum):
    SCAFFOLD_AND_TEST_HARNESS = "scaffold_and_test_harness"
    CODEX_IMPLEMENTATION = "codex_implementation"
    SEAL = "seal"
    EVALUATE = "evaluate"
    PROMOTE = "promote"
    EXECUTE = "execute"
    PROJECT = "project"


CAPABILITY_LIVE_STAGES = tuple(CapabilityLiveStage)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )


class CapabilityLiveInput(_FrozenModel):
    """Digest-only description of one repository-owned canonical input."""

    input_id: str = Field(pattern=IDENTIFIER_PATTERN)
    conversation_pattern: Literal["sequential", "selector_group_chat", "swarm"]
    source_ref: ArtifactRef
    integration_intent: Literal["n8n", "none"]
    conflicting_input: bool
    human_escalation_required: bool
    required_tool_marker: Literal["TODO_TOOL.v1"] | None

    @model_validator(mode="after")
    def require_canonical_demo_semantics(self) -> "CapabilityLiveInput":
        if self.conversation_pattern == "sequential":
            if self.integration_intent != "n8n":
                raise ValueError("sequential demo input requires n8n")
            if (
                self.conflicting_input
                or self.human_escalation_required
                or self.required_tool_marker is not None
            ):
                raise ValueError("sequential demo input has foreign pattern semantics")
        elif self.conversation_pattern == "selector_group_chat":
            if (
                self.integration_intent != "none"
                or not self.conflicting_input
                or not self.human_escalation_required
                or self.required_tool_marker is not None
            ):
                raise ValueError(
                    "selector group-chat input requires conflict and human escalation"
                )
        elif (
            self.integration_intent != "none"
            or self.conflicting_input
            or self.human_escalation_required
            or self.required_tool_marker != "TODO_TOOL.v1"
        ):
            raise ValueError("swarm input requires one missing required API marker")
        return self


class CapabilityLiveReservationRequest(_FrozenModel):
    reservation_id: UUID
    run_id: UUID
    input_id: str = Field(pattern=IDENTIFIER_PATTERN)
    maximum_cost_usd: Decimal

    @field_validator("maximum_cost_usd", mode="before")
    @classmethod
    def require_positive_cost(cls, value: object) -> Decimal:
        return _positive_decimal(value, "maximum_cost_usd")


class CapabilityLiveReservation(CapabilityLiveReservationRequest):
    """One Captain-authorized input share of the total live ceiling."""


class CapabilityLiveCapabilityEvidence(_FrozenModel):
    """Redacted authority receipts for one completed capability."""

    input_id: str = Field(pattern=IDENTIFIER_PATTERN)
    conversation_pattern: Literal["sequential", "selector_group_chat", "swarm"]
    job_id: UUID
    correlation_id: UUID
    capability_id: str = Field(min_length=1)
    capability_version: int = Field(ge=1, strict=True)
    controlled_recovery_id: UUID | None
    normal_trace_ids: tuple[UUID, ...]
    release_authority_id: UUID | None
    execution_authority_id: UUID | None
    execution_command_id: UUID | None
    execution_result_id: UUID | None
    projection_event_id: UUID | None
    projection_readback_id: UUID | None
    suite_ref: ArtifactRef | None
    hermes_result_ref: ArtifactRef | None
    booked_cost_usd: Decimal
    checkpoint_refs: tuple[ArtifactRef, ...]

    @field_validator("booked_cost_usd", mode="before")
    @classmethod
    def require_known_cost(cls, value: object) -> Decimal:
        return _nonnegative_decimal(value, "booked_cost_usd")


class CapabilityLiveRestartEvidence(_FrozenModel):
    """Immutable original and resumed execution identities for one restart."""

    run_id: UUID
    job_id: UUID
    reservation_id: UUID
    correlation_id: UUID
    capability_id: str = Field(min_length=1)
    capability_version: int = Field(ge=1, strict=True)
    original_execution_command_id: UUID
    original_execution_result_id: UUID
    resumed_execution_authority_id: UUID
    resumed_execution_command_id: UUID
    resumed_execution_result_id: UUID
    projection_event_id: UUID | None
    projection_readback_id: UUID | None


class CapabilityLiveStageRequest(_FrozenModel):
    run_id: UUID
    input: CapabilityLiveInput
    correlation_id: UUID
    reservation: CapabilityLiveReservation
    stage: CapabilityLiveStage
    controlled_recovery_required: bool
    suite_ref: ArtifactRef | None = None
    hermes_result_ref: ArtifactRef | None = None
    retry_number: int = Field(default=0, ge=0, le=5, strict=True)

    @model_validator(mode="after")
    def require_plan_binding(self) -> "CapabilityLiveStageRequest":
        if (
            self.reservation.run_id != self.run_id
            or self.reservation.input_id != self.input.input_id
        ):
            raise ValueError("stage request does not match its budget reservation")
        return self


class CapabilityLiveStageResult(_FrozenModel):
    stage: CapabilityLiveStage
    suite_ref: ArtifactRef
    hermes_result_ref: ArtifactRef
    evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    booked_cost_usd: Decimal
    capability_evidence: CapabilityLiveCapabilityEvidence | None = None

    @field_validator("booked_cost_usd", mode="before")
    @classmethod
    def require_known_cost(cls, value: object) -> Decimal:
        return _nonnegative_decimal(value, "booked_cost_usd")

    @model_validator(mode="after")
    def require_project_evidence_only_at_project(self) -> "CapabilityLiveStageResult":
        at_project = self.stage is CapabilityLiveStage.PROJECT
        if at_project != (self.capability_evidence is not None):
            raise ValueError("only the project stage may return final capability evidence")
        return self


class CapabilityLiveStageCheckpoint(_FrozenModel):
    schema_name: Literal["captain.capability-live-stage-checkpoint.v1"] = Field(
        default="captain.capability-live-stage-checkpoint.v1",
        alias="schema",
        serialization_alias="schema",
    )
    run_id: UUID
    input_id: str = Field(pattern=IDENTIFIER_PATTERN)
    correlation_id: UUID
    reservation: CapabilityLiveReservation
    stage: CapabilityLiveStage
    stage_index: int = Field(ge=0, le=6, strict=True)
    suite_ref: ArtifactRef
    hermes_result_ref: ArtifactRef
    evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    booked_cost_usd: Decimal
    capability_evidence: CapabilityLiveCapabilityEvidence | None = None
    completed_at: datetime

    @field_validator("booked_cost_usd", mode="before")
    @classmethod
    def require_known_cost(cls, value: object) -> Decimal:
        return _nonnegative_decimal(value, "booked_cost_usd")

    @field_validator("completed_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def require_stage_and_reservation_binding(self) -> "CapabilityLiveStageCheckpoint":
        if CAPABILITY_LIVE_STAGES[self.stage_index] is not self.stage:
            raise ValueError("checkpoint stage index does not match stage")
        if (
            self.reservation.run_id != self.run_id
            or self.reservation.input_id != self.input_id
            or self.booked_cost_usd > self.reservation.maximum_cost_usd
        ):
            raise ValueError("checkpoint does not match its reservation")
        if (self.stage is CapabilityLiveStage.PROJECT) != (
            self.capability_evidence is not None
        ):
            raise ValueError("project checkpoint final evidence is incomplete")
        return self


class CapabilityLiveRunPlanItem(_FrozenModel):
    input: CapabilityLiveInput
    correlation_id: UUID
    reservation: CapabilityLiveReservation


class CapabilityLiveRunPlan(_FrozenModel):
    schema_name: Literal["captain.capability-live-run-plan.v1"] = Field(
        default="captain.capability-live-run-plan.v1",
        alias="schema",
        serialization_alias="schema",
    )
    run_id: UUID
    maximum_cost_usd: Decimal
    items: tuple[CapabilityLiveRunPlanItem, ...] = Field(min_length=3, max_length=3)
    created_at: datetime

    @field_validator("maximum_cost_usd", mode="before")
    @classmethod
    def require_positive_cost(cls, value: object) -> Decimal:
        return _positive_decimal(value, "maximum_cost_usd")

    @field_validator("created_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def require_exact_reservation_envelope(self) -> "CapabilityLiveRunPlan":
        if any(item.reservation.run_id != self.run_id for item in self.items):
            raise ValueError("run plan contains a foreign reservation")
        if sum(
            (item.reservation.maximum_cost_usd for item in self.items),
            Decimal("0"),
        ) != self.maximum_cost_usd:
            raise ValueError("run plan reservations do not equal the total ceiling")
        return self


class CapabilityLiveDemoEvidence(_FrozenModel):
    schema_name: Literal["captain.capability-live-demo-evidence.v1"] = Field(
        default="captain.capability-live-demo-evidence.v1",
        alias="schema",
        serialization_alias="schema",
    )
    status: Literal["ready_to_use"] = "ready_to_use"
    run_id: UUID
    maximum_cost_usd: Decimal
    booked_cost_usd: Decimal
    reservations: tuple[CapabilityLiveReservation, ...]
    capabilities: tuple[CapabilityLiveCapabilityEvidence, ...]
    restart: CapabilityLiveRestartEvidence

    @field_validator("maximum_cost_usd", mode="before")
    @classmethod
    def require_positive_cost(cls, value: object) -> Decimal:
        return _positive_decimal(value, "maximum_cost_usd")

    @field_validator("booked_cost_usd", mode="before")
    @classmethod
    def require_known_cost(cls, value: object) -> Decimal:
        return _nonnegative_decimal(value, "booked_cost_usd")


class CapabilityLiveBudgetPort(Protocol):
    def reserve(
        self, request: CapabilityLiveReservationRequest
    ) -> CapabilityLiveReservation: ...


class CapabilityLiveReservationAuthority:
    """Bind the runner's three local reservations before any provider effect.

    The durable run plan is the reservation ledger for this suite.  This
    adapter does not mint a provider receipt or change the runner-selected
    identity; it only rejects malformed requests and returns the exact frozen
    envelope that will be persisted before stage one starts.
    """

    def reserve(
        self,
        request: CapabilityLiveReservationRequest,
    ) -> CapabilityLiveReservation:
        return CapabilityLiveReservation.model_validate(request.model_dump())


class CapabilityLivePipelinePort(Protocol):
    """Delegate every domain decision to current authoritative compositions."""

    async def run_stage(
        self, request: CapabilityLiveStageRequest
    ) -> CapabilityLiveStageResult: ...

    async def restart_and_resume(
        self, request: "CapabilityLiveRestartRequest"
    ) -> CapabilityLiveRestartEvidence: ...


class CapabilityLiveRestartRequest(_FrozenModel):
    run_id: UUID
    input: CapabilityLiveInput
    reservation: CapabilityLiveReservation
    capability: CapabilityLiveCapabilityEvidence

    @model_validator(mode="after")
    def require_stable_resume_scope(self) -> "CapabilityLiveRestartRequest":
        if (
            self.reservation.run_id != self.run_id
            or self.reservation.input_id != self.input.input_id
            or self.capability.input_id != self.input.input_id
        ):
            raise ValueError("restart request changed its run, input, or reservation")
        return self


class CapabilityLiveCheckpointPort(Protocol):
    def load_plan(self, run_id: UUID) -> CapabilityLiveRunPlan | None: ...

    def persist_plan(self, plan: CapabilityLiveRunPlan) -> None: ...

    def checkpoints(
        self, run_id: UUID, input_id: str
    ) -> tuple[CapabilityLiveStageCheckpoint, ...]: ...

    def persist_checkpoint(
        self, checkpoint: CapabilityLiveStageCheckpoint
    ) -> ArtifactRef: ...

    def reference_for(self, checkpoint: CapabilityLiveStageCheckpoint) -> ArtifactRef: ...

    def load_final(self, run_id: UUID) -> CapabilityLiveDemoEvidence | None: ...

    def persist_final(self, evidence: CapabilityLiveDemoEvidence) -> ArtifactRef: ...


class FilesystemCapabilityLiveCheckpointStore:
    """Write-once, handle-confined redacted plan and stage checkpoints."""

    def __init__(self, root: Path) -> None:
        self._files = ConfinedFileStore(root)

    def load_plan(self, run_id: UUID) -> CapabilityLiveRunPlan | None:
        return self._read_optional(
            f"{run_id}/plan.json",
            CapabilityLiveRunPlan,
        )

    def persist_plan(self, plan: CapabilityLiveRunPlan) -> None:
        self._files.write_once(
            f"{plan.run_id}/plan.json",
            _canonical_model_bytes(plan),
            conflict="capability live plan already exists with different content",
        )

    def checkpoints(
        self, run_id: UUID, input_id: str
    ) -> tuple[CapabilityLiveStageCheckpoint, ...]:
        _require_identifier(input_id)
        found: list[CapabilityLiveStageCheckpoint] = []
        missing_seen = False
        for index, stage in enumerate(CAPABILITY_LIVE_STAGES):
            relative = self._checkpoint_path(run_id, input_id, index, stage)
            value = self._read_optional(relative, CapabilityLiveStageCheckpoint)
            if value is None:
                missing_seen = True
                continue
            if missing_seen:
                raise CapabilityLiveEvidenceError(
                    "capability stage checkpoints are not a contiguous prefix"
                )
            found.append(value)
        return tuple(found)

    def persist_checkpoint(
        self, checkpoint: CapabilityLiveStageCheckpoint
    ) -> ArtifactRef:
        relative = self._checkpoint_path(
            checkpoint.run_id,
            checkpoint.input_id,
            checkpoint.stage_index,
            checkpoint.stage,
        )
        content = _canonical_model_bytes(checkpoint)
        self._files.write_once(
            relative,
            content,
            conflict="capability live checkpoint already exists with different content",
        )
        return self.reference_for(checkpoint)

    def reference_for(self, checkpoint: CapabilityLiveStageCheckpoint) -> ArtifactRef:
        content = _canonical_model_bytes(checkpoint)
        digest = hashlib.sha256(content).hexdigest()
        return ArtifactRef(
            uri=(
                "artifact://capability-live-checkpoint/"
                f"{checkpoint.run_id}/{checkpoint.input_id}/"
                f"{checkpoint.stage.value}/{digest}"
            ),
            sha256=digest,
            media_type="application/json",
        )

    def load_final(self, run_id: UUID) -> CapabilityLiveDemoEvidence | None:
        return self._read_optional(
            f"{run_id}/final-evidence.json",
            CapabilityLiveDemoEvidence,
        )

    def persist_final(self, evidence: CapabilityLiveDemoEvidence) -> ArtifactRef:
        content = _canonical_model_bytes(evidence)
        self._files.write_once(
            f"{evidence.run_id}/final-evidence.json",
            content,
            conflict="capability live final evidence already exists with different content",
        )
        digest = hashlib.sha256(content).hexdigest()
        return ArtifactRef(
            uri=f"artifact://capability-live-final/{evidence.run_id}/{digest}",
            sha256=digest,
            media_type="application/json",
        )

    def _read_optional(
        self,
        relative: str,
        model_type: type[_ModelT],
    ) -> _ModelT | None:
        try:
            content = self._files.read(relative)
        except ConfinedFileError:
            if not (self._files.root / relative).exists():
                return None
            raise
        try:
            return model_type.model_validate_json(content)
        except (TypeError, ValueError):
            raise CapabilityLiveEvidenceError(
                "capability live checkpoint is malformed"
            ) from None

    @staticmethod
    def _checkpoint_path(
        run_id: UUID,
        input_id: str,
        stage_index: int,
        stage: CapabilityLiveStage,
    ) -> str:
        return f"{run_id}/{input_id}/{stage_index:02d}-{stage.value}.json"


def derive_capability_reservations(
    maximum_cost_usd: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """Split one total ceiling; all decimal remainder belongs to input three."""

    maximum = _positive_decimal(maximum_cost_usd, "maximum_cost_usd")
    share = (maximum / Decimal(3)).quantize(_CENT, rounding=ROUND_DOWN)
    final = maximum - share - share
    if share <= 0 or final <= 0:
        raise ValueError("maximum_cost_usd must fund three positive reservations")
    return share, share, final


class CapabilityLiveRunner:
    """Resume a three-input live suite without replacing domain authorities."""

    def __init__(
        self,
        *,
        pipeline: CapabilityLivePipelinePort,
        budget: CapabilityLiveBudgetPort,
        checkpoints: CapabilityLiveCheckpointPort,
        clock: Callable[[], datetime],
        maximum_runtime_retries: int = 1,
    ) -> None:
        if not callable(clock):
            raise ValueError("capability live clock is required")
        if maximum_runtime_retries < 0 or maximum_runtime_retries > 5:
            raise ValueError("runtime retry bound must be between zero and five")
        self._pipeline = pipeline
        self._budget = budget
        self._checkpoints = checkpoints
        self._clock = clock
        self._maximum_runtime_retries = maximum_runtime_retries

    async def run(
        self,
        *,
        run_id: UUID,
        inputs: tuple[CapabilityLiveInput, ...],
        maximum_cost_usd: Decimal,
    ) -> CapabilityLiveDemoEvidence:
        _validate_inputs(inputs)
        maximum = _positive_decimal(maximum_cost_usd, "maximum_cost_usd")
        plan = self._load_or_create_plan(run_id, inputs, maximum)
        existing_final = self._checkpoints.load_final(run_id)
        if existing_final is not None:
            _require_plan_matches_evidence(plan, existing_final)
            return validate_live_demo_evidence(existing_final)

        capabilities: list[CapabilityLiveCapabilityEvidence] = []
        for input_index, item in enumerate(plan.items):
            capability = await self._run_input(
                plan=plan,
                item=item,
                input_index=input_index,
            )
            capabilities.append(capability)

        first_item = plan.items[0]
        restart = await self._pipeline.restart_and_resume(
            CapabilityLiveRestartRequest(
                run_id=run_id,
                input=first_item.input,
                reservation=first_item.reservation,
                capability=capabilities[0],
            )
        )
        booked = sum(
            (capability.booked_cost_usd for capability in capabilities),
            Decimal("0"),
        )
        evidence = CapabilityLiveDemoEvidence(
            run_id=run_id,
            maximum_cost_usd=plan.maximum_cost_usd,
            booked_cost_usd=booked,
            reservations=tuple(item.reservation for item in plan.items),
            capabilities=tuple(capabilities),
            restart=restart,
        )
        validated = validate_live_demo_evidence(evidence)
        self._checkpoints.persist_final(validated)
        return validated

    def _load_or_create_plan(
        self,
        run_id: UUID,
        inputs: tuple[CapabilityLiveInput, ...],
        maximum: Decimal,
    ) -> CapabilityLiveRunPlan:
        existing = self._checkpoints.load_plan(run_id)
        if existing is not None:
            if (
                existing.maximum_cost_usd != maximum
                or tuple(item.input for item in existing.items) != inputs
            ):
                raise CapabilityLiveEvidenceError(
                    "resume request does not match the durable capability suite"
                )
            return existing

        allocations = derive_capability_reservations(maximum)
        items: list[CapabilityLiveRunPlanItem] = []
        for input_item, allocation in zip(inputs, allocations, strict=True):
            request = CapabilityLiveReservationRequest(
                reservation_id=uuid5(
                    _LIVE_NAMESPACE,
                    f"{run_id}|{input_item.input_id}|budget-reservation",
                ),
                run_id=run_id,
                input_id=input_item.input_id,
                maximum_cost_usd=allocation,
            )
            reservation = self._budget.reserve(request)
            if reservation != CapabilityLiveReservation.model_validate(
                request.model_dump()
            ):
                raise CapabilityLiveEvidenceError(
                    "budget authority changed the requested reservation"
                )
            items.append(
                CapabilityLiveRunPlanItem(
                    input=input_item,
                    correlation_id=uuid5(
                        _LIVE_NAMESPACE,
                        f"{run_id}|{input_item.input_id}|correlation",
                    ),
                    reservation=reservation,
                )
            )
        plan = CapabilityLiveRunPlan(
            run_id=run_id,
            maximum_cost_usd=maximum,
            items=tuple(items),
            created_at=_utc(self._clock()),
        )
        self._checkpoints.persist_plan(plan)
        return plan

    async def _run_input(
        self,
        *,
        plan: CapabilityLiveRunPlan,
        item: CapabilityLiveRunPlanItem,
        input_index: int,
    ) -> CapabilityLiveCapabilityEvidence:
        stored = self._checkpoints.checkpoints(plan.run_id, item.input.input_id)
        suite_ref: ArtifactRef | None = None
        hermes_result_ref: ArtifactRef | None = None
        booked = Decimal("0")
        checkpoint_refs: list[ArtifactRef] = []
        capability: CapabilityLiveCapabilityEvidence | None = None
        for expected_index, checkpoint in enumerate(stored):
            if (
                checkpoint.stage_index != expected_index
                or checkpoint.stage is not CAPABILITY_LIVE_STAGES[expected_index]
                or checkpoint.run_id != plan.run_id
                or checkpoint.input_id != item.input.input_id
                or checkpoint.correlation_id != item.correlation_id
                or checkpoint.reservation != item.reservation
                or checkpoint.booked_cost_usd < booked
            ):
                raise CapabilityLiveEvidenceError(
                    "durable stage checkpoint does not match the live run plan"
                )
            if suite_ref is not None and checkpoint.suite_ref != suite_ref:
                raise CapabilityLiveEvidenceError("checkpoint changed the benchmark suite")
            if (
                hermes_result_ref is not None
                and checkpoint.hermes_result_ref != hermes_result_ref
            ):
                raise CapabilityLiveEvidenceError("checkpoint changed the Hermes result")
            suite_ref = checkpoint.suite_ref
            hermes_result_ref = checkpoint.hermes_result_ref
            booked = checkpoint.booked_cost_usd
            capability = checkpoint.capability_evidence
            checkpoint_refs.append(self._checkpoints.reference_for(checkpoint))

        for stage_index in range(len(stored), len(CAPABILITY_LIVE_STAGES)):
            stage = CAPABILITY_LIVE_STAGES[stage_index]
            retry_number = 0
            while True:
                request = CapabilityLiveStageRequest(
                    run_id=plan.run_id,
                    input=item.input,
                    correlation_id=item.correlation_id,
                    reservation=item.reservation,
                    stage=stage,
                    controlled_recovery_required=input_index == 0,
                    suite_ref=suite_ref,
                    hermes_result_ref=hermes_result_ref,
                    retry_number=retry_number,
                )
                try:
                    result = await self._pipeline.run_stage(request)
                    break
                except CapabilityLiveRuntimeRetry:
                    if retry_number >= self._maximum_runtime_retries:
                        raise
                    retry_number += 1
            if result.stage is not stage:
                raise CapabilityLiveEvidenceError(
                    "pipeline returned evidence for a different stage"
                )
            if suite_ref is not None and result.suite_ref != suite_ref:
                raise CapabilityLiveEvidenceError("runtime retry changed the benchmark suite")
            if (
                hermes_result_ref is not None
                and result.hermes_result_ref != hermes_result_ref
            ):
                raise CapabilityLiveEvidenceError("runtime retry changed the Hermes result")
            if (
                result.booked_cost_usd < booked
                or result.booked_cost_usd > item.reservation.maximum_cost_usd
            ):
                raise CapabilityLiveEvidenceError(
                    "stage cost exceeds or rewinds its reservation"
                )
            suite_ref = result.suite_ref
            hermes_result_ref = result.hermes_result_ref
            booked = result.booked_cost_usd
            capability = result.capability_evidence
            checkpoint = CapabilityLiveStageCheckpoint(
                run_id=plan.run_id,
                input_id=item.input.input_id,
                correlation_id=item.correlation_id,
                reservation=item.reservation,
                stage=stage,
                stage_index=stage_index,
                suite_ref=suite_ref,
                hermes_result_ref=hermes_result_ref,
                evidence_refs=result.evidence_refs,
                booked_cost_usd=booked,
                capability_evidence=capability,
                completed_at=_utc(self._clock()),
            )
            checkpoint_refs.append(self._checkpoints.persist_checkpoint(checkpoint))

        if capability is None:
            raise CapabilityLiveEvidenceError("project stage returned no capability evidence")
        if (
            capability.input_id != item.input.input_id
            or capability.conversation_pattern != item.input.conversation_pattern
            or capability.correlation_id != item.correlation_id
            or capability.suite_ref != suite_ref
            or capability.hermes_result_ref != hermes_result_ref
            or capability.booked_cost_usd != booked
        ):
            raise CapabilityLiveEvidenceError(
                "project evidence does not match the durable stage chain"
            )
        return capability.model_copy(update={"checkpoint_refs": tuple(checkpoint_refs)})


def validate_live_demo_evidence(
    evidence: CapabilityLiveDemoEvidence,
) -> CapabilityLiveDemoEvidence:
    """Fail closed unless every externally authoritative receipt is present."""

    if len(evidence.capabilities) != 3 or len(evidence.reservations) != 3:
        raise CapabilityLiveEvidenceError(
            "live demo evidence requires exactly three capabilities and reservations"
        )
    patterns = tuple(item.conversation_pattern for item in evidence.capabilities)
    if len(set(patterns)) != 3:
        raise CapabilityLiveEvidenceError(
            "live demo requires three distinct conversation patterns"
        )
    input_ids = tuple(item.input_id for item in evidence.capabilities)
    if len(set(input_ids)) != 3:
        raise CapabilityLiveEvidenceError("live demo requires three distinct inputs")
    if evidence.capabilities[0].controlled_recovery_id is None:
        raise CapabilityLiveEvidenceError(
            "first capability has no controlled recovery evidence"
        )
    for capability in evidence.capabilities:
        if (
            len(capability.normal_trace_ids) < 3
            or len(set(capability.normal_trace_ids)) != len(capability.normal_trace_ids)
        ):
            raise CapabilityLiveEvidenceError(
                "each capability requires at least three distinct normal traces"
            )
        if capability.release_authority_id is None:
            raise CapabilityLiveEvidenceError("capability has no release authority")
        if capability.execution_authority_id is None:
            raise CapabilityLiveEvidenceError("capability has no execution authority")
        if capability.execution_command_id is None or capability.execution_result_id is None:
            raise CapabilityLiveEvidenceError("capability execution evidence is incomplete")
        if capability.projection_event_id is None:
            raise CapabilityLiveEvidenceError("capability has no Minibook projection")
        if capability.projection_readback_id is None:
            raise CapabilityLiveEvidenceError("capability has no projection read-back")
        if capability.suite_ref is None or capability.hermes_result_ref is None:
            raise CapabilityLiveEvidenceError(
                "capability lost its benchmark suite or Hermes result binding"
            )

    first = evidence.capabilities[0]
    first_reservation = next(
        (item for item in evidence.reservations if item.input_id == first.input_id),
        None,
    )
    if first_reservation is None:
        raise CapabilityLiveEvidenceError("restart reservation is unavailable")
    if (
        evidence.restart.run_id != evidence.run_id
        or evidence.restart.job_id != first.job_id
        or evidence.restart.reservation_id != first_reservation.reservation_id
        or evidence.restart.correlation_id != first.correlation_id
        or evidence.restart.capability_id != first.capability_id
        or evidence.restart.capability_version != first.capability_version
        or evidence.restart.original_execution_command_id != first.execution_command_id
        or evidence.restart.original_execution_result_id != first.execution_result_id
        or evidence.restart.projection_event_id != first.projection_event_id
        or evidence.restart.projection_readback_id != first.projection_readback_id
    ):
        raise CapabilityLiveEvidenceError("restart evidence changed stable authority binding")
    if (
        evidence.restart.resumed_execution_command_id
        == evidence.restart.original_execution_command_id
    ):
        raise CapabilityLiveEvidenceError("reused restart execution command")
    if (
        evidence.restart.resumed_execution_result_id
        == evidence.restart.original_execution_result_id
    ):
        raise CapabilityLiveEvidenceError("reused restart execution result")

    reservation_total = sum(
        (item.maximum_cost_usd for item in evidence.reservations), Decimal("0")
    )
    capability_total = sum(
        (item.booked_cost_usd for item in evidence.capabilities), Decimal("0")
    )
    if (
        evidence.booked_cost_usd > evidence.maximum_cost_usd
        or capability_total > evidence.maximum_cost_usd
        or reservation_total > evidence.maximum_cost_usd
    ):
        raise CapabilityLiveEvidenceError("booked provider cost exceeds the cost ceiling")
    if reservation_total != evidence.maximum_cost_usd:
        raise CapabilityLiveEvidenceError("three reservations do not equal the cost ceiling")
    if evidence.booked_cost_usd != capability_total:
        raise CapabilityLiveEvidenceError("booked cost does not match capability receipts")
    reservations = {item.input_id: item for item in evidence.reservations}
    if set(reservations) != set(input_ids):
        raise CapabilityLiveEvidenceError("reservation scope does not match capabilities")
    for capability in evidence.capabilities:
        reservation = reservations[capability.input_id]
        if (
            reservation.run_id != evidence.run_id
            or capability.booked_cost_usd > reservation.maximum_cost_usd
        ):
            raise CapabilityLiveEvidenceError(
                "capability cost is not bound to its reservation"
            )
    return evidence


def _validate_inputs(inputs: tuple[CapabilityLiveInput, ...]) -> None:
    if len(inputs) != 3:
        raise ValueError("capability live demo requires exactly three inputs")
    if len({item.input_id for item in inputs}) != 3:
        raise ValueError("capability live demo requires distinct input IDs")
    patterns = tuple(item.conversation_pattern for item in inputs)
    if patterns != ("sequential", "selector_group_chat", "swarm"):
        raise ValueError(
            "capability live demo requires sequential, selector group-chat, then swarm"
        )


def _require_plan_matches_evidence(
    plan: CapabilityLiveRunPlan,
    evidence: CapabilityLiveDemoEvidence,
) -> None:
    if (
        evidence.run_id != plan.run_id
        or evidence.maximum_cost_usd != plan.maximum_cost_usd
        or evidence.reservations != tuple(item.reservation for item in plan.items)
        or tuple(item.input_id for item in evidence.capabilities)
        != tuple(item.input.input_id for item in plan.items)
    ):
        raise CapabilityLiveEvidenceError(
            "stored final evidence does not match the durable live plan"
        )


def _canonical_model_bytes(value: BaseModel) -> bytes:
    return (
        json.dumps(
            value.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _positive_decimal(value: object, name: str) -> Decimal:
    parsed = _nonnegative_decimal(value, name)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _nonnegative_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite decimal")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a finite decimal") from None
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{name} must be a non-negative finite decimal")
    return parsed


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("capability live timestamps must be UTC")
    return value.astimezone(timezone.utc)


def _require_identifier(value: str) -> None:
    if re.fullmatch(IDENTIFIER_PATTERN, value) is None:
        raise ValueError("capability input ID is invalid")


__all__ = [
    "CAPABILITY_LIVE_STAGES",
    "CapabilityLiveBudgetPort",
    "CapabilityLiveCapabilityEvidence",
    "CapabilityLiveCheckpointPort",
    "CapabilityLiveDemoEvidence",
    "CapabilityLiveEvidenceError",
    "CapabilityLiveInput",
    "CapabilityLivePipelinePort",
    "CapabilityLiveReservation",
    "CapabilityLiveReservationAuthority",
    "CapabilityLiveReservationRequest",
    "CapabilityLiveRestartEvidence",
    "CapabilityLiveRestartRequest",
    "CapabilityLiveRunPlan",
    "CapabilityLiveRunner",
    "CapabilityLiveRuntimeRetry",
    "CapabilityLiveStage",
    "CapabilityLiveStageCheckpoint",
    "CapabilityLiveStageRequest",
    "CapabilityLiveStageResult",
    "FilesystemCapabilityLiveCheckpointStore",
    "derive_capability_reservations",
    "validate_live_demo_evidence",
]
