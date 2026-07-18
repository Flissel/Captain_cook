import json
from pathlib import Path

import pytest
from autogen_agentchat.agents import SocietyOfMindAgent
from autogen_core.models import ModelFamily, ModelInfo
from autogen_ext.models.replay import ReplayChatCompletionClient

from agenten.evaluation.society import build_evaluation_society
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


@pytest.mark.parametrize("max_rounds", (0, 4))
def test_build_society_rejects_round_limits_outside_captain_ceiling(tmp_path: Path, max_rounds: int) -> None:
    with pytest.raises(ValueError, match="one and three"):
        build_evaluation_society(
            model_client=_function_calling_replay_client(),
            tools=EvaluationToolService(JsonEvaluationStore(tmp_path)),
            max_rounds=max_rounds,
        )
