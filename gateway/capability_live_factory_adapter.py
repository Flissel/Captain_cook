"""Bind capability-live Factory stages to the current Captain workflow.

The adapter never fabricates a Factory job, promotion, benchmark, or Hermes
receipt.  It advances only the existing production dispatch runner and derives
its redacted observations from Gateway's persisted workflow state.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agenten.agent_factory.capability_live_runner import (
    CapabilityLiveStage,
    CapabilityLiveStageRequest,
)
from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryPhase
from agenten.agent_factory.skill_workflow_contracts import (
    CodebaseInventoryV1,
    CodexBuildEvidenceV1,
    FactorySkillStep,
    TeamEvaluationV1,
    TeamExecutionEvidenceV1,
)
from agenten.agent_factory.state_machine import FactoryActionKind, FactoryLifecycleStatus
from agenten.agent_factory.workflow_promotion import build_release_workflow_promotion
from agenten.agent_runtime.contracts import ArtifactRef
from gateway.agent_factory_live_composition import AgentFactoryLiveComposition
from gateway.capability_live_pipeline import (
    CapabilityFactoryAuthority,
    CapabilityLiveAuthorityUnavailable,
)
from gateway.contracts import FactoryJobProjection


class CapabilityFactoryJobBinding(BaseModel):
    """Captain-provisioned immutable binding for one canonical live input."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    input_id: str = Field(min_length=1)
    source_ref: ArtifactRef
    job: AgentFactoryJobV3
    suite_ref: ArtifactRef
    forge_requested_event_id: UUID
    n8n_work_batch_id: str | None = Field(default=None, min_length=1)


class CapabilityFactoryProvisioningPort(Protocol):
    """Resolve a Captain-created V3 Factory job, never create a replacement."""

    def provision(
        self, request: CapabilityLiveStageRequest
    ) -> CapabilityFactoryJobBinding | None: ...

    def binding_for(
        self, request: CapabilityLiveStageRequest
    ) -> CapabilityFactoryJobBinding | None: ...


class CurrentCapabilityFactoryAdapter:
    """Advance only real Captain Factory boundaries for one capability input."""

    def __init__(
        self,
        *,
        composition: AgentFactoryLiveComposition,
        provisioning: CapabilityFactoryProvisioningPort,
        clock: Callable[[], datetime],
    ) -> None:
        self._composition = composition
        self._provisioning = provisioning
        self._clock = clock

    def job_id_for(self, request: CapabilityLiveStageRequest) -> UUID | None:
        binding = self._provisioning.binding_for(request)
        if binding is None:
            return None
        self._require_binding(request, binding)
        return binding.job.job_id

    async def scaffold(
        self, request: CapabilityLiveStageRequest
    ) -> CapabilityFactoryAuthority | None:
        # The first stage is the only admission point: it may create the
        # current Captain V3 binding, while later stages only replay it.
        binding = self._binding_or_provision(request)
        self._require_released_skills(binding.job)
        await self._advance(
            binding,
            FactoryPhase.BLUEPRINT_CREATED,
            FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
        )
        return self._stage_authority(
            request,
            binding,
            CapabilityLiveStage.SCAFFOLD_AND_TEST_HARNESS,
            FactoryPhase.BLUEPRINT_CREATED,
        )

    def _binding_or_provision(
        self,
        request: CapabilityLiveStageRequest,
    ) -> CapabilityFactoryJobBinding:
        binding = self._provisioning.binding_for(request)
        if binding is None:
            binding = self._provisioning.provision(request)
        if binding is None:
            raise CapabilityLiveAuthorityUnavailable(
                "Factory provisioning binding is unavailable"
            )
        self._require_binding(request, binding)
        return binding

    async def implement(
        self, request: CapabilityLiveStageRequest
    ) -> CapabilityFactoryAuthority | None:
        binding = self._binding(request, provision=False)
        if not self._has_phase(
            self._blocks(binding.job.job_id), FactoryPhase.TOOL_CANDIDATE_TESTED
        ):
            binding = self._binding(request, provision=True)
        self._require_released_skills(binding.job)
        await self._advance(
            binding,
            FactoryPhase.TOOL_CANDIDATE_TESTED,
            FactoryActionKind.SUBMIT_FORGE_JOB,
        )
        return self._stage_authority(
            request,
            binding,
            CapabilityLiveStage.CODEX_IMPLEMENTATION,
            FactoryPhase.TOOL_CANDIDATE_TESTED,
        )

    async def seal(
        self, request: CapabilityLiveStageRequest
    ) -> CapabilityFactoryAuthority | None:
        binding = self._binding(request, provision=False)
        self._require_released_skills(binding.job)
        await self._advance(
            binding,
            FactoryPhase.BUILD_PASSED,
            FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
        )
        return self._stage_authority(
            request,
            binding,
            CapabilityLiveStage.SEAL,
            FactoryPhase.BUILD_PASSED,
        )

    async def promote(
        self, request: CapabilityLiveStageRequest
    ) -> CapabilityFactoryAuthority | None:
        binding = self._binding(request, provision=False)
        self._require_released_skills(binding.job)
        projection = self._composition.projection(binding.job.job_id)
        blocks = self._blocks(binding.job.job_id)
        if not self._has_phase(blocks, FactoryPhase.CAPABILITY_PROMOTED):
            if (
                projection.status is not FactoryLifecycleStatus.RUNNING
                or projection.phase is not FactoryPhase.QUALITY_REVIEWED
            ):
                raise CapabilityLiveAuthorityUnavailable(
                    "Factory quality review is not promotable"
                )
            self._release_evidence(request, binding)
            snapshot = FactoryJobProjection.model_construct(
                job=binding.job,
                blocks=blocks,
                leases=(),
                projection=projection,
            )
            try:
                promotion = build_release_workflow_promotion(
                    snapshot,
                    occurred_at=self._clock(),
                )
                self._composition.coordinator.record(promotion)
            except (RuntimeError, ValueError) as exc:
                raise CapabilityLiveAuthorityUnavailable(
                    "Captain promotion authority is unavailable"
                ) from exc
        return self._released_authority(request, binding)

    async def released(
        self, request: CapabilityLiveStageRequest
    ) -> CapabilityFactoryAuthority | None:
        return self._released_authority(request, self._binding(request, provision=False))

    def _binding(
        self,
        request: CapabilityLiveStageRequest,
        *,
        provision: bool,
    ) -> CapabilityFactoryJobBinding:
        binding = (
            self._provisioning.provision(request)
            if provision
            else self._provisioning.binding_for(request)
        )
        if binding is None:
            raise CapabilityLiveAuthorityUnavailable(
                "Factory provisioning binding is unavailable"
            )
        self._require_binding(request, binding)
        return binding

    @staticmethod
    def _require_binding(
        request: CapabilityLiveStageRequest,
        binding: CapabilityFactoryJobBinding,
    ) -> None:
        job = binding.job
        if (
            binding.input_id != request.input.input_id
            or binding.source_ref != request.input.source_ref
            or job.input_ref != request.input.source_ref
            or job.correlation_id != request.correlation_id
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "Factory provisioning binding does not match the input source"
            )
        if request.suite_ref is not None and request.suite_ref != binding.suite_ref:
            raise CapabilityLiveAuthorityUnavailable(
                "Factory provisioning binding changed the benchmark suite"
            )
        if request.input.integration_intent == "n8n" and not binding.n8n_work_batch_id:
            raise CapabilityLiveAuthorityUnavailable(
                "Factory provisioning binding lacks the Captain n8n WorkBatch"
            )

    def _require_released_skills(self, job: AgentFactoryJobV3) -> None:
        try:
            for step in FactorySkillStep:
                self._composition.repository.released_for(job, step)
        except (RuntimeError, ValueError) as exc:
            raise CapabilityLiveAuthorityUnavailable(
                "Factory released skill assignment is unavailable"
            ) from exc

    async def _advance(
        self,
        binding: CapabilityFactoryJobBinding,
        target: FactoryPhase,
        stop_before: FactoryActionKind,
    ) -> None:
        if self._has_phase(self._blocks(binding.job.job_id), target):
            return
        try:
            await self._composition.run(
                binding.job.job_id,
                stop_before_action=stop_before,
            )
        except (RuntimeError, ValueError) as exc:
            raise CapabilityLiveAuthorityUnavailable(
                "current Factory dispatch boundary is unavailable"
            ) from exc
        if not self._has_phase(self._blocks(binding.job.job_id), target):
            raise CapabilityLiveAuthorityUnavailable(
                "current Factory dispatch did not reach its required boundary"
            )

    def _stage_authority(
        self,
        request: CapabilityLiveStageRequest,
        binding: CapabilityFactoryJobBinding,
        stage: CapabilityLiveStage,
        phase: FactoryPhase,
    ) -> CapabilityFactoryAuthority:
        blocks = self._blocks(binding.job.job_id)
        block = self._one_phase(blocks, phase)
        hermes_result_ref = request.hermes_result_ref or self._first_artifact_ref(
            self._artifacts(binding.job.job_id)
        )
        if hermes_result_ref is None:
            raise CapabilityLiveAuthorityUnavailable(
                "Factory stage lacks a Hermes result artifact"
            )
        return CapabilityFactoryAuthority(
            stage=stage,
            input_id=request.input.input_id,
            source_ref=request.input.source_ref,
            job_id=binding.job.job_id,
            correlation_id=binding.job.correlation_id,
            subject_version=binding.job.subject_version,
            capability_id=binding.job.required_capability,
            capability_version=binding.job.subject_version,
            suite_ref=binding.suite_ref,
            hermes_result_ref=hermes_result_ref,
            evidence_refs=self._evidence_refs((*blocks, *self._artifacts(binding.job.job_id))),
            booked_cost_usd=self._booked_cost(binding.job.job_id),
            release_authority_id=(block.event_id if phase is FactoryPhase.CAPABILITY_PROMOTED else None),
        )

    def _released_authority(
        self,
        request: CapabilityLiveStageRequest,
        binding: CapabilityFactoryJobBinding,
    ) -> CapabilityFactoryAuthority:
        blocks = self._blocks(binding.job.job_id)
        promotion = self._one_phase(blocks, FactoryPhase.CAPABILITY_PROMOTED)
        if self._composition.projection(binding.job.job_id).status is not FactoryLifecycleStatus.READY_TO_USE:
            raise CapabilityLiveAuthorityUnavailable(
                "Factory promotion has not become ready_to_use"
            )
        artifacts = self._artifacts(binding.job.job_id)
        evaluation, summary, runs, recovery = self._release_evidence(request, binding)
        hermes_result_ref = request.hermes_result_ref or evaluation.artifact_ref
        return CapabilityFactoryAuthority(
            stage=CapabilityLiveStage.PROMOTE,
            input_id=request.input.input_id,
            source_ref=request.input.source_ref,
            job_id=binding.job.job_id,
            correlation_id=binding.job.correlation_id,
            subject_version=binding.job.subject_version,
            capability_id=binding.job.required_capability,
            capability_version=binding.job.subject_version,
            suite_ref=binding.suite_ref,
            hermes_result_ref=hermes_result_ref,
            evidence_refs=self._evidence_refs((*blocks, *artifacts)),
            booked_cost_usd=self._booked_cost(binding.job.job_id),
            controlled_recovery_id=(
                None if recovery is None else recovery.invocation_id
            ),
            normal_trace_ids=tuple(run.invocation_id for run in runs),
            benchmark_authority_id=summary.summary_id,
            benchmark_summary_ref=summary.artifact_ref,
            release_authority_id=promotion.event_id,
        )

    def _release_evidence(
        self,
        request: CapabilityLiveStageRequest,
        binding: CapabilityFactoryJobBinding,
    ) -> tuple[object, object, tuple[TeamExecutionEvidenceV1, ...], object | None]:
        artifacts = self._artifacts(binding.job.job_id)
        evaluation = self._one_artifact(artifacts, TeamEvaluationV1)
        summary = self._composition.repository.business_benchmark_summary_by_artifact(
            evaluation.benchmark_summary_ref
        )
        runs = tuple(
            artifact
            for artifact in artifacts
            if isinstance(artifact, TeamExecutionEvidenceV1)
            and artifact.status == "succeeded"
        )
        if len(runs) < 3 or len({run.invocation_id for run in runs}) != len(runs):
            raise CapabilityLiveAuthorityUnavailable(
                "Factory release lacks three distinct successful team runs"
            )
        build = self._one_artifact(artifacts, CodexBuildEvidenceV1)
        recovery = getattr(build, "runtime_retry_binding", None)
        if request.controlled_recovery_required and recovery is None:
            raise CapabilityLiveAuthorityUnavailable(
                "Factory release lacks controlled recovery evidence"
            )
        if summary is None:
            raise CapabilityLiveAuthorityUnavailable(
                "Factory release lacks a current business benchmark summary"
            )
        return evaluation, summary, runs, recovery

    def _blocks(self, job_id: UUID) -> tuple[object, ...]:
        return tuple(self._composition.repository.blocks(job_id))

    def _artifacts(self, job_id: UUID) -> tuple[object, ...]:
        return tuple(self._composition.repository.workflow_artifacts(job_id))

    @staticmethod
    def _has_phase(blocks: tuple[object, ...], phase: FactoryPhase) -> bool:
        return any(getattr(block, "phase", None) is phase for block in blocks)

    @staticmethod
    def _one_phase(blocks: tuple[object, ...], phase: FactoryPhase) -> object:
        matches = tuple(block for block in blocks if getattr(block, "phase", None) is phase)
        if len(matches) != 1:
            raise CapabilityLiveAuthorityUnavailable(
                f"Factory authority requires exactly one {phase.value} block"
            )
        return matches[0]

    @staticmethod
    def _one_artifact(artifacts: tuple[object, ...], artifact_type: type[object]) -> object:
        matches = tuple(item for item in artifacts if isinstance(item, artifact_type))
        if len(matches) != 1:
            raise CapabilityLiveAuthorityUnavailable(
                "Factory authority has ambiguous or absent workflow evidence"
            )
        return matches[0]

    @staticmethod
    def _first_artifact_ref(artifacts: tuple[object, ...]) -> ArtifactRef | None:
        for item in artifacts:
            reference = getattr(item, "artifact_ref", None)
            if isinstance(reference, ArtifactRef):
                return reference
        return None

    @staticmethod
    def _evidence_refs(items: tuple[object, ...]) -> tuple[ArtifactRef, ...]:
        references: list[ArtifactRef] = []
        for item in items:
            for name in ("artifact_refs", "evidence_refs"):
                value = getattr(item, name, ())
                references.extend(reference for reference in value if isinstance(reference, ArtifactRef))
            artifact_ref = getattr(item, "artifact_ref", None)
            if isinstance(artifact_ref, ArtifactRef):
                references.append(artifact_ref)
        unique = tuple(dict.fromkeys(references))
        if not unique:
            raise CapabilityLiveAuthorityUnavailable("Factory authority lacks evidence refs")
        return unique

    def _booked_cost(self, job_id: UUID):
        budget = self._composition.repository.workflow_budget_projection(job_id)
        return budget.consumed_usd


__all__ = [
    "CapabilityFactoryJobBinding",
    "CapabilityFactoryProvisioningPort",
    "CurrentCapabilityFactoryAdapter",
]
