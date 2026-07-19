"""Pure event contracts and projection for the gateway batch lifecycle."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Sequence, TypeAlias
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    CapabilityGrant,
    CapabilityGrantRevocation,
)


DeliveryEventType: TypeAlias = Literal[
    "codex_task",
    "codex_session",
    "codex_session_started",
    "codex_session_event",
    "codex_session_warning",
    "codex_session_finished",
    "artifact_built",
    "deploy",
    "validation_run",
    "repair_request",
    "batch_done",
    "e2e_run",
    "evaluation",
    "release_decision",
    "registry_mirror",
]
ReleaseStatus: TypeAlias = Literal["blocked", "ready"]


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeWriteReceipt(_FrozenContract):
    operation_id: UUID
    replayed: bool


class RuntimeOperationProjection(_FrozenContract):
    operation_id: UUID
    command: AgentRuntimeCommand
    grant: CapabilityGrant | None = None
    revocation: CapabilityGrantRevocation | None = None
    result: AgentRuntimeResult | None = None


class TraceContext(_FrozenContract):
    project_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    batch_id: str | None = Field(default=None, min_length=1)
    worker_id: str | None = Field(default=None, min_length=1)
    claim_id: str | None = Field(default=None, min_length=1)
    fencing_token: int | None = Field(default=None, ge=0, strict=True)
    artifact_id: str | None = Field(default=None, min_length=1)
    session_id: str | None = Field(default=None, min_length=1)
    case_id: str | None = Field(default=None, min_length=1)


class CodexTaskPayload(_FrozenContract):
    event_type: Literal["codex_task"]
    task_id: str = Field(min_length=1)
    target: str = Field(min_length=1)
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_ref: str = Field(min_length=1)
    permissions: tuple[str, ...] = Field(min_length=1)
    budget: int = Field(gt=0, strict=True)

    @field_validator("workspace_ref")
    @classmethod
    def require_opaque_workspace_reference(cls, value: str) -> str:
        if not value.startswith("artifact://"):
            raise ValueError("workspace_ref must be an opaque artifact reference")
        return value


class CodexSessionPayload(_FrozenContract):
    event_type: Literal["codex_session"]
    session_id: str = Field(min_length=1)
    process_ref: str = Field(min_length=1)
    started_at: datetime
    ended_at: datetime
    exit_class: Literal["completed", "failed", "cancelled", "timed_out"]

    @field_validator("process_ref")
    @classmethod
    def require_opaque_process_reference(cls, value: str) -> str:
        if not value.startswith("artifact://"):
            raise ValueError("process_ref must be an opaque artifact reference")
        return value

    @model_validator(mode="after")
    def require_ordered_timestamps(self) -> CodexSessionPayload:
        if _as_utc(self.ended_at) < _as_utc(self.started_at):
            raise ValueError("ended_at cannot precede started_at")
        return self


CodexSessionOutcome: TypeAlias = Literal[
    "succeeded",
    "behavioral_failure",
    "infrastructure_failure",
    "policy_failure",
    "cancelled",
    "lost_process",
]
CodexCancellationReason: TypeAlias = Literal[
    "operator",
    "timeout",
    "shutdown",
    "claim_lost",
]


class CodexSessionStartedPayload(_FrozenContract):
    event_type: Literal["codex_session_started"]
    session_id: str = Field(min_length=1)
    process_ref: str = Field(pattern=r"^artifact://[^\\]+$")
    started_at: datetime
    iteration: int = Field(ge=1, strict=True)
    command_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CodexSessionEventPayload(_FrozenContract):
    event_type: Literal["codex_session_event"]
    session_id: str = Field(min_length=1)
    external_session_id: str | None = Field(default=None, min_length=1)
    source_sequence: int = Field(ge=0, strict=True)
    lifecycle: Literal[
        "started",
        "turn_started",
        "turn_completed",
        "item_started",
        "item_updated",
        "item_completed",
        "failed",
    ]
    item_id: str | None = Field(default=None, min_length=1)
    item_type: str | None = Field(default=None, min_length=1)
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def require_lifecycle_owned_metadata(self) -> CodexSessionEventPayload:
        item_fields = (self.item_id, self.item_type)
        token_fields = (
            self.input_tokens,
            self.cached_input_tokens,
            self.output_tokens,
        )
        if self.lifecycle in {"item_started", "item_updated", "item_completed"}:
            if any(value is None for value in item_fields) or any(
                value is not None for value in token_fields
            ):
                raise ValueError("item_completed requires only item metadata")
        elif self.lifecycle == "turn_completed":
            usage_is_partial = any(value is not None for value in token_fields) and any(
                value is None for value in token_fields
            )
            if any(value is not None for value in item_fields) or usage_is_partial:
                raise ValueError(
                    "turn_completed requires no usage or complete token metadata"
                )
        elif any(value is not None for value in (*item_fields, *token_fields)):
            raise ValueError(f"{self.lifecycle} forbids item and token metadata")
        return self


class CodexSessionWarningPayload(_FrozenContract):
    event_type: Literal["codex_session_warning"]
    session_id: str = Field(min_length=1)
    source_sequence: int = Field(ge=0, strict=True)
    warning_type: Literal["malformed_json", "unknown_event", "invalid_event"]
    line_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CodexSessionFinishedPayload(_FrozenContract):
    event_type: Literal["codex_session_finished"]
    session_id: str = Field(min_length=1)
    process_ref: str = Field(pattern=r"^artifact://[^\\]+$")
    started_at: datetime
    ended_at: datetime
    outcome: CodexSessionOutcome
    exit_code: int | None = None
    cancellation_reason: CodexCancellationReason | None = None
    behavioral_repair_increment: Literal[0, 1] = 0

    @model_validator(mode="after")
    def require_truthful_terminal_outcome(self) -> CodexSessionFinishedPayload:
        if _as_utc(self.ended_at) < _as_utc(self.started_at):
            raise ValueError("ended_at cannot precede started_at")
        expected_increment = 1 if self.outcome == "behavioral_failure" else 0
        if self.behavioral_repair_increment != expected_increment:
            raise ValueError("only behavioral_failure increments repair")
        if self.outcome == "cancelled":
            if self.cancellation_reason is None:
                raise ValueError("cancelled outcome requires cancellation_reason")
        elif self.cancellation_reason is not None:
            raise ValueError("cancellation_reason requires cancelled outcome")
        return self


class ArtifactBuiltPayload(_FrozenContract):
    event_type: Literal["artifact_built"]
    artifact_id: str = Field(min_length=1)
    artifact_version: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_type: str = Field(min_length=1)
    sealed_ref: str = Field(min_length=1)

    @field_validator("sealed_ref")
    @classmethod
    def require_opaque_sealed_reference(cls, value: str) -> str:
        if not value.startswith("artifact://"):
            raise ValueError("sealed_ref must be an opaque artifact reference")
        return value


class DeployPayload(_FrozenContract):
    event_type: Literal["deploy"]
    deployment_id: str = Field(min_length=1)
    target: str = Field(min_length=1)
    artifact_version: str = Field(min_length=1)
    external_deployment_ref: str = Field(min_length=1)
    result: Literal["succeeded", "failed"]

    @field_validator("external_deployment_ref")
    @classmethod
    def require_opaque_deployment_reference(cls, value: str) -> str:
        if not value.startswith("artifact://"):
            raise ValueError("external_deployment_ref must be an opaque artifact reference")
        return value


class AssertionResult(_FrozenContract):
    assertion_id: str = Field(min_length=1)
    outcome: Literal["passed", "failed"]


class ValidationRunPayload(_FrozenContract):
    event_type: Literal["validation_run"]
    validation_id: str = Field(min_length=1)
    layer: str = Field(min_length=1)
    case_ids: tuple[str, ...] = Field(min_length=1)
    assertion_results: tuple[AssertionResult, ...] = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    artifact_version: str = Field(min_length=1)
    passed: bool

    @field_validator("assertion_results", mode="before")
    @classmethod
    def normalize_assertion_result_mapping(cls, value: object) -> object:
        if isinstance(value, Mapping):
            return tuple(
                {"assertion_id": assertion_id, "outcome": outcome}
                for assertion_id, outcome in value.items()
            )
        return value

    @field_validator("case_ids")
    @classmethod
    def require_distinct_case_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not case_id for case_id in value) or len(set(value)) != len(value):
            raise ValueError("case_ids must be non-empty and distinct")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def require_opaque_evidence_references(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not reference.startswith("artifact://") for reference in value):
            raise ValueError("evidence_refs must be opaque artifact references")
        return value

    @model_validator(mode="after")
    def require_passed_to_match_assertion_results(self) -> ValidationRunPayload:
        assertion_results_passed = all(
            result.outcome == "passed" for result in self.assertion_results
        )
        if self.passed != assertion_results_passed:
            raise ValueError("passed must equal whether all assertion_results passed")
        return self


class RepairRequestPayload(_FrozenContract):
    event_type: Literal["repair_request"]
    repair_id: str = Field(min_length=1)
    iteration: int = Field(ge=1, strict=True)
    failure_class: str = Field(min_length=1)
    report_ref: str = Field(min_length=1)

    @field_validator("report_ref")
    @classmethod
    def require_opaque_report_reference(cls, value: str) -> str:
        if not value.startswith("artifact://"):
            raise ValueError("report_ref must be an opaque artifact reference")
        return value


class DeliveryBatchDonePayload(_FrozenContract):
    event_type: Literal["batch_done"]
    outcome: Literal["succeeded", "failed", "blocked", "escalated"]


class E2ERunPayload(_FrozenContract):
    event_type: Literal["e2e_run"]
    e2e_run_id: str = Field(min_length=1)
    run_index: int = Field(ge=1, strict=True)
    clean: bool
    trace_complete: bool
    evidence_refs: tuple[str, ...] = Field(min_length=1)

    @field_validator("evidence_refs")
    @classmethod
    def require_opaque_e2e_evidence_references(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not reference.startswith("artifact://") for reference in value):
            raise ValueError("evidence_refs must be opaque artifact references")
        return value


class EvaluationPayload(_FrozenContract):
    event_type: Literal["evaluation"]
    evaluation_id: str = Field(min_length=1)
    hard_passed: bool
    semantic_score: float = Field(ge=0, le=1)
    safety_passed: bool


class ReleaseDecisionPayload(_FrozenContract):
    event_type: Literal["release_decision"]
    decision: Literal["accepted", "rejected"]
    policy_version: str = Field(min_length=1)
    reasons: tuple[str, ...] = Field(min_length=1)


class RegistryMirrorPayload(_FrozenContract):
    event_type: Literal["registry_mirror"]
    capability_id: str = Field(min_length=1)
    capability_version: str = Field(min_length=1)
    outcome: Literal["mirrored", "failed"]


DeliveryEventPayload: TypeAlias = Annotated[
    CodexTaskPayload
    | CodexSessionPayload
    | CodexSessionStartedPayload
    | CodexSessionEventPayload
    | CodexSessionWarningPayload
    | CodexSessionFinishedPayload
    | ArtifactBuiltPayload
    | DeployPayload
    | ValidationRunPayload
    | RepairRequestPayload
    | DeliveryBatchDonePayload
    | E2ERunPayload
    | EvaluationPayload
    | ReleaseDecisionPayload
    | RegistryMirrorPayload,
    Field(discriminator="event_type"),
]


class DeliveryEventEnvelope(_FrozenContract):
    event_id: UUID
    event_type: DeliveryEventType
    occurred_at: datetime
    actor: str = Field(min_length=1)
    trace: TraceContext
    payload: DeliveryEventPayload

    @model_validator(mode="after")
    def require_matching_event_type_and_trace_context(self) -> DeliveryEventEnvelope:
        if self.event_type != self.payload.event_type:
            raise ValueError("payload event_type must match envelope event_type")

        required_trace_fields: dict[DeliveryEventType, tuple[str, ...]] = {
            "codex_task": ("batch_id",),
            "codex_session": ("batch_id", "session_id"),
            "codex_session_started": ("batch_id", "worker_id", "claim_id", "fencing_token", "session_id"),
            "codex_session_event": ("batch_id", "worker_id", "claim_id", "fencing_token", "session_id"),
            "codex_session_warning": ("batch_id", "worker_id", "claim_id", "fencing_token", "session_id"),
            "codex_session_finished": ("batch_id", "worker_id", "claim_id", "fencing_token", "session_id"),
            "artifact_built": ("batch_id", "artifact_id"),
            "deploy": ("batch_id", "artifact_id"),
            "validation_run": ("batch_id", "artifact_id", "case_id"),
            "repair_request": ("batch_id",),
            "batch_done": ("batch_id",),
            "e2e_run": ("batch_id",),
            "evaluation": ("batch_id", "case_id"),
            "release_decision": (),
            "registry_mirror": ("artifact_id",),
        }
        missing = [
            field_name
            for field_name in required_trace_fields[self.event_type]
            if getattr(self.trace, field_name) is None
        ]
        if missing:
            raise ValueError(f"trace requires {', '.join(missing)} for {self.event_type}")
        if isinstance(self.payload, CodexSessionPayload) and self.trace.session_id != self.payload.session_id:
            raise ValueError("trace session_id must match codex_session payload")
        if isinstance(
            self.payload,
            (
                CodexSessionStartedPayload,
                CodexSessionEventPayload,
                CodexSessionWarningPayload,
                CodexSessionFinishedPayload,
            ),
        ) and self.trace.session_id != self.payload.session_id:
            raise ValueError("trace session_id must match Codex session payload")
        if isinstance(self.payload, ArtifactBuiltPayload) and self.trace.artifact_id != self.payload.artifact_id:
            raise ValueError("trace artifact_id must match artifact_built payload")
        if isinstance(self.payload, ValidationRunPayload) and self.trace.case_id not in self.payload.case_ids:
            raise ValueError("trace case_id must appear in validation_run case_ids")
        return self


class ReleaseProjection(_FrozenContract):
    status: ReleaseStatus
    clean_e2e_run_ids: tuple[str, ...]
    missing_clean_e2e_runs: int = Field(ge=0, le=3)


def project_release(events: Sequence[DeliveryEventEnvelope]) -> ReleaseProjection:
    """Derive the release gate from unique, complete clean E2E evidence."""

    clean_runs: dict[str, E2ERunPayload] = {}
    ordered_events = sorted(
        events,
        key=lambda event: (_as_utc(event.occurred_at), event.event_id.int),
    )
    for event in ordered_events:
        if not isinstance(event.payload, E2ERunPayload):
            continue
        payload = event.payload
        if payload.clean and payload.trace_complete:
            clean_runs.setdefault(payload.e2e_run_id, payload)

    clean_e2e_run_ids = tuple(clean_runs)
    missing_clean_e2e_runs = max(0, 3 - len(clean_e2e_run_ids))
    return ReleaseProjection(
        status="ready" if missing_clean_e2e_runs == 0 else "blocked",
        clean_e2e_run_ids=clean_e2e_run_ids,
        missing_clean_e2e_runs=missing_clean_e2e_runs,
    )


BatchStatus: TypeAlias = Literal[
    "pending_review",
    "pending",
    "claimed",
    "succeeded",
    "failed",
    "rejected",
    "cancelled",
    "failed_after_max_iterations",
    "aborted_infra",
]
TerminalOutcome: TypeAlias = Literal[
    "succeeded",
    "failed",
    "rejected",
    "cancelled",
    "failed_after_max_iterations",
    "aborted_infra",
]


class BatchProjection(BaseModel):
    batch_id: str
    parent_index: int
    status: BatchStatus
    claim_token_sha256: str | None = None
    claim_id: str | None = None
    fencing_token: int | None = None
    claim_expires_at: datetime | None = None
    claim_iteration: int = 0
    codex_session_recorded: bool = False
    validation_run_recorded: bool = False
    recovery_recorded: bool = False
    recovered_iteration: int | None = None
    passing_review_recorded: bool = False
    failed_review_count: int = 0


class ClaimEvent(BaseModel):
    batch_id: str
    claim_id: str = Field(min_length=1)
    fencing_token: int = Field(ge=1, strict=True)
    claim_token_sha256: str
    claim_expires_at: datetime


class HeartbeatEvent(BaseModel):
    batch_id: str
    claim_expires_at: datetime


class EvidenceEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    batch_id: str
    iteration: int = Field(ge=1, strict=True)


class ReasoningSliceEvent(EvidenceEvent):
    """Opaque, integrity-bound reasoning summary reference; never raw reasoning."""

    model_config = ConfigDict(extra="forbid")

    slice_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    summary_ref: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("summary_ref")
    @classmethod
    def require_opaque_reference(cls, value: str) -> str:
        lowered = value.lower()
        forbidden = ("chain-of-thought", "chain_of_thought", "workspace", "\\", "file://")
        if not value.startswith("artifact://") or any(item in lowered for item in forbidden):
            raise ValueError("summary_ref must be an opaque artifact reference")
        return value


class CodexProcessEvent(EvidenceEvent):
    model_config = ConfigDict(extra="forbid")

    process_id: str = Field(min_length=1, max_length=128)
    state: Literal["started", "heartbeat", "exited", "cancelled"]
    command_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class BatchDoneEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    batch_id: str
    outcome: TerminalOutcome


class RecoveryDecisionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(min_length=1, max_length=32)
    iteration: int = Field(ge=1, strict=True)
    reason: Literal["claim_expired"]
    decision: Literal["requeue", "aborted_infra"]


class ReviewDecisionEvent(EvidenceEvent):
    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    decision: Literal["passed", "failed"]
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=64)

    @field_validator("evidence_refs")
    @classmethod
    def require_opaque_unique_references(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("evidence_refs must be unique")
        forbidden = ("\\", "file://", "workspace", "chain-of-thought", "bearer ")
        if any(
            not reference.startswith("artifact://")
            or any(item in reference.lower() for item in forbidden)
            for reference in value
        ):
            raise ValueError("evidence_refs must contain opaque artifact references")
        return value


_LIFECYCLE_BLOCK_TYPES = frozenset(
    {
        "batch_approved",
        "batch_claimed",
        "batch_heartbeat",
        "codex_session",
        "codex_process",
        "reasoning_slice",
        "recovery_decision",
        "review_decision",
        "validation_run",
        "batch_done",
    }
)


def _ordered_blocks(blocks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = list(blocks)
    previous_index: int | None = None
    for block in ordered:
        if not isinstance(block, dict):
            raise ValueError("each block must be a dictionary")
        index = block.get("index")
        if type(index) is not int:  # bool is not a valid ledger index
            raise ValueError("each block requires an integer index")
        if previous_index is not None and index <= previous_index:
            raise ValueError("block indexes must be strictly increasing")
        previous_index = index
    return ordered


def _block_data(block: dict[str, Any], *, context: str) -> dict[str, Any]:
    data = block.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"{context} data must be a dictionary")
    return data


def _event_data(block: dict[str, Any], model: type[BaseModel]) -> BaseModel:
    block_type = str(block.get("block_type", "lifecycle event"))
    data = _block_data(block, context=block_type)
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"invalid {block_type} data") from exc


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _batch_parent(
    blocks: Sequence[dict[str, Any]],
    batch_id: str,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for block in blocks:
        if block.get("block_type") != "work_batch":
            continue
        data = block.get("data")
        if isinstance(data, dict) and data.get("batch_id") == batch_id:
            matches.append(block)

    if not matches:
        raise ValueError(f"missing work_batch for batch_id {batch_id!r}")
    if len(matches) > 1:
        raise ValueError(f"duplicate work_batch for batch_id {batch_id!r}")

    parent = matches[0]
    if parent.get("parent_index") is not None:
        raise ValueError("work_batch must be a root work_batch without a parent")
    if parent.get("status") not in {"pending", "pending_review"}:
        raise ValueError("work_batch must start in pending or pending_review")
    return parent


def _batch_children(
    blocks: Sequence[dict[str, Any]],
    *,
    batch_id: str,
    parent: dict[str, Any],
) -> list[dict[str, Any]]:
    parent_index = parent["index"]
    children: list[dict[str, Any]] = []
    for block in blocks:
        if block is parent:
            continue

        data = block.get("data")
        data_batch_id = data.get("batch_id") if isinstance(data, dict) else None
        child_parent_index = block.get("parent_index")
        points_to_parent = type(child_parent_index) is int and child_parent_index == parent_index
        belongs_to_batch = data_batch_id == batch_id
        if not points_to_parent and not belongs_to_batch:
            continue
        if not points_to_parent or not belongs_to_batch or block["index"] <= parent_index:
            raise ValueError("child relationship must match the work_batch parent and batch_id")
        children.append(block)
    return children


def project_batch(
    blocks: Sequence[dict[str, Any]],
    batch_id: str,
    *,
    now: datetime | None = None,
) -> BatchProjection:
    """Derive the current state from a work_batch and ordered child events."""

    ordered = _ordered_blocks(blocks)
    parent = _batch_parent(ordered, batch_id)
    children = _batch_children(ordered, batch_id=batch_id, parent=parent)
    current_time = _as_utc(now if now is not None else datetime.now(timezone.utc))

    status: BatchStatus = parent["status"]
    claim_token_sha256: str | None = None
    claim_id: str | None = None
    fencing_token: int | None = None
    claim_expires_at: datetime | None = None
    claim_iteration = 0
    codex_session_recorded = False
    validation_run_recorded = False
    terminal = False
    recovered_iterations: set[int] = set()
    recovery_recorded = False
    recovered_iteration: int | None = None
    passing_review_recorded = False
    failed_review_count = 0
    review_ids: set[str] = set()

    for block in children:
        block_type = block.get("block_type")
        if block_type not in _LIFECYCLE_BLOCK_TYPES:
            continue
        if terminal:
            raise ValueError("lifecycle event cannot appear after terminal batch_done")

        if block_type == "batch_approved":
            if status != "pending_review":
                raise ValueError("batch approval is invalid or duplicated")
            status = "pending"
            continue

        if block_type == "batch_claimed":
            if status == "pending_review":
                raise ValueError("batch approval is required before a claim")
            event = _event_data(block, ClaimEvent)
            assert isinstance(event, ClaimEvent)
            claim_iteration += 1
            claim_token_sha256 = event.claim_token_sha256
            claim_id = event.claim_id
            fencing_token = event.fencing_token
            claim_expires_at = _as_utc(event.claim_expires_at)
            codex_session_recorded = False
            validation_run_recorded = False
            passing_review_recorded = False
            status = "claimed"
            continue

        if block_type == "batch_heartbeat":
            if claim_iteration == 0:
                raise ValueError("heartbeat before claim is invalid")
            event = _event_data(block, HeartbeatEvent)
            assert isinstance(event, HeartbeatEvent)
            claim_expires_at = _as_utc(event.claim_expires_at)
            continue

        if block_type in {"codex_session", "codex_process", "reasoning_slice", "validation_run"}:
            event_model: type[BaseModel] = (
                CodexProcessEvent
                if block_type == "codex_process"
                else ReasoningSliceEvent
                if block_type == "reasoning_slice"
                else EvidenceEvent
            )
            event = _event_data(block, event_model)
            assert isinstance(event, EvidenceEvent)
            if claim_iteration == 0 or event.iteration != claim_iteration:
                raise ValueError("evidence must match the current claim iteration")
            if block_type == "codex_session":
                codex_session_recorded = True
            elif block_type == "validation_run":
                validation_run_recorded = True
            continue

        if block_type == "review_decision":
            event = _event_data(block, ReviewDecisionEvent)
            assert isinstance(event, ReviewDecisionEvent)
            if claim_iteration == 0 or event.iteration != claim_iteration:
                raise ValueError("review must match the current claim iteration")
            if not validation_run_recorded:
                raise ValueError("review requires current-iteration validation_run evidence")
            if event.review_id in review_ids:
                raise ValueError("review_id must be immutable and unique")
            review_ids.add(event.review_id)
            if event.decision == "passed":
                passing_review_recorded = True
            else:
                failed_review_count += 1
            continue

        if block_type == "recovery_decision":
            event = _event_data(block, RecoveryDecisionEvent)
            assert isinstance(event, RecoveryDecisionEvent)
            if (
                claim_iteration == 0
                or event.iteration != claim_iteration
                or claim_expires_at is None
                or claim_expires_at > current_time
                or event.iteration in recovered_iterations
            ):
                raise ValueError("recovery decision requires one expired current claim")
            recovered_iterations.add(event.iteration)
            recovery_recorded = True
            recovered_iteration = event.iteration
            claim_token_sha256 = None
            claim_id = None
            fencing_token = None
            claim_expires_at = None
            if event.decision == "aborted_infra":
                status = "aborted_infra"
                terminal = True
            else:
                status = "pending"
            continue

        if block_type == "batch_done":
            if claim_iteration == 0:
                raise ValueError("terminal before claim is invalid")
            event = _event_data(block, BatchDoneEvent)
            assert isinstance(event, BatchDoneEvent)
            if event.outcome == "succeeded":
                if not validation_run_recorded:
                    raise ValueError("succeeded batch_done requires current-iteration validation_run evidence")
                if not passing_review_recorded:
                    raise ValueError("succeeded batch_done requires a current-iteration passing review")
            if (
                event.outcome == "failed_after_max_iterations"
                and failed_review_count < 5
            ):
                raise ValueError("failed_after_max_iterations requires five failed reviews")
            status = event.outcome
            terminal = True

    if not terminal and claim_iteration and not recovery_recorded:
        assert claim_expires_at is not None
        status = "claimed" if claim_expires_at > current_time else "pending"

    return BatchProjection(
        batch_id=batch_id,
        parent_index=parent["index"],
        status=status,
        claim_token_sha256=claim_token_sha256,
        claim_id=claim_id,
        fencing_token=fencing_token,
        claim_expires_at=claim_expires_at,
        claim_iteration=claim_iteration,
        codex_session_recorded=codex_session_recorded,
        validation_run_recorded=validation_run_recorded,
        recovery_recorded=recovery_recorded,
        recovered_iteration=recovered_iteration,
        passing_review_recorded=passing_review_recorded,
        failed_review_count=failed_review_count,
    )
