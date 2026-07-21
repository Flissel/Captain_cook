"""Captain-owned persistence boundary for the generated-agent lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from agenten.agent_factory.contracts import (
    AgentFactoryJobV3,
    FactoryEvidenceBlock,
    FactoryJob,
)
from agenten.agent_factory.execution_budget import (
    FactoryBudgetProjection,
    FactoryUsageReceiptV1,
)
from agenten.agent_factory.release_gate import (
    FactoryReleaseDecision,
    evaluate_factory_workflow_release,
)
from agenten.agent_factory.skill_store import StoredSkillEvaluation
from agenten.agent_factory.skill_workflow_contracts import (
    CandidateRevisionV1,
    CodebaseInventoryV1,
    CodexBuildBriefV1,
    FactoryFeedbackV1,
    TeamEvaluationV1,
    TeamExecutionEvidenceV1,
)
from agenten.agent_factory.state_machine import (
    FactoryAction,
    FactoryLifecycleError,
    FactoryProjection,
    apply_block,
    next_action,
)


class FactoryRepositoryError(RuntimeError):
    """The append-only factory record cannot be accepted."""


FactoryWorkflowArtifact = (
    CodebaseInventoryV1
    | CodexBuildBriefV1
    | TeamExecutionEvidenceV1
    | TeamEvaluationV1
    | CandidateRevisionV1
    | FactoryFeedbackV1
)


class FactoryRepository(Protocol):
    """Append-only storage port implemented by Captain's gateway adapter."""

    def register(self, job: FactoryJob) -> None:
        """Persist a newly authorized Captain job."""

    def job(self, job_id: UUID) -> FactoryJob:
        """Return the authorized job or raise FactoryRepositoryError."""

    def append(self, block: FactoryEvidenceBlock) -> bool:
        """Append an evidence block, returning false for an identical replay."""

    def blocks(self, job_id: UUID) -> tuple[FactoryEvidenceBlock, ...]:
        """Return blocks in their append order."""

    def evaluation_for_job(self, job_id: UUID) -> StoredSkillEvaluation | None:
        """Return Captain-readable evaluation evidence without granting writes."""

    def release_decision_for_job(self, job_id: UUID) -> FactoryReleaseDecision | None:
        """Return the Gateway-accepted Captain release decision, if present."""

    def workflow_artifacts(
        self,
        job_id: UUID,
    ) -> tuple[FactoryWorkflowArtifact, ...]:
        """Return read-only Gateway workflow artifacts in append order."""

    def workflow_budget_projection(
        self,
        job_id: UUID,
    ) -> FactoryBudgetProjection | None:
        """Return the Gateway-owned V3 budget projection without granting writes."""

    def workflow_usage_receipts(
        self,
        job_id: UUID,
    ) -> tuple[FactoryUsageReceiptV1, ...]:
        """Return Gateway-accepted provider receipts in append order."""


@dataclass
class InMemoryFactoryRepository:
    """Deterministic test adapter; production must use the gateway ledger port."""

    _jobs: dict[UUID, FactoryJob] = field(default_factory=dict)
    _blocks: dict[UUID, list[FactoryEvidenceBlock]] = field(default_factory=dict)
    _event_ids: dict[UUID, FactoryEvidenceBlock] = field(default_factory=dict)
    _evaluations_by_job: dict[UUID, StoredSkillEvaluation] = field(default_factory=dict)
    _release_decisions_by_job: dict[UUID, FactoryReleaseDecision] = field(
        default_factory=dict
    )

    def register(self, job: FactoryJob) -> None:
        existing = self._jobs.get(job.job_id)
        if existing is not None:
            if existing != job:
                raise FactoryRepositoryError("job_id already exists with different content")
            return
        self._jobs[job.job_id] = job
        self._blocks[job.job_id] = []

    def job(self, job_id: UUID) -> FactoryJob:
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise FactoryRepositoryError("factory job not found") from exc

    def append(self, block: FactoryEvidenceBlock) -> bool:
        self.job(block.job_id)
        existing = self._event_ids.get(block.event_id)
        if existing is not None:
            if existing != block:
                raise FactoryRepositoryError("event_id already exists with different content")
            return False
        self._event_ids[block.event_id] = block
        self._blocks[block.job_id].append(block)
        return True

    def blocks(self, job_id: UUID) -> tuple[FactoryEvidenceBlock, ...]:
        self.job(job_id)
        return tuple(self._blocks[job_id])

    def evaluation_for_job(self, job_id: UUID) -> StoredSkillEvaluation | None:
        self.job(job_id)
        return self._evaluations_by_job.get(job_id)

    def release_decision_for_job(self, job_id: UUID) -> FactoryReleaseDecision | None:
        self.job(job_id)
        return self._release_decisions_by_job.get(job_id)

    def workflow_artifacts(
        self,
        job_id: UUID,
    ) -> tuple[FactoryWorkflowArtifact, ...]:
        self.job(job_id)
        return ()

    def workflow_budget_projection(
        self,
        job_id: UUID,
    ) -> FactoryBudgetProjection | None:
        self.job(job_id)
        return None

    def workflow_usage_receipts(
        self,
        job_id: UUID,
    ) -> tuple[FactoryUsageReceiptV1, ...]:
        self.job(job_id)
        return ()


class FactoryCoordinator:
    """Rebuild state before every append; no worker may bypass Captain policy."""

    def __init__(self, repository: FactoryRepository):
        self._repository = repository

    def register(self, job: FactoryJob) -> None:
        self._repository.register(job)

    def record(self, block: FactoryEvidenceBlock) -> bool:
        existing = self._existing_block(block)
        if existing is not None:
            if existing == block:
                return False
            raise FactoryRepositoryError("event_id already exists with different content")
        projection = self.projection(block.job_id)
        promotion = block.phase.value == "capability_promoted"
        legacy_promotion = promotion and not isinstance(
            projection.job, AgentFactoryJobV3
        )
        evaluation = (
            self.evaluation_for_job(block.job_id) if legacy_promotion else None
        )
        release_decision = (
            self.release_decision_for_job(block.job_id)
            if legacy_promotion
            else None
        )
        workflow_evaluation, feedback = (
            self._workflow_review(block.job_id, attempt=block.attempt)
            if isinstance(projection.job, AgentFactoryJobV3)
            else (None, None)
        )
        workflow_release_decision = (
            self._workflow_release_decision(
                projection.job,
                attempt=block.attempt,
                evaluation=workflow_evaluation,
            )
            if isinstance(projection.job, AgentFactoryJobV3)
            and block.phase.value == "capability_promoted"
            else None
        )
        apply_block(
            projection,
            block,
            evaluation=evaluation,
            workflow_evaluation=(
                workflow_evaluation
                if block.phase.value in {"quality_reviewed", "capability_promoted"}
                else None
            ),
            feedback=(
                feedback
                if block.phase.value in {"quality_reviewed", "capability_promoted"}
                else None
            ),
            release_decision=(
                workflow_release_decision
                if isinstance(projection.job, AgentFactoryJobV3)
                else release_decision
            ),
        )
        return self._repository.append(block)

    def projection(self, job_id: UUID) -> FactoryProjection:
        projection = FactoryProjection.from_job(self._repository.job(job_id))
        for stored_block in self._repository.blocks(job_id):
            evaluation = (
                self.evaluation_for_job(job_id)
                if stored_block.phase.value == "capability_promoted"
                and not isinstance(projection.job, AgentFactoryJobV3)
                else None
            )
            release_decision = (
                self.release_decision_for_job(job_id)
                if stored_block.phase.value == "capability_promoted"
                and not isinstance(projection.job, AgentFactoryJobV3)
                else None
            )
            workflow_evaluation, feedback = (
                self._workflow_review(job_id, attempt=stored_block.attempt)
                if isinstance(projection.job, AgentFactoryJobV3)
                else (None, None)
            )
            workflow_release_decision = (
                self._workflow_release_decision(
                    projection.job,
                    attempt=stored_block.attempt,
                    evaluation=workflow_evaluation,
                )
                if isinstance(projection.job, AgentFactoryJobV3)
                and stored_block.phase.value == "capability_promoted"
                else None
            )
            projection = apply_block(
                projection,
                stored_block,
                evaluation=evaluation,
                release_decision=(
                    workflow_release_decision
                    if isinstance(projection.job, AgentFactoryJobV3)
                    else release_decision
                ),
                workflow_evaluation=(
                    workflow_evaluation
                    if stored_block.phase.value
                    in {"quality_reviewed", "capability_promoted"}
                    else None
                ),
                feedback=(
                    feedback
                    if stored_block.phase.value
                    in {"quality_reviewed", "capability_promoted"}
                    else None
                ),
            )
        return projection

    def next_action(self, job_id: UUID) -> FactoryAction:
        projection = self.projection(job_id)
        evaluation = (
            self.evaluation_for_job(job_id)
            if projection.phase is not None and projection.phase.value == "quality_reviewed"
            else None
        )
        workflow_evaluation, feedback = (
            self._workflow_review(job_id, attempt=projection.attempt)
            if isinstance(projection.job, AgentFactoryJobV3)
            else (None, None)
        )
        workflow_release_decision = (
            self._workflow_release_decision(
                projection.job,
                attempt=projection.attempt,
                evaluation=workflow_evaluation,
            )
            if isinstance(projection.job, AgentFactoryJobV3)
            else None
        )
        return next_action(
            projection,
            evaluation=evaluation,
            workflow_evaluation=workflow_evaluation,
            feedback=feedback,
            workflow_release_decision=workflow_release_decision,
        ).model_copy(update={"job_id": job_id})

    def blocks(self, job_id: UUID) -> tuple[FactoryEvidenceBlock, ...]:
        return self._repository.blocks(job_id)

    def evaluation_for_job(self, job_id: UUID) -> StoredSkillEvaluation | None:
        lookup = getattr(self._repository, "evaluation_for_job", None)
        if lookup is None:
            return None
        return lookup(job_id)

    def release_decision_for_job(self, job_id: UUID) -> FactoryReleaseDecision | None:
        lookup = getattr(self._repository, "release_decision_for_job", None)
        if lookup is None:
            return None
        return lookup(job_id)

    def workflow_artifacts(
        self,
        job_id: UUID,
    ) -> tuple[FactoryWorkflowArtifact, ...]:
        lookup = getattr(self._repository, "workflow_artifacts", None)
        if lookup is None:
            return ()
        return lookup(job_id)

    def workflow_budget_projection(
        self,
        job_id: UUID,
    ) -> FactoryBudgetProjection | None:
        lookup = getattr(self._repository, "workflow_budget_projection", None)
        if lookup is None:
            return None
        return lookup(job_id)

    def workflow_usage_receipts(
        self,
        job_id: UUID,
    ) -> tuple[FactoryUsageReceiptV1, ...]:
        lookup = getattr(self._repository, "workflow_usage_receipts", None)
        if lookup is None:
            return ()
        return lookup(job_id)

    def _workflow_release_decision(
        self,
        job: AgentFactoryJobV3,
        *,
        attempt: int,
        evaluation: TeamEvaluationV1 | None,
    ) -> FactoryReleaseDecision | None:
        if evaluation is None:
            return None
        executions = tuple(
            artifact
            for artifact in self.workflow_artifacts(job.job_id)
            if isinstance(artifact, TeamExecutionEvidenceV1)
            and artifact.attempt == attempt
        )
        return evaluate_factory_workflow_release(
            job,
            executions,
            evaluation,
            budget_projection=self.workflow_budget_projection(job.job_id),
            usage_receipts=self.workflow_usage_receipts(job.job_id),
        )

    def _workflow_review(
        self,
        job_id: UUID,
        *,
        attempt: int,
    ) -> tuple[TeamEvaluationV1 | None, FactoryFeedbackV1 | None]:
        artifacts = self.workflow_artifacts(job_id)
        evaluations = tuple(
            artifact
            for artifact in artifacts
            if isinstance(artifact, TeamEvaluationV1)
            and artifact.attempt == attempt
        )
        feedback_items = tuple(
            artifact
            for artifact in artifacts
            if isinstance(artifact, FactoryFeedbackV1)
            and artifact.attempt == attempt
        )
        if len(evaluations) > 1 or len(feedback_items) > 1:
            raise FactoryRepositoryError(
                "factory workflow review is ambiguous for the current attempt"
            )
        return (
            evaluations[0] if evaluations else None,
            feedback_items[0] if feedback_items else None,
        )

    def _existing_block(self, incoming: FactoryEvidenceBlock) -> FactoryEvidenceBlock | None:
        return next(
            (
                block
                for block in self._repository.blocks(incoming.job_id)
                if block.event_id == incoming.event_id
            ),
            None,
        )
