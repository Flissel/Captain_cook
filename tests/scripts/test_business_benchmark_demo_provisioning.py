from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID

import pytest

from agenten.agent_factory.business_benchmark_demo_provisioning import (
    BusinessBenchmarkDemoProvisioner,
    BusinessBenchmarkDemoPlanSettings,
    BusinessBenchmarkDemoProvisioningSettings,
    BusinessBenchmarkDemoResumeStateV1,
    BusinessBenchmarkDemoTeamPlanV1,
    assert_local_captain_test_dsn,
)
from agenten.agent_factory.business_benchmark_bootstrap import (
    ProductionBusinessBenchmarkBootstrapConfig,
)
from agenten.agent_factory.business_benchmark_live import (
    BusinessBenchmarkTeamSelectionV1,
    LiveBusinessBenchmarkSettings,
)
from agenten.agent_factory.business_benchmark_paths import (
    canonical_business_benchmark_authority_root,
)
from agenten.agent_factory.business_benchmark_production_ports import (
    BusinessBenchmarkCandidateAuthority,
    BusinessBenchmarkContentAddressedArtifactStore,
)
from agenten.agent_factory.business_benchmark_production import (
    CaptainBusinessBenchmarkPolicyBindingV1,
)
from agenten.agent_factory.business_benchmark_provisioning import (
    CaptainPrivateBusinessBenchmarkSuiteLoader,
)
from agenten.agent_factory.business_benchmark_technical_holdout import (
    CaptainTechnicalBusinessHoldoutEvaluator,
)
from agenten.agent_factory.contracts import (
    AgentFactoryJobV3,
    FactoryEvidenceBlock,
    FactoryLease,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.execution_budget import FactoryBudgetProjection
from agenten.agent_factory.hermes_cli import HermesCliFactory, HermesCliSettings
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.orchestration import FactoryDispatch
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import (
    FactorySkillInvocationV1,
    FactorySkillStep,
)
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from agenten.agent_factory.team_execution import CaptainReleasedSkillAuthority
from agenten.validation.contracts import AssertionKind, WorkBatch


ISSUED_AT = datetime(2026, 7, 28, 18, 0, tzinfo=timezone.utc)
LOCAL_DSN = "mariadb://captain_test:redacted@127.0.0.1:3306/captain_test"


class RecordingGateway:
    def __init__(self) -> None:
        self.jobs: dict[UUID, AgentFactoryJobV3] = {}
        self.skill_assignments: dict[
            tuple[UUID, FactorySkillStep], ReleasedHermesSkill
        ] = {}
        self.blocks: dict[UUID, FactoryEvidenceBlock] = {}
        self.leases: dict[str, FactoryLease] = {}
        self.work_batches: dict[str, tuple[UUID, WorkBatch]] = {}
        self.register_calls = 0
        self.resume_overrides: dict[UUID, BusinessBenchmarkDemoResumeStateV1] = {}

    def resume_state(self, job_id: UUID) -> BusinessBenchmarkDemoResumeStateV1 | None:
        if job_id in self.resume_overrides:
            return self.resume_overrides[job_id]
        current = self.jobs.get(job_id)
        if current is None:
            return None
        phase = (
            FactoryPhase.FORGE_REQUESTED
            if any(block.job_id == job_id for block in self.blocks.values())
            else None
        )
        return BusinessBenchmarkDemoResumeStateV1(
            job=current,
            phase=phase,
            attempt=1,
        )

    def register(self, job: AgentFactoryJobV3) -> None:
        self.register_calls += 1
        current = self.jobs.setdefault(job.job_id, job)
        if current != job:
            raise ValueError("factory job already exists with different content")

    def assign_released_skills(
        self,
        job: AgentFactoryJobV3,
        skills: dict[FactorySkillStep, ReleasedHermesSkill],
    ) -> None:
        for step, skill in skills.items():
            key = (job.job_id, step)
            current = self.skill_assignments.setdefault(key, skill)
            if current != skill:
                raise ValueError("skill assignment conflict")

    def append_forge_requested(self, block: FactoryEvidenceBlock) -> None:
        current = self.blocks.setdefault(block.event_id, block)
        if current != block:
            raise ValueError("factory block conflict")

    def record_lease(self, lease: FactoryLease) -> None:
        current = self.leases.setdefault(lease.lease_id, lease)
        if current != lease:
            raise ValueError("factory lease conflict")

    def persist_work_batch(self, job: AgentFactoryJobV3, batch: WorkBatch) -> None:
        current = self.work_batches.setdefault(batch.batch_id, (job.job_id, batch))
        if current != (job.job_id, batch):
            raise ValueError("work batch conflict")

    def budget_projection(self, job_id: UUID) -> FactoryBudgetProjection:
        job = self.jobs[job_id]
        amount = job.execution_policy.max_cost_usd
        return FactoryBudgetProjection(
            job_id=job_id,
            limit_usd=amount,
            consumed_usd=Decimal("0"),
            reserved_usd=Decimal("0"),
            remaining_usd=amount,
        )


def settings(tmp_path: Path, **overrides: object) -> BusinessBenchmarkDemoProvisioningSettings:
    payload: dict[str, object] = {
        "workspace_root": tmp_path,
        "test_mariadb_dsn": LOCAL_DSN,
        "issued_at": ISSUED_AT,
        "model": "gpt-4.1-mini",
        "maximum_usd_per_team": "5.00",
        "suite_version": 1,
        "seed_version_id": "business-benchmark-demo-2026-07",
    }
    payload.update(overrides)
    return BusinessBenchmarkDemoProvisioningSettings.model_validate(payload)


@pytest.mark.parametrize(
    "dsn",
    (
        "mariadb://captain_test:redacted@127.0.0.1:3306/production",
        "mariadb://captain_test:redacted@db.example.test:3306/captain_test",
        "postgresql://captain_test:redacted@127.0.0.1:5432/captain_test",
        "mariadb://captain_test:redacted@127.0.0.1:3306/captain_test/extra",
    ),
)
def test_dsn_guard_requires_exact_local_captain_test_database(dsn: str) -> None:
    with pytest.raises(ValueError, match="local.*captain_test"):
        assert_local_captain_test_dsn(dsn)


def test_dry_run_is_side_effect_free_and_contains_two_redacted_stable_plans(
    tmp_path: Path,
) -> None:
    provisioner = BusinessBenchmarkDemoProvisioner(settings(tmp_path))

    first = provisioner.plan()
    second = provisioner.plan()

    assert first == second
    assert first.mode == "dry_run"
    assert tuple(team.profile for team in first.teams) == ("claims", "renewal")
    assert all(len(team.released_skills) == 7 for team in first.teams)
    for team in first.teams:
        releases = dict(
            zip(team.released_workflow_steps, team.released_skills, strict=True)
        )
        assert releases[FactorySkillStep.DISCOVER].version == 5
        assert releases[FactorySkillStep.BRIEF_CODEX].version == 2
        assert releases[FactorySkillStep.IMPROVE_TEAM].version == 2
        assert all(
            release.version == 1
            for step, release in releases.items()
            if step
            not in {
                FactorySkillStep.DISCOVER,
                FactorySkillStep.BRIEF_CODEX,
                FactorySkillStep.IMPROVE_TEAM,
            }
        )
    assert all(
        team.initial_lease.role is FactoryRole.AGENT_ARCHITECT
        for team in first.teams
    )
    assert all(team.blocker.schema_name == "TODO_TOOL.v1" for team in first.teams)
    assert all(team.blocker.severity == "required" for team in first.teams)
    assert all(team.production_scope_resolvable is False for team in first.teams)
    assert all(
        team.candidate_ref.uri.endswith(team.candidate_ref.sha256)
        for team in first.teams
    )
    assert all(
        team.team_manifest_ref.uri.endswith(team.team_manifest_ref.sha256)
        for team in first.teams
    )
    assert all(len(team.job.private_holdout_refs) == 2 for team in first.teams)
    assert all(
        team.job.private_holdout_refs
        == (team.technical_holdout.holdout_ref, team.suite.suite_ref)
        for team in first.teams
    )
    serialized = first.model_dump_json().lower()
    assert "redacted_input" not in serialized
    assert "expected_decision" not in serialized
    assert "password" not in serialized
    assert LOCAL_DSN not in serialized
    assert not (tmp_path / ".captain-cook").exists()


def test_dry_run_binds_the_explicit_v35_business_value_policy(tmp_path: Path) -> None:
    from agenten.agent_factory.business_benchmark_contracts import (
        BusinessBenchmarkPolicyV1,
    )

    v35_policy = BusinessBenchmarkPolicyV1(
        schema="captain.business-benchmark-policy.v1",
        policy_id="captain-business-value-v35",
        candidate_only_safety_gates=True,
        enforce_relative_efficiency_gates=False,
        minimum_correctness_uplift_bps=500,
        minimum_completion_uplift_bps=1000,
    )

    result = BusinessBenchmarkDemoProvisioner(
        settings(
            tmp_path,
            suite_version=35,
            seed_version_id="business-benchmark-demo-2026-08-v35",
            benchmark_policy=v35_policy,
        )
    ).plan()

    assert all(team.policy == v35_policy for team in result.teams)
    assert len({team.policy_binding_ref.sha256 for team in result.teams}) == 2


def test_dry_run_plans_one_job_bound_public_read_only_renewal_work_batch(
    tmp_path: Path,
) -> None:
    first = BusinessBenchmarkDemoProvisioner(settings(tmp_path)).plan()
    second = BusinessBenchmarkDemoProvisioner(settings(tmp_path)).plan()
    claims, renewal = first.teams

    assert claims.profile == "claims"
    assert claims.work_batch is None
    assert renewal.profile == "renewal"
    assert renewal.work_batch == second.teams[1].work_batch
    assert renewal.work_batch is not None
    batch = renewal.work_batch
    assert batch.batch_id == f"renewal-{renewal.job.job_id.hex[:24]}"
    assert len(batch.batch_id) == 32
    assert batch.subtask_ids == ["renewal_context_read"]
    assert batch.target == "n8n"
    assert batch.runtime == "n8n"
    assert batch.interface_schema == "captain-n8n-artifact/v1"
    assert batch.capability_tags == ["n8n-builder"]
    assert batch.constraints == [
        f"factory-job-id:{renewal.job.job_id}",
        "effect:read_only",
        "external-mutation:forbidden",
    ]
    assert len(batch.acceptance_criteria) == 1
    assertion = batch.acceptance_criteria[0]
    assert assertion.assertion_id == "read-only"
    assert assertion.kind is AssertionKind.STATUS_EQUALS
    assert assertion.expected == "succeeded"
    assert "read-only" in assertion.description
    assert "mutation" in assertion.description
    public = json.dumps(batch.model_dump(mode="json"), sort_keys=True).lower()
    assert "holdout://" not in public
    assert "password" not in public
    assert LOCAL_DSN not in public


def test_execute_team_release_uses_canonical_factory_workflow_capability(
    tmp_path: Path,
) -> None:
    team = BusinessBenchmarkDemoProvisioner(settings(tmp_path)).plan().teams[0]
    released_by_step = dict(
        zip(team.released_workflow_steps, team.released_skills, strict=True)
    )
    released = released_by_step[FactorySkillStep.EXECUTE_TEAM]
    lease = issue_factory_lease(
        job=team.job,
        role=FactoryRole.REAL_CASE_TESTER,
        attempt=1,
        workspace_ref="workspace://factory/business-benchmark-execute-team",
        now=ISSUED_AT,
    )
    invocation = FactorySkillInvocationV1(
        schema_name="captain.factory-skill-invocation.v1",
        invocation_id=UUID("71000000-0000-0000-0000-000000000001"),
        job_id=team.job.job_id,
        correlation_id=team.job.correlation_id,
        subject_version=team.job.subject_version,
        attempt=1,
        step=FactorySkillStep.EXECUTE_TEAM,
        released_skill=released,
        input_ref=team.job.input_ref,
        input_sha256=team.job.input_ref.sha256,
        lease=lease,
        idempotency_key="f" * 64,
        acceptance_assertion_ids=team.job.acceptance_assertion_ids,
        execution_scope_ref=team.job.private_holdout_refs[0],
    )

    class Catalog:
        def released_for(
            self, job: AgentFactoryJobV3, step: FactorySkillStep
        ) -> ReleasedHermesSkill:
            assert job == team.job
            assert step is FactorySkillStep.EXECUTE_TEAM
            return released

    authorized = CaptainReleasedSkillAuthority(
        catalog=Catalog(),  # type: ignore[arg-type]
        skill_root=(
            Path(__file__).resolve().parents[2]
            / "agenten"
            / "agent_factory"
            / "skills"
        ),
    ).authorize(
        job=team.job,
        invocation=invocation,
        now=ISSUED_AT + timedelta(minutes=1),
    )

    assert team.job.required_capability == "factory_workflow"
    assert released.capability == team.job.required_capability
    assert authorized == released


def test_apply_persists_only_legal_initial_gateway_authority_and_is_idempotent(
    tmp_path: Path,
) -> None:
    gateway = RecordingGateway()
    provisioner = BusinessBenchmarkDemoProvisioner(
        settings(tmp_path),
        gateway=gateway,
        clock=lambda: ISSUED_AT + timedelta(minutes=1),
    )

    first = provisioner.apply()
    snapshot = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in (tmp_path / ".captain-cook").rglob("*")
        if path.is_file()
    }
    second = provisioner.apply()

    assert first.created_job_ids == tuple(team.job.job_id for team in first.teams)
    assert first.resumed_job_ids == ()
    assert second.created_job_ids == ()
    assert second.resumed_job_ids == tuple(team.job.job_id for team in second.teams)
    assert first.mode == "applied"
    assert len(gateway.jobs) == 2
    assert gateway.register_calls == 2
    assert len(gateway.skill_assignments) == 14
    assert len(gateway.blocks) == 2
    assert all(block.phase is FactoryPhase.FORGE_REQUESTED for block in gateway.blocks.values())
    assert len(gateway.leases) == 2
    assert len(gateway.work_batches) == 1
    renewal = next(team for team in first.teams if team.profile == "renewal")
    assert renewal.work_batch is not None
    assert gateway.work_batches == {
        renewal.work_batch.batch_id: (renewal.job.job_id, renewal.work_batch)
    }
    assert all(lease.role is FactoryRole.AGENT_ARCHITECT for lease in gateway.leases.values())
    assert all(team.gateway_budget_remaining_usd == Decimal("5.00") for team in first.teams)
    cas = BusinessBenchmarkContentAddressedArtifactStore(
        tmp_path
        / ".captain-cook"
        / "private"
        / "business-benchmarks"
        / "cas"
    )
    for team in first.teams:
        candidate = BusinessBenchmarkCandidateAuthority(cas).resolve(
            job=team.job,
            expected_candidate_id=team.candidate_id,
            expected_candidate_ref=team.candidate_ref,
        )
        assert candidate.candidate.team_manifest.reference == team.team_manifest_ref
        assert cas.binding("benchmark-policy", f"{team.job.job_id}:1") == team.policy_binding_ref
        technical = CaptainTechnicalBusinessHoldoutEvaluator(
            tmp_path
            / ".captain-cook"
            / "private"
            / "business-benchmarks"
            / "technical-holdouts",
            candidate_ref=team.candidate_ref,
            allowed_tools=(),
            clock=lambda: ISSUED_AT,
        )
        resolved = asyncio.run(technical.resolve(team.technical_holdout.holdout_ref))
        assert resolved.reference == team.job.private_holdout_refs[0]
    assert {
        path.relative_to(tmp_path): path.read_bytes()
        for path in (tmp_path / ".captain-cook").rglob("*")
        if path.is_file()
    } == snapshot


def test_compiled_specs_embed_normative_public_team_contracts_without_holdouts(
    tmp_path: Path,
) -> None:
    gateway = RecordingGateway()
    applied = BusinessBenchmarkDemoProvisioner(
        settings(tmp_path),
        gateway=gateway,
        clock=lambda: ISSUED_AT + timedelta(minutes=1),
    ).apply()
    cas = BusinessBenchmarkContentAddressedArtifactStore(
        tmp_path
        / ".captain-cook"
        / "private"
        / "business-benchmarks"
        / "cas"
    )

    for team in applied.teams:
        compiled = json.loads(cas.read_bytes(team.job.compiled_spec_ref))
        contract = compiled["public_team_build_contract"]
        encoded = json.dumps(contract, sort_keys=True)
        assert contract["profile_id"] == team.profile_id
        assert contract["conversation_pattern"] == "swarm"
        assert len(contract["agents"]) == 3
        assert contract["terminal_output"]["schema"] == (
            "captain.business-benchmark-terminal.v1"
        )
        assert "expected_decision" not in encoded
        assert "required_rationale_fact_ids" not in encoded
        assert "case_id" not in encoded


def test_apply_resumes_stable_jobs_with_fresh_epoch_and_renews_active_leases(
    tmp_path: Path,
) -> None:
    gateway = RecordingGateway()
    first = BusinessBenchmarkDemoProvisioner(
        settings(tmp_path),
        gateway=gateway,
        clock=lambda: ISSUED_AT + timedelta(minutes=1),
    ).apply()
    original_jobs = dict(gateway.jobs)
    original_blocks = dict(gateway.blocks)
    original_assignments = dict(gateway.skill_assignments)
    original_files = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in (tmp_path / ".captain-cook").rglob("*")
        if path.is_file()
    }

    fresh_epoch = ISSUED_AT + timedelta(minutes=5)
    resumed = BusinessBenchmarkDemoProvisioner(
        settings(tmp_path, issued_at=fresh_epoch),
        gateway=gateway,
        clock=lambda: fresh_epoch + timedelta(minutes=1),
    ).apply()

    assert resumed.issued_at == fresh_epoch
    assert resumed.created_job_ids == ()
    assert resumed.resumed_job_ids == tuple(team.job.job_id for team in resumed.teams)
    assert tuple(team.job for team in resumed.teams) == tuple(
        original_jobs[team.job.job_id] for team in first.teams
    )
    assert gateway.jobs == original_jobs
    assert gateway.register_calls == 2
    assert gateway.blocks == original_blocks
    assert gateway.skill_assignments == original_assignments
    assert len(gateway.leases) == 4
    renewed = tuple(team.initial_lease for team in resumed.teams)
    assert all(lease is not None for lease in renewed)
    assert all(lease.issued_at == fresh_epoch for lease in renewed)
    assert all(lease.expires_at > fresh_epoch for lease in renewed)
    assert {team.initial_lease.lease_id for team in first.teams}.isdisjoint(
        {lease.lease_id for lease in renewed}
    )
    assert all(block.phase is FactoryPhase.FORGE_REQUESTED for block in gateway.blocks.values())
    assert {
        path.relative_to(tmp_path): path.read_bytes()
        for path in (tmp_path / ".captain-cook").rglob("*")
        if path.is_file()
    } == original_files
    assert tuple(team.policy_binding_ref for team in resumed.teams) == tuple(
        team.policy_binding_ref for team in first.teams
    )


def test_apply_restores_canonical_jobs_from_cas_after_database_reset(
    tmp_path: Path,
) -> None:
    first_gateway = RecordingGateway()
    first = BusinessBenchmarkDemoProvisioner(
        settings(tmp_path),
        gateway=first_gateway,
        clock=lambda: ISSUED_AT + timedelta(minutes=1),
    ).apply()
    canonical_jobs = tuple(team.job for team in first.teams)
    reset_gateway = RecordingGateway()
    fresh_epoch = ISSUED_AT + timedelta(minutes=5)

    restored = BusinessBenchmarkDemoProvisioner(
        settings(tmp_path, issued_at=fresh_epoch),
        gateway=reset_gateway,
        clock=lambda: fresh_epoch + timedelta(minutes=1),
    ).apply()

    assert restored.created_job_ids == tuple(job.job_id for job in canonical_jobs)
    assert restored.resumed_job_ids == ()
    assert restored.checkpoint_job_ids == ()
    assert tuple(team.job for team in restored.teams) == canonical_jobs
    assert tuple(reset_gateway.jobs.values()) == canonical_jobs
    assert all(team.initial_lease is not None for team in restored.teams)
    assert all(team.initial_lease.issued_at == fresh_epoch for team in restored.teams)


def test_apply_reports_later_projection_without_rewinding_or_renewing_lease(
    tmp_path: Path,
) -> None:
    gateway = RecordingGateway()
    first = BusinessBenchmarkDemoProvisioner(
        settings(tmp_path),
        gateway=gateway,
        clock=lambda: ISSUED_AT + timedelta(minutes=1),
    ).apply()
    for team in first.teams:
        gateway.resume_overrides[team.job.job_id] = BusinessBenchmarkDemoResumeStateV1(
            job=team.job,
            phase=FactoryPhase.BLUEPRINT_CREATED,
            attempt=3,
        )
    snapshot = (
        dict(gateway.jobs),
        dict(gateway.blocks),
        dict(gateway.skill_assignments),
        dict(gateway.leases),
    )
    fresh_epoch = ISSUED_AT + timedelta(minutes=5)

    resumed = BusinessBenchmarkDemoProvisioner(
        settings(tmp_path, issued_at=fresh_epoch),
        gateway=gateway,
        clock=lambda: fresh_epoch + timedelta(minutes=1),
    ).apply()

    assert resumed.created_job_ids == ()
    assert resumed.resumed_job_ids == ()
    assert resumed.checkpoint_job_ids == tuple(team.job.job_id for team in resumed.teams)
    assert all(team.next_action == "continue_existing_lifecycle" for team in resumed.teams)
    assert all(team.next_dispatch is None for team in resumed.teams)
    assert all(team.initial_lease is None for team in resumed.teams)
    cas = BusinessBenchmarkContentAddressedArtifactStore(
        tmp_path
        / ".captain-cook"
        / "private"
        / "business-benchmarks"
        / "cas"
    )
    for team in resumed.teams:
        assert cas.binding(
            "benchmark-policy",
            f"{team.job.job_id}:3",
        ) == team.policy_binding_ref
        binding = CaptainBusinessBenchmarkPolicyBindingV1.model_validate_json(
            cas.read_bytes(team.policy_binding_ref)
        )
        assert binding.attempt == 3
    assert (
        gateway.jobs,
        gateway.blocks,
        gateway.skill_assignments,
        gateway.leases,
    ) == snapshot


def test_apply_rejects_changed_static_job_before_resume_writes(tmp_path: Path) -> None:
    gateway = RecordingGateway()
    BusinessBenchmarkDemoProvisioner(
        settings(tmp_path),
        gateway=gateway,
        clock=lambda: ISSUED_AT + timedelta(minutes=1),
    ).apply()
    snapshot = (
        dict(gateway.jobs),
        dict(gateway.blocks),
        dict(gateway.skill_assignments),
        dict(gateway.leases),
    )
    fresh_epoch = ISSUED_AT + timedelta(minutes=5)

    with pytest.raises(ValueError, match="stable demo job binding changed"):
        BusinessBenchmarkDemoProvisioner(
            settings(tmp_path, issued_at=fresh_epoch, model="gpt-4.1"),
            gateway=gateway,
            clock=lambda: fresh_epoch + timedelta(minutes=1),
        ).apply()

    assert (
        gateway.jobs,
        gateway.blocks,
        gateway.skill_assignments,
        gateway.leases,
    ) == snapshot


def test_apply_rejects_expired_initial_lease_before_writing(tmp_path: Path) -> None:
    gateway = RecordingGateway()
    provisioner = BusinessBenchmarkDemoProvisioner(
        settings(tmp_path),
        gateway=gateway,
        clock=lambda: ISSUED_AT + timedelta(minutes=15),
    )

    with pytest.raises(ValueError, match="provisioning epoch.*active lease"):
        provisioner.apply()

    assert gateway.jobs == {}
    assert not (tmp_path / ".captain-cook").exists()


def test_provisioner_and_restart_bootstrap_share_one_stable_authority_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    gateway = RecordingGateway()
    configured = settings(tmp_path)
    applied = BusinessBenchmarkDemoProvisioner(
        configured,
        gateway=gateway,
        clock=lambda: ISSUED_AT + timedelta(minutes=1),
    ).apply()
    team = applied.teams[0]

    def live_settings(run_name: str) -> LiveBusinessBenchmarkSettings:
        return LiveBusinessBenchmarkSettings(
            profile=team.profile,
            provider="openai",
            model=configured.model,
            redaction_policy_sha256="a" * 64,
            selections=(
                BusinessBenchmarkTeamSelectionV1(
                    profile=team.profile,
                    job_id=team.job.job_id,
                    candidate_id=team.candidate_id,
                    suite_version=configured.suite_version,
                    attempt=1,
                    maximum_usd=configured.maximum_usd_per_team,
                    captain_remaining_usd=configured.maximum_usd_per_team,
                ),
            ),
            maximum_usd=configured.maximum_usd_per_team,
            allowed_models=(configured.model,),
            evidence_root=(
                tmp_path
                / ".captain-cook"
                / "evidence"
                / "business-benchmarks"
                / run_name
            ),
            runtime_url="http://127.0.0.1:8000",
            provider_secret_name="OPENAI_API_KEY",
        )

    environment = {
        "CAPTAIN_BENCHMARK_SEED_VERSION_ID": configured.seed_version_id,
    }
    first = ProductionBusinessBenchmarkBootstrapConfig.from_environment(
        live_settings("run-1"), environment
    )
    restarted = ProductionBusinessBenchmarkBootstrapConfig.from_environment(
        live_settings("run-2"), environment
    )
    authority_root = canonical_business_benchmark_authority_root(tmp_path)

    assert first.authority_root == restarted.authority_root == authority_root
    assert first.cas_root == restarted.cas_root == authority_root / "cas"
    assert (
        first.private_suite_root
        == restarted.private_suite_root
        == authority_root / "suites"
    )
    cas = BusinessBenchmarkContentAddressedArtifactStore(first.cas_root)
    candidate = BusinessBenchmarkCandidateAuthority(cas).resolve(
        job=team.job,
        expected_candidate_id=team.candidate_id,
        expected_candidate_ref=team.candidate_ref,
    )
    suite = CaptainPrivateBusinessBenchmarkSuiteLoader(
        first.private_suite_root
    ).load_suite(
        team.suite.suite_ref,
        expected_profile_id=team.suite.profile_id,
        expected_suite_version=team.suite.suite_version,
    )

    assert candidate.candidate.source_archive_ref == team.candidate_ref
    assert suite.profile_id == team.profile_id
    assert cas.binding("benchmark-policy", f"{team.job.job_id}:1") == (
        team.policy_binding_ref
    )


def test_changed_replay_fails_closed_at_immutable_job_binding(tmp_path: Path) -> None:
    gateway = RecordingGateway()
    BusinessBenchmarkDemoProvisioner(
        settings(tmp_path),
        gateway=gateway,
        clock=lambda: ISSUED_AT + timedelta(minutes=1),
    ).apply()

    changed = BusinessBenchmarkDemoProvisioner(
        settings(tmp_path, model="gpt-4.1"),
        gateway=gateway,
        clock=lambda: ISSUED_AT + timedelta(minutes=1),
    )
    with pytest.raises(ValueError, match="immutable.*binding changed"):
        changed.apply()


def test_team_plan_exposes_exact_next_workflow_requirement(tmp_path: Path) -> None:
    result = BusinessBenchmarkDemoProvisioner(settings(tmp_path)).plan()

    for team in result.teams:
        assert isinstance(team, BusinessBenchmarkDemoTeamPlanV1)
        assert team.next_action == "dispatch_agent_architect"
        assert team.next_dispatch.job_id == team.job.job_id
        assert team.next_dispatch.role is FactoryRole.AGENT_ARCHITECT
        assert team.next_dispatch.lease_id == team.initial_lease.lease_id
        assert team.next_dispatch.steps == (FactorySkillStep.DISCOVER,)
        assert team.released_workflow_steps == tuple(FactorySkillStep)
        assert team.initial_workflow_steps == (
            FactorySkillStep.DISCOVER,
            FactorySkillStep.BRIEF_CODEX,
            FactorySkillStep.SEAL_CODEX_BUILD,
            FactorySkillStep.EXECUTE_TEAM,
            FactorySkillStep.EVALUATE_TEAM,
            FactorySkillStep.REPORT_CAPTAIN,
        )
        assert team.missing_gateway_evidence == (
            "codebase_inventory",
            "codex_build_brief",
            "codex_build_evidence",
            "team_execution_evidence",
            "real_case_tester_lease",
            "quality_warden_lease",
        )


def test_next_dispatch_skill_release_is_accepted_by_real_hermes_adapter(
    tmp_path: Path,
) -> None:
    team = BusinessBenchmarkDemoProvisioner(settings(tmp_path)).plan().teams[0]

    class Catalog:
        def released_for(self, job, step):
            assert job == team.job
            assert step is FactorySkillStep.DISCOVER
            return team.next_dispatch.released_skill

    factory = HermesCliFactory(
        settings=HermesCliSettings(
            skill_root=Path(__file__).resolve().parents[2]
            / "agenten"
            / "agent_factory"
            / "skills",
            evidence_root=tmp_path / "evidence",
        ),
        released_skill_catalog=Catalog(),
        clock=lambda: ISSUED_AT + timedelta(minutes=1),
    )
    factory.validate_dispatch_configuration(
        FactoryDispatch(
            job=team.job,
            action=FactoryAction(
                kind=FactoryActionKind.DISPATCH_AGENT_ARCHITECT,
                attempt=1,
            ),
            role=FactoryRole.AGENT_ARCHITECT,
            lease=team.initial_lease,
        )
    )


def test_cli_defaults_to_dry_run_and_never_echoes_the_dsn(tmp_path: Path) -> None:
    command = [
        sys.executable,
        "scripts/provision-business-benchmark-demo.py",
        "--workspace-root",
        str(tmp_path),
        "--issued-at",
        ISSUED_AT.isoformat(),
        "--model",
        "gpt-4.1-mini",
        "--maximum-usd-per-team",
        "5.00",
    ]
    environment = dict(os.environ)
    environment["TEST_MARIADB_DSN"] = LOCAL_DSN

    completed = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["mode"] == "dry_run"
    assert payload["database"] == "captain_test"
    assert LOCAL_DSN not in completed.stdout
    assert not (tmp_path / ".captain-cook").exists()


def test_plan_only_cli_needs_no_dsn_or_credential_environment(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sentinel = workspace / "no-writes-marker"
    sentinel.write_text("unchanged", encoding="utf-8")
    environment = {
        key: value
        for key, value in os.environ.items()
        if "DSN" not in key and "TOKEN" not in key and "KEY" not in key and "SECRET" not in key
    }

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/provision-business-benchmark-demo.py",
            "--plan-only",
            "--workspace-root",
            str(workspace),
            "--issued-at",
            ISSUED_AT.isoformat(),
            "--model",
            "gpt-4.1-mini",
            "--maximum-usd-per-team",
            "5.00",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["mode"] == "dry_run"
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert not (workspace / ".captain-cook").exists()
    assert "provider" not in completed.stdout.lower()

    inherited_environment = dict(environment)
    inherited_environment["TEST_MARIADB_DSN"] = "mariadb://poison.invalid/not-captain-test"
    inherited = subprocess.run(
        [
            sys.executable,
            "scripts/provision-business-benchmark-demo.py",
            "--plan-only",
            "--workspace-root",
            str(workspace),
            "--issued-at",
            ISSUED_AT.isoformat(),
            "--model",
            "gpt-4.1-mini",
            "--maximum-usd-per-team",
            "5.00",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=inherited_environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert inherited.returncode == 0, inherited.stderr
    assert "poison.invalid" not in inherited.stdout + inherited.stderr


def test_plan_settings_cannot_be_applied(tmp_path: Path) -> None:
    plan_settings = BusinessBenchmarkDemoPlanSettings(
        workspace_root=tmp_path,
        issued_at=ISSUED_AT,
        model="gpt-4.1-mini",
        maximum_usd_per_team="5.00",
        suite_version=1,
        seed_version_id="business-benchmark-demo-2026-07",
    )
    provisioner = BusinessBenchmarkDemoProvisioner(plan_settings)

    assert provisioner.plan().mode == "dry_run"
    with pytest.raises(ValueError, match="plan-only settings"):
        provisioner.apply()


def test_cli_does_not_swallow_unexpected_programming_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "provision-business-benchmark-demo.py"
    )
    spec = importlib.util.spec_from_file_location(
        "provision_business_benchmark_demo_cli",
        script_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("TEST_MARIADB_DSN", LOCAL_DSN)

    def programming_error(_provisioner: BusinessBenchmarkDemoProvisioner) -> None:
        raise RuntimeError("unexpected programming error")

    monkeypatch.setattr(
        module.BusinessBenchmarkDemoProvisioner,
        "plan",
        programming_error,
    )

    with pytest.raises(RuntimeError, match="unexpected programming error"):
        module.main(
            [
                "--workspace-root",
                str(tmp_path),
                "--issued-at",
                ISSUED_AT.isoformat(),
            ]
        )
