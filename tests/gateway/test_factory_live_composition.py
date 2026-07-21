from __future__ import annotations

import importlib
from decimal import Decimal
from pathlib import Path

import pytest

from agenten.agent_factory.factory_live_entrypoint import (
    FactoryLiveConfigurationError,
    FactoryLivePreflightSettings,
)
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import (
    FACTORY_SKILL_ID_BY_STEP,
    FactorySkillStep,
)
from agenten.agent_runtime.contracts import ArtifactRef
from gateway.factory_repository import (
    GatewayFactoryBudgetLedger,
    GatewayFactoryLeases,
    GatewayFactoryLiveEffectLedger,
    GatewayFactoryWorkflowArtifactSink,
)
from tests.agent_factory.test_release_gate import workflow_job


class Lifecycle:
    def next_action(self, _job_id):
        raise AssertionError("composition must not advance the lifecycle")

    def projection(self, _job_id):
        raise AssertionError("composition must not read lifecycle projection twice")

    def record(self, _block):
        raise AssertionError("composition must not record a lifecycle block")

    def promotion_block(self, _job_id):
        raise AssertionError("composition must not read promotion evidence")


class Dispatcher:
    def validate_next(self, _job, _action, _expected_skill_digests):
        raise AssertionError("composition must not stage a dispatch")

    async def dispatch_next(self, _job_id):
        raise AssertionError("composition must not dispatch")


class Runner:
    def history(self, _job_id):
        return ()

    async def run(self, _job, *, mode):
        raise AssertionError(f"composition must not run {mode} effects")


class Repository:
    def __init__(self, job, digests: dict[str, str]) -> None:
        self._job = job
        self._digests = digests
        self.job_reads = []
        self.release_reads = []

    def job(self, job_id):
        self.job_reads.append(job_id)
        return self._job

    def released_for(self, job, step):
        self.release_reads.append((job, step))
        skill_id = FACTORY_SKILL_ID_BY_STEP[step]
        digest = self._digests[skill_id]
        return ReleasedHermesSkill(
            schema_name="captain.released-hermes-skill.v1",
            skill_id=skill_id,
            version=1,
            capability=job.required_capability,
            content_ref=ArtifactRef(
                uri=f"artifact://released-skills/{skill_id}/v1",
                sha256=digest,
                media_type="application/json",
            ),
            content_sha256=digest,
            status="released",
            released_at=job.occurred_at,
            producer="captain",
        )

    def workflow_artifacts(self, _job_id):
        return ()


def skill_digests() -> dict[str, str]:
    return {
        skill_id: f"{index:x}" * 64
        for index, skill_id in enumerate(FACTORY_SKILL_ID_BY_STEP.values(), start=1)
    }


def live_settings(
    tmp_path: Path,
    *,
    mode: str = "demo",
    max_cost_usd: str = "5.00",
    model: str = "approved-model-id",
) -> FactoryLivePreflightSettings:
    report_directory = tmp_path / f"external-{mode}-{max_cost_usd}-{model}"
    report_directory.mkdir()
    return FactoryLivePreflightSettings(
        mode=mode,
        max_cost_usd=Decimal(max_cost_usd),
        model=model,
        repository_root=Path(__file__).resolve().parents[2],
        report_directory=report_directory,
        output=report_directory / "preflight.json",
        database_dsn="mysql://captain:secret@127.0.0.1:3306/captain_test",
        with_n8n=False,
    )


def live_constructors(module, repository, store, compositions):
    def repository_constructor(supplied_store):
        assert supplied_store is store
        return repository

    def lifecycle_constructor(composition):
        compositions.append(composition)
        return Lifecycle()

    def dispatcher_constructor(composition):
        compositions.append(composition)
        return Dispatcher()

    def runner_constructor(composition):
        compositions.append(composition)
        return Runner()

    return module.GatewayFactoryLiveConstructors(
        repository=repository_constructor,
        lifecycle=lifecycle_constructor,
        dispatcher=dispatcher_constructor,
        runner=runner_constructor,
    )


def test_gateway_factory_builds_one_authority_bound_prepared_composition(
    tmp_path: Path,
) -> None:
    try:
        module = importlib.import_module("gateway.factory_live_composition")
    except ModuleNotFoundError:
        module = None
    assert module is not None

    job = workflow_job(mode="demo")
    digests = skill_digests()
    repository = Repository(job, digests)
    store = object()
    compositions = []
    constructors = live_constructors(module, repository, store, compositions)
    factory = module.GatewayPreparedFactoryLiveAdapterFactory(
        store=store,
        job_id=job.job_id,
        constructors=constructors,
    )
    settings = live_settings(tmp_path)

    prepared = factory.prepare(settings, digests)

    assert prepared.lifecycle.__class__ is Lifecycle
    assert prepared.repository is repository
    assert prepared.dispatcher.__class__ is Dispatcher
    assert prepared.live_runner.__class__ is Runner
    assert repository.job_reads == [job.job_id]
    assert repository.release_reads == [(job, step) for step in FactorySkillStep]
    assert len(compositions) == 3
    assert compositions[0] is compositions[1] is compositions[2]
    composition = compositions[0]
    assert composition.job == job
    assert composition.settings is settings
    assert dict(composition.skill_digests) == digests
    assert isinstance(composition.leases, GatewayFactoryLeases)
    assert isinstance(composition.budget, GatewayFactoryBudgetLedger)
    assert isinstance(composition.live_effects, GatewayFactoryLiveEffectLedger)
    assert isinstance(composition.workflow_sink, GatewayFactoryWorkflowArtifactSink)


def test_gateway_factory_rejects_a_different_authoritative_job_id(
    tmp_path: Path,
) -> None:
    module = importlib.import_module("gateway.factory_live_composition")
    requested = workflow_job(mode="demo")
    different = requested.model_copy(
        update={"job_id": "00000000-0000-0000-0000-000000000999"}
    )
    digests = skill_digests()
    repository = Repository(different, digests)
    store = object()
    compositions = []
    factory = module.GatewayPreparedFactoryLiveAdapterFactory(
        store=store,
        job_id=requested.job_id,
        constructors=live_constructors(module, repository, store, compositions),
    )

    with pytest.raises(FactoryLiveConfigurationError, match="job"):
        factory.prepare(live_settings(tmp_path), digests)

    assert compositions == []


@pytest.mark.parametrize(
    ("settings_overrides", "expected_marker"),
    (
        ({"mode": "release"}, "mode"),
        ({"max_cost_usd": "4.00"}, "budget"),
        ({"model": "unapproved-model"}, "model"),
    ),
)
def test_gateway_factory_rejects_settings_outside_the_job_execution_policy(
    tmp_path: Path,
    settings_overrides: dict[str, str],
    expected_marker: str,
) -> None:
    module = importlib.import_module("gateway.factory_live_composition")
    job = workflow_job(mode="demo")
    digests = skill_digests()
    repository = Repository(job, digests)
    store = object()
    compositions = []
    factory = module.GatewayPreparedFactoryLiveAdapterFactory(
        store=store,
        job_id=job.job_id,
        constructors=live_constructors(module, repository, store, compositions),
    )

    with pytest.raises(FactoryLiveConfigurationError, match=expected_marker):
        factory.prepare(live_settings(tmp_path, **settings_overrides), digests)

    assert compositions == []


def test_gateway_factory_rejects_changed_released_skill_digest(
    tmp_path: Path,
) -> None:
    module = importlib.import_module("gateway.factory_live_composition")
    job = workflow_job(mode="demo")
    authoritative = skill_digests()
    expected = dict(authoritative)
    expected[FACTORY_SKILL_ID_BY_STEP[FactorySkillStep.EXECUTE_TEAM]] = "f" * 64
    repository = Repository(job, authoritative)
    store = object()
    compositions = []
    factory = module.GatewayPreparedFactoryLiveAdapterFactory(
        store=store,
        job_id=job.job_id,
        constructors=live_constructors(module, repository, store, compositions),
    )

    with pytest.raises(FactoryLiveConfigurationError, match="skill digest"):
        factory.prepare(live_settings(tmp_path), expected)

    assert compositions == []


def test_gateway_store_dependency_stays_out_of_agent_factory() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    gateway_source = (
        repository_root / "gateway" / "factory_live_composition.py"
    ).read_text(encoding="utf-8")
    entrypoint_source = (
        repository_root
        / "agenten"
        / "agent_factory"
        / "factory_live_entrypoint.py"
    ).read_text(encoding="utf-8")

    assert "from gateway.store import GatewayStore" in gateway_source
    assert "GatewayStore" not in entrypoint_source
    assert "MariaDBStorage" not in entrypoint_source
