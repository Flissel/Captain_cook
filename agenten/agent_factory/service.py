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
from agenten.agent_factory.release_gate import FactoryReleaseDecision
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
    _workflow_artifacts_by_job: dict[UUID, list[FactoryWorkflowArtifact]] = field(
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
        self._workflow_artifacts_by_job[job.job_id] = []

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
        return tuple(self._workflow_artifacts_by_job[job_id])

    def record_workflow_artifact(self, artifact: FactoryWorkflowArtifact) -> bool:
        """Deterministic test adapter for the Gateway artifact endpoint."""

        job = self.job(artifact.job_id)
        if (
            artifact.correlation_id != job.correlation_id
            or artifact.subject_version != job.subject_version
            or artifact.acceptance_assertion_ids != job.acceptance_assertion_ids
        ):
            raise FactoryRepositoryError(
                "workflow artifact does not match the factory job"
            )
        for existing in self._workflow_artifacts_by_job[artifact.job_id]:
            if existing.invocation_id == artifact.invocation_id:
                if existing == artifact:
                    return False
                raise FactoryRepositoryError(
                    "workflow invocation already exists with different content"
                )
        self._workflow_artifacts_by_job[artifact.job_id].append(artifact)
        return True


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
        evaluation = self.evaluation_for_job(block.job_id) if promotion else None
        release_decision = self.release_decision_for_job(block.job_id) if promotion else None
        workflow_evaluation, feedback = (
            self._workflow_review(block.job_id, attempt=block.attempt)
            if isinstance(projection.job, AgentFactoryJobV3)
            else (None, None)
        )
        apply_block(
            projection,
            block,
            evaluation=evaluation,
            release_decision=release_decision,
            workflow_evaluation=(
                workflow_evaluation
                if block.phase.value == "quality_reviewed"
                else None
            ),
            feedback=(feedback if block.phase.value == "quality_reviewed" else None),
        )
        return self._repository.append(block)

    def projection(self, job_id: UUID) -> FactoryProjection:
        projection = FactoryProjection.from_job(self._repository.job(job_id))
        for stored_block in self._repository.blocks(job_id):
            evaluation = (
                self.evaluation_for_job(job_id)
                if stored_block.phase.value == "capability_promoted"
                else None
            )
            release_decision = (
                self.release_decision_for_job(job_id)
                if stored_block.phase.value == "capability_promoted"
                else None
            )
            workflow_evaluation, feedback = (
                self._workflow_review(job_id, attempt=stored_block.attempt)
                if isinstance(projection.job, AgentFactoryJobV3)
                else (None, None)
            )
            projection = apply_block(
                projection,
                stored_block,
                evaluation=evaluation,
                release_decision=release_decision,
                workflow_evaluation=(
                    workflow_evaluation
                    if stored_block.phase.value == "quality_reviewed"
                    else None
                ),
                feedback=(
                    feedback
                    if stored_block.phase.value == "quality_reviewed"
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
        return next_action(
            projection,
            evaluation=evaluation,
            workflow_evaluation=workflow_evaluation,
            feedback=feedback,
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
