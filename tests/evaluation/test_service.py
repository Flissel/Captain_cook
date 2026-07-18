from __future__ import annotations

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


def _field(task: str, name: str) -> str:
    match = re.search(rf"(?:^|\s){name}=([^\s]+)", task)
    assert match is not None
    return match.group(1)


def _service(
    tmp_path: Path,
    decisions: tuple[str, ...],
    *,
    max_calls: int = 10,
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
        max_rounds=3,
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
        society=society,
    )

    manifest = await service.run(run.run_id)

    assert manifest.status is EvaluationStatus.ACCEPTED
    assert society.tasks == ["QA_SLICE run_id=eval-001 component_key=crm revision=1"]
    assert not (tmp_path / "eval-001" / "candidates" / "crm" / "revision-2.json").exists()
