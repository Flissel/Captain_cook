"""Checkpointed execution of one persisted creation job."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID

from .contracts import CreationFailure, CreationJobV1, CreationJobV2, CreationJobV3, CreationResultV1, CreationResultV2
from .job_store import CreationJobStore


@dataclass(frozen=True)
class StepOutcome:
    snapshot: dict[str, Any]
    effect_receipt: dict[str, Any] | None = None
    yield_status: str | None = None
    yield_transition_persisted: bool = False


class PipelineStepPort(Protocol):
    steps: tuple[str, ...]
    effectful_steps: frozenset[str]

    async def run_step(
        self,
        job: CreationJobV1 | CreationJobV2 | CreationJobV3,
        step: str,
        prior_snapshot: dict[str, Any],
        effect_key: str,
        accepted_effect: dict[str, Any] | None,
    ) -> StepOutcome: ...

    def assemble_result(
        self, job: CreationJobV1, snapshot: dict[str, Any]
    ) -> CreationResultV1 | CreationResultV2: ...


class CreationRunner:
    def __init__(self, store: CreationJobStore, pipeline: PipelineStepPort) -> None:
        self.store = store
        self.pipeline = pipeline

    def _terminal(
        self, job: CreationJobV1 | CreationJobV2 | CreationJobV3, status: str, failure: CreationFailure
    ) -> CreationResultV1 | CreationResultV2:
        if isinstance(job, CreationJobV3):
            return CreationResultV2(creation_job_id=job.creation_job_id, factory_job_id=job.factory_job_id, correlation_id=job.correlation_id, subject_version=job.subject_version, attempt=job.attempt, status=status, failure=failure)
        return CreationResultV1(
            creation_job_id=job.creation_job_id,
            correlation_id=job.correlation_id,
            subject_version=job.subject_version,
            attempt=job.attempt,
            status=status,
            failure=failure,
        )

    @staticmethod
    def _effect_key(job: CreationJobV1 | CreationJobV2 | CreationJobV3, step: str) -> str:
        identity = ":".join(
            (
                str(job.creation_job_id),
                str(job.subject_version),
                str(job.attempt),
                job.idempotency_key,
                step,
            )
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    async def run_slice(self, job_id: UUID) -> CreationResultV1 | CreationResultV2 | None:
        existing = self.store.result(job_id)
        if existing is not None:
            return existing
        job = self.store.job(job_id)
        progress = self.store.progress(job_id)
        if progress.status == "cancelled":
            return self.store.finish(
                self._terminal(job, "cancelled", CreationFailure(code="cancelled", summary="creation cancelled")),
                expected_version=progress.version,
            )
        if datetime.now(timezone.utc) >= job.deadline_at:
            return self.store.finish(
                self._terminal(
                    job,
                    "blocked",
                    CreationFailure(code="deadline_expired", summary="creation deadline expired"),
                ),
                expected_version=progress.version,
            )
        if progress.status == "awaiting_user_input":
            return None
        completed = self.store.completed_steps(job_id)
        snapshot = self.store.snapshot(job_id)
        for step in self.pipeline.steps:
            if step in completed:
                continue
            step_expected_version = self.store.progress(job_id).version
            effect_key = self._effect_key(job, step)
            accepted = (
                self.store.external_effect(job_id, effect_key)
                if step in self.pipeline.effectful_steps
                else None
            )
            outcome = await self.pipeline.run_step(
                job, step, snapshot, effect_key, accepted
            )
            if outcome.yield_status is not None:
                if outcome.yield_status not in {"awaiting_build", "awaiting_user_input"}:
                    raise ValueError("unsupported creation yield status")
                if outcome.yield_transition_persisted:
                    persisted = self.store.progress(job_id)
                    if (
                        persisted.status != outcome.yield_status
                        or persisted.version != step_expected_version + 1
                    ):
                        raise ValueError("pipeline persisted an invalid creation yield")
                else:
                    self.store.transition(job_id, outcome.yield_status, expected_version=step_expected_version, checkpoint=step, snapshot=outcome.snapshot)
                return None
            if step in self.pipeline.effectful_steps and outcome.effect_receipt is not None:
                self.store.record_external_effect(
                    job_id, effect_key, outcome.effect_receipt,
                    expected_version=step_expected_version,
                )
            self.store.complete_step(
                job_id, step, effect_key, outcome.snapshot,
                expected_version=step_expected_version,
            )
            snapshot = outcome.snapshot
        return self.store.finish(
            self.pipeline.assemble_result(job, snapshot),
            expected_version=self.store.progress(job_id).version,
        )

    def cancel(self, job_id: UUID, expected_version: int):
        return self.store.cancel(job_id, expected_version)

    def result(self, job_id: UUID) -> CreationResultV1 | None:
        return self.store.result(job_id)
