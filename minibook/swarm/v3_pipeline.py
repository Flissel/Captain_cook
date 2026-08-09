"""Explicit dependency boundary for the Minibook V3 creation pipeline."""

from __future__ import annotations

from typing import Any, Protocol

from .artifact_store import FilesystemCreationArtifactStore
from .contracts import CreationJobV3, CreationResultV2
from .job_store import CreationJobStore
from .runner import StepOutcome


class V3CreationValidator(Protocol):
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

    steps = ("validate",)
    effectful_steps = frozenset[str]()

    def __init__(
        self,
        *,
        store: CreationJobStore,
        artifact_store: FilesystemCreationArtifactStore,
        validator: V3CreationValidator,
    ) -> None:
        if store is None or artifact_store is None or validator is None:
            raise RuntimeError("V3 creation pipeline dependencies are required")
        if not callable(getattr(validator, "validate", None)) or not callable(
            getattr(validator, "assemble_result", None)
        ):
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
        del effect_key, accepted_effect
        if not isinstance(job, CreationJobV3) or step != "validate":
            raise ValueError("V3 creation pipeline received an unsupported job or step")
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
