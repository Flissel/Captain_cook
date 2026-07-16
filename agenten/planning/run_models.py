"""Durable Captain planning-run checkpoint contracts."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agenten.validation.contracts import HoldoutSuite, WorkBatch


class CaptainRunStatus(str, Enum):
    PLANNING = "planning"
    RELEASING = "releasing"
    PARTIALLY_RELEASED = "partially_released"
    RELEASED = "released"
    FAILED = "failed"


class CaptainRunConflictError(RuntimeError):
    """A run id was reused for a different immutable project input."""


class PartialReleaseError(RuntimeError):
    """A release attempt stopped and the durable run can be resumed."""

    def __init__(
        self,
        run_id: str,
        released_batch_ids: list[str],
        failed_batch_id: str,
    ) -> None:
        super().__init__(f"run {run_id!r} stopped while releasing batch {failed_batch_id!r}")
        self.run_id = run_id
        self.released_batch_ids = tuple(released_batch_ids)
        self.failed_batch_id = failed_batch_id


class CaptainRunState(BaseModel):
    """Complete immutable plan plus the monotonic release checkpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    project_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: CaptainRunStatus
    batches: tuple[WorkBatch, ...] = Field(min_length=1)
    holdouts: tuple[HoldoutSuite, ...] = Field(min_length=1)
    released_batch_ids: tuple[str, ...] = Field(default_factory=tuple)
    failed_batch_id: str | None = None
    error_kind: str | None = None

    @model_validator(mode="after")
    def validate_checkpoint(self) -> "CaptainRunState":
        batch_ids = [batch.batch_id for batch in self.batches]
        holdout_ids = [holdout.batch_id for holdout in self.holdouts]
        if batch_ids != holdout_ids:
            raise ValueError("batches and holdouts must have identical ordering")
        if len(batch_ids) != len(set(batch_ids)):
            raise ValueError("batch ids must be unique")
        if len(self.released_batch_ids) != len(set(self.released_batch_ids)):
            raise ValueError("released_batch_ids must not contain duplicates")
        if not set(self.released_batch_ids).issubset(batch_ids):
            raise ValueError("released_batch_ids must refer to planned batches")
        if list(self.released_batch_ids) != batch_ids[: len(self.released_batch_ids)]:
            raise ValueError("released_batch_ids must be a dependency-ordered prefix")
        if self.failed_batch_id is not None and self.failed_batch_id not in batch_ids:
            raise ValueError("failed_batch_id must refer to a planned batch")
        if (
            self.status is CaptainRunStatus.RELEASED
            and list(self.released_batch_ids) != batch_ids
        ):
            raise ValueError("released status requires every batch checkpoint")
        if self.status is not CaptainRunStatus.RELEASED and list(self.released_batch_ids) == batch_ids:
            raise ValueError("every batch checkpoint requires released status")
        if self.status is CaptainRunStatus.PARTIALLY_RELEASED:
            next_index = len(self.released_batch_ids)
            if (
                self.failed_batch_id is None
                or self.error_kind is None
                or self.failed_batch_id != batch_ids[next_index]
            ):
                raise ValueError("partial release requires the next failed batch and error kind")
        return self
