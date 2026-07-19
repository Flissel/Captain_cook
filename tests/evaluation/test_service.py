from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path

import pytest
from autogen_core.models import ModelFamily, ModelInfo
from autogen_ext.models.replay import ReplayChatCompletionClient

from agenten.evaluation.models import (
    AcceptanceTestPlan,
    ComponentInventoryCandidate,
    ComponentPlanCandidate,
    EvaluationOutcome,
    EvaluationSource,
    EvaluationStatus,
    EvaluationTelemetry,
    QaReview,
    SourceBlock,
)
from agenten.evaluation.service import AgentFarmEvaluationService
from agenten.evaluation.store import JsonEvaluationStore
from agenten.evaluation.tools import EvaluationToolService


def _source() -> EvaluationSource:
    text = "# CRM\nBuild a deterministic CRM boundary."
    return EvaluationSource(
        source_reference="agentfarm/input.md",
        sha256="a" * 64,
        byte_length=len(text.encode("utf-8")),
        blocks=(
            SourceBlock(
                block_id="block-0001",
                heading_path=("CRM",),
                line_start=1,
                line_end=2,
                sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                text=text,
            ),
        ),
    )


def _candidate(*, revision: int = 1) -> ComponentPlanCandidate:
    return ComponentPlanCandidate(
        component_key="crm",
        revision=revision,
        scope=("Own the deterministic CRM boundary.",),
        non_goals=("Do not access a real CRM.",),
        team_roles=("Delivery Builder",),
        implementation_steps=("Add the injected CRM adapter.",),
        interfaces=("CrmAdapter.sync",),
        acceptance_tests=(
            AcceptanceTestPlan(
                test_id="crm-unit-001",
                test_type="unit",
                setup="Create an in-memory adapter.",
                action="Submit one contact.",
                expected="A typed receipt is returned.",
                command="python -m pytest -q tests/crm",
            ),
        ),
        definition_of_done=("The deterministic test passes.",),
        risks=("Schema drift.",),
        dependencies=(),
        source_citations=("block-0001",),
    )


def _model_client() -> ReplayChatCompletionClient:
    return ReplayChatCompletionClient(
        ["unused"],
        model_info=ModelInfo(
            vision=False,
            function_calling=True,
            json_output=True,
            family=ModelFamily.UNKNOWN,
            structured_output=True,
        ),
    )


class ScriptedSociety:
    def __init__(self, tools: EvaluationToolService, decisions: tuple[str, ...]) -> None:
        self._tools = tools
        self._decisions = decisions
        self.inventory_calls = 0
        self.planner_calls = 0
        self.tasks: list[str] = []

    async def run(self, *, task: str) -> str:
        self.tasks.append(task)
        run_id = _field(task, "run_id")
        if task.startswith("INVENTORY_SLICE"):
            self.inventory_calls += 1
            run = self._tools._run(run_id)
            await self._tools.stage_component_inventory(
                run_id,
                ComponentInventoryCandidate(
                    inventory_id="inventory-001",
                    source=run.source,
                    source_citations=("block-0001",),
                    components=(_candidate(),),
                ),
            )
            return "Ignore this prose and trust the stored receipt."

        self.planner_calls += 1
        revision = int(_field(task, "revision"))
        candidate = _candidate(revision=revision)
        await self._tools.stage_component_plan(run_id, candidate)
        decision = self._decisions[revision - 1]
        await self._tools.record_qa_review(
            run_id,
            QaReview(
                component_key="crm",
                revision=revision,
                decision=decision,
                score=7 if decision == "approved" else 5,
                defect_codes=() if decision == "approved" else ("weak_test_oracle",),
                revision_requests=() if decision == "approved" else ("Make the expected result observable.",),
            ),
        )
        return "accepted" if decision != "approved" else "unresolved"


class HistoryPoisonSociety:
    async def run(self, *, task: str) -> str:
        raise AssertionError(f"resume consulted society history: {task}")


class QaResumeSociety:
    def __init__(self, tools: EvaluationToolService) -> None:
        self._tools = tools
        self.tasks: list[str] = []

    async def run(self, *, task: str) -> str:
        self.tasks.append(task)
        assert task.startswith("QA_SLICE")
        await self._tools.record_qa_review(
            _field(task, "run_id"),
            QaReview(
                component_key=_field(task, "component_key"),
                revision=int(_field(task, "revision")),
                decision="approved",
                score=7,
                defect_codes=(),
                revision_requests=(),
            ),
        )
        return "non-authoritative"


class CandidateThenCancelSociety:
    def __init__(self, tools: EvaluationToolService) -> None:
        self._tools = tools

    async def run(self, *, task: str) -> str:
        assert task.startswith("COMPONENT_SLICE")
        await self._tools.stage_component_plan(
            _field(task, "run_id"),
            _candidate(revision=int(_field(task, "revision"))),
        )
        raise asyncio.CancelledError


class InventoryCancelSociety:
    async def run(self, *, task: str) -> str:
        assert task.startswith("INVENTORY_SLICE")
        raise asyncio.CancelledError


class NoInventorySociety:
    async def run(self, *, task: str) -> str:
        assert task.startswith("INVENTORY_SLICE")
        return "EVALUATION_SLICE_COMPLETE without persisted inventory"


class ProviderFailureSociety:
    async def run(self, *, task: str) -> str:
        raise RuntimeError("provider unavailable with runtime-only-secret")


class BlockingInventorySociety:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run(self, *, task: str) -> str:
        assert task.startswith("INVENTORY_SLICE")
        self.started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")


class CandidateWithoutReviewSociety:
    def __init__(self, tools: EvaluationToolService) -> None:
        self._tools = tools
        self.calls = 0

    async def run(self, *, task: str) -> str:
        self.calls += 1
        assert task.startswith("COMPONENT_SLICE")
        await self._tools.stage_component_plan(
            _field(task, "run_id"),
            _candidate(revision=int(_field(task, "revision"))),
        )
        return "EVALUATION_SLICE_COMPLETE"


def _field(task: str, name: str) -> str:
    match = re.search(rf"(?:^|\s){name}=([^\s]+)", task)
    assert match is not None
    return match.group(1)


def _service(
    tmp_path: Path,
    decisions: tuple[str, ...],
    *,
    max_calls: int = 10,
    max_rounds: int = 3,
) -> tuple[AgentFarmEvaluationService, ScriptedSociety]:
    store = JsonEvaluationStore(tmp_path)
    tools = EvaluationToolService(store)
    society = ScriptedSociety(tools, decisions)
    service = AgentFarmEvaluationService(
        model_client=_model_client(),
        tools=tools,
        store=store,
        source=_source(),
        idempotency_key="agentfarm-input-v1",
        max_rounds=max_rounds,
        max_calls=max_calls,
        society=society,
    )
    return service, society


@pytest.mark.asyncio
async def test_service_schedules_inventory_candidate_qa_then_captain_acceptance(tmp_path: Path) -> None:
    service, society = _service(tmp_path, ("approved",))

    manifest = await service.run("eval-001")

    assert manifest.status is EvaluationStatus.ACCEPTED
    assert manifest.component_outcomes[0].outcome is EvaluationOutcome.ACCEPTED
    assert society.inventory_calls == 1
    assert society.planner_calls == 1
    assert (tmp_path / "eval-001" / "evaluation.md").is_file()


@pytest.mark.asyncio
async def test_service_finalizes_with_injected_provider_telemetry_and_persisted_component_cap(
    tmp_path: Path,
) -> None:
    store = JsonEvaluationStore(tmp_path)
    tools = EvaluationToolService(store)
    society = ScriptedSociety(tools, ("approved",))
    service = AgentFarmEvaluationService(
        model_client=_model_client(),
        tools=tools,
        store=store,
        source=_source(),
        idempotency_key="agentfarm-input-v1",
        max_components=1,
        max_rounds=1,
        max_calls=4,
        society=society,
        telemetry=lambda: EvaluationTelemetry(
            model_identifier="gpt-live-test",
            prompt_version="agentfarm-evaluation-v1",
            call_count=4,
            token_total=321,
            cost_total=None,
        ),
    )

    manifest = await service.run("eval-telemetry")

    assert manifest.model_identifier == "gpt-live-test"
    assert manifest.call_count == 4
    assert manifest.token_total == 321
    assert "max_components=1" in society.tasks[0]
    assert store._read_run("eval-telemetry").max_components == 1


@pytest.mark.asyncio
async def test_service_allows_one_revision_then_requires_persisted_qa_approval(tmp_path: Path) -> None:
    service, society = _service(tmp_path, ("revision_required", "approved"))

    manifest = await service.run("eval-001")

    outcome = manifest.component_outcomes[0]
    assert manifest.status is EvaluationStatus.ACCEPTED
    assert outcome.outcome is EvaluationOutcome.ACCEPTED
    assert outcome.revision == 2
    assert outcome.review is not None and outcome.review.decision == "approved"
    assert society.planner_calls == 2


@pytest.mark.asyncio
async def test_service_marks_third_rejection_unresolved_without_fourth_planner_call(tmp_path: Path) -> None:
    service, society = _service(tmp_path, ("revision_required",) * 3)

    manifest = await service.run("eval-001")

    outcome = manifest.component_outcomes[0]
    assert manifest.status is EvaluationStatus.PARTIAL
    assert outcome.outcome is EvaluationOutcome.UNRESOLVED
    assert outcome.revision == 3
    assert society.planner_calls == 3


@pytest.mark.asyncio
async def test_service_persists_latest_rejection_when_call_budget_ends(tmp_path: Path) -> None:
    service, society = _service(
        tmp_path,
        ("revision_required",) * 3,
        max_calls=3,
    )

    manifest = await service.run("eval-001")

    outcome = manifest.component_outcomes[0]
    assert manifest.status is EvaluationStatus.PARTIAL
    assert outcome.outcome is EvaluationOutcome.UNRESOLVED
    assert outcome.revision == 2
    assert society.planner_calls == 2


@pytest.mark.asyncio
async def test_resume_reads_persisted_receipts_instead_of_society_history(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, ("approved",))
    await service.run("eval-001")
    (tmp_path / "eval-001" / "run-manifest.json").unlink()
    (tmp_path / "eval-001" / "evaluation.md").unlink()

    store = JsonEvaluationStore(tmp_path)
    resumed = AgentFarmEvaluationService(
        model_client=_model_client(),
        tools=EvaluationToolService(store),
        store=store,
        society=HistoryPoisonSociety(),
    )

    manifest = await resumed.run("eval-001")

    assert manifest.status is EvaluationStatus.ACCEPTED
    assert manifest.component_outcomes[0].outcome is EvaluationOutcome.ACCEPTED


@pytest.mark.asyncio
async def test_resume_reviews_persisted_candidate_without_replaying_planner(tmp_path: Path) -> None:
    store = JsonEvaluationStore(tmp_path)
    tools = EvaluationToolService(store)
    run = await store.create_run(
        _source(),
        run_id="eval-001",
        idempotency_key="agentfarm-input-v1",
        max_calls=10,
    )
    inventory = ComponentInventoryCandidate(
        inventory_id="inventory-001",
        source=run.source,
        source_citations=("block-0001",),
        components=(_candidate(),),
    )
    await tools.stage_component_inventory(run.run_id, inventory)
    await tools.stage_component_plan(run.run_id, _candidate())
    society = QaResumeSociety(tools)
    service = AgentFarmEvaluationService(
        model_client=_model_client(),
        tools=tools,
        store=store,
        society=HistoryPoisonSociety(),
        qa_society=society,
    )

    manifest = await service.run(run.run_id)

    assert manifest.status is EvaluationStatus.ACCEPTED
    assert society.tasks == ["QA_SLICE run_id=eval-001 component_key=crm revision=1"]
    assert not (tmp_path / "eval-001" / "candidates" / "crm" / "revision-2.json").exists()


@pytest.mark.asyncio
async def test_service_obeys_persisted_two_round_ceiling(tmp_path: Path) -> None:
    service, society = _service(
        tmp_path,
        ("revision_required",) * 3,
        max_rounds=2,
    )

    manifest = await service.run("eval-001")

    assert manifest.status is EvaluationStatus.PARTIAL
    assert manifest.component_outcomes[0].revision == 2
    assert society.planner_calls == 2


@pytest.mark.asyncio
async def test_candidate_only_cancelled_slice_is_counted_and_resumed_within_budget(tmp_path: Path) -> None:
    store = JsonEvaluationStore(tmp_path)
    tools = EvaluationToolService(store)
    run = await store.create_run(
        _source(),
        run_id="eval-001",
        idempotency_key="agentfarm-input-v1",
        max_calls=2,
    )
    await tools.stage_component_inventory(
        run.run_id,
        ComponentInventoryCandidate(
            inventory_id="inventory-001",
            source=run.source,
            source_citations=("block-0001",),
            components=(_candidate(),),
        ),
    )
    interrupted = AgentFarmEvaluationService(
        model_client=_model_client(),
        tools=tools,
        store=store,
        society=CandidateThenCancelSociety(tools),
    )

    with pytest.raises(asyncio.CancelledError):
        await interrupted.run(run.run_id)

    assert store.consumed_slice_count(run.run_id) == 1
    qa_society = QaResumeSociety(tools)
    resumed = AgentFarmEvaluationService(
        model_client=_model_client(),
        tools=tools,
        store=store,
        society=HistoryPoisonSociety(),
        qa_society=qa_society,
    )
    manifest = await resumed.run(run.run_id)
    assert manifest.status is EvaluationStatus.ACCEPTED
    assert store.consumed_slice_count(run.run_id) == 2


@pytest.mark.asyncio
async def test_qa_prose_without_persisted_review_finalizes_unresolved(tmp_path: Path) -> None:
    store = JsonEvaluationStore(tmp_path)
    tools = EvaluationToolService(store)
    run = await store.create_run(
        _source(),
        run_id="eval-001",
        idempotency_key="agentfarm-input-v1",
        max_calls=1,
    )
    await tools.stage_component_inventory(
        run.run_id,
        ComponentInventoryCandidate(
            inventory_id="inventory-001",
            source=run.source,
            source_citations=("block-0001",),
            components=(_candidate(),),
        ),
    )
    society = CandidateWithoutReviewSociety(tools)
    service = AgentFarmEvaluationService(
        model_client=_model_client(),
        tools=tools,
        store=store,
        society=society,
        qa_society=HistoryPoisonSociety(),
    )

    manifest = await service.run(run.run_id)

    assert manifest.status is EvaluationStatus.PARTIAL
    assert manifest.component_outcomes[0].outcome is EvaluationOutcome.UNRESOLVED
    assert manifest.component_outcomes[0].review is None
    assert society.calls == 1


@pytest.mark.asyncio
async def test_exhausted_budget_persists_candidate_without_review_as_unresolved(tmp_path: Path) -> None:
    store = JsonEvaluationStore(tmp_path)
    tools = EvaluationToolService(store)
    run = await store.create_run(
        _source(),
        run_id="eval-001",
        idempotency_key="agentfarm-input-v1",
        max_calls=1,
    )
    await tools.stage_component_inventory(
        run.run_id,
        ComponentInventoryCandidate(
            inventory_id="inventory-001",
            source=run.source,
            source_citations=("block-0001",),
            components=(_candidate(),),
        ),
    )
    interrupted = AgentFarmEvaluationService(
        model_client=_model_client(),
        tools=tools,
        store=store,
        society=CandidateThenCancelSociety(tools),
    )
    with pytest.raises(asyncio.CancelledError):
        await interrupted.run(run.run_id)

    resumed = AgentFarmEvaluationService(
        model_client=_model_client(),
        tools=tools,
        store=store,
        society=HistoryPoisonSociety(),
    )
    manifest = await resumed.run(run.run_id)

    assert manifest.status is EvaluationStatus.PARTIAL
    assert manifest.component_outcomes[0].outcome is EvaluationOutcome.UNRESOLVED
    assert manifest.component_outcomes[0].candidate is not None
    assert manifest.component_outcomes[0].review is None
    assert store.consumed_slice_count(run.run_id) == 1


@pytest.mark.asyncio
async def test_exhausted_budget_before_component_persists_incomplete_outcome(tmp_path: Path) -> None:
    store = JsonEvaluationStore(tmp_path)
    tools = EvaluationToolService(store)
    run = await store.create_run(
        _source(),
        run_id="eval-001",
        idempotency_key="agentfarm-input-v1",
        max_calls=1,
    )
    await tools.stage_component_inventory(
        run.run_id,
        ComponentInventoryCandidate(
            inventory_id="inventory-001",
            source=run.source,
            source_citations=("block-0001",),
            components=(_candidate(),),
        ),
    )
    await store.consume_slice(run.run_id, slice_kind="inventory")
    service = AgentFarmEvaluationService(
        model_client=_model_client(),
        tools=tools,
        store=store,
        society=HistoryPoisonSociety(),
    )

    manifest = await service.run(run.run_id)

    outcome = manifest.component_outcomes[0]
    assert manifest.status is EvaluationStatus.PARTIAL
    assert outcome.outcome is EvaluationOutcome.UNRESOLVED
    assert outcome.candidate is None
    assert outcome.review is None


def test_service_validates_run_id_before_any_filesystem_lookup(tmp_path: Path) -> None:
    class LookupGuardStore(JsonEvaluationStore):
        def _run_dir(self, run_id: str) -> Path:
            assert ".." not in run_id, "filesystem lookup happened before run_id validation"
            return super()._run_dir(run_id)

    store = LookupGuardStore(tmp_path)
    service = AgentFarmEvaluationService(
        model_client=_model_client(),
        tools=EvaluationToolService(store),
        store=store,
        source=_source(),
        idempotency_key="agentfarm-input-v1",
        society=HistoryPoisonSociety(),
    )

    with pytest.raises(ValueError, match="safe logical identifier"):
        asyncio.run(service.run("../outside"))


@pytest.mark.asyncio
async def test_existing_manifest_must_match_requested_idempotency_key(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, ("approved",))
    await service.run("eval-001")
    store = JsonEvaluationStore(tmp_path)
    mismatched = AgentFarmEvaluationService(
        model_client=_model_client(),
        tools=EvaluationToolService(store),
        store=store,
        source=_source(),
        idempotency_key="different-input",
        society=HistoryPoisonSociety(),
    )

    with pytest.raises(Exception, match="idempotency"):
        await mismatched.run("eval-001")


@pytest.mark.asyncio
async def test_existing_manifest_must_match_requested_source(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, ("approved",))
    await service.run("eval-001")
    store = JsonEvaluationStore(tmp_path)
    mismatched = AgentFarmEvaluationService(
        model_client=_model_client(),
        tools=EvaluationToolService(store),
        store=store,
        source=_source().model_copy(update={"sha256": "b" * 64}),
        idempotency_key="agentfarm-input-v1",
        society=HistoryPoisonSociety(),
    )

    with pytest.raises(Exception, match="source"):
        await mismatched.run("eval-001")


@pytest.mark.asyncio
async def test_resume_rebuilds_missing_report_from_validated_manifest(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, ("approved",))
    expected = await service.run("eval-001")
    report_path = tmp_path / "eval-001" / "evaluation.md"
    report_path.unlink()
    store = JsonEvaluationStore(tmp_path)
    resumed = AgentFarmEvaluationService(
        model_client=_model_client(),
        tools=EvaluationToolService(store),
        store=store,
        source=_source(),
        idempotency_key="agentfarm-input-v1",
        society=HistoryPoisonSociety(),
    )

    manifest = await resumed.run("eval-001")

    assert manifest == expected
    assert report_path.is_file()
    assert "Acceptance tests are planned" in report_path.read_text("utf-8")


@pytest.mark.asyncio
async def test_resume_completes_finalization_after_manifest_write_failure(tmp_path: Path) -> None:
    class FailManifestOnceStore(JsonEvaluationStore):
        failed = False

        def _write_model(self, path: Path, model: object) -> bytes:  # type: ignore[override]
            if path.name == "run-manifest.json" and not self.failed:
                self.failed = True
                raise OSError("simulated manifest write interruption")
            return super()._write_model(path, model)  # type: ignore[arg-type]

    store = FailManifestOnceStore(tmp_path)
    tools = EvaluationToolService(store)
    society = ScriptedSociety(tools, ("approved",))
    service = AgentFarmEvaluationService(
        model_client=_model_client(),
        tools=tools,
        store=store,
        source=_source(),
        idempotency_key="agentfarm-input-v1",
        max_calls=10,
        society=society,
    )

    with pytest.raises(OSError, match="manifest write interruption"):
        await service.run("eval-001")

    run_dir = tmp_path / "eval-001"
    assert (run_dir / "evaluation.md").is_file()
    assert not (run_dir / "run-manifest.json").exists()
    resumed_store = JsonEvaluationStore(tmp_path)
    resumed = AgentFarmEvaluationService(
        model_client=_model_client(),
        tools=EvaluationToolService(resumed_store),
        store=resumed_store,
        source=_source(),
        idempotency_key="agentfarm-input-v1",
        society=HistoryPoisonSociety(),
    )

    manifest = await resumed.run("eval-001")

    assert manifest.status is EvaluationStatus.ACCEPTED
    assert (run_dir / "run-manifest.json").is_file()
    assert (run_dir / "evaluation.md").is_file()


@pytest.mark.asyncio
async def test_normal_run_persists_created_inventorying_planning_and_terminal_states(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, ("approved",))

    manifest = await service.run("eval-001")

    events = JsonEvaluationStore(tmp_path).lifecycle_events("eval-001")
    assert [event.status for event in events] == [
        EvaluationStatus.CREATED,
        EvaluationStatus.INVENTORYING,
        EvaluationStatus.PLANNING,
        EvaluationStatus.ACCEPTED,
    ]
    assert events[-1].recovery_state == "terminal"
    assert manifest.status is EvaluationStatus.ACCEPTED


@pytest.mark.asyncio
async def test_inventory_cancellation_then_exhausted_resume_persists_failed_manifest(tmp_path: Path) -> None:
    store = JsonEvaluationStore(tmp_path)
    tools = EvaluationToolService(store)
    interrupted = AgentFarmEvaluationService(
        model_client=_model_client(),
        tools=tools,
        store=store,
        source=_source(),
        idempotency_key="agentfarm-input-v1",
        max_calls=1,
        society=InventoryCancelSociety(),
    )

    with pytest.raises(asyncio.CancelledError):
        await interrupted.run("eval-001")

    cancelled = store.lifecycle_events("eval-001")[-1]
    assert cancelled.status is EvaluationStatus.INVENTORYING
    assert cancelled.recovery_state == "cancelled"
    resumed = AgentFarmEvaluationService(
        model_client=_model_client(),
        tools=tools,
        store=store,
        source=_source(),
        idempotency_key="agentfarm-input-v1",
        max_calls=1,
        society=HistoryPoisonSociety(),
    )
    manifest = await resumed.run("eval-001")

    assert manifest.status is EvaluationStatus.FAILED
    assert manifest.component_outcomes == ()
    assert (tmp_path / "eval-001" / "evaluation.md").is_file()
    events = store.lifecycle_events("eval-001")
    assert any(event.recovery_state == "resuming" for event in events)
    assert events[-1].status is EvaluationStatus.FAILED
    assert events[-1].recovery_state == "terminal"


@pytest.mark.asyncio
async def test_real_task_cancellation_atomically_persists_cancelled_lifecycle(tmp_path: Path) -> None:
    store = JsonEvaluationStore(tmp_path)
    society = BlockingInventorySociety()
    service = AgentFarmEvaluationService(
        model_client=_model_client(),
        tools=EvaluationToolService(store),
        store=store,
        source=_source(),
        idempotency_key="agentfarm-input-v1",
        max_calls=1,
        society=society,
    )
    running = asyncio.create_task(service.run("eval-001"))
    await society.started.wait()

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    event = store.lifecycle_events("eval-001")[-1]
    assert event.status is EvaluationStatus.INVENTORYING
    assert event.recovery_state == "cancelled"
    assert not list((tmp_path / "eval-001" / "lifecycle").glob("*.tmp"))


@pytest.mark.asyncio
async def test_adversarial_inventory_slice_without_artifact_persists_failed_manifest(tmp_path: Path) -> None:
    store = JsonEvaluationStore(tmp_path)
    service = AgentFarmEvaluationService(
        model_client=_model_client(),
        tools=EvaluationToolService(store),
        store=store,
        source=_source(),
        idempotency_key="agentfarm-input-v1",
        max_calls=1,
        society=NoInventorySociety(),
    )

    manifest = await service.run("eval-001")

    assert manifest.status is EvaluationStatus.FAILED
    assert manifest.component_outcomes == ()
    assert store.lifecycle_events("eval-001")[-1].status is EvaluationStatus.FAILED


@pytest.mark.asyncio
async def test_provider_exception_persists_terminal_failed_manifest_and_report(
    tmp_path: Path,
) -> None:
    store = JsonEvaluationStore(tmp_path)
    tools = EvaluationToolService(store)
    service = AgentFarmEvaluationService(
        model_client=_model_client(),
        tools=tools,
        store=store,
        source=_source(),
        idempotency_key="agentfarm-input-v1",
        max_calls=4,
        society=ProviderFailureSociety(),
        telemetry=lambda: EvaluationTelemetry(
            model_identifier="gpt-provider-failure",
            prompt_version="agentfarm-evaluation-v1",
            call_count=1,
            token_total=0,
            cost_total=None,
        ),
    )

    manifest = await service.run("eval-provider-failure")

    assert manifest.status is EvaluationStatus.FAILED
    assert manifest.call_count == 1
    assert manifest.cost_total is None
    assert (tmp_path / "eval-provider-failure" / "run-manifest.json").is_file()
    assert (tmp_path / "eval-provider-failure" / "evaluation.md").is_file()
    assert store.lifecycle_events("eval-provider-failure")[-1].recovery_state == "terminal"
