"""Explicit dependency boundary for the Minibook V3 creation pipeline."""

from __future__ import annotations

from typing import Any, Protocol

from .artifact_store import FilesystemCreationArtifactStore
from .contracts import (
    CreationFailure,
    CreationJobV3,
    CreationResultV2,
    FactoryBuildAssignmentV2,
    FactoryBuildEffectV2,
    ToolGapMarkerV1,
    validate_build_effect_binding,
)
from .job_store import CreationJobStore
from .runner import StepOutcome
from .team_package import (
    seal_team_package,
    validate_team_package,
    verify_sealed_team_package,
)


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


class TeamPackageV3Validator:
    """Production validator that turns a durable build effect into sealed CAS output."""

    def build_assignment(
        self, job: CreationJobV3, checkpoint_version: int, effect_key: str
    ) -> FactoryBuildAssignmentV2:
        return FactoryBuildAssignmentV2(
            assignment_id=job.command_id,
            creation_job_id=job.creation_job_id,
            factory_job_id=job.factory_job_id,
            correlation_id=job.correlation_id,
            subject_version=job.subject_version,
            checkpoint_version=checkpoint_version,
            attempt=job.attempt,
            effect_key=effect_key,
            workspace_ref=f"workspace://captain/minibook-v3/{job.creation_job_id}",
            workspace_policy_sha256=job.workspace_policy_ref.sha256,
            required_agent_ids=job.required_agent_ids,
            ordered_handoffs=job.ordered_handoffs,
            tool_requirements=job.tool_requirements,
            allowed_output_roots=("src", "tests", "evidence"),
            allowed_commands=(
                ("python", "-m", "compileall", "src"),
                ("python", "-m", "pytest"),
            ),
            limits={
                "max_wall_clock_seconds": 300,
                "max_provider_cost_usd": 5.0,
                "max_retries": 0,
                "max_files": 4096,
            },
            lease_ref=job.lease_ref,
            deadline_at=job.deadline_at,
        )

    async def validate_build_effect(
        self,
        job: CreationJobV3,
        assignment: FactoryBuildAssignmentV2,
        effect: FactoryBuildEffectV2,
        artifact_store: FilesystemCreationArtifactStore,
        snapshot: dict[str, Any],
    ) -> StepOutcome:
        validate_build_effect_binding(assignment, effect)
        validated = validate_team_package(
            authority_job=job,
            authority_assignment=assignment,
            authority_effect=effect,
            compiled_spec_ref=job.compiled_spec_ref,
            team_manifest_ref=effect.preliminary_team_manifest_ref,
            source_archive_ref=effect.source_archive_ref,
            artifact_store=artifact_store,
            tool_gaps=effect.tool_gaps,
            hermes_usage_receipt_ref=effect.hermes_usage_receipt_ref,
            codex_build_receipt_ref=effect.codex_build_receipt_ref,
            build_evidence_refs=effect.build_evidence_refs,
            test_evidence_refs=effect.test_evidence_refs,
        )
        sealed = seal_team_package(validated, artifact_store=artifact_store)
        refs = {
            "team_manifest_ref": sealed.team_manifest_ref,
            "candidate_manifest_ref": sealed.candidate_manifest_ref,
            "source_archive_ref": sealed.source_archive_ref,
            "package_manifest_ref": sealed.package_manifest_ref,
            "hermes_usage_receipt_ref": sealed.hermes_usage_receipt_ref,
            "codex_build_receipt_ref": sealed.codex_build_receipt_ref,
            "build_evidence_refs": sealed.build_evidence_refs,
            "real_case_evidence_refs": sealed.real_case_evidence_refs,
            "cas_readback_receipt_ref": sealed.readback_receipt_ref,
        }
        serialized = {
            key: (
                [item.model_dump(mode="json") for item in value]
                if isinstance(value, tuple)
                else value.model_dump(mode="json")
            )
            for key, value in refs.items()
        }
        return StepOutcome(
            snapshot=snapshot
            | {
                "sealed_team_package": serialized,
                "authority_assignment": assignment.model_dump(mode="json"),
                "tool_gaps": [gap.model_dump(mode="json", by_alias=True) for gap in effect.tool_gaps],
            }
        )

    async def validate(
        self,
        job: CreationJobV3,
        snapshot: dict[str, Any],
        artifact_store: FilesystemCreationArtifactStore,
    ) -> StepOutcome:
        package = snapshot.get("sealed_team_package")
        if not isinstance(package, dict):
            raise ValueError("V3 sealed team package is unavailable")
        try:
            authority_assignment = FactoryBuildAssignmentV2.model_validate(
                snapshot.get("authority_assignment")
            )
        except Exception as exc:
            raise ValueError("V3 assignment authority is unavailable") from exc
        validated_snapshot = snapshot | {"team_package_validated": True}
        result = self.assemble_result(job, validated_snapshot)
        if result.status != "succeeded":
            raise ValueError("V3 sealed team package is incomplete")
        verify_sealed_team_package(
            result,
            authority_job=job,
            authority_assignment=authority_assignment,
            artifact_store=artifact_store,
        )
        return StepOutcome(snapshot=validated_snapshot)

    def assemble_result(
        self, job: CreationJobV3, snapshot: dict[str, Any]
    ) -> CreationResultV2:
        package = snapshot.get("sealed_team_package")
        if not isinstance(package, dict) or snapshot.get("team_package_validated") is not True:
            return CreationResultV2(
                creation_job_id=job.creation_job_id,
                factory_job_id=job.factory_job_id,
                correlation_id=job.correlation_id,
                subject_version=job.subject_version,
                attempt=job.attempt,
                status="blocked",
                failure=CreationFailure(
                    code="validation_failed",
                    summary="sealed team package evidence is incomplete",
                ),
            )
        return CreationResultV2(
            creation_job_id=job.creation_job_id,
            factory_job_id=job.factory_job_id,
            correlation_id=job.correlation_id,
            subject_version=job.subject_version,
            attempt=job.attempt,
            status="succeeded",
            team_manifest_ref=package.get("team_manifest_ref"),
            candidate_manifest_ref=package.get("candidate_manifest_ref"),
            source_archive_ref=package.get("source_archive_ref"),
            package_manifest_ref=package.get("package_manifest_ref"),
            hermes_usage_receipt_ref=package.get("hermes_usage_receipt_ref"),
            codex_build_receipt_ref=package.get("codex_build_receipt_ref"),
            build_evidence_refs=package.get("build_evidence_refs", ()),
            real_case_evidence_refs=package.get("real_case_evidence_refs", ()),
            cas_readback_receipt_ref=package.get("cas_readback_receipt_ref"),
            tool_gaps=tuple(
                ToolGapMarkerV1.model_validate(value)
                for value in snapshot.get("tool_gaps", ())
            ),
        )


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
                completion_status="packaging",
            )
        validate = getattr(self.validator, "validate", None)
        if not callable(validate):
            raise RuntimeError("V3 creation validator is not configured")
        outcome = await validate(job, prior_snapshot, self.artifact_store)
        if not isinstance(outcome, StepOutcome):
            raise TypeError("V3 creation validator returned an invalid outcome")
        return StepOutcome(
            snapshot=outcome.snapshot,
            effect_receipt=outcome.effect_receipt,
            yield_status=outcome.yield_status,
            yield_transition_persisted=outcome.yield_transition_persisted,
            completion_status="readback",
        )

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


__all__ = ["TeamPackageV3Validator", "V3CreationPipeline", "V3CreationValidator"]
