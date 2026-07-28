from __future__ import annotations

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
    BusinessBenchmarkDemoProvisioningSettings,
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
from agenten.agent_factory.business_benchmark_provisioning import (
    CaptainPrivateBusinessBenchmarkSuiteLoader,
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
from agenten.agent_factory.orchestration import FactoryDispatch
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import FactorySkillStep
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind


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

    def register(self, job: AgentFactoryJobV3) -> None:
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
    assert all(len(team.released_skills) == 6 for team in first.teams)
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
    serialized = first.model_dump_json().lower()
    assert "redacted_input" not in serialized
    assert "expected_decision" not in serialized
    assert "password" not in serialized
    assert LOCAL_DSN not in serialized
    assert not (tmp_path / ".captain-cook").exists()


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

    assert first == second
    assert first.mode == "applied"
    assert len(gateway.jobs) == 2
    assert len(gateway.skill_assignments) == 12
    assert len(gateway.blocks) == 2
    assert all(block.phase is FactoryPhase.FORGE_REQUESTED for block in gateway.blocks.values())
    assert len(gateway.leases) == 2
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
    assert {
        path.relative_to(tmp_path): path.read_bytes()
        for path in (tmp_path / ".captain-cook").rglob("*")
        if path.is_file()
    } == snapshot


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
            FactorySkillStep.EXECUTE_TEAM,
            FactorySkillStep.EVALUATE_TEAM,
            FactorySkillStep.REPORT_CAPTAIN,
        )
        assert team.missing_gateway_evidence == (
            "codebase_inventory",
            "codex_build_brief",
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
