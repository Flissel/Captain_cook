from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from agenten.agent_factory.capability_live_runner import (
    CapabilityLiveCapabilityEvidence,
    CapabilityLiveInput,
    CapabilityLiveReservation,
    CapabilityLiveReservationRequest,
    CapabilityLiveRestartRequest,
    CapabilityLiveRunner,
    CapabilityLiveStage,
    CapabilityLiveStageRequest,
    FilesystemCapabilityLiveCheckpointStore,
)
from agenten.agent_runtime.contracts import ArtifactRef
from gateway.capability_live_pipeline import (
    CapabilityBusinessBenchmarkAuthority,
    CapabilityExecutionAuthority,
    CapabilityFactoryAuthority,
    CapabilityLiveAuthorityUnavailable,
    CapabilityProjectionAuthority,
    GatewayCapabilityLivePipeline,
)
from gateway.agent_factory_live_composition import (
    GatewayCapabilityLiveCompositionError,
    compose_gateway_capability_live_runner,
)
from gateway.capability_live_factory_adapter import CurrentCapabilityFactoryAdapter
from gateway.capability_live_current_adapters import (
    GatewayCapabilityBusinessBenchmarkAdapter,
    GatewayMinibookCapabilityProjectionAdapter,
    RecordedFactoryCapabilityExecutionAdapter,
)


NOW = datetime(2026, 8, 8, 14, 0, tzinfo=timezone.utc)
RUN_ID = UUID("82000000-0000-0000-0000-000000000061")


def _uuid(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, value)


def _ref(namespace: str, value: str) -> ArtifactRef:
    digest = _uuid(f"{namespace}:{value}").hex * 2
    return ArtifactRef(
        uri=f"artifact://{namespace}/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def _inputs() -> tuple[CapabilityLiveInput, ...]:
    return (
        CapabilityLiveInput(
            input_id="sequential-n8n",
            conversation_pattern="sequential",
            source_ref=_ref("factory-input", "sequential"),
            integration_intent="n8n",
            conflicting_input=False,
            human_escalation_required=False,
            required_tool_marker=None,
        ),
        CapabilityLiveInput(
            input_id="selector-review",
            conversation_pattern="selector_group_chat",
            source_ref=_ref("factory-input", "selector"),
            integration_intent="none",
            conflicting_input=True,
            human_escalation_required=True,
            required_tool_marker=None,
        ),
        CapabilityLiveInput(
            input_id="swarm-api-gap",
            conversation_pattern="swarm",
            source_ref=_ref("factory-input", "swarm"),
            integration_intent="none",
            conflicting_input=False,
            human_escalation_required=False,
            required_tool_marker="TODO_TOOL.v1",
        ),
    )


class _Budget:
    def reserve(
        self,
        request: CapabilityLiveReservationRequest,
    ) -> CapabilityLiveReservation:
        return CapabilityLiveReservation.model_validate(request.model_dump())


class _Factory:
    def __init__(self, *, unavailable: str | None = None) -> None:
        self.unavailable = unavailable
        self.calls: list[tuple[str, str]] = []

    async def scaffold(
        self,
        request: CapabilityLiveStageRequest,
    ) -> CapabilityFactoryAuthority | None:
        self.calls.append((request.input.input_id, "scaffold"))
        if self.unavailable == "job":
            return None
        return self._authority(request, CapabilityLiveStage.SCAFFOLD_AND_TEST_HARNESS)

    async def implement(
        self,
        request: CapabilityLiveStageRequest,
    ) -> CapabilityFactoryAuthority | None:
        self.calls.append((request.input.input_id, "implement"))
        return self._authority(request, CapabilityLiveStage.CODEX_IMPLEMENTATION)

    async def seal(
        self,
        request: CapabilityLiveStageRequest,
    ) -> CapabilityFactoryAuthority | None:
        self.calls.append((request.input.input_id, "seal"))
        return self._authority(request, CapabilityLiveStage.SEAL)

    async def promote(
        self,
        request: CapabilityLiveStageRequest,
    ) -> CapabilityFactoryAuthority | None:
        self.calls.append((request.input.input_id, "promote"))
        if self.unavailable == "release":
            return None
        return self._authority(
            request,
            CapabilityLiveStage.PROMOTE,
            promoted=True,
        )

    async def released(
        self,
        request: CapabilityLiveStageRequest,
    ) -> CapabilityFactoryAuthority | None:
        self.calls.append((request.input.input_id, "released"))
        if self.unavailable == "release_readback":
            return None
        return self._authority(
            request,
            CapabilityLiveStage.PROMOTE,
            promoted=True,
        )

    def _authority(
        self,
        request: CapabilityLiveStageRequest,
        stage: CapabilityLiveStage,
        *,
        promoted: bool = False,
    ) -> CapabilityFactoryAuthority:
        input_id = request.input.input_id
        suite_ref = request.suite_ref or _ref("benchmark-suite", input_id)
        hermes_ref = request.hermes_result_ref or _ref("hermes-result", input_id)
        return CapabilityFactoryAuthority(
            stage=stage,
            input_id=input_id,
            source_ref=request.input.source_ref,
            job_id=_uuid(f"job:{input_id}"),
            correlation_id=request.correlation_id,
            subject_version=1,
            capability_id=f"capability-{input_id}",
            capability_version=1,
            suite_ref=suite_ref,
            hermes_result_ref=hermes_ref,
            evidence_refs=(_ref("factory-stage", f"{input_id}:{stage.value}"),),
            booked_cost_usd=(
                request.reservation.maximum_cost_usd - Decimal("0.01")
                if promoted
                else Decimal("0")
            ),
            controlled_recovery_id=(
                _uuid(f"recovery:{input_id}")
                if promoted and request.controlled_recovery_required
                else None
            ),
            normal_trace_ids=(
                tuple(_uuid(f"trace:{input_id}:{index}") for index in range(1, 4))
                if promoted
                else ()
            ),
            benchmark_authority_id=(
                _uuid(f"benchmark:{input_id}") if promoted else None
            ),
            benchmark_summary_ref=(
                _ref("benchmark-summary", input_id) if promoted else None
            ),
            release_authority_id=(
                _uuid(f"release:{input_id}") if promoted else None
            ),
        )


class _Benchmark:
    def __init__(self, factory: _Factory, *, unavailable: bool = False) -> None:
        self.factory = factory
        self.unavailable = unavailable
        self.calls: list[str] = []

    async def evaluate(
        self,
        request: CapabilityLiveStageRequest,
    ) -> CapabilityBusinessBenchmarkAuthority | None:
        self.calls.append(request.input.input_id)
        if self.unavailable:
            return None
        factory = self.factory._authority(
            request,
            CapabilityLiveStage.EVALUATE,
        )
        input_id = request.input.input_id
        payload = factory.model_dump()
        payload.update(
            {
                "benchmark_authority_id": _uuid(f"benchmark:{input_id}"),
                "benchmark_summary_ref": _ref("benchmark-summary", input_id),
                "normal_trace_ids": tuple(
                    _uuid(f"trace:{input_id}:{index}") for index in range(1, 4)
                ),
            }
        )
        return CapabilityBusinessBenchmarkAuthority.model_validate(payload)


class _Execution:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.calls: list[tuple[str, str]] = []

    async def execute(
        self,
        request: CapabilityLiveStageRequest,
        release: CapabilityFactoryAuthority,
    ) -> CapabilityExecutionAuthority | None:
        self.calls.append((request.input.input_id, "execute"))
        return None if self.unavailable else self._authority(release)

    async def readback(
        self,
        request: CapabilityLiveStageRequest,
        release: CapabilityFactoryAuthority,
    ) -> CapabilityExecutionAuthority | None:
        self.calls.append((request.input.input_id, "readback"))
        return None if self.unavailable else self._authority(release)

    async def restart_and_resume(
        self,
        request: CapabilityLiveRestartRequest,
        release: CapabilityFactoryAuthority,
    ) -> CapabilityExecutionAuthority | None:
        capability = request.capability
        self.calls.append((capability.input_id, "restart_and_resume"))
        return None if self.unavailable else self._authority(release, resumed=True)

    @staticmethod
    def _authority(
        release: CapabilityFactoryAuthority,
        *,
        resumed: bool = False,
    ) -> CapabilityExecutionAuthority:
        input_id = release.input_id
        return CapabilityExecutionAuthority(
            job_id=release.job_id,
            correlation_id=release.correlation_id,
            capability_id=release.capability_id,
            capability_version=release.capability_version,
            execution_authority_id=_uuid(
                f"{'resume-' if resumed else ''}execution-authority:{input_id}"
            ),
            execution_command_id=_uuid(
                f"{'resume-' if resumed else ''}command:{input_id}"
            ),
            execution_result_id=_uuid(
                f"{'resume-' if resumed else ''}result:{input_id}"
            ),
            evidence_refs=(_ref("execution", input_id),),
            booked_cost_usd=(
                release.booked_cost_usd + Decimal("0.01")
                if resumed
                else release.booked_cost_usd
            ),
            resume_actual_spend_usd=Decimal("0.01") if resumed else None,
            resume_budget_violation=False,
            terminal_status="succeeded",
            replayed=False,
        )


class _Projection:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.calls: list[tuple[str, str]] = []

    async def project_and_readback(
        self,
        request: CapabilityLiveStageRequest,
        release: CapabilityFactoryAuthority,
        execution: CapabilityExecutionAuthority,
    ) -> CapabilityProjectionAuthority | None:
        self.calls.append((request.input.input_id, "project_and_readback"))
        return None if self.unavailable else self._authority(release, execution)

    async def readback(
        self,
        capability: CapabilityLiveCapabilityEvidence,
        release: CapabilityFactoryAuthority,
        execution: CapabilityExecutionAuthority,
    ) -> CapabilityProjectionAuthority | None:
        self.calls.append((capability.input_id, "readback"))
        return None if self.unavailable else self._authority(release, execution)

    @staticmethod
    def _authority(
        release: CapabilityFactoryAuthority,
        execution: CapabilityExecutionAuthority,
    ) -> CapabilityProjectionAuthority:
        input_id = release.input_id
        return CapabilityProjectionAuthority(
            job_id=release.job_id,
            correlation_id=release.correlation_id,
            capability_id=release.capability_id,
            capability_version=release.capability_version,
            execution_command_id=execution.execution_command_id,
            execution_result_id=execution.execution_result_id,
            projection_event_id=_uuid(f"projection:{input_id}"),
            projection_readback_id=_uuid(f"projection-readback:{input_id}"),
            evidence_refs=(_ref("minibook-readback", input_id),),
        )


def _runner(
    tmp_path: Path,
    *,
    factory: _Factory | None = None,
    benchmark_unavailable: bool = False,
    execution_unavailable: bool = False,
    projection_unavailable: bool = False,
) -> tuple[
    CapabilityLiveRunner,
    _Factory,
    _Benchmark,
    _Execution,
    _Projection,
]:
    selected_factory = factory or _Factory()
    benchmark = _Benchmark(selected_factory, unavailable=benchmark_unavailable)
    execution = _Execution(unavailable=execution_unavailable)
    projection = _Projection(unavailable=projection_unavailable)
    pipeline = GatewayCapabilityLivePipeline(
        factory=selected_factory,
        benchmark=benchmark,
        execution=execution,
        projection=projection,
    )
    runner = CapabilityLiveRunner(
        pipeline=pipeline,
        budget=_Budget(),
        checkpoints=FilesystemCapabilityLiveCheckpointStore(tmp_path / "state"),
        clock=lambda: NOW,
    )
    return runner, selected_factory, benchmark, execution, projection


def test_real_gateway_pipeline_composition_runs_all_seven_authority_stages(
    tmp_path: Path,
) -> None:
    runner, factory, benchmark, execution, projection = _runner(tmp_path)

    evidence = asyncio.run(
        runner.run(
            run_id=RUN_ID,
            inputs=_inputs(),
            maximum_cost_usd=Decimal("1.00"),
        )
    )

    assert evidence.status == "ready_to_use"
    assert tuple(item.conversation_pattern for item in evidence.capabilities) == (
        "sequential",
        "selector_group_chat",
        "swarm",
    )
    for index, item in enumerate(_inputs()):
        assert [
            action for input_id, action in factory.calls if input_id == item.input_id
        ] == [
            "scaffold",
            "implement",
            "seal",
            "promote",
            "released",
            "released",
            *(["released"] if index == 0 else []),
        ]
    assert benchmark.calls == [item.input_id for item in _inputs()]
    assert [action for _, action in execution.calls].count("execute") == 3
    assert [action for _, action in projection.calls].count("project_and_readback") == 3
    assert execution.calls[-1] == ("sequential-n8n", "restart_and_resume")
    assert projection.calls[-1] == ("sequential-n8n", "readback")
    assert evidence.restart.resume_actual_spend_usd == Decimal("0.01")
    assert (
        evidence.restart.total_booked_cost_usd
        == evidence.capabilities[0].booked_cost_usd + Decimal("0.01")
    )
    assert evidence.restart.resume_budget_violation is False


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("job", "factory job authority"),
        ("benchmark", "business benchmark authority"),
        ("release", "release authority"),
        ("execution", "execution authority"),
        ("projection", "Minibook projection authority"),
    ],
)
def test_pipeline_fails_closed_when_current_authority_is_missing(
    tmp_path: Path,
    failure: str,
    message: str,
) -> None:
    runner, *_ = _runner(
        tmp_path,
        factory=_Factory(unavailable=failure if failure in {"job", "release"} else None),
        benchmark_unavailable=failure == "benchmark",
        execution_unavailable=failure == "execution",
        projection_unavailable=failure == "projection",
    )

    with pytest.raises(CapabilityLiveAuthorityUnavailable, match=message):
        asyncio.run(
            runner.run(
                run_id=RUN_ID,
                inputs=_inputs(),
                maximum_cost_usd=Decimal("1.00"),
            )
        )


def test_production_composition_builds_the_real_pipeline_from_four_ports(
    tmp_path: Path,
) -> None:
    factory = _Factory()
    benchmark = _Benchmark(factory)
    execution = _Execution()
    projection = _Projection()
    runner = compose_gateway_capability_live_runner(
        repository_root=tmp_path,
        factory=factory,
        benchmark=benchmark,
        execution=execution,
        projection=projection,
        clock=lambda: NOW,
    )

    evidence = asyncio.run(
        runner.run(
            run_id=RUN_ID,
            inputs=_inputs(),
            maximum_cost_usd=Decimal("1.00"),
        )
    )

    assert evidence.status == "ready_to_use"
    assert len(evidence.capabilities) == 3


def test_production_composition_fails_closed_without_generic_live_ports(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        GatewayCapabilityLiveCompositionError,
        match="generic capability Factory provisioning",
    ):
        compose_gateway_capability_live_runner(repository_root=tmp_path)


def test_production_composition_builds_current_authority_adapters_as_one_binding(
    tmp_path: Path,
) -> None:
    composition = object()
    provisioning = object()
    effects = object()
    resume_execution = object()

    runner = compose_gateway_capability_live_runner(
        repository_root=tmp_path,
        factory_composition=composition,
        factory_provisioning=provisioning,
        minibook_effects=effects,
        resume_execution=resume_execution,
        clock=lambda: NOW,
    )

    pipeline = runner._pipeline
    assert isinstance(pipeline._factory, CurrentCapabilityFactoryAdapter)
    assert isinstance(pipeline._benchmark, GatewayCapabilityBusinessBenchmarkAdapter)
    assert isinstance(pipeline._execution, RecordedFactoryCapabilityExecutionAdapter)
    assert pipeline._execution._resume_execution is resume_execution
    assert isinstance(pipeline._projection, GatewayMinibookCapabilityProjectionAdapter)


def test_current_composition_fails_closed_without_resume_execution_authority(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        GatewayCapabilityLiveCompositionError,
        match="resume execution authority",
    ):
        compose_gateway_capability_live_runner(
            repository_root=tmp_path,
            factory_composition=object(),
            factory_provisioning=object(),
            minibook_effects=object(),
            clock=lambda: NOW,
        )
