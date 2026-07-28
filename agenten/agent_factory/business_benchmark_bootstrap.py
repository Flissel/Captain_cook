"""Concrete authority adapters for the provider-backed business benchmark.

The module intentionally stops at external authorities that do not yet have a
production implementation in this checkout.  It never substitutes an
in-memory repository, automatic human approval, or an unscoped n8n client.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkRunReceiptV1,
    BusinessBenchmarkSuiteV1,
)
from agenten.agent_factory.business_benchmark_live import (
    BusinessBenchmarkFinalizedReceiptV1,
    LiveBusinessBenchmarkSettings,
    ProductionAdapterUnavailableError,
    ProductionBusinessBenchmarkCompositionPort,
)
from agenten.agent_factory.business_benchmark_provisioning import (
    CanonicalPrivateBusinessBenchmarkProvisioner,
    CaptainPrivateBusinessBenchmarkSuiteLoader,
)
from agenten.agent_factory.business_benchmark_store import (
    FilesystemBusinessBenchmarkEvidenceStore,
)
from agenten.agent_factory.business_benchmark_production import (
    CaptainBusinessBenchmarkPolicyBindingV1,
    ProductionBusinessBenchmarkScope,
)
from agenten.agent_factory.business_benchmark_production_ports import (
    BusinessBenchmarkContentAddressedArtifactStore,
    BusinessBenchmarkProductionPortError,
)
from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryLease, FactoryRole
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_factory.execution_budget import FactoryBudgetProjection
from agenten.agent_factory.leases import validate_factory_lease
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import (
    FACTORY_SKILL_ID_BY_STEP,
    FactorySkillInvocationV1,
    FactorySkillStep,
    TeamEvaluationV1,
    TeamExecutionEvidenceV1,
)
from agenten.agent_runtime.contracts import ArtifactRef


class GatewayBusinessBenchmarkRepositoryPort(Protocol):
    """Read-only Gateway projection used by the benchmark scope resolver."""

    def job(self, job_id: UUID) -> object: ...

    def workflow_artifacts(self, job_id: UUID) -> tuple[object, ...]: ...

    def workflow_budget_projection(
        self, job_id: UUID
    ) -> FactoryBudgetProjection | None: ...


class GatewayBusinessBenchmarkAuthority:
    """Project exact benchmark inputs from Captain's Gateway repository."""

    def __init__(self, repository: GatewayBusinessBenchmarkRepositoryPort) -> None:
        self._repository = repository

    def factory_job(self, job_id: UUID) -> object | None:
        try:
            return self._repository.job(job_id)
        except (KeyError, LookupError):
            return None

    def team_execution_evidence(
        self, job_id: UUID, attempt: int
    ) -> tuple[TeamExecutionEvidenceV1, ...]:
        return tuple(
            item
            for item in self._repository.workflow_artifacts(job_id)
            if isinstance(item, TeamExecutionEvidenceV1)
            and item.attempt == attempt
        )

    def budget_projection(self, job_id: UUID) -> FactoryBudgetProjection | None:
        return self._repository.workflow_budget_projection(job_id)

    def candidate_ref(
        self, job_id: UUID, attempt: int, candidate_id: str
    ) -> ArtifactRef | None:
        if not candidate_id.strip():
            raise ValueError("candidate ID is required")
        references = {
            item.candidate_ref
            for item in self.team_execution_evidence(job_id, attempt)
            if isinstance(getattr(item, "candidate_ref", None), ArtifactRef)
        }
        if not references:
            return None
        if len(references) != 1:
            raise ValueError(
                "Gateway workflow evidence contains a mixed candidate reference"
            )
        return next(iter(references))


class ContentAddressedBenchmarkPolicyAuthority:
    """Load an immutable Captain policy binding from the benchmark CAS."""

    _BINDING_KIND = "benchmark-policy"

    def __init__(
        self, artifacts: BusinessBenchmarkContentAddressedArtifactStore
    ) -> None:
        self._artifacts = artifacts

    def policy_for(
        self, scope: ProductionBusinessBenchmarkScope
    ) -> CaptainBusinessBenchmarkPolicyBindingV1:
        identity = f"{scope.job.job_id}:{scope.selection.attempt}"
        reference = self._artifacts.binding(self._BINDING_KIND, identity)
        if reference is None:
            raise BusinessBenchmarkProductionPortError(
                "Captain benchmark policy binding is missing"
            )
        try:
            binding = CaptainBusinessBenchmarkPolicyBindingV1.model_validate_json(
                self._artifacts.read_bytes(reference)
            )
        except ValueError as exc:
            raise BusinessBenchmarkProductionPortError(
                "Captain benchmark policy binding is invalid"
            ) from exc
        if (
            binding.job_id != scope.job.job_id
            or binding.correlation_id != scope.job.correlation_id
            or binding.subject_version != scope.job.subject_version
            or binding.attempt != scope.selection.attempt
        ):
            raise BusinessBenchmarkProductionPortError(
                "Captain benchmark policy binding is stale or mixed"
            )
        return binding


class CaptainCanonicalSuiteAuthority:
    """Provision and reload deterministic suites inside Captain's private root."""

    def __init__(self, *, root: Path, seed_version_id: str) -> None:
        self._provisioner = CanonicalPrivateBusinessBenchmarkProvisioner(root)
        self._loader = CaptainPrivateBusinessBenchmarkSuiteLoader(root)
        self._seed_version_id = seed_version_id

    def canonical_suite(
        self, *, profile_id: str, suite_version: int
    ) -> tuple[PrivateHoldoutRef, BusinessBenchmarkSuiteV1]:
        provisioned = self._provisioner.provision(
            suite_version=suite_version,
            seed_version_id=self._seed_version_id,
        )
        selected = tuple(
            item for item in provisioned.suites if item.profile_id == profile_id
        )
        if len(selected) != 1:
            raise ValueError("canonical business benchmark profile is unsupported")
        item = selected[0]
        suite = self._loader.load_suite(
            item.suite_ref,
            expected_profile_id=item.profile_id,
            expected_suite_version=suite_version,
        )
        return item.suite_ref, suite


class ReleasedSkillCatalogPort(Protocol):
    def released_for(
        self, job: AgentFactoryJobV3, step: FactorySkillStep
    ) -> ReleasedHermesSkill: ...


class ActiveFactoryLeasePort(Protocol):
    def active(
        self,
        job: AgentFactoryJobV3,
        role: FactoryRole,
        attempt: int,
        now: datetime,
    ) -> FactoryLease: ...


class GatewayBenchmarkInvocationAuthority:
    """Reconstruct quality invocations only from Gateway skills and leases."""

    def __init__(
        self,
        *,
        repository: GatewayBusinessBenchmarkRepositoryPort,
        released_skills: ReleasedSkillCatalogPort,
        leases: ActiveFactoryLeasePort,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._released_skills = released_skills
        self._leases = leases
        self._clock = clock

    def runtime_invocation(
        self, *, job: AgentFactoryJobV3, attempt: int
    ) -> FactorySkillInvocationV1:
        observed = tuple(
            item.invocation
            for item in self._repository.workflow_artifacts(job.job_id)
            if isinstance(item, TeamExecutionEvidenceV1)
            and item.attempt == attempt
            and item.invocation.step is FactorySkillStep.EXECUTE_TEAM
        )
        invocations = tuple(
            invocation
            for index, invocation in enumerate(observed)
            if invocation not in observed[:index]
        )
        if len(invocations) != 1:
            raise ValueError(
                "Gateway requires one exact execute-team invocation for the attempt"
            )
        return invocations[0]

    def evaluation_invocation(
        self, *, job: AgentFactoryJobV3, attempt: int
    ) -> FactorySkillInvocationV1:
        return self._quality_invocation(
            job=job,
            attempt=attempt,
            step=FactorySkillStep.EVALUATE_TEAM,
            input_ref=job.input_ref,
        )

    def require_active_report(
        self, *, job: AgentFactoryJobV3, attempt: int
    ) -> None:
        self._quality_invocation(
            job=job,
            attempt=attempt,
            step=FactorySkillStep.REPORT_CAPTAIN,
            input_ref=job.input_ref,
        )

    def report_invocation(
        self,
        *,
        job: AgentFactoryJobV3,
        attempt: int,
        evaluation: TeamEvaluationV1,
    ) -> FactorySkillInvocationV1:
        return self._quality_invocation(
            job=job,
            attempt=attempt,
            step=FactorySkillStep.REPORT_CAPTAIN,
            input_ref=evaluation.artifact_ref,
        )

    def _quality_invocation(
        self,
        *,
        job: AgentFactoryJobV3,
        attempt: int,
        step: FactorySkillStep,
        input_ref: ArtifactRef,
    ) -> FactorySkillInvocationV1:
        now = self._utc_now()
        lease = self._leases.active(
            job,
            FactoryRole.QUALITY_WARDEN,
            attempt,
            now,
        )
        validate_factory_lease(
            lease,
            job=job,
            role=FactoryRole.QUALITY_WARDEN,
            attempt=attempt,
            now=now,
        )
        released = self._released_skills.released_for(job, step)
        if (
            released.skill_id != FACTORY_SKILL_ID_BY_STEP[step]
            or released.status != "released"
            or released.released_at > now
        ):
            raise ValueError("quality skill release is unavailable or stale")
        payload = {
            "job_id": str(job.job_id),
            "correlation_id": str(job.correlation_id),
            "subject_version": job.subject_version,
            "attempt": attempt,
            "step": step.value,
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        idempotency_key = hashlib.sha256(encoded).hexdigest()
        return FactorySkillInvocationV1(
            schema="captain.factory-skill-invocation.v1",
            invocation_id=uuid5(
                NAMESPACE_URL,
                f"captain.factory-skill:{idempotency_key}",
            ),
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            subject_version=job.subject_version,
            attempt=attempt,
            step=step,
            released_skill=released,
            input_ref=input_ref,
            input_sha256=input_ref.sha256,
            lease=lease,
            idempotency_key=idempotency_key,
            acceptance_assertion_ids=job.acceptance_assertion_ids,
        )

    def _utc_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("benchmark invocation clock must be UTC")
        return value


class FilesystemBenchmarkReceiptFinalizer:
    """Finalize a run receipt through Captain's append-only evidence store."""

    def __init__(self, evidence: FilesystemBusinessBenchmarkEvidenceStore) -> None:
        self._evidence = evidence

    def finalize(
        self,
        *,
        profile: Literal["claims", "renewal"],
        receipt: BusinessBenchmarkRunReceiptV1,
    ) -> BusinessBenchmarkFinalizedReceiptV1:
        reference = self._evidence.record_run_receipt(receipt)
        return BusinessBenchmarkFinalizedReceiptV1(
            profile=profile,
            receipt=receipt,
            receipt_ref=reference,
        )


def build_production_business_benchmark_composition(
    settings: LiveBusinessBenchmarkSettings,
) -> ProductionBusinessBenchmarkCompositionPort:
    """Build from real ports, or report the first exact missing authority.

    ``CaptainHumanReviewPort`` is required for the mandatory-escalation cases
    in both canonical suites.  No durable Captain implementation exists in the
    current checkout, so construction must stop before a Gateway connection,
    provider client, or n8n call can be created.
    """

    LiveBusinessBenchmarkSettings.model_validate(settings.model_dump(mode="python"))
    raise ProductionAdapterUnavailableError(
        "CaptainHumanReviewPort has no durable Captain implementation; "
        "automatic completion or an in-memory receipt is forbidden"
    )


__all__ = [
    "CaptainCanonicalSuiteAuthority",
    "ContentAddressedBenchmarkPolicyAuthority",
    "FilesystemBenchmarkReceiptFinalizer",
    "GatewayBenchmarkInvocationAuthority",
    "GatewayBusinessBenchmarkAuthority",
    "GatewayBusinessBenchmarkRepositoryPort",
    "build_production_business_benchmark_composition",
]
