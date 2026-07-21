from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from agenten.agent_factory.factory_live_prepared_dispatch import (
    FactoryLivePreparedDispatch,
    PreparedFactoryLiveEffectExecutor,
    PreparedFactoryLivePlan,
)
from agenten.agent_factory.factory_live_runner import (
    FactoryLiveEffectOutcomeV1,
    InMemoryFactoryLiveEffectLedger,
)
from agenten.agent_factory.service import InMemoryFactoryRepository
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from agenten.agent_factory.state_machine import FactoryProjection
from tests.agent_factory.test_factory_live_prepared_dispatch import (
    ActionSource,
    artifact,
    codex_request,
)
from tests.agent_factory.test_release_gate import workflow_job
from tests.gateway.test_factory_live_composition import live_settings, skill_digests


NOW = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)


class PreparedDelegate:
    def __init__(self, prepared: FactoryLivePreparedDispatch) -> None:
        self.prepared = prepared
        self.prepare_calls = 0
        self.execute_calls = 0

    def prepare(self, **_values: object) -> FactoryLivePreparedDispatch:
        self.prepare_calls += 1
        return self.prepared

    async def execute(self, request):
        self.execute_calls += 1
        return FactoryLiveEffectOutcomeV1(
            schema_name="captain.factory-live-effect-outcome.v1",
            outcome_id=request.effect_id,
            effect_id=request.effect_id,
            job_id=request.job_id,
            correlation_id=request.correlation_id,
            subject_version=request.subject_version,
            attempt=request.attempt,
            status="succeeded",
            evidence_ref=artifact("production-runtime-outcome"),
            completed_at=NOW,
        )

    async def recover(self, request):
        return await self.execute(request)


class Materializer:
    def __init__(self, action: FactoryAction) -> None:
        self.action = action
        self.validate_calls = 0
        self.dispatch_calls = 0

    def validate_next(self, job, action, expected_skill_digests):
        assert job.job_id == action.job_id
        assert expected_skill_digests
        self.validate_calls += 1
        return action

    async def dispatch_next(self, job_id: UUID) -> FactoryAction:
        assert job_id == self.action.job_id
        self.dispatch_calls += 1
        return self.action


class WorkflowRepository(InMemoryFactoryRepository):
    def workflow_artifacts(self, job_id: UUID) -> tuple[object, ...]:
        self.job(job_id)
        return ()


class LifecycleSource(ActionSource):
    def __init__(self, action: FactoryAction, job) -> None:
        super().__init__(action)
        self._projection = FactoryProjection.from_job(job)

    def projection(self, _job_id: UUID) -> FactoryProjection:
        return self._projection


@pytest.mark.asyncio
async def test_materializing_dispatcher_and_runner_share_one_prepared_graph() -> None:
    from gateway.factory_live_runtime import (
        GatewayBoundFactoryLivePreparedDispatchPort,
        MaterializingFactoryLiveDispatcher,
        build_factory_live_runner,
    )

    job = workflow_job(mode="demo")
    action = FactoryAction(
        kind=FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
        attempt=1,
        job_id=job.job_id,
    )
    request = codex_request(job)
    source = LifecycleSource(action, job)
    delegate = PreparedDelegate(
        FactoryLivePreparedDispatch(action=action, requests=(request,))
    )
    repository = WorkflowRepository()
    repository.register(job)
    prepared = GatewayBoundFactoryLivePreparedDispatchPort(
        job=job,
        actions=source,
        repository=repository,
        delegate=delegate,
    )
    plan = PreparedFactoryLivePlan(
        actions=source,
        dispatch=prepared,
        expected_skill_digests={"captain-factory-brief-codex": "a" * 64},
    )
    materializer = Materializer(action)
    dispatcher = MaterializingFactoryLiveDispatcher(
        job=job,
        actions=source,
        repository=repository,
        plan=plan,
        delegate=materializer,
        expected_skill_digests={"captain-factory-brief-codex": "a" * 64},
    )
    runner = build_factory_live_runner(
        repository=repository,
        effect_ledger=InMemoryFactoryLiveEffectLedger(),
        plan=plan,
        prepared_dispatch=prepared,
        clock=lambda: request.invocation.lease.issued_at,
    )

    assert dispatcher.validate_next(
        job,
        action,
        {"captain-factory-brief-codex": "a" * 64},
    ) == action
    report = await runner.run(job, mode="demo")
    assert await dispatcher.dispatch_next(job.job_id) == action

    assert report.effects[0].status == "succeeded"
    assert delegate.prepare_calls == 1
    assert delegate.execute_calls == 1
    assert materializer.validate_calls == 1
    assert materializer.dispatch_calls == 1


def test_gateway_lifecycle_reads_only_captain_promotion_from_repository() -> None:
    from agenten.agent_factory.contracts import FactoryBlockStatus, FactoryEvidenceBlock, FactoryPhase
    from gateway.factory_live_runtime import GatewayFactoryLiveLifecycle

    job = workflow_job(mode="demo")
    repository = WorkflowRepository()
    repository.register(job)
    unrelated = FactoryEvidenceBlock(
        schema_name="captain.agent-factory-block.v1",
        event_id="00000000-0000-0000-0000-000000000901",
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        causation_id=job.event_id,
        occurred_at=NOW,
        producer="captain",
        subject_version=job.subject_version,
        attempt=1,
        phase=FactoryPhase.FORGE_REQUESTED,
        role=None,
        status=FactoryBlockStatus.SUCCEEDED,
    )
    captain = unrelated.model_copy(
        update={
            "event_id": UUID("00000000-0000-0000-0000-000000000902"),
            "producer": "captain",
            "phase": FactoryPhase.CAPABILITY_PROMOTED,
            "role": None,
        }
    )
    repository.append(unrelated)
    lifecycle = GatewayFactoryLiveLifecycle(repository)

    assert lifecycle.promotion_block(job.job_id) is None

    repository.append(captain)
    assert lifecycle.promotion_block(job.job_id) == captain


def _write_attested_adapter(root: Path) -> tuple[Path, str]:
    module = root / "production_factory_adapter.py"
    module.write_text(
        "def build_factory_live_runtime(context):\n"
        "    return context\n",
        encoding="utf-8",
    )
    module_sha256 = hashlib.sha256(module.read_bytes()).hexdigest()
    manifest = root / "factory-adapter.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "captain.factory-live-runtime-adapter-manifest.v1",
                "module_path": module.name,
                "module_sha256": module_sha256,
                "factory_symbol": "build_factory_live_runtime",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return manifest, hashlib.sha256(manifest.read_bytes()).hexdigest()


def test_gateway_environment_attests_runtime_and_reuses_package_c_manifest(
    tmp_path: Path,
) -> None:
    from gateway.factory_live_runtime import load_factory_live_environment

    manifest, digest = _write_attested_adapter(tmp_path)
    job = workflow_job(mode="demo")
    loaded = load_factory_live_environment(
        {
            "CAPTAIN_FACTORY_JOB_ID": str(job.job_id),
            "CAPTAIN_RUNTIME_URL": "http://127.0.0.1:8091",
            "CAPTAIN_RUNTIME_TOKEN": "runtime-token-value",
            "FACTORY_LIVE_RUNTIME_ADAPTER_MANIFEST": manifest.name,
            "FACTORY_LIVE_RUNTIME_ADAPTER_SHA256": digest,
        },
        workspace_root=tmp_path,
    )

    assert loaded.job_id == job.job_id
    assert loaded.runtime_url == "http://127.0.0.1:8091"
    assert loaded.runtime_token.get_secret_value() == "runtime-token-value"
    assert loaded.graph_factory(object()) is not None


def test_gateway_environment_fails_closed_without_exposing_token(
    tmp_path: Path,
) -> None:
    from gateway.factory_live_runtime import FactoryLiveBootstrapError, load_factory_live_environment

    manifest, _ = _write_attested_adapter(tmp_path)
    token = "runtime-token-must-not-leak"
    with pytest.raises(FactoryLiveBootstrapError) as raised:
        load_factory_live_environment(
            {
                "CAPTAIN_FACTORY_JOB_ID": "00000000-0000-0000-0000-000000000001",
                "CAPTAIN_RUNTIME_URL": "http://provider.example.invalid:8091",
                "CAPTAIN_RUNTIME_TOKEN": token,
                "FACTORY_LIVE_RUNTIME_ADAPTER_MANIFEST": manifest.name,
                "FACTORY_LIVE_RUNTIME_ADAPTER_SHA256": "0" * 64,
            },
            workspace_root=tmp_path,
        )

    assert token not in str(raised.value)


def test_gateway_constructors_build_every_port_from_one_external_graph(
    tmp_path: Path,
) -> None:
    from gateway.factory_live_composition import GatewayFactoryLiveComposition
    from gateway.factory_live_runtime import (
        FactoryLiveEnvironment,
        FactoryLiveExternalRuntimeGraph,
        GatewayFactoryLiveRuntimeConstructors,
    )
    from pydantic import SecretStr

    job = workflow_job(mode="demo")
    action = FactoryAction(
        kind=FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
        attempt=1,
        job_id=job.job_id,
    )
    repository = WorkflowRepository()
    repository.register(job)
    delegate = PreparedDelegate(
        FactoryLivePreparedDispatch(action=action, requests=(codex_request(job),))
    )
    materializer = Materializer(action)
    calls: list[object] = []

    def graph_factory(context):
        calls.append(context)
        return FactoryLiveExternalRuntimeGraph(
            prepared_dispatch=delegate,
            materializer=materializer,
            clock=lambda: context.composition.job.occurred_at,
        )

    environment = FactoryLiveEnvironment(
        job_id=job.job_id,
        runtime_url="http://127.0.0.1:8091",
        runtime_token=SecretStr("runtime-token"),
        graph_factory=graph_factory,
        manifest_sha256="a" * 64,
        module_sha256="b" * 64,
    )
    composition = GatewayFactoryLiveComposition(
        settings=live_settings(tmp_path),
        job=job,
        skill_digests=skill_digests(),
        repository=repository,
        leases=object(),
        budget=object(),
        live_effects=InMemoryFactoryLiveEffectLedger(),
        workflow_sink=object(),
    )
    constructors = GatewayFactoryLiveRuntimeConstructors(environment)

    lifecycle = constructors.lifecycle(composition)
    dispatcher = constructors.dispatcher(composition)
    runner = constructors.runner(composition)

    assert len(calls) == 1
    assert calls[0].composition is composition
    assert calls[0].runtime_url == environment.runtime_url
    assert lifecycle is constructors.lifecycle(composition)
    assert dispatcher is constructors.dispatcher(composition)
    assert runner is constructors.runner(composition)
