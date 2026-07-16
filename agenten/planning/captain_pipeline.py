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


class CaptainCompiledPlan(BaseModel):
    """Complete plan output before any publication side effect occurs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    batches: tuple[WorkBatch, ...]
    holdouts: tuple[HoldoutSuite, ...]

    @model_validator(mode="after")
    def batches_and_holdouts_match(self) -> "CaptainCompiledPlan":
        batch_ids = [batch.batch_id for batch in self.batches]
        holdout_ids = [holdout.batch_id for holdout in self.holdouts]
        if batch_ids != holdout_ids:
            raise ValueError("compiled batches and holdouts must have identical ordering")
        return self


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
        policy: PlanningPolicy | None = None,
        capability_resolver: CapabilityResolver | None = None,
        target: str,
        allowed_targets: frozenset[str] | None = None,
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
        self._allowed_targets = allowed_targets or frozenset({target})
        if target not in self._allowed_targets:
            raise ValueError("default target must be included in allowed_targets")
        self._max_alignment_attempts = max_alignment_attempts

    async def compile(self, project_description: str) -> CaptainCompiledPlan:
        """Build the complete reviewable plan without publishing it."""

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
        enriched_drafts: List[tuple[BatchDraft, BatchEnrichment]] = []
        for draft in ordered_drafts:
            selected_subtasks = [subtasks_by_id[subtask_id] for subtask_id in draft.subtask_ids]
            enrichment = await self._enrich(project_description, draft, selected_subtasks)
            if self._policy is not None:
                self._policy.validate_enrichment(enrichment)
            enriched_drafts.append((draft, enrichment))

        if self._policy is not None:
            self._policy.validate_run_isolation(
                enrichment for _, enrichment in enriched_drafts
            )

        batches: List[WorkBatch] = []
        holdout_suites: List[HoldoutSuite] = []
        for draft, enrichment in enriched_drafts:
            batch_target = draft.target or self._target
            if batch_target not in self._allowed_targets:
                raise CaptainPlanningError(
                    f"batch {draft.batch_id} requested disallowed target: {batch_target}"
                )
            capability_tags = (
                self._policy.canonical_capability_tags(enrichment.capability_tags)
                if self._policy is not None
                else enrichment.capability_tags
            )
            satisfied_by = None
            if self._capability_resolver is not None:
                satisfied_by = await self._capability_resolver.find_match(
                    batch_target,
                    capability_tags,
                )
            batch = WorkBatch(
                batch_id=draft.batch_id,
                title=draft.title,
                goal=enrichment.goal,
                subtask_ids=draft.subtask_ids,
                target=batch_target,
                runtime=batch_target,
                runtime_version="v1",
                interface_schema=f"captain-{batch_target}-artifact/v1",
                capability_tags=capability_tags,
                depends_on=draft.depends_on,
                constraints=enrichment.constraints,
                acceptance_criteria=enrichment.acceptance_criteria,
                golden_cases=enrichment.golden_cases,
                satisfied_by=satisfied_by,
            )
            holdouts = HoldoutSuite(batch_id=draft.batch_id, cases=enrichment.holdout_cases)
            batches.append(batch)
            holdout_suites.append(holdouts)

        return CaptainCompiledPlan(
            batches=tuple(batches),
            holdouts=tuple(holdout_suites),
        )

    async def run(self, project_description: str) -> CaptainRunResult:
        """Compatibility path: compile first, then publish each batch contract."""

        compiled = await self.compile(project_description)
        for batch, holdouts in zip(compiled.batches, compiled.holdouts):
            await self._release_client.release(batch, holdouts)

        return CaptainRunResult(batches=list(compiled.batches))
