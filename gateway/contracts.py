"""Pure event contracts and projection for the gateway batch lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Sequence, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


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
    model_config = ConfigDict(extra="forbid")

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
        claim_expires_at=claim_expires_at,
        claim_iteration=claim_iteration,
        codex_session_recorded=codex_session_recorded,
        validation_run_recorded=validation_run_recorded,
        recovery_recorded=recovery_recorded,
        recovered_iteration=recovered_iteration,
        passing_review_recorded=passing_review_recorded,
        failed_review_count=failed_review_count,
    )
