"""Current-authority provisioning for a generic capability-live Factory job.

The live runner owns neither input interpretation nor lifecycle state.  This
adapter re-reads the exact bytes already admitted to Captain CAS, validates the
canonical input again, and records the initial Captain-owned state once.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid5

from agenten.agent_factory.capability_live_runner import CapabilityLiveStageRequest
from agenten.agent_factory.contracts import (
    AgentFactoryJobV3,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
)
from agenten.agent_factory.execution_policy import FactoryExecutionPolicyV1
from agenten.agent_factory.holdout_store import PrivateHoldoutStore
from agenten.agent_factory.input_compiler import FactoryInputCompiler
from agenten.agent_factory.input_document import load_factory_input
from agenten.agent_factory.job_builder import build_factory_job_v3
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.service import FactoryRepositoryError
from agenten.agent_factory.skill_workflow_contracts import FactorySkillStep
from agenten.agent_runtime.captain_production_adapters import (
    ContentAddressedArtifactAdapter,
)
from agenten.agent_runtime.confined_files import ConfinedFileStore
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.validation.contracts import AcceptanceAssertion, AssertionKind, WorkBatch
from gateway.capability_live_factory_adapter import CapabilityFactoryJobBinding


class CapabilityLiveProvisioningError(RuntimeError):
    """A generic input cannot be admitted to the current Factory authority."""


class CapabilityFactoryProvisioningRepository(Protocol):
    def register(self, job: AgentFactoryJobV3) -> None: ...

    def job(self, job_id: UUID) -> object: ...

    def seed_released_skill_assignments(self, job: AgentFactoryJobV3, source: object) -> None: ...

    def append(self, block: FactoryEvidenceBlock) -> bool: ...


class CapabilityWorkBatchWriter(Protocol):
    def persist_work_batch(self, job: AgentFactoryJobV3, batch: WorkBatch) -> None: ...


class CapabilityN8nWorkBatchRegistry(Protocol):
    def register_n8n_work_batch(self, job: AgentFactoryJobV3, batch_id: str) -> None: ...


class CapabilityReleasedSkills(Protocol):
    def released_for(
        self, job: AgentFactoryJobV3, step: FactorySkillStep
    ) -> ReleasedHermesSkill: ...


class CurrentAuthorityCapabilityFactoryProvisioner:
    """Provision one input through the existing V3 Factory and Gateway seams.

    It deliberately has no Claims/Renewal profile selector.  The input bytes,
    its correlation, and its released execution policy are the full authority
    for one generic capability job.
    """

    def __init__(
        self,
        *,
        repository: CapabilityFactoryProvisioningRepository,
        source_artifacts: ContentAddressedArtifactAdapter,
        holdout_store: PrivateHoldoutStore,
        released_skills: CapabilityReleasedSkills,
        execution_policy: FactoryExecutionPolicyV1,
        authority_root: Path,
        clock: Callable[[], datetime],
        work_batches: CapabilityWorkBatchWriter | None = None,
        n8n_registry: CapabilityN8nWorkBatchRegistry | None = None,
    ) -> None:
        if not callable(clock):
            raise ValueError("capability live provisioning clock is required")
        self._repository = repository
        self._source_artifacts = source_artifacts
        self._compiler = FactoryInputCompiler(holdout_store=holdout_store)
        self._released_skills = released_skills
        self._execution_policy = execution_policy
        self._authority_files = ConfinedFileStore(authority_root)
        self._clock = clock
        self._work_batches = work_batches
        self._n8n_registry = n8n_registry
        self._bindings: dict[tuple[UUID, str], CapabilityFactoryJobBinding] = {}

    def binding_for(
        self, request: CapabilityLiveStageRequest
    ) -> CapabilityFactoryJobBinding | None:
        return self._bindings.get((request.run_id, request.input.input_id))

    def provision(
        self, request: CapabilityLiveStageRequest
    ) -> CapabilityFactoryJobBinding | None:
        key = (request.run_id, request.input.input_id)
        cached = self._bindings.get(key)
        if cached is not None:
            self._require_request_binding(request, cached)
            return cached

        document = self._load_current_input(request.input.source_ref)
        n8n_required = any(
            integration.required for integration in document.integrations
        )
        if request.input.integration_intent == "n8n" and not n8n_required:
            raise CapabilityLiveProvisioningError(
                "n8n integration is not required by the current input"
            )
        self._preflight_n8n_dependencies(
            request,
            n8n_required=n8n_required,
        )
        compiled = self._compiler.compile(document, subject_version=1)
        candidate = build_factory_job_v3(
            compiled,
            correlation_id=request.correlation_id,
            now=self._clock(),
            execution_policy=self._execution_policy,
        )
        job, created = self._existing_or_register(candidate)
        self._require_job_binding(request, job)
        work_batch_id = None
        if created:
            self._repository.seed_released_skill_assignments(job, self._released_skills)
            self._append_forge_requested(job)
            work_batch_id = self._persist_n8n_work_batch(
                job,
                request,
                n8n_required=n8n_required,
            )
        elif request.input.integration_intent == "n8n":
            work_batch_id = f"n8n-{job.job_id.hex[:24]}"
        binding = CapabilityFactoryJobBinding(
            input_id=request.input.input_id,
            source_ref=request.input.source_ref,
            job=job,
            suite_ref=job.compiled_spec_ref,
            forge_requested_event_id=_forge_event_id(job),
            n8n_work_batch_id=work_batch_id,
        )
        self._bindings[key] = binding
        return binding

    def _load_current_input(self, source_ref: ArtifactRef):
        try:
            source = self._source_artifacts.read_bytes(source_ref)
            relative = f"inputs/{source_ref.sha256}/TO_BE_BUILT.md"
            self._authority_files.write_once(
                relative,
                source,
                conflict="Captain CAS input digest is bound to different bytes",
            )
            document = load_factory_input(self._authority_files.root / relative)
        except (OSError, ValueError) as exc:
            raise CapabilityLiveProvisioningError(
                "current Captain CAS input is unavailable or invalid"
            ) from exc
        if document.input_ref.sha256 != source_ref.sha256:
            raise CapabilityLiveProvisioningError("Captain CAS input digest changed")
        return document.model_copy(update={"input_ref": source_ref})

    def _existing_or_register(
        self, candidate: AgentFactoryJobV3
    ) -> tuple[AgentFactoryJobV3, bool]:
        try:
            existing = self._repository.job(candidate.job_id)
        except (KeyError, LookupError):
            self._repository.register(candidate)
            return candidate, True
        except FactoryRepositoryError as exc:
            if "not found" not in str(exc).casefold():
                raise CapabilityLiveProvisioningError(
                    "current Gateway Factory job cannot be read"
                ) from exc
            self._repository.register(candidate)
            return candidate, True
        if not isinstance(existing, AgentFactoryJobV3):
            raise CapabilityLiveProvisioningError("current Factory job is not V3")
        if not _same_immutable_job(existing, candidate):
            raise CapabilityLiveProvisioningError(
                "current Factory job identity is already bound to different authority"
            )
        return existing, False

    def _append_forge_requested(self, job: AgentFactoryJobV3) -> None:
        self._repository.append(
            FactoryEvidenceBlock(
                schema="captain.agent-factory-block.v1",
                event_id=_forge_event_id(job),
                job_id=job.job_id,
                correlation_id=job.correlation_id,
                occurred_at=job.occurred_at,
                producer="captain",
                subject_version=job.subject_version,
                attempt=1,
                phase=FactoryPhase.FORGE_REQUESTED,
                status=FactoryBlockStatus.SUCCEEDED,
                artifact_refs=(job.input_ref, job.compiled_spec_ref, job.dependency_graph_ref),
                evidence_refs=(job.input_ref,),
                assertion_ids=job.acceptance_assertion_ids,
            )
        )

    def _persist_n8n_work_batch(
        self,
        job: AgentFactoryJobV3,
        request: CapabilityLiveStageRequest,
        *,
        n8n_required: bool,
    ) -> str | None:
        if request.input.integration_intent != "n8n":
            return None
        if not n8n_required:
            raise CapabilityLiveProvisioningError(
                "n8n integration is not required by the current input"
            )
        assert self._work_batches is not None
        assert self._n8n_registry is not None
        batch_id = f"n8n-{job.job_id.hex[:24]}"
        batch = WorkBatch(
            batch_id=batch_id,
            title=f"n8n integration for {job.required_capability}",
            goal="Execute only the Captain-released n8n integration for this job.",
            subtask_ids=["n8n-workflow"],
            target="n8n",
            runtime="n8n",
            capability_tags=["n8n-builder"],
            constraints=[f"factory-job-id:{job.job_id}"],
            acceptance_criteria=[
                AcceptanceAssertion(
                    assertion_id=assertion_id,
                    kind=AssertionKind.SIDE_EFFECT_OBSERVED,
                    description="Bound Factory integration assertion",
                )
                for assertion_id in job.acceptance_assertion_ids
            ],
        )
        self._work_batches.persist_work_batch(job, batch)
        self._n8n_registry.register_n8n_work_batch(job, batch_id)
        return batch_id

    def _preflight_n8n_dependencies(
        self,
        request: CapabilityLiveStageRequest,
        *,
        n8n_required: bool,
    ) -> None:
        if request.input.integration_intent != "n8n" or not n8n_required:
            return
        if self._work_batches is None:
            raise CapabilityLiveProvisioningError(
                "required n8n integration lacks a Gateway WorkBatch writer"
            )
        if self._n8n_registry is None:
            raise CapabilityLiveProvisioningError(
                "required n8n integration lacks a Gateway lease registry"
            )

    @staticmethod
    def _require_request_binding(
        request: CapabilityLiveStageRequest,
        binding: CapabilityFactoryJobBinding,
    ) -> None:
        if binding.input_id != request.input.input_id or binding.source_ref != request.input.source_ref:
            raise CapabilityLiveProvisioningError("cached Factory binding changed input authority")

    @staticmethod
    def _require_job_binding(
        request: CapabilityLiveStageRequest,
        job: AgentFactoryJobV3,
    ) -> None:
        if job.input_ref != request.input.source_ref or job.correlation_id != request.correlation_id:
            raise CapabilityLiveProvisioningError("Factory job does not match current input authority")


def _forge_event_id(job: AgentFactoryJobV3) -> UUID:
    return uuid5(job.job_id, f"forge-requested:{job.subject_version}:1")


def _same_immutable_job(
    existing: AgentFactoryJobV3,
    candidate: AgentFactoryJobV3,
) -> bool:
    """Ignore first-admission wall time while fencing every authority input."""

    return (
        existing.job_id == candidate.job_id
        and existing.event_id == candidate.event_id
        and existing.correlation_id == candidate.correlation_id
        and existing.subject_version == candidate.subject_version
        and existing.input_ref == candidate.input_ref
        and existing.compiled_spec_ref == candidate.compiled_spec_ref
        and existing.dependency_graph_ref == candidate.dependency_graph_ref
        and existing.required_capability == candidate.required_capability
        and existing.acceptance_assertion_ids == candidate.acceptance_assertion_ids
        and existing.private_holdout_refs == candidate.private_holdout_refs
        and existing.execution_policy == candidate.execution_policy
    )


__all__ = [
    "CapabilityLiveProvisioningError",
    "CurrentAuthorityCapabilityFactoryProvisioner",
]
