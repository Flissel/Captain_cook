"""Truthful Package-C V2 to paid six-skill V3 release-evidence bridge.

The bridge owns no provider client.  Normal runs are delegated to the existing
``TeamExecutionCandidateAdapter`` (which production composes from
``TeamExecutionService`` and ``HostAutoGenTeamRunner``).  The additional
controlled-recovery port is deliberately explicit because the current team
adapter can represent three normal release runs, but cannot represent a fourth,
independently identified post-effect recovery run.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_factory.candidate_evaluation import ResolvedFactoryCandidate
from agenten.agent_factory.capability_factory_entrypoint import (
    CapabilityReleaseRunReceipt,
)
from agenten.agent_factory.capability_factory_production import (
    EvidenceLifecycleRequest,
    EvidenceRunRequest,
)
from agenten.agent_factory.capability_live_adapters import (
    CaptainCapabilityReleaseReceipt,
    CaptainEvidenceIssuerAdapter,
    CapabilityReleaseObservation,
    ContentAddressedArtifactStore,
)
from agenten.agent_factory.contracts import (
    AgentFactoryJobV2,
    AgentFactoryJobV3,
    FactoryEvidenceBlock,
    FactoryLease,
    FactoryRole,
)
from agenten.agent_factory.execution_budget import FactoryUsageReceiptV1
from agenten.agent_factory.execution_policy import FactoryExecutionPolicyV1
from agenten.agent_factory.factory_live_runner import FactoryLiveRunReport
from agenten.agent_factory.forge_contracts import CreationResultV1
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_factory.leases import issue_factory_lease, validate_factory_lease
from agenten.agent_factory.outcome_contracts import (
    CapabilityAssertionResult,
    ForgeCapabilityPackageCandidateV1,
    PrivateHoldoutEvidence,
)
from agenten.agent_factory.orchestration import FactoryDispatch
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import (
    FACTORY_SKILL_ID_BY_STEP,
    FactorySkillStep,
    TeamExecutionEvidenceV1,
)
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from agenten.agent_factory.team_execution import TeamExecutionCandidateAdapter
from agenten.agent_runtime.contracts import ArtifactRef


CONTROLLED_RECOVERY_TODO_TOOL = (
    "TODO_TOOL.v1 required capability=controlled_provider_recovery; "
    "reason=TeamExecutionCandidateAdapter has three normal run identities but no "
    "independent durable post-effect recovery identity"
)


class CapabilityV3BridgeConfigurationError(ValueError):
    """The live bridge lacks an authority or provider-backed runtime port."""


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class CapabilityCandidateAttestationV1(_FrozenContract):
    """Sandbox-owned tree identity for the exact sealed Package-C candidate."""

    schema_name: Literal["captain.capability-candidate-attestation.v1"] = Field(
        default="captain.capability-candidate-attestation.v1",
        alias="schema",
        serialization_alias="schema",
    )
    job_id: UUID
    candidate_ref: ArtifactRef
    extracted_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sandbox_evidence_ref: ArtifactRef


class CapabilityControlledHoldoutReceiptV1(_FrozenContract):
    """Redacted Captain holdout result; the private body never crosses the port."""

    holdout_ref: PrivateHoldoutRef
    assertion_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    status: Literal["passed", "failed"]
    evidence_ref: ArtifactRef

class CapabilityControlledRecoveryResultV1(_FrozenContract):
    """One reserved effect recovered from durable provider evidence without replay."""

    recovery_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    recovery_assertion_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    execution: TeamExecutionEvidenceV1
    interrupted: FactoryLiveRunReport
    resumed: FactoryLiveRunReport
    provider_effect_receipt_ref: ArtifactRef
    holdout_receipts: tuple[CapabilityControlledHoldoutReceiptV1, ...] = Field(
        min_length=1
    )

    @model_validator(mode="after")
    def require_exact_recovery_transition(self) -> "CapabilityControlledRecoveryResultV1":
        interrupted = self.interrupted
        resumed = self.resumed
        if (
            interrupted.job_id != self.execution.job_id
            or resumed.job_id != self.execution.job_id
            or interrupted.correlation_id != self.execution.correlation_id
            or resumed.correlation_id != self.execution.correlation_id
            or interrupted.mode != "release"
            or resumed.mode != "release"
            or interrupted.status != "infrastructure_recovery_required"
            or interrupted.attempt != resumed.attempt
            or interrupted.next_attempt != resumed.next_attempt
            or len(interrupted.effects) != 1
            or len(resumed.effects) != 1
        ):
            raise ValueError("controlled recovery reports do not bind one release attempt")
        before = interrupted.effects[0]
        after = resumed.effects[0]
        if (
            before.effect_id != after.effect_id
            or before.kind.value != "provider"
            or after.kind.value != "provider"
            or before.status != "reserved"
            or before.provider_started is not None
            or after.status != "succeeded"
            or after.completion_origin != "recover"
            or after.provider_started is not True
            or after.evidence_ref != self.provider_effect_receipt_ref
        ):
            raise ValueError("controlled recovery lacks a durable recovered provider effect")
        return self


class CapabilityV3RunEvidenceV1(_FrozenContract):
    """Content-addressed bridge record binding V2 authority to V3 paid evidence."""

    schema_name: Literal["captain.capability-v3-run-evidence.v1"] = Field(
        default="captain.capability-v3-run-evidence.v1",
        alias="schema",
        serialization_alias="schema",
    )
    package_c_job_id: UUID
    factory_v3_job_id: UUID
    run_number: int = Field(ge=1, le=4, strict=True)
    execution: TeamExecutionEvidenceV1
    usage_receipts: tuple[FactoryUsageReceiptV1, ...] = Field(min_length=1)
    total_cost_usd: Decimal
    candidate_attestation: CapabilityCandidateAttestationV1
    recovery_id: str | None = None
    recovery_assertion_id: str | None = None
    provider_effect_receipt_ref: ArtifactRef | None = None
    holdout_receipts: tuple[CapabilityControlledHoldoutReceiptV1, ...] = ()

    @field_validator("total_cost_usd", mode="before")
    @classmethod
    def require_known_positive_cost(cls, value: object) -> Decimal:
        if isinstance(value, (bool, float)) or not isinstance(value, (str, Decimal)):
            raise ValueError("provider-backed run requires an exact decimal cost")
        amount = Decimal(value)
        if not amount.is_finite() or amount <= 0:
            raise ValueError("provider-backed run requires a positive known cost")
        return amount

    @model_validator(mode="after")
    def require_recovery_only_on_first_run(self) -> "CapabilityV3RunEvidenceV1":
        recovery = (
            self.recovery_id,
            self.recovery_assertion_id,
            self.provider_effect_receipt_ref,
        )
        if self.run_number == 1:
            if any(value is None for value in recovery) or not self.holdout_receipts:
                raise ValueError("first bridge run requires controlled recovery evidence")
        elif any(value is not None for value in recovery) or self.holdout_receipts:
            raise ValueError("normal bridge run cannot carry controlled recovery evidence")
        return self


class CapabilityV3AuthorityPort(Protocol):
    """Persistence needed from the Captain/Gateway V3 authority."""

    def register(self, job: AgentFactoryJobV3) -> None: ...

    def job(self, job_id: UUID) -> AgentFactoryJobV3: ...

    def seed_released_skill_assignments(
        self,
        job: AgentFactoryJobV3,
        source: "CapabilityReleasedSkillSourcePort",
    ) -> None: ...

    def released_for(
        self,
        job: AgentFactoryJobV3,
        step: FactorySkillStep,
    ) -> ReleasedHermesSkill: ...

    def record_lease(self, lease: FactoryLease) -> None: ...

    def usage_receipts(self, job_id: UUID) -> tuple[FactoryUsageReceiptV1, ...]: ...


class CapabilityReleasedSkillSourcePort(Protocol):
    def released_for(
        self,
        job: AgentFactoryJobV3,
        step: FactorySkillStep,
    ) -> ReleasedHermesSkill: ...


class CapabilityCandidateProviderPort(Protocol):
    def candidate_for(
        self,
        job: AgentFactoryJobV3,
        candidate: ForgeCapabilityPackageCandidateV1,
    ) -> ResolvedFactoryCandidate: ...


class CapabilityCandidateAttestationPort(Protocol):
    def attest(
        self,
        job: AgentFactoryJobV3,
        resolved: ResolvedFactoryCandidate,
        candidate: ForgeCapabilityPackageCandidateV1,
    ) -> CapabilityCandidateAttestationV1: ...


class CapabilityControlledRecoveryPort(Protocol):
    """Missing typed seam between the team adapter and FactoryLiveRunner recovery."""

    async def execute(
        self,
        job: AgentFactoryJobV3,
        dispatch: FactoryDispatch,
        candidate: ResolvedFactoryCandidate,
    ) -> CapabilityControlledRecoveryResultV1: ...


@dataclass(frozen=True)
class CapabilityV3EvidenceBuilderContext:
    """Constructor-only production dependencies; no secret is stored here."""

    authority: CapabilityV3AuthorityPort
    released_skills: CapabilityReleasedSkillSourcePort
    candidate_provider: CapabilityCandidateProviderPort
    team_execution: TeamExecutionCandidateAdapter
    controlled_recovery: CapabilityControlledRecoveryPort | None
    candidate_attestation: CapabilityCandidateAttestationPort
    artifact_store: ContentAddressedArtifactStore
    execution_policy: FactoryExecutionPolicyV1
    workspace_ref: str
    clock: Callable[[], datetime]


class PackageCV3CapabilityReleaseExecutor:
    """Concrete Package-C release executor backed only by paid V3 team evidence."""

    def __init__(self, context: CapabilityV3EvidenceBuilderContext) -> None:
        if context.controlled_recovery is None:
            raise CapabilityV3BridgeConfigurationError(CONTROLLED_RECOVERY_TODO_TOOL)
        if not isinstance(context.team_execution, TeamExecutionCandidateAdapter):
            raise CapabilityV3BridgeConfigurationError(
                "team_execution must be the governed TeamExecutionCandidateAdapter"
            )
        policy = context.execution_policy
        if (
            not policy.live_execution
            or policy.mode.value != "release"
            or policy.required_live_runs != 3
        ):
            raise CapabilityV3BridgeConfigurationError(
                "Package-C live release requires a V3 release policy with three normal runs"
            )
        if not context.workspace_ref.startswith("workspace://"):
            raise CapabilityV3BridgeConfigurationError(
                "V3 bridge workspace must use an opaque workspace:// reference"
            )
        self._context = context

    async def execute(
        self,
        job: AgentFactoryJobV2,
        creation_result: CreationResultV1,
        candidate: ForgeCapabilityPackageCandidateV1,
        run_number: int,
    ) -> CapabilityReleaseObservation | None:
        if run_number not in {1, 2, 3, 4}:
            raise ValueError("Package-C evidence run must be one through four")
        existing = self._context.artifact_store.binding(
            "v3-release-observation", f"{job.job_id}/{run_number}"
        )
        if existing is not None:
            return CapabilityReleaseObservation.model_validate_json(
                self._context.artifact_store.read_bytes(existing)
            )
        v3 = self._prepare_v3_authority(job, attempt=creation_result.attempt)
        resolved = self._context.candidate_provider.candidate_for(v3, candidate)
        if resolved.candidate.source_archive_ref != candidate.source_ref:
            raise ValueError("resolved V3 candidate does not match Package-C source authority")
        attestation = self._context.candidate_attestation.attest(v3, resolved, candidate)
        if attestation.job_id != v3.job_id or attestation.candidate_ref != candidate.source_ref:
            raise ValueError("candidate sandbox attestation does not match V3 authority")
        leases = self._leases(v3, attempt=creation_result.attempt)
        dispatch = FactoryDispatch(
            job=v3,
            action=FactoryAction(
                kind=FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
                attempt=creation_result.attempt,
                job_id=v3.job_id,
            ),
            role=FactoryRole.REAL_CASE_TESTER,
            lease=leases[FactoryRole.REAL_CASE_TESTER],
        )
        recovery: CapabilityControlledRecoveryResultV1 | None = None
        if run_number == 1:
            assert self._context.controlled_recovery is not None
            recovery = await self._context.controlled_recovery.execute(
                v3, dispatch, resolved
            )
            execution = recovery.execution
            self._require_recovery(v3, recovery)
        else:
            execution = await self._context.team_execution.execute_run(
                dispatch,
                resolved,
                run_number=run_number - 1,
            )
        usage = self._require_paid_execution(v3, execution, candidate)
        self._require_distinct_provider_run(job.job_id, run_number, execution, usage)
        run_evidence = CapabilityV3RunEvidenceV1(
            package_c_job_id=job.job_id,
            factory_v3_job_id=v3.job_id,
            run_number=run_number,
            execution=execution,
            usage_receipts=usage,
            total_cost_usd=sum(
                (receipt.cost_usd for receipt in usage), Decimal("0.00")
            ),
            candidate_attestation=attestation,
            recovery_id=recovery.recovery_id if recovery is not None else None,
            recovery_assertion_id=(
                recovery.recovery_assertion_id if recovery is not None else None
            ),
            provider_effect_receipt_ref=(
                recovery.provider_effect_receipt_ref if recovery is not None else None
            ),
            holdout_receipts=(
                recovery.holdout_receipts if recovery is not None else ()
            ),
        )
        run_ref = self._context.artifact_store.put(
            run_evidence.model_dump_json(by_alias=True).encode("utf-8"),
            "application/json",
            namespace="v3-run-evidence",
        )
        self._context.artifact_store.bind(
            "v3-run-evidence", f"{job.job_id}/{run_number}", run_ref
        )
        assertions = tuple(
            CapabilityAssertionResult(
                assertion_id=item.assertion_id,
                status=item.status,
                integration_intent=item.integration_intent,
                evidence_refs=(run_ref,),
            )
            for item in execution.execution_outcome.assertion_outcomes
        )
        now = self._utc_now()
        if not job.occurred_at <= now < job.deadline_at:
            raise ValueError("Package-C release observation exceeded its authority deadline")
        is_recovery = recovery is not None
        observation = CapabilityReleaseObservation(
            run_id=self._run_id(job.job_id, execution.invocation_id, run_number),
            capability_version=candidate.capability_version,
            extracted_tree_sha256=attestation.extracted_tree_sha256,
            kind="recovery" if is_recovery else "normal",
            outcome="expected_failure_recovered" if is_recovery else "succeeded",
            assertion_results=assertions,
            recovery_id=recovery.recovery_id if recovery is not None else None,
            recovery_assertion_id=(
                recovery.recovery_assertion_id if recovery is not None else None
            ),
            private_holdout_evidence=(
                tuple(
                    PrivateHoldoutEvidence(
                        holdout_id=item.holdout_ref.holdout_id,
                        assertion_id=item.assertion_id,
                        status=item.status,
                        evidence_ref=run_ref,
                    )
                    for item in recovery.holdout_receipts
                )
                if recovery is not None
                else ()
            ),
            build_lease_id=leases[FactoryRole.TOOL_INTEGRATOR].lease_id,
            tester_lease_id=leases[FactoryRole.REAL_CASE_TESTER].lease_id,
            quality_lease_id=leases[FactoryRole.QUALITY_WARDEN].lease_id,
            occurred_at=now,
        )
        observation_ref = self._context.artifact_store.put(
            observation.model_dump_json().encode("utf-8"),
            "application/json",
            namespace="v3-release-observation",
        )
        self._context.artifact_store.bind(
            "v3-release-observation", f"{job.job_id}/{run_number}", observation_ref
        )
        return observation

    def _prepare_v3_authority(
        self, job: AgentFactoryJobV2, *, attempt: int
    ) -> AgentFactoryJobV3:
        if isinstance(attempt, bool) or not 1 <= attempt <= job.max_behavioral_iterations:
            raise ValueError("creation attempt is outside Package-C authority")
        v3 = build_v3_job_from_package_c(job, self._context.execution_policy)
        self._context.authority.register(v3)
        if self._context.authority.job(v3.job_id) != v3:
            raise ValueError("persisted V3 job does not match the Package-C authority")
        self._context.authority.seed_released_skill_assignments(
            v3, self._context.released_skills
        )
        for step in FactorySkillStep:
            released = self._context.authority.released_for(v3, step)
            if (
                released.skill_id != FACTORY_SKILL_ID_BY_STEP[step]
                or released.capability != v3.required_capability
                or released != self._context.released_skills.released_for(v3, step)
            ):
                raise ValueError("V3 released six-skill assignment changed")
        return v3

    def _leases(
        self, job: AgentFactoryJobV3, *, attempt: int
    ) -> dict[FactoryRole, FactoryLease]:
        now = self._utc_now()
        leases: dict[FactoryRole, FactoryLease] = {}
        for role in FactoryRole:
            lease = issue_factory_lease(
                job=job,
                role=role,
                attempt=attempt,
                workspace_ref=self._context.workspace_ref,
                now=job.occurred_at,
            )
            validate_factory_lease(
                lease,
                job=job,
                role=role,
                attempt=attempt,
                now=now,
            )
            self._context.authority.record_lease(lease)
            leases[role] = lease
        return leases

    def _require_paid_execution(
        self,
        job: AgentFactoryJobV3,
        execution: TeamExecutionEvidenceV1,
        candidate: ForgeCapabilityPackageCandidateV1,
    ) -> tuple[FactoryUsageReceiptV1, ...]:
        outcome = execution.execution_outcome
        if (
            execution.job_id != job.job_id
            or execution.correlation_id != job.correlation_id
            or execution.subject_version != job.subject_version
            or execution.status != "succeeded"
            or outcome.status != "succeeded"
            or outcome.capability_id != candidate.capability_id
            or outcome.capability_version != candidate.capability_version
            or tuple(item.assertion_id for item in outcome.assertion_outcomes)
            != job.acceptance_assertion_ids
            or any(item.status != "passed" for item in outcome.assertion_outcomes)
            or execution.invocation.step is not FactorySkillStep.EXECUTE_TEAM
            or execution.holdout_ref not in job.private_holdout_refs
            or not execution.usage_receipt_refs
        ):
            raise ValueError("V3 team execution is not a successful provider-backed run")
        available = {
            receipt.evidence_ref: receipt
            for receipt in self._context.authority.usage_receipts(job.job_id)
        }
        try:
            receipts = tuple(available[ref] for ref in execution.usage_receipt_refs)
        except KeyError as exc:
            raise ValueError("V3 execution usage receipt is not Gateway-authoritative") from exc
        if len(receipts) != len(set(receipt.receipt_id for receipt in receipts)):
            raise ValueError("V3 execution usage receipt identities are not unique")
        for receipt in receipts:
            if (
                receipt.job_id != job.job_id
                or receipt.correlation_id != job.correlation_id
                or receipt.attempt != execution.attempt
                or receipt.invocation_id != execution.invocation_id
                or receipt.lease_id != execution.invocation.lease.lease_id
                or receipt.model not in job.execution_policy.allowed_models
                or receipt.cost_usd <= 0
            ):
                raise ValueError("V3 provider cost receipt does not match execution authority")
        all_receipts = self._context.authority.usage_receipts(job.job_id)
        if len({item.receipt_id for item in all_receipts}) != len(all_receipts):
            raise ValueError("Gateway V3 usage receipt identities are not unique")
        total_cost = sum(
            (item.cost_usd for item in all_receipts), Decimal("0.00")
        )
        if total_cost > job.execution_policy.max_cost_usd:
            raise ValueError("V3 provider cost exceeds the Captain job budget")
        return receipts

    def _require_recovery(
        self,
        job: AgentFactoryJobV3,
        recovery: CapabilityControlledRecoveryResultV1,
    ) -> None:
        if recovery.recovery_assertion_id not in job.acceptance_assertion_ids:
            raise ValueError("controlled recovery assertion is not Captain-authorized")
        if tuple(item.holdout_ref for item in recovery.holdout_receipts) != job.private_holdout_refs:
            raise ValueError("controlled recovery does not cover the exact private holdouts")
        if any(
            item.status != "passed" or item.assertion_id not in job.acceptance_assertion_ids
            for item in recovery.holdout_receipts
        ):
            raise ValueError("controlled recovery private holdout evidence did not pass")

    def _require_distinct_provider_run(
        self,
        package_c_job_id: UUID,
        run_number: int,
        execution: TeamExecutionEvidenceV1,
        usage: tuple[FactoryUsageReceiptV1, ...],
    ) -> None:
        prior_invocations: set[UUID] = set()
        prior_receipts: set[UUID] = set()
        for number in range(1, run_number):
            reference = self._context.artifact_store.binding(
                "v3-run-evidence", f"{package_c_job_id}/{number}"
            )
            if reference is None:
                raise ValueError("V3 provider run sequence has a gap")
            prior = CapabilityV3RunEvidenceV1.model_validate_json(
                self._context.artifact_store.read_bytes(reference)
            )
            prior_invocations.add(prior.execution.invocation_id)
            prior_receipts.update(item.receipt_id for item in prior.usage_receipts)
        if execution.invocation_id in prior_invocations or any(
            item.receipt_id in prior_receipts for item in usage
        ):
            raise ValueError("recovery and normal provider runs must be distinct")

    def _utc_now(self) -> datetime:
        now = self._context.clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("V3 evidence bridge clock must be UTC")
        return now

    @staticmethod
    def _run_id(job_id: UUID, invocation_id: UUID, run_number: int) -> str:
        digest = hashlib.sha256(
            f"{job_id}|{invocation_id}|{run_number}".encode("utf-8")
        ).hexdigest()
        return ("controlled-recovery-" if run_number == 1 else "normal-e2e-") + digest[:24]


class PackageCV3CapabilityEvidenceBackend:
    """Concrete authenticated-runtime backend returning Package-C receipt types."""

    def __init__(
        self,
        *,
        executor: PackageCV3CapabilityReleaseExecutor,
        artifact_store: ContentAddressedArtifactStore,
    ) -> None:
        self._issuer = CaptainEvidenceIssuerAdapter(
            executor=executor,
            artifact_store=artifact_store,
        )

    async def run(
        self, request: EvidenceRunRequest
    ) -> CapabilityReleaseRunReceipt | None:
        receipt = await self._issuer.run(
            request.job,
            request.creation_result,
            request.candidate,
            request.run_number,
        )
        if receipt is None:
            return None
        return CapabilityReleaseRunReceipt(
            record=receipt.record,
            reference=receipt.reference,
        )

    async def lifecycle_blocks(
        self, request: EvidenceLifecycleRequest
    ) -> tuple[FactoryEvidenceBlock, FactoryEvidenceBlock, FactoryEvidenceBlock]:
        receipts = tuple(
            CaptainCapabilityReleaseReceipt(
                record=item.record,
                reference=item.reference,
            )
            for item in request.receipts
        )
        return await self._issuer.lifecycle_blocks(request.job, receipts)


def build_v3_job_from_package_c(
    job: AgentFactoryJobV2,
    execution_policy: FactoryExecutionPolicyV1,
) -> AgentFactoryJobV3:
    """Derive one byte-stable V3 job while preserving all V2 authority fields."""

    duration = job.deadline_at - job.occurred_at
    if duration.total_seconds() != execution_policy.max_runtime_seconds:
        raise CapabilityV3BridgeConfigurationError(
            "V3 execution policy deadline does not match Package-C authority"
        )
    policy_payload = execution_policy.model_dump(mode="json", by_alias=True)
    policy_payload["max_cost_usd"] = _decimal_string(execution_policy.max_cost_usd)
    binding_payload = {
        "schema": "captain.package-c-v2-to-v3-binding.v1",
        "package_c_job": job.model_dump(mode="json", by_alias=True),
        "execution_policy": policy_payload,
    }
    digest = hashlib.sha256(_canonical_json(binding_payload).encode("utf-8")).hexdigest()
    payload = job.model_dump(mode="json", by_alias=True)
    payload.update(
        {
            "schema": "captain.agent-factory-job.v3",
            "job_id": str(uuid5(job.job_id, f"captain.package-c-v3:{digest}")),
            "event_id": str(uuid5(job.event_id, f"captain.package-c-v3:{digest}")),
            "causation_id": str(job.event_id),
            "execution_policy": policy_payload,
        }
    )
    return AgentFactoryJobV3.model_validate(payload)


def build_capability_evidence_backend(
    *,
    context: CapabilityV3EvidenceBuilderContext,
) -> PackageCV3CapabilityEvidenceBackend:
    """Public production constructor used by the authenticated 8091 runtime app."""

    executor = PackageCV3CapabilityReleaseExecutor(context)
    return PackageCV3CapabilityEvidenceBackend(
        executor=executor,
        artifact_store=context.artifact_store,
    )


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decimal_string(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"
