"""Compile a planner-supplied plan through Captain's deterministic core.

OpenClaw (plan 2026-11-07, Phase 4/5) replaces the LLM stages decompose,
align and enrich: it supplies their results directly. They run through the
unchanged `CaptainPipeline` as fixed stages, so alignment validation,
planning policy, run isolation, target allow-lists and canonical batch
construction stay Captain code. Alignment gets exactly one attempt; there
is no model behind it to retry with feedback.

Publishing never accepts a compiled plan from the planner: it recompiles
from the same inputs and releases what Captain itself produced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agenten.planning.alignment import AlignmentPlan, BatchDraft
from agenten.planning.captain_pipeline import (
    BatchEnrichment,
    BatchReleaseClient,
    CapabilityResolver,
    CaptainCompiledPlan,
    CaptainPipeline,
    PlannedSubtask,
)
from agenten.planning.policy import PlanningPolicy
from agenten.validation.contracts import HoldoutSuite, WorkBatch


# Hard input bounds, independent of the constitution budget: they cap the
# work any single tool call can make Captain do.
MAX_PLAN_ITEMS = 500
MAX_PROJECT_DESCRIPTION_LENGTH = 20_000
MAX_ID_LENGTH = 128


class SuppliedPlan(BaseModel):
    """The three planning stage results, as the planner produced them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_description: str = Field(min_length=1, max_length=MAX_PROJECT_DESCRIPTION_LENGTH)
    subtasks: List[PlannedSubtask] = Field(min_length=1, max_length=MAX_PLAN_ITEMS)
    alignment: AlignmentPlan
    enrichments: dict[str, BatchEnrichment] = Field(max_length=MAX_PLAN_ITEMS)

    @model_validator(mode="after")
    def _one_enrichment_per_aligned_batch(self) -> "SuppliedPlan":
        if len(self.alignment.batches) > MAX_PLAN_ITEMS:
            raise ValueError(f"a plan may hold at most {MAX_PLAN_ITEMS} batches")
        if any(len(item.subtask_id) > MAX_ID_LENGTH for item in self.subtasks):
            raise ValueError(f"subtask ids may be at most {MAX_ID_LENGTH} characters")
        aligned = {draft.batch_id for draft in self.alignment.batches}
        supplied = set(self.enrichments)
        if aligned != supplied:
            missing = sorted(aligned - supplied)
            extra = sorted(supplied - aligned)
            raise ValueError(
                f"enrichments must match the aligned batches (missing {missing}, extra {extra})"
            )
        return self


@dataclass(frozen=True)
class PlanCompilerSettings:
    target: str
    allowed_targets: frozenset[str]
    capability_tags: frozenset[str]


class _NoRelease:
    async def release(self, batch: WorkBatch, holdouts: HoldoutSuite) -> None:
        raise RuntimeError("compile-only pipeline cannot release")


def _pipeline(
    plan: SuppliedPlan,
    settings: PlanCompilerSettings,
    *,
    release_client: BatchReleaseClient,
    capability_resolver: CapabilityResolver | None,
) -> CaptainPipeline:
    async def decompose(project_description: str) -> List[PlannedSubtask]:
        return list(plan.subtasks)

    async def align(
        project_description: str, subtasks: List[PlannedSubtask], feedback: str
    ) -> AlignmentPlan:
        return plan.alignment

    async def enrich(
        project_description: str, draft: BatchDraft, subtasks: List[PlannedSubtask]
    ) -> BatchEnrichment:
        return plan.enrichments[draft.batch_id]

    return CaptainPipeline(
        decompose=decompose,
        align=align,
        enrich=enrich,
        release_client=release_client,
        policy=PlanningPolicy(settings.capability_tags),
        capability_resolver=capability_resolver,
        target=settings.target,
        allowed_targets=settings.allowed_targets,
        max_alignment_attempts=1,
    )


async def compile_supplied_plan(
    plan: SuppliedPlan,
    settings: PlanCompilerSettings,
    *,
    capability_resolver: CapabilityResolver | None = None,
) -> CaptainCompiledPlan:
    """Validate and compile without touching the ledger."""

    pipeline = _pipeline(
        plan, settings, release_client=_NoRelease(), capability_resolver=capability_resolver
    )
    return await pipeline.compile(plan.project_description)


async def publish_supplied_plan(
    plan: SuppliedPlan,
    settings: PlanCompilerSettings,
    *,
    release_client: BatchReleaseClient,
    capability_resolver: CapabilityResolver | None = None,
) -> CaptainCompiledPlan:
    """Recompile from the supplied inputs, then release every batch."""

    pipeline = _pipeline(
        plan, settings, release_client=release_client, capability_resolver=capability_resolver
    )
    compiled = await pipeline.compile(plan.project_description)
    await pipeline.release(compiled)
    return compiled
