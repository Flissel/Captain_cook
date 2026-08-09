from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import importlib
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from agenten.agent_runtime.contracts import ArtifactRef
from agenten.agent_runtime.captain_production_adapters import (
    ContentAddressedArtifactAdapter,
)


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
RUN_ID = UUID("82000000-0000-0000-0000-000000000001")


def _module() -> Any:
    return importlib.import_module("agenten.agent_factory.capability_live_runner")


def _ref(namespace: str, value: str) -> ArtifactRef:
    digest = uuid5(NAMESPACE_URL, f"{namespace}:{value}").hex * 2
    return ArtifactRef(
        uri=f"artifact://{namespace}/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def _inputs(module: Any) -> tuple[Any, ...]:
    return (
        module.CapabilityLiveInput(
            input_id="sequential-n8n",
            conversation_pattern="sequential",
            source_ref=_ref("factory-input", "sequential"),
            integration_intent="n8n",
            conflicting_input=False,
            human_escalation_required=False,
            required_tool_marker=None,
        ),
        module.CapabilityLiveInput(
            input_id="selector-review",
            conversation_pattern="selector_group_chat",
            source_ref=_ref("factory-input", "selector"),
            integration_intent="none",
            conflicting_input=True,
            human_escalation_required=True,
            required_tool_marker=None,
        ),
        module.CapabilityLiveInput(
            input_id="swarm-api-gap",
            conversation_pattern="swarm",
            source_ref=_ref("factory-input", "swarm"),
            integration_intent="none",
            conflicting_input=False,
            human_escalation_required=False,
            required_tool_marker="TODO_TOOL.v1",
        ),
    )


def _capability_evidence(module: Any, request: Any) -> Any:
    seed = f"{request.run_id}:{request.input.input_id}"
    return module.CapabilityLiveCapabilityEvidence(
        input_id=request.input.input_id,
        conversation_pattern=request.input.conversation_pattern,
        job_id=uuid5(NAMESPACE_URL, f"{seed}:job"),
        correlation_id=request.correlation_id,
        capability_id=f"capability-{request.input.input_id}",
        capability_version=1,
        controlled_recovery_id=(
            uuid5(NAMESPACE_URL, f"{seed}:recovery")
            if request.controlled_recovery_required
            else None
        ),
        normal_trace_ids=tuple(
            uuid5(NAMESPACE_URL, f"{seed}:trace:{index}")
            for index in range(1, 4)
        ),
        release_authority_id=uuid5(NAMESPACE_URL, f"{seed}:release"),
        execution_authority_id=uuid5(NAMESPACE_URL, f"{seed}:execution-authority"),
        execution_command_id=uuid5(NAMESPACE_URL, f"{seed}:command"),
        execution_result_id=uuid5(NAMESPACE_URL, f"{seed}:result"),
        projection_event_id=uuid5(NAMESPACE_URL, f"{seed}:projection"),
        projection_readback_id=uuid5(NAMESPACE_URL, f"{seed}:readback"),
        suite_ref=request.suite_ref,
        hermes_result_ref=request.hermes_result_ref,
        booked_cost_usd=request.reservation.maximum_cost_usd,
        checkpoint_refs=(),
    )


class _Budget:
    def __init__(self, module: Any) -> None:
        self.module = module
        self.requests: list[Any] = []

    def reserve(self, request: Any) -> Any:
        self.requests.append(request)
        return self.module.CapabilityLiveReservation(
            reservation_id=request.reservation_id,
            run_id=request.run_id,
            input_id=request.input_id,
            maximum_cost_usd=request.maximum_cost_usd,
        )


class _Pipeline:
    def __init__(
        self,
        module: Any,
        *,
        retry_codex_once: bool = False,
        fail_after_scaffold: bool = False,
    ) -> None:
        self.module = module
        self.retry_codex_once = retry_codex_once
        self.fail_after_scaffold = fail_after_scaffold
        self.calls: list[Any] = []
        self.restart_calls: list[Any] = []
        self._retried = False

    async def run_stage(self, request: Any) -> Any:
        self.calls.append(request)
        if (
            self.retry_codex_once
            and not self._retried
            and request.input.input_id == "sequential-n8n"
            and request.stage.value == "codex_implementation"
        ):
            self._retried = True
            raise self.module.CapabilityLiveRuntimeRetry(
                "controlled runtime interruption"
            )
        if (
            self.fail_after_scaffold
            and request.input.input_id == "sequential-n8n"
            and request.stage.value == "codex_implementation"
        ):
            raise RuntimeError("simulated process exit")

        suite_ref = request.suite_ref or _ref(
            "benchmark-suite", request.input.input_id
        )
        hermes_ref = request.hermes_result_ref or _ref(
            "hermes-result", request.input.input_id
        )
        capability = None
        if request.stage.value == "project":
            bound = request.model_copy(
                update={
                    "suite_ref": suite_ref,
                    "hermes_result_ref": hermes_ref,
                }
            )
            capability = _capability_evidence(self.module, bound)
        return self.module.CapabilityLiveStageResult(
            stage=request.stage,
            suite_ref=suite_ref,
            hermes_result_ref=hermes_ref,
            evidence_refs=(
                _ref(
                    "capability-live-stage",
                    f"{request.input.input_id}:{request.stage.value}",
                ),
            ),
            booked_cost_usd=(
                request.reservation.maximum_cost_usd
                if request.stage.value == "project"
                else Decimal("0")
            ),
            capability_evidence=capability,
        )

    async def restart_and_resume(self, request: Any) -> Any:
        self.restart_calls.append(request)
        first = request.capability
        return self.module.CapabilityLiveRestartEvidence(
            run_id=request.run_id,
            job_id=first.job_id,
            reservation_id=request.reservation.reservation_id,
            correlation_id=first.correlation_id,
            capability_id=first.capability_id,
            capability_version=first.capability_version,
            original_execution_command_id=first.execution_command_id,
            original_execution_result_id=first.execution_result_id,
            resumed_execution_authority_id=uuid5(
                NAMESPACE_URL, f"{first.input_id}:resume-authority"
            ),
            resumed_execution_command_id=uuid5(
                NAMESPACE_URL, f"{first.input_id}:resume-command"
            ),
            resumed_execution_result_id=uuid5(
                NAMESPACE_URL, f"{first.input_id}:resume-result"
            ),
            projection_event_id=first.projection_event_id,
            projection_readback_id=first.projection_readback_id,
        )


def test_budget_is_split_into_three_reservations_with_remainder_last() -> None:
    module = _module()

    assert module.derive_capability_reservations(Decimal("1.00")) == (
        Decimal("0.33"),
        Decimal("0.33"),
        Decimal("0.34"),
    )
    assert sum(
        module.derive_capability_reservations(Decimal("6.00")), Decimal("0")
    ) == Decimal("6.00")


def test_runner_checkpoints_all_stages_and_retries_same_suite_and_hermes_result(
    tmp_path: Path,
) -> None:
    module = _module()
    budget = _Budget(module)
    pipeline = _Pipeline(module, retry_codex_once=True)
    store = module.FilesystemCapabilityLiveCheckpointStore(tmp_path / "state")
    runner = module.CapabilityLiveRunner(
        pipeline=pipeline,
        budget=budget,
        checkpoints=store,
        clock=lambda: NOW,
        maximum_runtime_retries=1,
    )

    evidence = asyncio.run(
        runner.run(
            run_id=RUN_ID,
            inputs=_inputs(module),
            maximum_cost_usd=Decimal("1.00"),
        )
    )

    assert [request.maximum_cost_usd for request in budget.requests] == [
        Decimal("0.33"),
        Decimal("0.33"),
        Decimal("0.34"),
    ]
    assert len({request.reservation_id for request in budget.requests}) == 3
    expected_stages = tuple(stage.value for stage in module.CAPABILITY_LIVE_STAGES)
    for input_item in _inputs(module):
        assert tuple(
            checkpoint.stage.value
            for checkpoint in store.checkpoints(RUN_ID, input_item.input_id)
        ) == expected_stages
    retried = [
        call
        for call in pipeline.calls
        if call.input.input_id == "sequential-n8n"
        and call.stage.value == "codex_implementation"
    ]
    assert len(retried) == 2
    assert retried[0].reservation == retried[1].reservation
    assert retried[0].correlation_id == retried[1].correlation_id
    assert retried[0].suite_ref == retried[1].suite_ref
    assert retried[0].hermes_result_ref == retried[1].hermes_result_ref
    assert len(pipeline.restart_calls) == 1
    assert evidence.booked_cost_usd == Decimal("1.00")


def test_restart_resumes_after_last_checkpoint_without_new_budget_or_plan(
    tmp_path: Path,
) -> None:
    module = _module()
    budget = _Budget(module)
    store = module.FilesystemCapabilityLiveCheckpointStore(tmp_path / "state")
    interrupted = module.CapabilityLiveRunner(
        pipeline=_Pipeline(module, fail_after_scaffold=True),
        budget=budget,
        checkpoints=store,
        clock=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="simulated process exit"):
        asyncio.run(
            interrupted.run(
                run_id=RUN_ID,
                inputs=_inputs(module),
                maximum_cost_usd=Decimal("1.00"),
            )
        )

    resumed_pipeline = _Pipeline(module)
    resumed = module.CapabilityLiveRunner(
        pipeline=resumed_pipeline,
        budget=budget,
        checkpoints=store,
        clock=lambda: NOW,
    )
    evidence = asyncio.run(
        resumed.run(
            run_id=RUN_ID,
            inputs=_inputs(module),
            maximum_cost_usd=Decimal("1.00"),
        )
    )

    assert len(budget.requests) == 3
    first_resumed = resumed_pipeline.calls[0]
    assert first_resumed.stage.value == "codex_implementation"
    assert first_resumed.suite_ref == _ref("benchmark-suite", "sequential-n8n")
    assert first_resumed.hermes_result_ref == _ref(
        "hermes-result", "sequential-n8n"
    )
    assert evidence.run_id == RUN_ID


def _complete_demo_evidence(module: Any) -> Any:
    inputs = _inputs(module)
    allocations = module.derive_capability_reservations(Decimal("1.00"))
    capabilities = []
    reservations = []
    for index, (input_item, allocation) in enumerate(
        zip(inputs, allocations, strict=True)
    ):
        correlation_id = uuid5(RUN_ID, input_item.input_id)
        reservation = module.CapabilityLiveReservation(
            reservation_id=uuid5(RUN_ID, f"reservation:{input_item.input_id}"),
            run_id=RUN_ID,
            input_id=input_item.input_id,
            maximum_cost_usd=allocation,
        )
        request = module.CapabilityLiveStageRequest(
            run_id=RUN_ID,
            input=input_item,
            correlation_id=correlation_id,
            reservation=reservation,
            stage=module.CapabilityLiveStage.PROJECT,
            controlled_recovery_required=index == 0,
            suite_ref=_ref("benchmark-suite", input_item.input_id),
            hermes_result_ref=_ref("hermes-result", input_item.input_id),
            retry_number=0,
        )
        reservations.append(reservation)
        capabilities.append(_capability_evidence(module, request))
    first = capabilities[0]
    return module.CapabilityLiveDemoEvidence(
        run_id=RUN_ID,
        maximum_cost_usd=Decimal("1.00"),
        booked_cost_usd=Decimal("1.00"),
        reservations=tuple(reservations),
        capabilities=tuple(capabilities),
        restart=module.CapabilityLiveRestartEvidence(
            run_id=RUN_ID,
            job_id=first.job_id,
            reservation_id=reservations[0].reservation_id,
            correlation_id=first.correlation_id,
            capability_id=first.capability_id,
            capability_version=first.capability_version,
            original_execution_command_id=first.execution_command_id,
            original_execution_result_id=first.execution_result_id,
            resumed_execution_authority_id=uuid5(RUN_ID, "restart-authority"),
            resumed_execution_command_id=uuid5(RUN_ID, "restart-command"),
            resumed_execution_result_id=uuid5(RUN_ID, "restart-result"),
            projection_event_id=first.projection_event_id,
            projection_readback_id=first.projection_readback_id,
        ),
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda module, value: value.model_copy(
                update={"capabilities": value.capabilities[:2]}
            ),
            "exactly three",
        ),
        (
            lambda module, value: value.model_copy(
                update={
                    "capabilities": (
                        value.capabilities[0],
                        value.capabilities[1].model_copy(
                            update={"conversation_pattern": "sequential"}
                        ),
                        value.capabilities[2],
                    )
                }
            ),
            "distinct conversation patterns",
        ),
        (
            lambda module, value: value.model_copy(
                update={
                    "capabilities": (
                        value.capabilities[0].model_copy(
                            update={"controlled_recovery_id": None}
                        ),
                        *value.capabilities[1:],
                    )
                }
            ),
            "controlled recovery",
        ),
        (
            lambda module, value: value.model_copy(
                update={
                    "capabilities": (
                        value.capabilities[0].model_copy(
                            update={
                                "normal_trace_ids": value.capabilities[
                                    0
                                ].normal_trace_ids[:2]
                            }
                        ),
                        *value.capabilities[1:],
                    )
                }
            ),
            "three distinct normal traces",
        ),
        (
            lambda module, value: value.model_copy(
                update={
                    "capabilities": (
                        value.capabilities[0].model_copy(
                            update={"release_authority_id": None}
                        ),
                        *value.capabilities[1:],
                    )
                }
            ),
            "release authority",
        ),
        (
            lambda module, value: value.model_copy(
                update={
                    "capabilities": (
                        value.capabilities[0].model_copy(
                            update={"execution_authority_id": None}
                        ),
                        *value.capabilities[1:],
                    )
                }
            ),
            "execution authority",
        ),
        (
            lambda module, value: value.model_copy(
                update={
                    "capabilities": (
                        value.capabilities[0].model_copy(
                            update={"projection_readback_id": None}
                        ),
                        *value.capabilities[1:],
                    )
                }
            ),
            "projection read-back",
        ),
        (
            lambda module, value: value.model_copy(
                update={
                    "restart": value.restart.model_copy(
                        update={
                            "resumed_execution_command_id": value.restart.original_execution_command_id
                        }
                    )
                }
            ),
            "reused restart execution command",
        ),
        (
            lambda module, value: value.model_copy(
                update={
                    "restart": value.restart.model_copy(
                        update={
                            "resumed_execution_result_id": value.restart.original_execution_result_id
                        }
                    )
                }
            ),
            "reused restart execution result",
        ),
        (
            lambda module, value: value.model_copy(
                update={"booked_cost_usd": Decimal("1.01")}
            ),
            "cost ceiling",
        ),
    ],
)
def test_final_gate_rejects_incomplete_or_unbound_evidence(
    mutate: Any,
    message: str,
) -> None:
    module = _module()
    evidence = mutate(module, _complete_demo_evidence(module))

    with pytest.raises(module.CapabilityLiveEvidenceError, match=message):
        module.validate_live_demo_evidence(evidence)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", uuid5(NAMESPACE_URL, "foreign-run")),
        ("job_id", uuid5(NAMESPACE_URL, "foreign-job")),
        ("reservation_id", uuid5(NAMESPACE_URL, "foreign-reservation")),
        ("correlation_id", uuid5(NAMESPACE_URL, "foreign-correlation")),
        ("capability_id", "foreign-capability"),
        ("capability_version", 2),
    ],
)
def test_final_gate_rejects_changed_restart_authority_binding(
    field: str,
    value: object,
) -> None:
    module = _module()
    evidence = _complete_demo_evidence(module)
    changed = evidence.model_copy(
        update={"restart": evidence.restart.model_copy(update={field: value})}
    )

    with pytest.raises(
        module.CapabilityLiveEvidenceError,
        match="stable authority binding",
    ):
        module.validate_live_demo_evidence(changed)


def test_input_contract_requires_the_three_exact_demo_semantics() -> None:
    module = _module()
    sequential = _inputs(module)[0]

    with pytest.raises(ValueError, match="requires n8n"):
        sequential.model_copy(update={"integration_intent": "none"}).model_validate(
            sequential.model_copy(
                update={"integration_intent": "none"}
            ).model_dump(mode="json")
        )


def test_entrypoint_binds_three_repository_sources_and_revalidates_output(
    tmp_path: Path,
) -> None:
    entrypoint = importlib.import_module(
        "agenten.agent_factory.capability_live_entrypoint"
    )
    runner_module = _module()
    repository = tmp_path / "repository"
    paths = tuple(
        repository / relative
        for relative in (
            "incoming/service-desk/TO_BE_BUILT.md",
            "incoming/security-review/TO_BE_BUILT.md",
            "incoming/operations-gap/TO_BE_BUILT.md",
        )
    )
    for index, path in enumerate(paths):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# canonical input {index}\n", encoding="utf-8")
    expected = _complete_demo_evidence(runner_module)

    class Runner:
        calls: list[dict[str, object]] = []

        async def run(self, **kwargs: object):
            self.calls.append(kwargs)
            return expected

    output = repository / ".captain-cook/live-demo/current/final-evidence.json"
    live_runner = Runner()

    evidence = asyncio.run(
        entrypoint.run_capability_live_demo(
            runner=live_runner,
            repository_root=repository,
            input_paths=paths,
            patterns=("sequential", "selector_group_chat", "swarm"),
            maximum_cost_usd=Decimal("1.00"),
            evidence_path=output,
            run_id=RUN_ID,
        )
    )

    assert evidence == expected
    assert entrypoint.verify_capability_live_evidence(output) == expected
    assert len(live_runner.calls) == 1
    call = live_runner.calls[0]
    inputs = call["inputs"]
    assert tuple(item.input_id for item in inputs) == (
        "sequential-n8n",
        "selector-review",
        "swarm-api-gap",
    )
    assert tuple(item.source_ref.sha256 for item in inputs) == tuple(
        hashlib.sha256(path.read_bytes()).hexdigest() for path in paths
    )
    source_store = ContentAddressedArtifactAdapter(
        repository / ".captain-cook/artifacts/sha256"
    )
    assert tuple(source_store.read_bytes(item.source_ref) for item in inputs) == tuple(
        path.read_bytes() for path in paths
    )
    assert all("C:\\Users" not in item.source_ref.uri for item in inputs)


def test_entrypoint_main_runs_all_seven_stages_with_typed_offline_fakes(
    tmp_path: Path,
) -> None:
    entrypoint = importlib.import_module(
        "agenten.agent_factory.capability_live_entrypoint"
    )
    runner_module = _module()
    repository = tmp_path / "repository"
    paths = tuple(
        repository / relative
        for relative in (
            "incoming/team-a/TO_BE_BUILT.md",
            "incoming/team-b/TO_BE_BUILT.md",
            "incoming/team-c/TO_BE_BUILT.md",
        )
    )
    for index, path in enumerate(paths):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# generic input {index}\n", encoding="utf-8")
    pipeline = _Pipeline(runner_module)

    def runner_factory(root: Path) -> object:
        assert root == repository
        return runner_module.CapabilityLiveRunner(
            pipeline=pipeline,
            budget=_Budget(runner_module),
            checkpoints=runner_module.FilesystemCapabilityLiveCheckpointStore(
                root / ".captain-cook/live-demo/checkpoints"
            ),
            clock=lambda: NOW,
        )

    evidence_path = repository / ".captain-cook/live-demo/current/final.json"
    result = entrypoint.main(
        [
            "--repository-root",
            str(repository),
            "--input",
            str(paths[0]),
            "--input",
            str(paths[1]),
            "--input",
            str(paths[2]),
            "--pattern",
            "sequential",
            "--pattern",
            "selector_group_chat",
            "--pattern",
            "swarm",
            "--max-cost-usd",
            "1.00",
            "--evidence-path",
            str(evidence_path),
            "--run-id",
            str(RUN_ID),
        ],
        runner_factory=runner_factory,
    )

    assert result == 0
    assert len(pipeline.calls) == 21
    assert tuple(
        call.stage for call in pipeline.calls[:7]
    ) == runner_module.CAPABILITY_LIVE_STAGES
    assert entrypoint.verify_capability_live_evidence(evidence_path).run_id == RUN_ID


def test_entrypoint_rejects_foreign_source_before_runner(tmp_path: Path) -> None:
    entrypoint = importlib.import_module(
        "agenten.agent_factory.capability_live_entrypoint"
    )
    repository = tmp_path / "repository"
    repository.mkdir()
    foreign = tmp_path / "TO_BE_BUILT.md"
    foreign.write_text("# foreign\n", encoding="utf-8")

    class Runner:
        calls = 0

        async def run(self, **_kwargs: object):
            self.calls += 1
            raise AssertionError("foreign input reached the live runner")

    runner = Runner()
    with pytest.raises(ValueError, match="repository-owned"):
        asyncio.run(
            entrypoint.run_capability_live_demo(
                runner=runner,
                repository_root=repository,
                input_paths=(foreign, foreign, foreign),
                patterns=("sequential", "selector_group_chat", "swarm"),
                maximum_cost_usd=Decimal("1.00"),
                evidence_path=repository / ".captain-cook/live-demo/evidence.json",
                run_id=RUN_ID,
            )
        )
    assert runner.calls == 0
