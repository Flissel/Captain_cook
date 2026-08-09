from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid5

import pytest

from agenten.agent_factory.capability_live_runner import (
    CapabilityLiveInput,
    CapabilityLiveReservation,
    CapabilityLiveStage,
    CapabilityLiveStageRequest,
)
from agenten.agent_factory.contracts import FactoryPhase
from agenten.agent_factory.execution_policy import (
    FactoryExecutionMode,
    FactoryExecutionPolicyV1,
    FactoryLiveCapability,
)
from agenten.agent_factory.holdout_store import InMemoryPrivateHoldoutStore
from agenten.agent_factory.skill_workflow_contracts import FactorySkillStep
from agenten.agent_factory.service import FactoryRepositoryError
from agenten.agent_runtime.captain_production_adapters import (
    ContentAddressedArtifactAdapter,
)


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
RUN_ID = UUID("82000000-0000-0000-0000-000000000081")


class _Repository:
    def __init__(self) -> None:
        self.jobs: dict[UUID, object] = {}
        self.blocks: list[object] = []
        self.skills: list[tuple[object, object]] = []
        self.batches: list[tuple[object, object]] = []
        self.n8n_batches: dict[UUID, str] = {}

    def register(self, job: object) -> None:
        job_id = getattr(job, "job_id")
        existing = self.jobs.get(job_id)
        if existing is not None and existing != job:
            raise ValueError("factory job identity changed")
        self.jobs[job_id] = job

    def job(self, job_id: UUID) -> object:
        return self.jobs[job_id]

    def seed_released_skill_assignments(self, job: object, source: object) -> None:
        self.skills.append((job, source))

    def append(self, block: object) -> bool:
        if block not in self.blocks:
            self.blocks.append(block)
            return True
        return False

    def persist_work_batch(self, job: object, batch: object) -> None:
        entry = (job, batch)
        if entry not in self.batches:
            self.batches.append(entry)

    def register_n8n_work_batch(self, job: object, batch_id: str) -> None:
        self.n8n_batches[getattr(job, "job_id")] = batch_id


class _ReleasedSkills:
    def released_for(self, job: object, step: FactorySkillStep) -> object:
        return (getattr(job, "job_id"), step)


def _canonical_input() -> bytes:
    fixture = (
        Path(__file__).resolve().parents[2]
        / "demo_inputs"
        / "agent_factory"
        / "incident_command"
        / "TO_BE_BUILT.md"
    )
    return fixture.read_bytes()


def _request(
    source_ref: object,
    *,
    integration_intent: str = "n8n",
) -> CapabilityLiveStageRequest:
    input_item = CapabilityLiveInput(
        input_id="sequential-n8n",
        conversation_pattern="sequential",
        source_ref=source_ref,
        integration_intent=integration_intent,
        conflicting_input=False,
        human_escalation_required=False,
        required_tool_marker=None,
    )
    return CapabilityLiveStageRequest(
        run_id=RUN_ID,
        input=input_item,
        correlation_id=uuid5(RUN_ID, input_item.input_id),
        reservation=CapabilityLiveReservation(
            reservation_id=uuid5(RUN_ID, "reservation"),
            run_id=RUN_ID,
            input_id=input_item.input_id,
            maximum_cost_usd=Decimal("1.00"),
        ),
        stage=CapabilityLiveStage.SCAFFOLD_AND_TEST_HARNESS,
        controlled_recovery_required=True,
    )


def test_current_authority_provisioner_revalidates_cas_and_replays_one_v3_binding(
    tmp_path: Path,
) -> None:
    from gateway.capability_live_provisioning import (
        CurrentAuthorityCapabilityFactoryProvisioner,
    )

    artifacts = ContentAddressedArtifactAdapter(tmp_path / "cas")
    source_ref = artifacts.put(_canonical_input(), "text/markdown")
    repository = _Repository()
    policy = FactoryExecutionPolicyV1(
        schema="captain.factory-execution-policy.v1",
        mode=FactoryExecutionMode.RELEASE,
        live_execution=True,
        max_cost_usd="1.00",
        max_runtime_seconds=900,
        required_live_runs=3,
        allowed_models=("gpt-5.6-terra",),
        live_capabilities=(FactoryLiveCapability.MODEL_INVOKE,),
    )
    request = _request(source_ref)

    provisioner = CurrentAuthorityCapabilityFactoryProvisioner(
        repository=repository,
        source_artifacts=artifacts,
        holdout_store=InMemoryPrivateHoldoutStore(),
        released_skills=_ReleasedSkills(),
        execution_policy=policy,
        authority_root=tmp_path / "authority",
        clock=lambda: NOW,
        work_batches=repository,
        n8n_registry=repository,
    )
    first = provisioner.provision(request)
    replay = CurrentAuthorityCapabilityFactoryProvisioner(
        repository=repository,
        source_artifacts=artifacts,
        holdout_store=InMemoryPrivateHoldoutStore(),
        released_skills=_ReleasedSkills(),
        execution_policy=policy,
        authority_root=tmp_path / "authority",
        clock=lambda: NOW.replace(second=1),
        work_batches=repository,
        n8n_registry=repository,
    ).provision(request)

    assert replay == first
    assert first.job.input_ref == source_ref
    assert first.n8n_work_batch_id == f"n8n-{first.job.job_id.hex[:24]}"
    assert len(repository.jobs) == 1
    assert len(repository.skills) == 1
    assert len(repository.batches) == 1
    assert repository.n8n_batches[first.job.job_id] == first.n8n_work_batch_id
    assert getattr(repository.blocks[0], "phase") is FactoryPhase.FORGE_REQUESTED


def test_production_builder_binds_the_current_resume_execution_port(
    tmp_path: Path,
) -> None:
    from gateway.capability_live_production_builder import (
        CapabilityLiveCurrentAuthorities,
        build_current_authority_capability_live_runner,
    )

    composition = object()
    provisioning = object()
    projection = object()
    resume_execution = object()

    runner = build_current_authority_capability_live_runner(
        repository_root=tmp_path,
        authorities=CapabilityLiveCurrentAuthorities(
            factory=composition,
            provisioning=provisioning,
            minibook_effects=projection,
            resume_execution=resume_execution,
        ),
        clock=lambda: NOW,
    )

    assert runner._pipeline._execution._resume_execution is resume_execution


def test_provisioner_registers_when_gateway_reports_a_missing_job(
    tmp_path: Path,
) -> None:
    from gateway.capability_live_provisioning import (
        CurrentAuthorityCapabilityFactoryProvisioner,
    )

    class GatewayRepository(_Repository):
        def job(self, job_id: UUID) -> object:
            if job_id not in self.jobs:
                raise FactoryRepositoryError("factory job not found")
            return self.jobs[job_id]

    artifacts = ContentAddressedArtifactAdapter(tmp_path / "cas")
    source_ref = artifacts.put(_canonical_input(), "text/markdown")
    repository = GatewayRepository()
    provisioner = CurrentAuthorityCapabilityFactoryProvisioner(
        repository=repository,
        source_artifacts=artifacts,
        holdout_store=InMemoryPrivateHoldoutStore(),
        released_skills=_ReleasedSkills(),
        execution_policy=FactoryExecutionPolicyV1(
            schema="captain.factory-execution-policy.v1",
            mode=FactoryExecutionMode.RELEASE,
            live_execution=True,
            max_cost_usd="1.00",
            max_runtime_seconds=900,
            required_live_runs=3,
            allowed_models=("gpt-5.6-terra",),
            live_capabilities=(FactoryLiveCapability.MODEL_INVOKE,),
        ),
        authority_root=tmp_path / "authority",
        clock=lambda: NOW,
        work_batches=repository,
        n8n_registry=repository,
    )

    binding = provisioner.provision(_request(source_ref))

    assert binding is not None
    assert tuple(repository.jobs) == (binding.job.job_id,)


def test_provisioner_rejects_n8n_batch_without_required_input_integration(
    tmp_path: Path,
) -> None:
    from gateway.capability_live_provisioning import (
        CapabilityLiveProvisioningError,
        CurrentAuthorityCapabilityFactoryProvisioner,
    )

    artifacts = ContentAddressedArtifactAdapter(tmp_path / "cas")
    source_ref = artifacts.put(
        _canonical_input().replace(
            b"#### Requirement\r\nrequired",
            b"#### Requirement\r\noptional",
        ),
        "text/markdown",
    )
    repository = _Repository()
    provisioner = CurrentAuthorityCapabilityFactoryProvisioner(
        repository=repository,
        source_artifacts=artifacts,
        holdout_store=InMemoryPrivateHoldoutStore(),
        released_skills=_ReleasedSkills(),
        execution_policy=FactoryExecutionPolicyV1(
            schema="captain.factory-execution-policy.v1",
            mode=FactoryExecutionMode.RELEASE,
            live_execution=True,
            max_cost_usd="1.00",
            max_runtime_seconds=900,
            required_live_runs=3,
            allowed_models=("gpt-5.6-terra",),
            live_capabilities=(FactoryLiveCapability.MODEL_INVOKE,),
        ),
        authority_root=tmp_path / "authority",
        clock=lambda: NOW,
        work_batches=repository,
        n8n_registry=repository,
    )

    with pytest.raises(
        CapabilityLiveProvisioningError,
        match="n8n integration is not required",
    ):
        provisioner.provision(_request(source_ref))

    assert repository.batches == []


def test_production_settings_preflight_uses_current_gateway_and_minibook_names() -> None:
    from gateway.capability_live_production_builder import (
        CapabilityLiveProductionSettings,
    )

    settings = CapabilityLiveProductionSettings.from_environment(
        {
            "CAPTAIN_GATEWAY_URL": "http://127.0.0.1:8090",
            "CAPTAIN_GATEWAY_TOKEN": "test-token",
            "CAPTAIN_BENCHMARK_PROVIDER": "openai",
            "CAPTAIN_BENCHMARK_MODEL": "gpt-5.6-terra",
            "MINIBOOK_BACKEND_URL": "http://127.0.0.1:3456",
            "MINIBOOK_PROJECTION_API_KEY": "test-projection-key",
        }
    )

    assert settings.gateway_url == "http://127.0.0.1:8090"
    assert settings.minibook_backend_url == "http://127.0.0.1:3456"


def test_provisioner_preflights_n8n_writers_before_registering_a_factory_job(
    tmp_path: Path,
) -> None:
    from gateway.capability_live_provisioning import (
        CapabilityLiveProvisioningError,
        CurrentAuthorityCapabilityFactoryProvisioner,
    )

    artifacts = ContentAddressedArtifactAdapter(tmp_path / "cas")
    source_ref = artifacts.put(_canonical_input(), "text/markdown")
    repository = _Repository()
    provisioner = CurrentAuthorityCapabilityFactoryProvisioner(
        repository=repository,
        source_artifacts=artifacts,
        holdout_store=InMemoryPrivateHoldoutStore(),
        released_skills=_ReleasedSkills(),
        execution_policy=FactoryExecutionPolicyV1(
            schema="captain.factory-execution-policy.v1",
            mode=FactoryExecutionMode.RELEASE,
            live_execution=True,
            max_cost_usd="1.00",
            max_runtime_seconds=900,
            required_live_runs=3,
            allowed_models=("gpt-5.6-terra",),
            live_capabilities=(FactoryLiveCapability.MODEL_INVOKE,),
        ),
        authority_root=tmp_path / "authority",
        clock=lambda: NOW,
        work_batches=repository,
    )

    with pytest.raises(
        CapabilityLiveProvisioningError,
        match="Gateway lease registry",
    ):
        provisioner.provision(_request(source_ref))

    assert repository.jobs == {}
    assert repository.batches == []
