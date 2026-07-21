"""Named, resumable step adapter around the legacy SwarmPipeline."""
from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from .contracts import ArtifactRef, CreationFailure, CreationJobV1, CreationResultV1
from .runner import StepOutcome


class SwarmStep(str, Enum):
    MANAGER = "manager"
    CATALOG = "catalog"
    ARCHITECT = "architect"
    CODER = "coder"
    REVIEWER = "reviewer"
    TESTER = "tester"
    VALIDATOR = "validator"
    BUILDER = "builder"
    EXECUTOR = "executor"
    OUTPUT_EVALUATION = "output_evaluation"
    TODO_IMPLEMENTATION = "todo_implementation"
    TOOLFORGE = "toolforge"
    FEEDBACK_LOOP = "feedback_loop"
    EVALUATION_REPORT = "evaluation_report"
    EXPORT = "export"


PIPELINE_STEP_ORDER = tuple(SwarmStep)


class SwarmSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    creation_job_id: object
    completed_steps: tuple[str, ...] = ()
    output_digests: dict[str, str] = Field(default_factory=dict)
    artifact_bindings: dict[str, ArtifactRef] = Field(default_factory=dict)
    decisions: dict[str, str] = Field(default_factory=dict)
    external_receipt_ids: dict[str, str] = Field(default_factory=dict)
    package_manifest_ref: ArtifactRef | None = None
    skill_usage_receipt_ref: ArtifactRef | None = None


_KNOWN_FAILURES = {
    "DocumentationUnavailable": "documentation_unavailable",
    "ToolResolutionError": "tool_unresolved",
    "CodexExecutionError": "codex_failed",
    "N8nExecutionError": "n8n_failed",
    "BuildError": "build_failed",
    "ValidationError": "validation_failed",
}


def translate_creation_failure(exc: Exception) -> CreationFailure:
    code = _KNOWN_FAILURES.get(type(exc).__name__, "internal_error")
    return CreationFailure(
        code=code,
        summary="creation step failed",
        exception_type=type(exc).__name__,
    )


class SwarmPipelineAdapter:
    steps = tuple(step.value for step in PIPELINE_STEP_ORDER)
    effectful_steps = frozenset(
        {
            SwarmStep.CATALOG.value,
            SwarmStep.CODER.value,
            SwarmStep.BUILDER.value,
            SwarmStep.EXECUTOR.value,
            SwarmStep.TODO_IMPLEMENTATION.value,
            SwarmStep.TOOLFORGE.value,
            SwarmStep.EXPORT.value,
        }
    )

    def __init__(self, pipeline_factory: Callable[[SwarmSnapshot], Any], *, session: object) -> None:
        self._pipeline_factory = pipeline_factory
        self._session = session

    async def run_step(
        self,
        job: CreationJobV1,
        step: str,
        prior_snapshot: dict[str, Any],
        effect_key: str,
        accepted_effect: dict[str, Any] | None,
    ) -> StepOutcome:
        named_step = SwarmStep(step)
        prior = SwarmSnapshot.model_validate(prior_snapshot)
        pipeline = self._pipeline_factory(prior)
        output: Any = accepted_effect
        if accepted_effect is None:
            output = await self._dispatch(pipeline, named_step, job)
        digest = hashlib.sha256(
            json.dumps(output, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        completed = tuple(dict.fromkeys((*prior.completed_steps, named_step.value)))
        receipts = dict(prior.external_receipt_ids)
        receipt = None
        if named_step.value in self.effectful_steps:
            receipt = accepted_effect or {
                "receipt_id": effect_key,
                "output_sha256": digest,
            }
            receipts[named_step.value] = str(receipt["receipt_id"])
        snapshot = prior.model_copy(
            update={
                "completed_steps": completed,
                "output_digests": prior.output_digests | {named_step.value: digest},
                "external_receipt_ids": receipts,
            }
        )
        return StepOutcome(snapshot=snapshot.model_dump(mode="json"), effect_receipt=receipt)

    async def _dispatch(self, pipeline: Any, step: SwarmStep, job: CreationJobV1) -> Any:
        if step is SwarmStep.MANAGER:
            return await pipeline.step_swarm_manager(self._session, str(job.input_ref.uri))
        method_names = {
            SwarmStep.CATALOG: "step_catalog",
            SwarmStep.ARCHITECT: "step_architect",
            SwarmStep.CODER: "step_coder",
            SwarmStep.REVIEWER: "step_reviewer",
            SwarmStep.TESTER: "step_tester",
            SwarmStep.VALIDATOR: "step_validator",
            SwarmStep.BUILDER: "step_builder",
            SwarmStep.EXECUTOR: "step_executor",
            SwarmStep.OUTPUT_EVALUATION: "step_output_eval",
            SwarmStep.TODO_IMPLEMENTATION: "step_todo_implement",
            SwarmStep.TOOLFORGE: "step_toolforge",
            SwarmStep.FEEDBACK_LOOP: "step_feedback_loop",
            SwarmStep.EVALUATION_REPORT: "step_eval_reporter",
            SwarmStep.EXPORT: "step_export",
        }
        method = getattr(pipeline, method_names[step])
        return await method(self._session)

    def assemble_result(
        self, job: CreationJobV1, snapshot: dict[str, Any]
    ) -> CreationResultV1:
        state = SwarmSnapshot.model_validate(snapshot)
        if state.package_manifest_ref is None or state.skill_usage_receipt_ref is None:
            return CreationResultV1(
                creation_job_id=job.creation_job_id,
                correlation_id=job.correlation_id,
                subject_version=job.subject_version,
                attempt=job.attempt,
                status="blocked",
                failure=CreationFailure(
                    code="validation_failed",
                    summary="creation package evidence incomplete",
                ),
            )
        return CreationResultV1(
            creation_job_id=job.creation_job_id,
            correlation_id=job.correlation_id,
            subject_version=job.subject_version,
            attempt=job.attempt,
            status="succeeded",
            package_manifest_ref=state.package_manifest_ref,
            artifact_refs=tuple(state.artifact_bindings.values()),
            skill_usage_receipt_ref=state.skill_usage_receipt_ref,
        )
