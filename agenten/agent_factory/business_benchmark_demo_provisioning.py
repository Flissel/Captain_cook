"""Idempotent Captain/Gateway bootstrap for the two benchmark demo teams.

This module provisions only authority that can be stated truthfully before the
Hermes six-skill workflow runs.  In particular it never fabricates workflow or
provider evidence to make :class:`ProductionBusinessBenchmarkScope` resolve.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import unquote, urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_factory.business_benchmark_candidate_seeds import (
    CLAIMS_SEED_PROFILE,
    RENEWAL_SEED_PROFILE,
    package_business_benchmark_seed,
)
from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkPolicyV1,
)
from agenten.agent_factory.business_benchmark_production import (
    CaptainBusinessBenchmarkPolicyBindingV1,
)
from agenten.agent_factory.business_benchmark_production_ports import (
    BusinessBenchmarkCandidateAuthority,
    BusinessBenchmarkContentAddressedArtifactStore,
)
from agenten.agent_factory.business_benchmark_paths import (
    canonical_business_benchmark_authority_root,
)
from agenten.agent_factory.business_benchmark_provisioning import (
    CaptainPrivateBusinessBenchmarkSuiteLoader,
    CanonicalPrivateBusinessBenchmarkProvisioner,
    ProvisionedBusinessBenchmarkSuiteRefV1,
)
from agenten.agent_factory.business_benchmark_technical_holdout import (
    CanonicalTechnicalBusinessHoldoutProvisioner,
    ProvisionedTechnicalBusinessHoldoutV1,
)
from agenten.agent_factory.candidate_evaluation import (
    FactoryCandidateArtifact,
    FactoryCandidateManifest,
)
from agenten.agent_factory.contracts import (
    AgentFactoryJobV3,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryLease,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.execution_budget import FactoryBudgetProjection
from agenten.agent_factory.execution_policy import (
    FactoryExecutionMode,
    FactoryExecutionPolicyV1,
    FactoryLiveCapability,
    FactorySandboxMode,
)
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.n8n_tools import TypedN8nTool
from agenten.agent_factory.skill_evaluation import (
    ReleasedHermesSkill,
    ToolGapMarker,
    ToolImplementationOption,
)
from agenten.agent_factory.skill_workflow_contracts import (
    FACTORY_SKILL_ID_BY_STEP,
    FactorySkillStep,
)
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.validation.contracts import (
    AcceptanceAssertion,
    AssertionKind,
    WorkBatch,
)


_ARTIFACT_PREFIX = "artifact://business-benchmark-production/"
_JOB_NAMESPACE = UUID("ca7843bd-d67a-4105-adbd-c7011f24706a")
_SKILL_RELEASED_AT = datetime(2026, 7, 1, tzinfo=timezone.utc)
_PROFILE_ORDER: tuple[tuple[Literal["claims", "renewal"], str], ...] = (
    ("claims", CLAIMS_SEED_PROFILE),
    ("renewal", RENEWAL_SEED_PROFILE),
)
_CAPABILITY = "factory_workflow"
_ASSERTION_IDS = (
    "business_value",
    "safe_tool_use",
    "mandatory_handoff",
)
_MISSING_WORKFLOW_EVIDENCE = (
    "codebase_inventory",
    "codex_build_brief",
    "codex_build_evidence",
    "team_execution_evidence",
    "real_case_tester_lease",
    "quality_warden_lease",
)
_INITIAL_WORKFLOW_STEPS = (
    FactorySkillStep.DISCOVER,
    FactorySkillStep.BRIEF_CODEX,
    FactorySkillStep.SEAL_CODEX_BUILD,
    FactorySkillStep.EXECUTE_TEAM,
    FactorySkillStep.EVALUATE_TEAM,
    FactorySkillStep.REPORT_CAPTAIN,
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class BusinessBenchmarkDemoProvisioningSettings(_FrozenModel):
    """Explicit non-secret inputs for a reproducible isolated bootstrap."""

    workspace_root: Path
    test_mariadb_dsn: str = Field(repr=False, exclude=True)
    issued_at: datetime
    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    maximum_usd_per_team: Decimal
    suite_version: int = Field(ge=1, strict=True)
    seed_version_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

    @field_validator("issued_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("provisioning epoch must be UTC")
        return value.astimezone(timezone.utc)

    @field_validator("maximum_usd_per_team", mode="before")
    @classmethod
    def require_positive_cents(cls, value: object) -> Decimal:
        if isinstance(value, float) or isinstance(value, bool):
            raise TypeError("maximum USD must be a decimal string")
        amount = Decimal(value)
        if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
            raise ValueError("maximum USD must be positive and use cents")
        return amount

    @field_validator("test_mariadb_dsn")
    @classmethod
    def require_safe_database(cls, value: str) -> str:
        assert_local_captain_test_dsn(value)
        return value


class BusinessBenchmarkNextDispatchV1(_FrozenModel):
    """Exact legal dispatcher input after the provisioning transaction."""

    schema_name: Literal["captain.business-benchmark-next-dispatch.v1"] = Field(
        default="captain.business-benchmark-next-dispatch.v1",
        alias="schema",
        serialization_alias="schema",
    )
    job_id: UUID
    action: Literal["dispatch_agent_architect"]
    role: Literal[FactoryRole.AGENT_ARCHITECT]
    attempt: Literal[1] = 1
    lease_id: str
    steps: tuple[Literal[FactorySkillStep.DISCOVER]] = (
        FactorySkillStep.DISCOVER,
    )
    input_ref: ArtifactRef
    released_skill: ReleasedHermesSkill


class BusinessBenchmarkDemoTeamPlanV1(_FrozenModel):
    profile: Literal["claims", "renewal"]
    profile_id: str
    job: AgentFactoryJobV3
    suite: ProvisionedBusinessBenchmarkSuiteRefV1
    technical_holdout: ProvisionedTechnicalBusinessHoldoutV1
    candidate_id: str
    candidate_ref: ArtifactRef
    candidate_manifest_ref: ArtifactRef
    team_manifest_ref: ArtifactRef
    policy_binding_ref: ArtifactRef
    released_skills: tuple[ReleasedHermesSkill, ...]
    initial_lease: FactoryLease | None
    blocker: ToolGapMarker
    work_batch: WorkBatch | None = None
    released_workflow_steps: tuple[FactorySkillStep, ...]
    initial_workflow_steps: tuple[FactorySkillStep, ...]
    missing_gateway_evidence: tuple[str, ...]
    next_action: Literal["dispatch_agent_architect", "continue_existing_lifecycle"]
    next_dispatch: BusinessBenchmarkNextDispatchV1 | None
    production_scope_resolvable: Literal[False] = False
    gateway_budget_remaining_usd: Decimal | None = None


class BusinessBenchmarkDemoProvisioningResultV1(_FrozenModel):
    schema_name: Literal["captain.business-benchmark-demo-provisioning.v1"] = Field(
        default="captain.business-benchmark-demo-provisioning.v1",
        alias="schema",
        serialization_alias="schema",
    )
    mode: Literal["dry_run", "applied"]
    issued_at: datetime
    database: Literal["captain_test"] = "captain_test"
    teams: tuple[BusinessBenchmarkDemoTeamPlanV1, BusinessBenchmarkDemoTeamPlanV1]
    created_job_ids: tuple[UUID, ...] = ()
    resumed_job_ids: tuple[UUID, ...] = ()
    checkpoint_job_ids: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def require_exact_apply_disposition(self) -> "BusinessBenchmarkDemoProvisioningResultV1":
        created = set(self.created_job_ids)
        resumed = set(self.resumed_job_ids)
        checkpoint = set(self.checkpoint_job_ids)
        if created & resumed or created & checkpoint or resumed & checkpoint:
            raise ValueError("benchmark job apply dispositions must be disjoint")
        if self.mode == "dry_run":
            if created or resumed or checkpoint:
                raise ValueError("dry-run benchmark provisioning cannot report writes")
            return self
        team_ids = {team.job.job_id for team in self.teams}
        if created | resumed | checkpoint != team_ids:
            raise ValueError("applied benchmark jobs require one exact disposition")
        return self


class BusinessBenchmarkDemoResumeStateV1(_FrozenModel):
    """Read-only Gateway projection needed to resume without rewinding lifecycle."""

    job: AgentFactoryJobV3
    phase: FactoryPhase | None
    attempt: int = Field(ge=1, le=5, strict=True)


class BusinessBenchmarkDemoGatewayPort(Protocol):
    """The narrow sole-writer surface used by the bootstrap."""

    def resume_state(self, job_id: UUID) -> BusinessBenchmarkDemoResumeStateV1 | None: ...

    def register(self, job: AgentFactoryJobV3) -> None: ...

    def assign_released_skills(
        self,
        job: AgentFactoryJobV3,
        skills: Mapping[FactorySkillStep, ReleasedHermesSkill],
    ) -> None: ...

    def append_forge_requested(self, block: FactoryEvidenceBlock) -> None: ...

    def record_lease(self, lease: FactoryLease) -> None: ...

    def persist_work_batch(
        self,
        job: AgentFactoryJobV3,
        batch: WorkBatch,
    ) -> None: ...

    def budget_projection(self, job_id: UUID) -> FactoryBudgetProjection: ...


@dataclass(frozen=True)
class _PreparedTeam:
    plan: BusinessBenchmarkDemoTeamPlanV1
    candidate_manifest: FactoryCandidateManifest
    artifacts: tuple[tuple[ArtifactRef, bytes], ...]
    policy_binding: CaptainBusinessBenchmarkPolicyBindingV1
    forge_requested: FactoryEvidenceBlock
    resumed: bool = False
    checkpoint: bool = False


class BusinessBenchmarkDemoProvisioner:
    """Prepare or apply two immutable demo jobs without inventing evidence."""

    def __init__(
        self,
        settings: BusinessBenchmarkDemoProvisioningSettings,
        *,
        gateway: BusinessBenchmarkDemoGatewayPort | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = BusinessBenchmarkDemoProvisioningSettings.model_validate(
            settings.model_dump(mode="python")
            | {"test_mariadb_dsn": settings.test_mariadb_dsn}
        )
        self._gateway = gateway
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def plan(self) -> BusinessBenchmarkDemoProvisioningResultV1:
        prepared = self._prepare()
        return BusinessBenchmarkDemoProvisioningResultV1(
            mode="dry_run",
            issued_at=self._settings.issued_at,
            teams=(prepared[0].plan, prepared[1].plan),
        )

    def apply(self) -> BusinessBenchmarkDemoProvisioningResultV1:
        self.validate_apply_preconditions()
        prepared = self._prepare()
        if self._gateway is None:
            raise ValueError(
                "Gateway business benchmark provisioning authority is required for --apply"
            )
        gateway = self._gateway
        authority_root = self._authority_root
        suites = CanonicalPrivateBusinessBenchmarkProvisioner(
            authority_root / "suites"
        ).provision(
            suite_version=self._settings.suite_version,
            seed_version_id=self._settings.seed_version_id,
        )
        suite_loader = CaptainPrivateBusinessBenchmarkSuiteLoader(
            authority_root / "suites"
        )
        technical_provisioner = CanonicalTechnicalBusinessHoldoutProvisioner(
            authority_root / "technical-holdouts"
        )
        technical_holdouts = {
            item.profile_id: technical_provisioner.provision(
                suite_loader.load_suite(
                    item.suite_ref,
                    expected_profile_id=item.profile_id,
                    expected_suite_version=item.suite_version,
                )
            )
            for item in suites.suites
        }
        cas = BusinessBenchmarkContentAddressedArtifactStore(authority_root / "cas")
        prepared = tuple(
            self._prepare_for_apply(
                item,
                gateway.resume_state(item.plan.job.job_id),
                cas,
            )
            for item in prepared
        )

        applied: list[BusinessBenchmarkDemoTeamPlanV1] = []
        for item in prepared:
            if item.checkpoint:
                budget = gateway.budget_projection(item.plan.job.job_id)
                applied.append(
                    item.plan.model_copy(
                        update={"gateway_budget_remaining_usd": budget.remaining_usd}
                    )
                )
                continue
            expected_suite = next(
                suite for suite in suites.suites if suite.profile_id == item.plan.profile_id
            )
            if expected_suite != item.plan.suite:
                raise ValueError("canonical benchmark suite changed after dry-run planning")
            if technical_holdouts[item.plan.profile_id] != item.plan.technical_holdout:
                raise ValueError(
                    "canonical technical holdout changed after dry-run planning"
                )
            for expected_ref, content in item.artifacts:
                actual = cas.put(
                    content,
                    expected_ref.media_type,
                    namespace=_namespace_from_ref(expected_ref),
                )
                if actual != expected_ref:
                    raise ValueError("content-addressed artifact changed after planning")
            BusinessBenchmarkCandidateAuthority(cas).bind_candidate(
                job=item.plan.job,
                manifest_ref=item.plan.candidate_manifest_ref,
            )
            cas.bind(
                "benchmark-policy",
                f"{item.plan.job.job_id}:1",
                item.plan.policy_binding_ref,
            )
            job_binding_payload = _canonical_json(
                item.plan.job.model_dump(mode="json", by_alias=True)
            )
            job_binding_ref = cas.put(
                job_binding_payload,
                "application/json",
                namespace="demo-job-binding",
            )
            try:
                cas.bind(
                    "demo-team-job",
                    self._demo_team_job_identity(item.plan.profile_id),
                    job_binding_ref,
                )
            except ValueError as exc:
                raise ValueError("immutable demo team job binding changed") from exc

            if not item.resumed:
                gateway.register(item.plan.job)
            if item.plan.work_batch is not None:
                gateway.persist_work_batch(item.plan.job, item.plan.work_batch)
            gateway.assign_released_skills(
                item.plan.job,
                {
                    step: skill
                    for step, skill in zip(
                        item.plan.released_workflow_steps,
                        item.plan.released_skills,
                        strict=True,
                    )
                },
            )
            gateway.append_forge_requested(item.forge_requested)
            if item.plan.initial_lease is None:
                raise ValueError("resumable benchmark dispatch requires an active lease")
            gateway.record_lease(item.plan.initial_lease)
            budget = gateway.budget_projection(item.plan.job.job_id)
            if (
                budget.job_id != item.plan.job.job_id
                or budget.limit_usd != self._settings.maximum_usd_per_team
                or budget.consumed_usd != 0
                or budget.reserved_usd != 0
                or budget.remaining_usd != self._settings.maximum_usd_per_team
            ):
                raise ValueError("Gateway benchmark budget projection is not pristine")
            applied.append(
                item.plan.model_copy(
                    update={"gateway_budget_remaining_usd": budget.remaining_usd}
                )
            )
        return BusinessBenchmarkDemoProvisioningResultV1(
            mode="applied",
            issued_at=self._settings.issued_at,
            teams=(applied[0], applied[1]),
            created_job_ids=tuple(
                item.plan.job.job_id for item in prepared if not item.resumed
            ),
            resumed_job_ids=tuple(
                item.plan.job.job_id
                for item in prepared
                if item.resumed and not item.checkpoint
            ),
            checkpoint_job_ids=tuple(
                item.plan.job.job_id for item in prepared if item.checkpoint
            ),
        )

    def _prepare_for_apply(
        self,
        item: _PreparedTeam,
        state: BusinessBenchmarkDemoResumeStateV1 | None,
        cas: BusinessBenchmarkContentAddressedArtifactStore,
    ) -> _PreparedTeam:
        if state is not None:
            return self._resume_prepared(item, state)
        binding = cas.binding(
            "demo-team-job",
            self._demo_team_job_identity(item.plan.profile_id),
        )
        if binding is None:
            return item
        try:
            canonical_job = AgentFactoryJobV3.model_validate_json(
                cas.read_bytes(binding)
            )
        except (OSError, ValueError) as exc:
            raise ValueError(
                "canonical demo team job binding is unavailable or invalid"
            ) from exc
        restored = self._resume_prepared(
            item,
            BusinessBenchmarkDemoResumeStateV1(
                job=canonical_job,
                phase=None,
                attempt=1,
            ),
        )
        return replace(restored, resumed=False)

    def _demo_team_job_identity(self, profile_id: str) -> str:
        return (
            f"{profile_id}:"
            f"{self._settings.suite_version}:"
            f"{self._settings.seed_version_id}"
        )

    def _resume_prepared(
        self,
        item: _PreparedTeam,
        state: BusinessBenchmarkDemoResumeStateV1 | None,
    ) -> _PreparedTeam:
        if state is None:
            return item
        existing = state.job
        planned_binding = item.plan.job.model_dump(
            mode="json",
            by_alias=True,
            exclude={"occurred_at", "deadline_at"},
        )
        existing_binding = existing.model_dump(
            mode="json",
            by_alias=True,
            exclude={"occurred_at", "deadline_at"},
        )
        if existing_binding != planned_binding:
            raise ValueError("immutable stable demo job binding changed")
        if state.phase not in {None, FactoryPhase.FORGE_REQUESTED} or state.attempt != 1:
            return _PreparedTeam(
                plan=item.plan.model_copy(
                    update={
                        "job": existing,
                        "initial_lease": None,
                        "next_action": "continue_existing_lifecycle",
                        "next_dispatch": None,
                        "work_batch": (
                            _renewal_work_batch(existing)
                            if item.plan.profile == "renewal"
                            else None
                        ),
                    }
                ),
                candidate_manifest=item.candidate_manifest,
                artifacts=item.artifacts,
                policy_binding=CaptainBusinessBenchmarkPolicyBindingV1.create(
                    job=existing,
                    attempt=1,
                    policy=item.policy_binding.policy,
                ),
                forge_requested=_forge_requested(
                    existing,
                    candidate_ref=item.plan.candidate_ref,
                    manifest_ref=item.plan.candidate_manifest_ref,
                    team_ref=item.plan.team_manifest_ref,
                ),
                resumed=True,
                checkpoint=True,
            )
        lease = issue_factory_lease(
            job=existing,
            role=FactoryRole.AGENT_ARCHITECT,
            attempt=1,
            workspace_ref=_demo_workspace_ref(
                item.plan.profile,
                self._settings.issued_at,
            ),
            now=self._settings.issued_at,
        )
        blocker, _ = _prepare_blocker(existing, item.plan.profile)
        forge = _forge_requested(
            existing,
            candidate_ref=item.plan.candidate_ref,
            manifest_ref=item.plan.candidate_manifest_ref,
            team_ref=item.plan.team_manifest_ref,
        )
        plan = item.plan.model_copy(
            update={
                "job": existing,
                "initial_lease": lease,
                "blocker": blocker,
                "work_batch": (
                    _renewal_work_batch(existing)
                    if item.plan.profile == "renewal"
                    else None
                ),
                "next_dispatch": item.plan.next_dispatch.model_copy(
                    update={"lease_id": lease.lease_id}
                ),
            }
        )
        return _PreparedTeam(
            plan=plan,
            candidate_manifest=item.candidate_manifest,
            artifacts=item.artifacts,
            policy_binding=CaptainBusinessBenchmarkPolicyBindingV1.create(
                job=existing,
                attempt=1,
                policy=item.policy_binding.policy,
            ),
            forge_requested=forge,
            resumed=True,
        )

    def validate_apply_preconditions(self) -> None:
        """Fail before opening Gateway resources when the initial lease is stale."""

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
            raise ValueError("provisioning clock must be UTC")
        if not (
            self._settings.issued_at
            <= now
            < self._settings.issued_at + timedelta(minutes=15)
        ):
            raise ValueError(
                "provisioning epoch must still produce an active lease; choose a fresh UTC epoch"
            )

    @property
    def _authority_root(self) -> Path:
        return canonical_business_benchmark_authority_root(
            self._settings.workspace_root
        )

    def _prepare(self) -> tuple[_PreparedTeam, _PreparedTeam]:
        assert_local_captain_test_dsn(self._settings.test_mariadb_dsn)
        with tempfile.TemporaryDirectory(prefix="captain-benchmark-demo-plan-") as raw:
            temporary = Path(raw)
            preview = CanonicalPrivateBusinessBenchmarkProvisioner(
                temporary / ".captain-cook" / "private" / "suites"
            ).provision(
                suite_version=self._settings.suite_version,
                seed_version_id=self._settings.seed_version_id,
            )
            preview_suite_root = temporary / ".captain-cook" / "private" / "suites"
            preview_suite_loader = CaptainPrivateBusinessBenchmarkSuiteLoader(
                preview_suite_root
            )
            preview_technical_provisioner = CanonicalTechnicalBusinessHoldoutProvisioner(
                temporary / ".captain-cook" / "private" / "technical-holdouts"
            )
            skills, skill_artifacts = _prepare_released_skills(
                Path(__file__).resolve().parents[2],
            )
            prepared: list[_PreparedTeam] = []
            for profile, profile_id in _PROFILE_ORDER:
                suite = next(
                    item for item in preview.suites if item.profile_id == profile_id
                )
                technical_holdout = preview_technical_provisioner.provision(
                    preview_suite_loader.load_suite(
                        suite.suite_ref,
                        expected_profile_id=suite.profile_id,
                        expected_suite_version=suite.suite_version,
                    )
                )
                seed = package_business_benchmark_seed(
                    profile_id,
                    temporary / "candidates" / profile,
                )
                manifest, candidate_artifacts = _prepare_candidate(seed)
                job, job_artifacts = _prepare_job(
                    settings=self._settings,
                    profile=profile,
                    profile_id=profile_id,
                    suite=suite,
                    technical_holdout=technical_holdout,
                    candidate=manifest,
                )
                policy = CaptainBusinessBenchmarkPolicyBindingV1.create(
                    job=job,
                    attempt=1,
                    policy=BusinessBenchmarkPolicyV1(
                        schema="captain.business-benchmark-policy.v1"
                    ),
                )
                policy_bytes = _canonical_json(
                    policy.model_dump(mode="json", by_alias=True)
                )
                policy_ref = _predicted_ref(
                    policy_bytes,
                    "application/json",
                    "benchmark-policy",
                )
                blocker, blocker_artifacts = _prepare_blocker(job, profile)
                initial_lease = issue_factory_lease(
                    job=job,
                    role=FactoryRole.AGENT_ARCHITECT,
                    attempt=1,
                    workspace_ref=_demo_workspace_ref(
                        profile,
                        self._settings.issued_at,
                    ),
                    now=self._settings.issued_at,
                )
                ordered_skills = tuple(skills[step] for step in FactorySkillStep)
                forge = _forge_requested(
                    job,
                    candidate_ref=manifest.source_archive_ref,
                    manifest_ref=_manifest_ref(manifest),
                    team_ref=manifest.team_manifest.reference,
                )
                artifacts = _deduplicate_artifacts(
                    (
                        *skill_artifacts,
                        *candidate_artifacts,
                        *job_artifacts,
                        (policy_ref, policy_bytes),
                        *blocker_artifacts,
                    )
                )
                prepared.append(
                    _PreparedTeam(
                        plan=BusinessBenchmarkDemoTeamPlanV1(
                            profile=profile,
                            profile_id=profile_id,
                            job=job,
                            suite=suite,
                            technical_holdout=technical_holdout,
                            candidate_id=manifest.candidate_id,
                            candidate_ref=manifest.source_archive_ref,
                            candidate_manifest_ref=_manifest_ref(manifest),
                            team_manifest_ref=manifest.team_manifest.reference,
                            policy_binding_ref=policy_ref,
                            released_skills=ordered_skills,
                            initial_lease=initial_lease,
                            blocker=blocker,
                            work_batch=(
                                _renewal_work_batch(job)
                                if profile == "renewal"
                                else None
                            ),
                            released_workflow_steps=tuple(FactorySkillStep),
                            initial_workflow_steps=_INITIAL_WORKFLOW_STEPS,
                            missing_gateway_evidence=_MISSING_WORKFLOW_EVIDENCE,
                            next_action="dispatch_agent_architect",
                            next_dispatch=BusinessBenchmarkNextDispatchV1(
                                schema="captain.business-benchmark-next-dispatch.v1",
                                job_id=job.job_id,
                                action="dispatch_agent_architect",
                                role=FactoryRole.AGENT_ARCHITECT,
                                lease_id=initial_lease.lease_id,
                                input_ref=job.input_ref,
                                released_skill=skills[FactorySkillStep.DISCOVER],
                            ),
                        ),
                        candidate_manifest=manifest,
                        artifacts=artifacts,
                        policy_binding=policy,
                        forge_requested=forge,
                    )
                )
        return prepared[0], prepared[1]


def _demo_workspace_ref(profile: str, issued_at: datetime) -> str:
    epoch = hashlib.sha256(issued_at.isoformat().encode("utf-8")).hexdigest()[:16]
    return f"workspace://business-benchmark-demo/{profile}/epoch-{epoch}"


def _renewal_work_batch(job: AgentFactoryJobV3) -> WorkBatch:
    """Return the public Captain authority for one read-only Renewal n8n node."""

    return WorkBatch(
        batch_id=f"renewal-{job.job_id.hex[:24]}",
        title="Renewal benchmark context read",
        goal="Authorize one Captain-scoped read-only renewal context operation.",
        subtask_ids=["renewal_context_read"],
        target="n8n",
        runtime="n8n",
        runtime_version="v1",
        interface_schema="captain-n8n-artifact/v1",
        capability_tags=["n8n-builder"],
        constraints=[
            f"factory-job-id:{job.job_id}",
            "effect:read_only",
            "external-mutation:forbidden",
        ],
        acceptance_criteria=[
            AcceptanceAssertion(
                assertion_id="read-only",
                kind=AssertionKind.STATUS_EQUALS,
                expected="succeeded",
                description=(
                    "renewal_context_read is read-only and performs no external mutation."
                ),
            )
        ],
    )


def assert_local_captain_test_dsn(dsn: str) -> None:
    """Reject every database other than an exact local isolated test target."""

    message = "TEST_MARIADB_DSN must target the exact local captain_test database"
    try:
        parsed = urlparse(dsn)
        _ = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError(message) from exc
    database = unquote(parsed.path[1:]) if parsed.path.startswith("/") else ""
    if (
        parsed.scheme not in {"mysql", "mariadb"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or database != "captain_test"
    ):
        raise ValueError(message)


def _prepare_candidate(seed) -> tuple[
    FactoryCandidateManifest,
    tuple[tuple[ArtifactRef, bytes], ...],
]:
    archive_bytes = seed.source_archive.read_bytes()
    source_ref = _predicted_ref(
        archive_bytes, "application/zip", "candidate-archive"
    )
    by_path: dict[str, bytes] = {}
    with zipfile.ZipFile(seed.source_archive) as archive:
        for name in archive.namelist():
            by_path[name] = archive.read(name)

    def candidate_artifact(
        value: FactoryCandidateArtifact,
        namespace: str,
    ) -> tuple[FactoryCandidateArtifact, tuple[ArtifactRef, bytes]]:
        content = by_path[value.relative_path]
        reference = _predicted_ref(
            content,
            value.reference.media_type,
            namespace,
        )
        return (
            FactoryCandidateArtifact(
                reference=reference,
                relative_path=value.relative_path,
            ),
            (reference, content),
        )

    team, team_artifact = candidate_artifact(
        seed.candidate.team_manifest, "candidate-team"
    )
    workflows = tuple(
        candidate_artifact(item, "candidate-workflow")
        for item in seed.candidate.workflow_artifacts
    )
    schemas = tuple(
        candidate_artifact(item, "candidate-schema")
        for item in seed.candidate.tool_schema_artifacts
    )
    schema_by_old_uri = {
        original.reference.uri: rewritten.reference.uri
        for original, (rewritten, _) in zip(
            seed.candidate.tool_schema_artifacts,
            schemas,
            strict=True,
        )
    }
    tools = tuple(
        TypedN8nTool(
            name=tool.name,
            description=tool.description,
            input_schema_ref=schema_by_old_uri[tool.input_schema_ref],
            output_schema_ref=schema_by_old_uri[tool.output_schema_ref],
        )
        for tool in seed.candidate.n8n_tools
    )
    manifest = seed.candidate.model_copy(
        update={
            "source_archive_ref": source_ref,
            "team_manifest": team,
            "workflow_artifacts": tuple(item[0] for item in workflows),
            "tool_schema_artifacts": tuple(item[0] for item in schemas),
            "n8n_tools": tools,
            "n8n_tool_references": tuple(tool.opaque_reference() for tool in tools),
        }
    )
    manifest = FactoryCandidateManifest.model_validate(
        manifest.model_dump(mode="json")
    )
    manifest_bytes = manifest.model_dump_json().encode("utf-8")
    manifest_ref = _predicted_ref(
        manifest_bytes, "application/json", "candidate-manifest"
    )
    return manifest, (
        (source_ref, archive_bytes),
        team_artifact,
        *(item[1] for item in workflows),
        *(item[1] for item in schemas),
        (manifest_ref, manifest_bytes),
    )


def _prepare_job(
    *,
    settings: BusinessBenchmarkDemoProvisioningSettings,
    profile: str,
    profile_id: str,
    suite: ProvisionedBusinessBenchmarkSuiteRefV1,
    technical_holdout: ProvisionedTechnicalBusinessHoldoutV1,
    candidate: FactoryCandidateManifest,
) -> tuple[AgentFactoryJobV3, tuple[tuple[ArtifactRef, bytes], ...]]:
    public_input = _canonical_json(
        {
            "schema": "captain.business-benchmark-demo-input.v1",
            "profile": profile,
            "profile_id": profile_id,
            "candidate_id": candidate.candidate_id,
            "suite_version": settings.suite_version,
        }
    )
    compiled = _canonical_json(
        {
            "schema": "captain.business-benchmark-demo-compiled-spec.v1",
            "profile_id": profile_id,
            "required_capability": _CAPABILITY,
            "assertion_ids": _ASSERTION_IDS,
            "candidate_sha256": candidate.source_archive_ref.sha256,
        }
    )
    graph = _canonical_json(
        {
            "schema": "captain.factory-work-graph.v1",
            "nodes": list(step.value for step in FactorySkillStep),
            "dependency_order": list(step.value for step in FactorySkillStep),
        }
    )
    input_ref = _predicted_ref(public_input, "application/json", "demo-input")
    compiled_ref = _predicted_ref(compiled, "application/json", "demo-spec")
    graph_ref = _predicted_ref(graph, "application/json", "demo-graph")
    identity = "|".join(
        (
            "business-benchmark-demo-job-v1",
            profile_id,
            str(settings.suite_version),
            settings.seed_version_id,
        )
    )
    job_id = uuid5(_JOB_NAMESPACE, identity)
    correlation_id = uuid5(_JOB_NAMESPACE, f"correlation|{identity}")
    event_id = uuid5(_JOB_NAMESPACE, f"event|{identity}")
    policy = FactoryExecutionPolicyV1(
        schema="captain.factory-execution-policy.v1",
        mode=FactoryExecutionMode.DEMO,
        live_execution=True,
        max_cost_usd=settings.maximum_usd_per_team,
        max_runtime_seconds=86400,
        required_live_runs=1,
        allowed_models=(settings.model,),
        live_capabilities=(
            FactoryLiveCapability.MODEL_INVOKE,
            FactoryLiveCapability.CAPTAIN_TEST_DATABASE,
        ),
        sandbox_mode=FactorySandboxMode.WORKSPACE_WRITE,
    )
    job = AgentFactoryJobV3(
        schema="captain.agent-factory-job.v3",
        event_id=event_id,
        correlation_id=correlation_id,
        occurred_at=settings.issued_at,
        producer="captain",
        job_id=job_id,
        subject_version=1,
        input_ref=input_ref,
        compiled_spec_ref=compiled_ref,
        dependency_graph_ref=graph_ref,
        required_capability=_CAPABILITY,
        acceptance_assertion_ids=_ASSERTION_IDS,
        private_holdout_refs=(technical_holdout.holdout_ref, suite.suite_ref),
        max_behavioral_iterations=5,
        deadline_at=settings.issued_at + timedelta(seconds=policy.max_runtime_seconds),
        execution_policy=policy,
    )
    return job, (
        (input_ref, public_input),
        (compiled_ref, compiled),
        (graph_ref, graph),
    )


def _prepare_released_skills(
    workspace_root: Path,
) -> tuple[
    dict[FactorySkillStep, ReleasedHermesSkill],
    tuple[tuple[ArtifactRef, bytes], ...],
]:
    skills: dict[FactorySkillStep, ReleasedHermesSkill] = {}
    artifacts: list[tuple[ArtifactRef, bytes]] = []
    root = workspace_root / "agenten" / "agent_factory" / "skills"
    for step in FactorySkillStep:
        skill_id = FACTORY_SKILL_ID_BY_STEP[step]
        release_version = {
            FactorySkillStep.DISCOVER: 5,
            FactorySkillStep.BRIEF_CODEX: 2,
        }.get(step, 1)
        directory = root / skill_id
        content = _directory_zip_bytes(directory)
        archived_reference = _predicted_ref(
            content, "application/zip", "released-skill"
        )
        directory_digest = _skill_directory_digest(directory)
        release_reference = ArtifactRef(
            uri=f"artifact://released-skills/{skill_id}/v{release_version}",
            sha256=directory_digest,
            media_type="application/json",
        )
        skills[step] = ReleasedHermesSkill(
            schema="captain.released-hermes-skill.v1",
            skill_id=skill_id,
            version=release_version,
            capability=_CAPABILITY,
            content_ref=release_reference,
            content_sha256=directory_digest,
            status="released",
            released_at=_SKILL_RELEASED_AT,
            producer="captain",
        )
        artifacts.append((archived_reference, content))
    return skills, tuple(artifacts)


def _prepare_blocker(
    job: AgentFactoryJobV3,
    profile: str,
) -> tuple[ToolGapMarker, tuple[tuple[ArtifactRef, bytes], ...]]:
    input_bytes = _canonical_json(
        {
            "schema": "captain.business-benchmark-workflow-input.v1",
            "job_id": str(job.job_id),
            "released_steps": [step.value for step in FactorySkillStep],
            "initial_steps": [step.value for step in _INITIAL_WORKFLOW_STEPS],
            "conditional_retry_step": FactorySkillStep.IMPROVE_TEAM.value,
        }
    )
    output_bytes = _canonical_json(
        {
            "schema": "captain.business-benchmark-workflow-output.v1",
            "required_evidence": list(_MISSING_WORKFLOW_EVIDENCE),
        }
    )
    evidence_bytes = _canonical_json(
        {
            "schema": "captain.business-benchmark-demo-blocker-evidence.v1",
            "code": "TODO_TOOL.v1",
            "reason": "real_gateway_workflow_evidence_required",
            "next_action": "dispatch_agent_architect",
            "profile": profile,
        }
    )
    input_ref = _predicted_ref(input_bytes, "application/json", "blocker-input")
    output_ref = _predicted_ref(output_bytes, "application/json", "blocker-output")
    evidence_ref = _predicted_ref(
        evidence_bytes, "application/json", "blocker-evidence"
    )
    marker = ToolGapMarker(
        schema="TODO_TOOL.v1",
        gap_id=f"benchmark_{profile}_real_workflow_evidence",
        severity="required",
        input_contract_ref=input_ref,
        output_contract_ref=output_ref,
        least_privilege_capability="factory_workflow",
        implementation_options=(
            ToolImplementationOption(
                option_id="execute_released_six_skill_chain",
                description=(
                    "Run the released Hermes skill chain and record real Gateway "
                    "workflow, lease, build, and execution evidence."
                ),
                acceptance_assertion_id="business_value",
            ),
        ),
        acceptance_assertion_ids=job.acceptance_assertion_ids,
        evidence_ref=evidence_ref,
        status="unresolved",
    )
    return marker, (
        (input_ref, input_bytes),
        (output_ref, output_bytes),
        (evidence_ref, evidence_bytes),
    )


def _forge_requested(
    job: AgentFactoryJobV3,
    *,
    candidate_ref: ArtifactRef,
    manifest_ref: ArtifactRef,
    team_ref: ArtifactRef,
) -> FactoryEvidenceBlock:
    event_id = uuid5(NAMESPACE_URL, f"factory-forge-requested|{job.job_id}|1")
    return FactoryEvidenceBlock(
        schema="captain.agent-factory-block.v1",
        event_id=event_id,
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        causation_id=job.event_id,
        occurred_at=job.occurred_at,
        producer="captain",
        subject_version=job.subject_version,
        attempt=1,
        phase=FactoryPhase.FORGE_REQUESTED,
        status=FactoryBlockStatus.SUCCEEDED,
        artifact_refs=(candidate_ref, manifest_ref, team_ref),
        assertion_ids=job.acceptance_assertion_ids,
    )


def _manifest_ref(manifest: FactoryCandidateManifest) -> ArtifactRef:
    return _predicted_ref(
        manifest.model_dump_json().encode("utf-8"),
        "application/json",
        "candidate-manifest",
    )


def _directory_zip_bytes(root: Path) -> bytes:
    if not root.is_dir():
        raise FileNotFoundError(f"released skill source is missing: {root.name}")
    with tempfile.TemporaryFile() as stream:
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
            for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
                if not path.is_file():
                    continue
                info = zipfile.ZipInfo(
                    path.relative_to(root).as_posix(),
                    date_time=(1980, 1, 1, 0, 0, 0),
                )
                info.compress_type = zipfile.ZIP_STORED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes())
        stream.seek(0)
        return stream.read()


def _skill_directory_digest(root: Path) -> str:
    manifest = []
    entries = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
    if any(item.is_symlink() for item in entries):
        raise ValueError("released skill source cannot contain symlinks")
    for path in (item for item in entries if item.is_file()):
        content = path.read_bytes()
        manifest.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        )
    return hashlib.sha256(_canonical_json(manifest)).hexdigest()


def _predicted_ref(
    content: bytes,
    media_type: str,
    namespace: str,
) -> ArtifactRef:
    digest = hashlib.sha256(content).hexdigest()
    return ArtifactRef(
        uri=f"{_ARTIFACT_PREFIX}{namespace}/{digest}",
        sha256=digest,
        media_type=media_type,
    )


def _namespace_from_ref(reference: ArtifactRef) -> str:
    if not reference.uri.startswith(_ARTIFACT_PREFIX):
        raise ValueError("planned artifact is outside the benchmark CAS")
    parts = reference.uri.removeprefix(_ARTIFACT_PREFIX).split("/")
    if len(parts) != 2 or parts[1] != reference.sha256:
        raise ValueError("planned artifact reference is invalid")
    return parts[0]


def _deduplicate_artifacts(
    artifacts: tuple[tuple[ArtifactRef, bytes], ...],
) -> tuple[tuple[ArtifactRef, bytes], ...]:
    unique: dict[tuple[str, str, str], tuple[ArtifactRef, bytes]] = {}
    for reference, content in artifacts:
        key = (reference.uri, reference.sha256, reference.media_type)
        previous = unique.setdefault(key, (reference, content))
        if previous[1] != content:
            raise ValueError("content-addressed artifact collision")
    return tuple(unique.values())


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


__all__ = [
    "BusinessBenchmarkDemoProvisioner",
    "BusinessBenchmarkDemoProvisioningResultV1",
    "BusinessBenchmarkDemoProvisioningSettings",
    "BusinessBenchmarkDemoTeamPlanV1",
    "BusinessBenchmarkNextDispatchV1",
    "assert_local_captain_test_dsn",
]
