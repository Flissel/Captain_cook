"""Validate and order model-proposed batch alignments.

LLMs may propose grouping and dependency edges, but correctness is enforced by
this deterministic module before any batch is enriched or released.
"""

from collections import Counter
from typing import Dict, List

from pydantic import BaseModel, ConfigDict, Field


class AlignmentError(ValueError):
    """The proposed batch plan cannot safely be executed."""


class BatchDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")
    title: str = Field(min_length=1)
    subtask_ids: List[str] = Field(min_length=1)
    depends_on: List[str] = Field(default_factory=list)
    target: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,31}$")


class AlignmentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    batches: List[BatchDraft] = Field(min_length=1)


def validate_alignment(
    plan: AlignmentPlan,
    expected_subtask_ids: List[str],
) -> List[BatchDraft]:
    """Return batches in dependency order or raise a precise plan error."""

    batch_ids = [batch.batch_id for batch in plan.batches]
    duplicate_batches = sorted(batch_id for batch_id, count in Counter(batch_ids).items() if count > 1)
    if duplicate_batches:
        raise AlignmentError(f"duplicate batch ids: {duplicate_batches}")

    assigned = [subtask_id for batch in plan.batches for subtask_id in batch.subtask_ids]
    counts = Counter(assigned)
    duplicates = sorted(subtask_id for subtask_id, count in counts.items() if count > 1)
    if duplicates:
        raise AlignmentError(f"duplicate subtask ids: {duplicates}")

    expected = set(expected_subtask_ids)
    actual = set(assigned)
    missing = sorted(expected - actual)
    if missing:
        raise AlignmentError(f"missing subtask ids: {missing}")
    unexpected = sorted(actual - expected)
    if unexpected:
        raise AlignmentError(f"unknown subtask ids: {unexpected}")

    known_batches = set(batch_ids)
    dependencies = [dependency for batch in plan.batches for dependency in batch.depends_on]
    unknown_dependencies = sorted(set(dependencies) - known_batches)
    if unknown_dependencies:
        raise AlignmentError(f"unknown dependencies: {unknown_dependencies}")
    if any(batch.batch_id in batch.depends_on for batch in plan.batches):
        raise AlignmentError("dependency cycle")

    by_id: Dict[str, BatchDraft] = {batch.batch_id: batch for batch in plan.batches}
    remaining = {batch_id: set(batch.depends_on) for batch_id, batch in by_id.items()}
    ordered: List[BatchDraft] = []
    while remaining:
        ready = [batch_id for batch_id in batch_ids if batch_id in remaining and not remaining[batch_id]]
        if not ready:
            raise AlignmentError("dependency cycle")
        for batch_id in ready:
            ordered.append(by_id[batch_id])
            del remaining[batch_id]
            for dependencies_for_batch in remaining.values():
                dependencies_for_batch.discard(batch_id)
    return ordered
