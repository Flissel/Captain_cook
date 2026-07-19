"""Captain-owned project planning and batch release pipeline."""

from collections.abc import Sequence
import hashlib
import re
from typing import Awaitable, Callable, List, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agenten.planning.alignment import (
    AlignmentError,
    AlignmentPlan,
    BatchDraft,
    validate_alignment,
)
from agenten.planning.policy import PlanningPolicy
from agenten.planning.hermes_plan import ValidatedPlanningInput
from agenten.agent_runtime.contracts import HermesPlanResult, IntegrationIntent
from agenten.planning.run_models import (
    CaptainRunConflictError,
    CaptainRunState,
    CaptainRunStatus,
    PartialReleaseError,
)
from agenten.planning.run_store import CaptainRunStore
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


class PlanCompilationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_project_id: str = Field(min_length=1)
    source_plan_version: int = Field(ge=1, strict=True)
    source_plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    compiled: CaptainCompiledPlan


class BatchReleaseClient(Protocol):
    async def release(self, batch: WorkBatch, holdouts: HoldoutSuite) -> None: ...


class CapabilityResolver(Protocol):
    async def find_match(
        self,
        target: str,
        capability_tags: Sequence[str],
    ) -> str | None: ...


class HermesPlanResultReader(Protocol):
    async def read(self, result: HermesPlanResult) -> ValidatedPlanningInput: ...


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
        run_store: CaptainRunStore | None = None,
        plan_reader: HermesPlanResultReader | None = None,
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
        self._run_store = run_store
        self._plan_reader = plan_reader

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
                else list(enrichment.capability_tags)
            )
            satisfied_by = None
            if self._capability_resolver is not None:
                satisfied_by = await self._capability_resolver.find_match(
                    batch_target,
                    list(capability_tags),
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
                capability_tags=list(capability_tags),
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

    async def compile_from_plan(
        self,
        source: ValidatedPlanningInput,
    ) -> PlanCompilationResult:
        """Compile validated Hermes input without crossing the release boundary."""

        compiled = await self.compile(source.objective)
        has_n8n_intent = any(
            blueprint.integration_intent is IntegrationIntent.N8N
            for blueprint in source.blueprints
        )
        if has_n8n_intent and not any(batch.target == "n8n" for batch in compiled.batches):
            raise CaptainPlanningError("n8n blueprint produced no n8n work batch")
        if has_n8n_intent:
            batches = tuple(
                batch.model_copy(
                    update={
                        "capability_tags": [*batch.capability_tags, "n8n-builder"]
                    }
                )
                if batch.target == "n8n" and "n8n-builder" not in batch.capability_tags
                else batch
                for batch in compiled.batches
            )
            compiled = CaptainCompiledPlan(
                batches=batches,
                holdouts=compiled.holdouts,
            )
        return PlanCompilationResult(
            source_project_id=source.project_id,
            source_plan_version=source.subject_version,
            source_plan_digest=source.plan_ref.sha256,
            compiled=compiled,
        )

    async def compile_from_hermes_result(
        self,
        result: HermesPlanResult,
    ) -> PlanCompilationResult:
        if self._plan_reader is None:
            raise CaptainPlanningError("Hermes plan compilation requires an injected reader")
        source = await self._plan_reader.read(result)
        return await self.compile_from_plan(source)

    async def run(
        self,
        project_description: str,
        *,
        run_id: str | None = None,
    ) -> CaptainRunResult:
        """Compatibility path: compile first, then publish each batch contract."""

        if run_id is not None:
            compiled = await self.compile_and_release(project_description, run_id=run_id)
            return CaptainRunResult(batches=list(compiled.batches))
        compiled = await self.compile(project_description)
        await self.release(compiled)
        return CaptainRunResult(batches=list(compiled.batches))

    async def compile_and_release(
        self,
        project_description: str,
        *,
        run_id: str,
    ) -> CaptainCompiledPlan:
        """Compile once and resume idempotent publication from a durable checkpoint."""

        await self.compile_checkpoint(project_description, run_id=run_id)
        return await self.release_checkpoint(project_description, run_id=run_id)

    async def compile_checkpoint(
        self,
        project_description: str,
        *,
        run_id: str,
    ) -> CaptainCompiledPlan:
        """Compile or reload an immutable plan without crossing the release boundary."""

        if self._run_store is None:
            raise ValueError("run_id requires an injected CaptainRunStore")
        digest = hashlib.sha256(project_description.encode("utf-8")).hexdigest()
        async with self._run_store.lock(run_id):
            state = await self._run_store.load(run_id)
            if state is None:
                compiled = await self.compile(project_description)
                state = CaptainRunState(
                    run_id=run_id,
                    project_id=f"project-{digest[:16]}",
                    project_digest=digest,
                    status=CaptainRunStatus.PLANNING,
                    batches=compiled.batches,
                    holdouts=compiled.holdouts,
                )
                await self._run_store.save(state)
                return compiled

            self._assert_project_digest(state, digest, run_id)
            return CaptainCompiledPlan(
                batches=tuple(state.batches),
                holdouts=tuple(state.holdouts),
            )

    async def release_checkpoint(
        self,
        project_description: str,
        *,
        run_id: str,
    ) -> CaptainCompiledPlan:
        """Release only after the caller has validated the checkpointed full plan."""

        if self._run_store is None:
            raise ValueError("run_id requires an injected CaptainRunStore")
        digest = hashlib.sha256(project_description.encode("utf-8")).hexdigest()
        return await self._release_checkpoint_digest(run_id, digest)

    async def release_compiled_checkpoint(
        self,
        compiled: CaptainCompiledPlan,
        *,
        source_digest: str,
        run_id: str,
    ) -> CaptainCompiledPlan:
        """Checkpoint externally validated Captain work before any release side effect."""

        if self._run_store is None:
            raise ValueError("run_id requires an injected CaptainRunStore")
        if not re.fullmatch(r"[0-9a-f]{64}", source_digest):
            raise ValueError("source_digest must be a SHA-256 hex digest")
        async with self._run_store.lock(run_id):
            state = await self._run_store.load(run_id)
            if state is None:
                state = CaptainRunState(
                    run_id=run_id,
                    project_id=f"evaluation-{source_digest[:16]}",
                    project_digest=source_digest,
                    status=CaptainRunStatus.PLANNING,
                    batches=compiled.batches,
                    holdouts=compiled.holdouts,
                )
                await self._run_store.save(state)
            else:
                self._assert_project_digest(state, source_digest, run_id)
                if tuple(state.batches) != compiled.batches or tuple(state.holdouts) != compiled.holdouts:
                    raise CaptainRunConflictError(
                        f"run {run_id!r} already contains different compiled work"
                    )
        return await self._release_checkpoint_digest(run_id, source_digest)

    async def _release_checkpoint_digest(
        self,
        run_id: str,
        digest: str,
    ) -> CaptainCompiledPlan:
        """Publish one persisted compiled plan exactly once per batch in order."""

        async with self._run_store.lock(run_id):
            state = await self._run_store.load(run_id)
            if state is None:
                raise ValueError("run checkpoint must be compiled before release")
            self._assert_project_digest(state, digest, run_id)
            compiled = CaptainCompiledPlan(
                batches=tuple(state.batches),
                holdouts=tuple(state.holdouts),
            )
            if state.status is CaptainRunStatus.RELEASED:
                return compiled
            state = self._updated_state(
                state,
                status=CaptainRunStatus.RELEASING,
                failed_batch_id=None,
                error_kind=None,
            )
            await self._run_store.save(state)

            released = list(state.released_batch_ids)
            released_set = set(released)
            for batch, holdouts in zip(compiled.batches, compiled.holdouts):
                if batch.batch_id in released_set:
                    continue
                try:
                    await self._release_client.release(
                        batch.model_copy(deep=True),
                        holdouts.model_copy(deep=True),
                    )
                except Exception as exc:
                    failed = self._updated_state(
                        state,
                        status=CaptainRunStatus.PARTIALLY_RELEASED,
                        released_batch_ids=released,
                        failed_batch_id=batch.batch_id,
                        error_kind=type(exc).__name__,
                    )
                    await self._run_store.save(failed)
                    raise PartialReleaseError(run_id, released, batch.batch_id) from exc

                released.append(batch.batch_id)
                released_set.add(batch.batch_id)
                state = self._updated_state(
                    state,
                    status=(
                        CaptainRunStatus.RELEASED
                        if len(released) == len(compiled.batches)
                        else CaptainRunStatus.RELEASING
                    ),
                    released_batch_ids=released,
                    failed_batch_id=None,
                    error_kind=None,
                )
                await self._run_store.save(state)
            return compiled

    @staticmethod
    def _assert_project_digest(
        state: CaptainRunState,
        digest: str,
        run_id: str,
    ) -> None:
        if state.project_digest != digest:
            raise CaptainRunConflictError(
                f"run {run_id!r} already belongs to a different project"
            )

    @staticmethod
    def _updated_state(state: CaptainRunState, **updates: object) -> CaptainRunState:
        return CaptainRunState.model_validate(state.model_dump() | updates)

    async def release(self, compiled: CaptainCompiledPlan) -> None:
        """Publish one already-reviewed compiled plan through the injected port."""

        for batch, holdouts in zip(compiled.batches, compiled.holdouts):
            await self._release_client.release(
                batch.model_copy(deep=True),
                holdouts.model_copy(deep=True),
            )
