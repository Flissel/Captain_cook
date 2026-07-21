"""Captain-owned persistence boundary for the generated-agent lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from agenten.agent_factory.contracts import FactoryEvidenceBlock, FactoryJob
from agenten.agent_factory.release_gate import FactoryReleaseDecision
from agenten.agent_factory.outcome_contracts import FactoryTerminalDecision
from agenten.agent_factory.skill_store import StoredSkillEvaluation
from agenten.agent_factory.state_machine import (
    FactoryAction,
    FactoryLifecycleError,
    FactoryProjection,
    apply_block,
    next_action,
    with_terminal_decision,
)


class FactoryRepositoryError(RuntimeError):
    """The append-only factory record cannot be accepted."""


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
    _terminal_decisions_by_job: dict[UUID, FactoryTerminalDecision] = field(
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

    def append_terminal_decision(self, decision: FactoryTerminalDecision) -> bool:
        job = self.job(decision.job_id)
        if (
            decision.correlation_id != job.correlation_id
            or decision.subject_version != job.subject_version
        ):
            raise FactoryRepositoryError("terminal decision does not match factory job")
        existing = self._terminal_decisions_by_job.get(decision.job_id)
        if existing is not None:
            if existing != decision:
                raise FactoryRepositoryError("terminal decision already exists with different content")
            return False
        self._terminal_decisions_by_job[decision.job_id] = decision
        return True

    def terminal_decision_for_job(self, job_id: UUID) -> FactoryTerminalDecision | None:
        self.job(job_id)
        return self._terminal_decisions_by_job.get(job_id)


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
        apply_block(
            projection,
            block,
            evaluation=evaluation,
            release_decision=release_decision,
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
            projection = apply_block(
                projection,
                stored_block,
                evaluation=evaluation,
                release_decision=release_decision,
            )
        terminal_decision = self.terminal_decision_for_job(job_id)
        if terminal_decision is not None:
            projection = with_terminal_decision(projection, terminal_decision)
        return projection

    def next_action(self, job_id: UUID, *, now: datetime | None = None) -> FactoryAction:
        projection = self.projection(job_id)
        evaluation = (
            self.evaluation_for_job(job_id)
            if projection.phase is not None and projection.phase.value == "quality_reviewed"
            else None
        )
        return next_action(projection, evaluation=evaluation, now=now).model_copy(update={"job_id": job_id})

    def record_terminal_decision(self, decision: FactoryTerminalDecision) -> bool:
        job = self._repository.job(decision.job_id)
        if (
            decision.correlation_id != job.correlation_id
            or decision.subject_version != job.subject_version
        ):
            raise FactoryRepositoryError("terminal decision does not match factory job")
        existing = self.terminal_decision_for_job(decision.job_id)
        if existing is not None:
            if existing != decision:
                raise FactoryRepositoryError("terminal decision already exists with different content")
            return False
        append = getattr(self._repository, "append_terminal_decision", None)
        if append is None:
            raise FactoryRepositoryError("factory repository does not support terminal decisions")
        return bool(append(decision))

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

    def terminal_decision_for_job(self, job_id: UUID) -> FactoryTerminalDecision | None:
        lookup = getattr(self._repository, "terminal_decision_for_job", None)
        if lookup is None:
            return None
        return lookup(job_id)

    def _existing_block(self, incoming: FactoryEvidenceBlock) -> FactoryEvidenceBlock | None:
        return next(
            (
                block
                for block in self._repository.blocks(incoming.job_id)
                if block.event_id == incoming.event_id
            ),
            None,
        )
