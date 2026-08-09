"""Explicit dependency boundary for the Minibook V3 creation pipeline."""

from __future__ import annotations

from typing import Any, Protocol

from .artifact_store import FilesystemCreationArtifactStore
from .contracts import (
    CreationJobV3,
    CreationResultV2,
    FactoryBuildAssignmentV2,
    FactoryBuildEffectV2,
)
from .job_store import CreationJobStore
from .runner import StepOutcome


class V3CreationValidator(Protocol):
    def build_assignment(
        self, job: CreationJobV3, checkpoint_version: int, effect_key: str
    ) -> FactoryBuildAssignmentV2: ...

    async def validate_build_effect(
        self,
        job: CreationJobV3,
        assignment: FactoryBuildAssignmentV2,
        effect: FactoryBuildEffectV2,
        artifact_store: FilesystemCreationArtifactStore,
        snapshot: dict[str, Any],
    ) -> StepOutcome: ...

    async def validate(
        self,
        job: CreationJobV3,
        snapshot: dict[str, Any],
        artifact_store: FilesystemCreationArtifactStore,
    ) -> StepOutcome: ...

    def assemble_result(
        self, job: CreationJobV3, snapshot: dict[str, Any]
    ) -> CreationResultV2: ...


class V3CreationPipeline:
    """V3 runner adapter composed only from explicit local authority ports.

    Team/package validation is supplied through the validator port and is
    intentionally implemented by the next plan task.
    """

    steps = ("planning", "build_handoff", "validate")
    effectful_steps = frozenset({"build_handoff"})

    def __init__(
        self,
        *,
        store: CreationJobStore,
        artifact_store: FilesystemCreationArtifactStore,
        validator: V3CreationValidator,
    ) -> None:
        if store is None or artifact_store is None or validator is None:
            raise RuntimeError("V3 creation pipeline dependencies are required")
        required_methods = (
            "build_assignment",
            "validate_build_effect",
            "validate",
            "assemble_result",
        )
        if any(not callable(getattr(validator, name, None)) for name in required_methods):
            raise RuntimeError("V3 creation validator is not configured")
        self.store = store
        self.artifact_store = artifact_store
        self.validator = validator

    async def run_step(
        self,
        job: CreationJobV3,
        step: str,
        prior_snapshot: dict[str, Any],
        effect_key: str,
        accepted_effect: dict[str, Any] | None,
    ) -> StepOutcome:
        if not isinstance(job, CreationJobV3) or step not in self.steps:
            raise ValueError("V3 creation pipeline received an unsupported job or step")
        if step == "planning":
            return StepOutcome(snapshot=prior_snapshot)
        if step == "build_handoff":
            assignment = self.store.build_assignment_for_job(job.creation_job_id)
            if assignment is None:
                build_assignment = getattr(self.validator, "build_assignment")
                assignment = build_assignment(
                    job,
                    self.store.progress(job.creation_job_id).version,
                    effect_key,
                )
                if not isinstance(assignment, FactoryBuildAssignmentV2):
                    raise TypeError("V3 assignment builder returned an invalid assignment")
                assignment = self.store.publish_build_assignment(assignment)
            durable_effect = self.store.build_effect(assignment.assignment_id)
            supplied_effect = (
                None
                if accepted_effect is None
                else FactoryBuildEffectV2.model_validate(accepted_effect)
            )
            if durable_effect is None:
                if supplied_effect is not None:
                    raise ValueError("accepted build effect is not durable")
                return StepOutcome(
                    snapshot=prior_snapshot,
                    yield_status="awaiting_build",
                    yield_transition_persisted=True,
                )
            if supplied_effect is not None and supplied_effect != durable_effect:
                raise ValueError("accepted build effect differs from durable effect")
            validate_effect = getattr(self.validator, "validate_build_effect")
            outcome = await validate_effect(
                job,
                assignment,
                durable_effect,
                self.artifact_store,
                prior_snapshot,
            )
            if not isinstance(outcome, StepOutcome):
                raise TypeError("V3 build-effect validator returned an invalid outcome")
            return StepOutcome(
                snapshot=outcome.snapshot,
                effect_receipt=durable_effect.model_dump(mode="json", by_alias=True),
            )
        validate = getattr(self.validator, "validate", None)
        if not callable(validate):
            raise RuntimeError("V3 creation validator is not configured")
        outcome = await validate(job, prior_snapshot, self.artifact_store)
        if not isinstance(outcome, StepOutcome):
            raise TypeError("V3 creation validator returned an invalid outcome")
        return outcome

    def assemble_result(
        self, job: CreationJobV3, snapshot: dict[str, Any]
    ) -> CreationResultV2:
        assemble = getattr(self.validator, "assemble_result", None)
        if not callable(assemble):
            raise RuntimeError("V3 creation result assembler is not configured")
        result = assemble(job, snapshot)
        if not isinstance(result, CreationResultV2):
            raise TypeError("V3 creation validator returned an invalid result")
        return result


__all__ = ["V3CreationPipeline", "V3CreationValidator"]
