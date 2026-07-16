"""Captain-owned project planning and batch release pipeline."""

from typing import Awaitable, Callable, List, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agenten.planning.alignment import (
    AlignmentError,
    AlignmentPlan,
    BatchDraft,
    validate_alignment,
)
from agenten.planning.policy import PlanningPolicy
from agenten.validation.contracts import (
    AcceptanceAssertion,
    ExampleCase,
    HoldoutSuite,
    WorkBatch,
)


class CaptainPlanningError(RuntimeError):
    """A project could not be converted into a safe executable plan."""


class PlannedSubtask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subtask_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    capability_tags: List[str] = Field(default_factory=list)


class BatchEnrichment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    goal: str = Field(min_length=1)
    constraints: List[str] = Field(default_factory=list)
    capability_tags: List[str] = Field(default_factory=list)
    acceptance_criteria: List[AcceptanceAssertion] = Field(min_length=1)
    golden_cases: List[ExampleCase] = Field(default_factory=list)
    holdout_cases: List[ExampleCase] = Field(min_length=1)

    @model_validator(mode="after")
    def ensure_test_case_isolation(self) -> "BatchEnrichment":
        golden_ids = {case.case_id for case in self.golden_cases}
        holdout_ids = [case.case_id for case in self.holdout_cases]
        if len(holdout_ids) != len(set(holdout_ids)):
            raise ValueError("holdout case ids must be unique")
        overlap = sorted(golden_ids.intersection(holdout_ids))
        if overlap:
            raise ValueError(f"golden and holdout case ids overlap: {overlap}")
        return self


class CaptainRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    batches: List[WorkBatch]


class BatchReleaseClient(Protocol):
    async def release(self, batch: WorkBatch, holdouts: HoldoutSuite) -> None: ...


class CapabilityResolver(Protocol):
    async def find_match(self, target: str, capability_tags: List[str]) -> str | None: ...


Decompose = Callable[[str], Awaitable[List[PlannedSubtask]]]
Align = Callable[[str, List[PlannedSubtask], str], Awaitable[AlignmentPlan]]
Enrich = Callable[[str, BatchDraft, List[PlannedSubtask]], Awaitable[BatchEnrichment]]


class CaptainPipeline:
    """Turn one project description into validated, released work batches."""

    def __init__(
        self,
        *,
        decompose: Decompose,
        align: Align,
        enrich: Enrich,
        release_client: BatchReleaseClient,
        policy: PlanningPolicy,
        capability_resolver: CapabilityResolver | None = None,
        target: str,
        max_alignment_attempts: int = 2,
    ) -> None:
        if max_alignment_attempts < 1:
            raise ValueError("max_alignment_attempts must be at least 1")
        self._decompose = decompose
        self._align = align
        self._enrich = enrich
        self._release_client = release_client
        self._policy = policy
        self._capability_resolver = capability_resolver
        self._target = target
        self._max_alignment_attempts = max_alignment_attempts

    async def run(self, project_description: str) -> CaptainRunResult:
        if not project_description.strip():
            raise CaptainPlanningError("project description must not be empty")

        subtasks = await self._decompose(project_description)
        if not subtasks:
            raise CaptainPlanningError("decomposition produced no subtasks")
        subtask_ids = [subtask.subtask_id for subtask in subtasks]
        if len(subtask_ids) != len(set(subtask_ids)):
            raise CaptainPlanningError("decomposition produced duplicate subtask ids")

        feedback = ""
        ordered_drafts: List[BatchDraft] | None = None
        for _ in range(self._max_alignment_attempts):
            proposal = await self._align(project_description, subtasks, feedback)
            try:
                ordered_drafts = validate_alignment(proposal, subtask_ids)
                break
            except AlignmentError as exc:
                feedback = str(exc)
        if ordered_drafts is None:
            raise CaptainPlanningError(
                f"alignment failed after {self._max_alignment_attempts} attempts: {feedback}"
            )

        subtasks_by_id = {subtask.subtask_id: subtask for subtask in subtasks}
        released: List[WorkBatch] = []
        for draft in ordered_drafts:
            selected_subtasks = [subtasks_by_id[subtask_id] for subtask_id in draft.subtask_ids]
            enrichment = await self._enrich(project_description, draft, selected_subtasks)
            self._policy.validate_enrichment(enrichment)
            satisfied_by = None
            if self._capability_resolver is not None:
                satisfied_by = await self._capability_resolver.find_match(
                    self._target,
                    enrichment.capability_tags,
                )
            batch = WorkBatch(
                batch_id=draft.batch_id,
                title=draft.title,
                goal=enrichment.goal,
                subtask_ids=draft.subtask_ids,
                target=self._target,
                capability_tags=enrichment.capability_tags,
                depends_on=draft.depends_on,
                constraints=enrichment.constraints,
                acceptance_criteria=enrichment.acceptance_criteria,
                golden_cases=enrichment.golden_cases,
                satisfied_by=satisfied_by,
            )
            holdouts = HoldoutSuite(batch_id=draft.batch_id, cases=enrichment.holdout_cases)
            await self._release_client.release(batch, holdouts)
            released.append(batch)

        return CaptainRunResult(batches=released)
