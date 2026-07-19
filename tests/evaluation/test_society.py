import json
import hashlib
from pathlib import Path

import pytest
from autogen_agentchat.agents import SocietyOfMindAgent
from autogen_core import FunctionCall
from autogen_core.models import CreateResult, ModelFamily, ModelInfo, RequestUsage
from autogen_core.tools import FunctionTool
from autogen_ext.models.replay import ReplayChatCompletionClient

from agenten.evaluation.models import (
    AcceptanceTestPlan,
    ComponentInventoryCandidate,
    ComponentPlanCandidate,
    EvaluationSource,
    QaReview,
    SourceBlock,
)
from agenten.evaluation.society import (
    _qa_review_tool,
    _stage_inventory_tool,
    _stage_plan_tool,
    build_evaluation_society,
    build_qa_review_team,
)
from agenten.evaluation.store import JsonEvaluationStore
from agenten.evaluation.tools import EvaluationToolService


def _function_calling_replay_client() -> ReplayChatCompletionClient:
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


class PromptAwareReplayClient(ReplayChatCompletionClient):
    """Replay the termination instruction each role actually receives."""

    def __init__(self) -> None:
        super().__init__(
            ["unused", "unused", "unused"],
            model_info=ModelInfo(
                vision=False,
                function_calling=True,
                json_output=True,
                family=ModelFamily.UNKNOWN,
                structured_output=True,
            ),
        )

    async def create(self, messages: object, **kwargs: object):  # type: ignore[no-untyped-def, override]
        system_text = str(getattr(messages[0], "content", ""))  # type: ignore[index]
        role = next(
            name
            for marker, name in (
                ("Source Analyst", "analyst"),
                ("Component Planner", "planner"),
                ("QA Reviewer", "qa"),
            )
            if marker in system_text
        )
        response = f"{role} complete"
        if role == "analyst":
            response += " EVALUATION_SLICE_COMPLETE"
        self.chat_completions[self._current_index] = response
        return await super().create(messages, **kwargs)  # type: ignore[arg-type]


def _tool_names(value: object) -> set[str]:
    names: set[str] = set()
    if isinstance(value, dict):
        if value.get("provider") == "autogen_core.tools.FunctionTool":
            config = value.get("config")
            if isinstance(config, dict) and isinstance(config.get("name"), str):
                names.add(config["name"])
        for child in value.values():
            names.update(_tool_names(child))
    elif isinstance(value, list):
        for child in value:
            names.update(_tool_names(child))
    return names


def test_build_society_uses_bounded_autogen_075_team_and_receipt_tools_only(tmp_path: Path) -> None:
    service = EvaluationToolService(JsonEvaluationStore(tmp_path))

    society = build_evaluation_society(
        model_client=_function_calling_replay_client(),
        tools=service,
        max_rounds=3,
    )

    assert isinstance(society, SocietyOfMindAgent)
    component = society.dump_component().model_dump(mode="json")
    serialized = json.dumps(component, sort_keys=True)
    assert "RoundRobinGroupChat" in serialized
    assert '"max_turns": 10' in serialized
    assert {"source_analyst", "component_planner", "qa_reviewer"} <= set(serialized.split('"'))
    assert _tool_names(component) == {
        "read_source_block",
        "stage_component_inventory",
        "stage_component_plan",
        "record_qa_review",
    }
    assert not hasattr(service, "finalize")


def test_inventory_tool_exposes_complete_candidate_schema_to_live_model(tmp_path: Path) -> None:
    service = EvaluationToolService(JsonEvaluationStore(tmp_path))
    tool = FunctionTool(
        _stage_inventory_tool(service),
        description="stage one inventory",
    )

    component_schema = tool.schema["parameters"]["properties"]["components"]["items"]
    required = set(component_schema["required"])

    assert {
        "component_key",
        "scope",
        "non_goals",
        "team_roles",
        "implementation_steps",
        "interfaces",
        "acceptance_tests",
        "definition_of_done",
        "risks",
        "dependencies",
        "source_citations",
    } <= required
    test_schema = component_schema["properties"]["acceptance_tests"]["items"]
    assert set(test_schema["required"]) == {
        "test_id",
        "test_type",
        "setup",
        "action",
        "expected",
        "command",
    }


def test_planner_and_qa_tools_expose_strict_json_friendly_schemas(tmp_path: Path) -> None:
    service = EvaluationToolService(JsonEvaluationStore(tmp_path))
    planner = FunctionTool(_stage_plan_tool(service), description="stage plan")
    qa = FunctionTool(_qa_review_tool(service), description="record review")

    candidate_schema = planner.schema["parameters"]["properties"]["candidate"]
    assert {
        "component_key",
        "scope",
        "non_goals",
        "implementation_steps",
        "acceptance_tests",
        "source_citations",
    } <= set(candidate_schema["required"])
    qa_schema = qa.schema["parameters"]["properties"]["review"]
    assert set(qa_schema["required"]) == {
        "component_key",
        "revision",
        "decision",
        "score",
        "defect_codes",
        "revision_requests",
    }
    assert qa_schema["properties"]["decision"]["enum"] == [
        "approved",
        "revision_required",
    ]


@pytest.mark.asyncio
async def test_live_shaped_society_reads_block_then_stages_inventory_plan_and_review_in_four_calls(
    tmp_path: Path,
) -> None:
    text = "# CRM\nBuild a deterministic CRM boundary."
    source = EvaluationSource(
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
    candidate = ComponentPlanCandidate(
        component_key="crm",
        scope=("Own CRM.",),
        non_goals=("No external CRM.",),
        team_roles=("Builder",),
        implementation_steps=("Build adapter.",),
        interfaces=("CrmAdapter",),
        acceptance_tests=(
            AcceptanceTestPlan(
                test_id="crm-unit",
                test_type="unit",
                setup="Create adapter.",
                action="Sync contact.",
                expected="Return receipt.",
                command="python -m pytest -q tests/crm",
            ),
        ),
        definition_of_done=("Tests pass.",),
        risks=("Schema drift.",),
        dependencies=(),
        source_citations=("block-0001",),
    )
    review = QaReview(
        component_key="crm",
        revision=1,
        decision="approved",
        score=7,
        defect_codes=(),
        revision_requests=(),
    )
    calls = [
        FunctionCall(
            id="read-001",
            name="read_source_block",
            arguments=json.dumps({"run_id": "eval-live-shape", "block_id": "block-0001"}),
        ),
        FunctionCall(
            id="inventory-001",
            name="stage_component_inventory",
            arguments=json.dumps(
                {
                    "run_id": "eval-live-shape",
                    "inventory_id": "inventory-001",
                    "source_citations": ["block-0001"],
                        "components": [
                            candidate.model_dump(mode="json", exclude={"qa_reviews"})
                        ],
                }
            ),
        ),
        FunctionCall(
            id="candidate-001",
            name="stage_component_plan",
            arguments=json.dumps(
                {
                    "run_id": "eval-live-shape",
                    "candidate": candidate.model_dump(mode="json", exclude={"qa_reviews"}),
                }
            ),
        ),
        FunctionCall(
            id="review-001",
            name="record_qa_review",
            arguments=json.dumps(
                {"run_id": "eval-live-shape", "review": review.model_dump(mode="json")}
            ),
        ),
    ]
    real_client = ReplayChatCompletionClient(
        [
            CreateResult(
                finish_reason="function_calls",
                content=[call],
                usage=RequestUsage(prompt_tokens=1, completion_tokens=1),
                cached=False,
            )
            for call in calls
        ],
        model_info=ModelInfo(
            vision=False,
            function_calling=True,
            json_output=True,
            family=ModelFamily.UNKNOWN,
            structured_output=True,
        ),
    )
    summary_client = ReplayChatCompletionClient(
        ["Non-authoritative planning slice summary."],
        model_info=ModelInfo(
            vision=False,
            function_calling=False,
            json_output=False,
            family=ModelFamily.UNKNOWN,
            structured_output=False,
        ),
    )
    store = JsonEvaluationStore(tmp_path)
    await store.create_run(
        source,
        run_id="eval-live-shape",
        idempotency_key="input-v1",
        max_components=1,
        max_rounds=1,
        max_calls=4,
    )
    society = build_evaluation_society(
        model_client=real_client,
        summary_model_client=summary_client,
        tools=EvaluationToolService(store),
        max_rounds=1,
    )

    assert society._model_client is summary_client  # type: ignore[attr-defined]
    result = await society._team.run(  # type: ignore[attr-defined]
        task=(
            "INVENTORY_SLICE run_id=eval-live-shape "
            "source_blocks=block-0001:CRM max_components=1"
        )
    )

    run_dir = tmp_path / "eval-live-shape"
    trace = [
        (message.source, type(message).__name__, str(message.content)[:300])
        for message in result.messages
    ]
    execution_results = [
        item
        for message in result.messages
        if type(message).__name__ == "ToolCallExecutionEvent"
        for item in message.content
    ]
    assert all(not item.is_error for item in execution_results), execution_results
    assert real_client._current_index == 4
    assert (run_dir / "component-inventory.json").is_file()
    assert (run_dir / "candidates" / "crm" / "revision-1.json").is_file(), trace
    assert (run_dir / "qa-reviews" / "crm" / "revision-1.json").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task",
    (
        "INVENTORY_SLICE run_id=eval-001 source_blocks=block-0001",
        "COMPONENT_SLICE run_id=eval-001 component_key=crm revision=1",
    ),
)
async def test_real_round_robin_reaches_qa_before_termination(tmp_path: Path, task: str) -> None:
    society = build_evaluation_society(
        model_client=PromptAwareReplayClient(),
        tools=EvaluationToolService(JsonEvaluationStore(tmp_path)),
    )

    result = await society._team.run(task=task)  # type: ignore[attr-defined]

    assert [message.source for message in result.messages[-3:]] == [
        "source_analyst",
        "component_planner",
        "qa_reviewer",
    ]
    assert result.stop_reason is not None and "qa_reviewer" in result.stop_reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "score", "defect_codes", "revision_requests"),
    (
        ("approved", 7, (), ()),
        (
            "revision_required",
            5,
            ("weak_test_oracle",),
            ("Make the expected result independently observable.",),
        ),
    ),
)
async def test_real_qa_resume_team_executes_only_typed_qa_review_tool(
    tmp_path: Path,
    decision: str,
    score: int,
    defect_codes: tuple[str, ...],
    revision_requests: tuple[str, ...],
) -> None:
    text = "# CRM\nBuild CRM."
    source = EvaluationSource(
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
    candidate = ComponentPlanCandidate(
        component_key="crm",
        scope=("Own CRM.",),
        non_goals=("No external CRM.",),
        team_roles=("Builder",),
        implementation_steps=("Build adapter.",),
        interfaces=("CrmAdapter",),
        acceptance_tests=(
            AcceptanceTestPlan(
                test_id="crm-unit",
                test_type="unit",
                setup="Create adapter.",
                action="Sync contact.",
                expected="Return receipt.",
                command="python -m pytest -q tests/crm",
            ),
        ),
        definition_of_done=("Tests pass.",),
        risks=("Schema drift.",),
        dependencies=(),
        source_citations=("block-0001",),
    )
    store = JsonEvaluationStore(tmp_path)
    run = await store.create_run(source, run_id="eval-001", idempotency_key="input-v1")
    tools = EvaluationToolService(store)
    await tools.stage_component_inventory(
        run.run_id,
        ComponentInventoryCandidate(
            inventory_id="inventory-001",
            source=source,
            source_citations=("block-0001",),
            components=(candidate,),
        ),
    )
    await tools.stage_component_plan(run.run_id, candidate)
    review = QaReview(
        component_key="crm",
        revision=1,
        decision=decision,
        score=score,
        defect_codes=defect_codes,
        revision_requests=revision_requests,
    )
    call = FunctionCall(
        id="qa-call-001",
        name="record_qa_review",
        arguments=json.dumps({"run_id": run.run_id, "review": review.model_dump(mode="json")}),
    )
    client = ReplayChatCompletionClient(
        [
            CreateResult(
                finish_reason="function_calls",
                content=[call],
                usage=RequestUsage(prompt_tokens=1, completion_tokens=1),
                cached=False,
            ),
        ],
        model_info=ModelInfo(
            vision=False,
            function_calling=True,
            json_output=True,
            family=ModelFamily.UNKNOWN,
            structured_output=True,
        ),
    )
    team = build_qa_review_team(model_client=client, tools=tools)

    result = await team.run(task="QA_SLICE run_id=eval-001 component_key=crm revision=1")

    assert {message.source for message in result.messages} <= {"user", "qa_reviewer"}
    review_path = tmp_path / "eval-001" / "qa-reviews" / "crm" / "revision-1.json"
    assert review_path.is_file(), result.messages
    persisted = QaReview.model_validate_json(review_path.read_bytes())
    assert persisted.decision == decision
    assert persisted.defect_codes == defect_codes
    assert persisted.revision_requests == revision_requests


@pytest.mark.parametrize("max_rounds", (0, 4))
def test_build_society_rejects_round_limits_outside_captain_ceiling(tmp_path: Path, max_rounds: int) -> None:
    with pytest.raises(ValueError, match="one and three"):
        build_evaluation_society(
            model_client=_function_calling_replay_client(),
            tools=EvaluationToolService(JsonEvaluationStore(tmp_path)),
            max_rounds=max_rounds,
        )
