import pytest
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.base import TaskResult
from autogen_agentchat.conditions import MaxMessageTermination
from autogen_agentchat.messages import TextMessage
from pydantic import ValidationError

from agenten.agent_factory.candidate_evaluation import FactoryAutoGenTeamManifestV1
from agenten.agent_factory.graph_flow_team import build_graph_flow, verdict_condition
from agenten.agent_factory.team_execution import _session_termination_reason
from agenten.llm.model_client import build_replay_model_client

AGENTS = ("architect", "builder", "tester", "reviewer", "reporter")
REF = {
    "uri": "artifact://sha256/" + "0" * 64,
    "sha256": "0" * 64,
    "media_type": "text/markdown",
}
LOOP_EDGES = [
    {"source": "architect", "target": "builder"},
    {"source": "builder", "target": "tester"},
    {"source": "tester", "target": "reviewer"},
    {"source": "reviewer", "target": "builder", "when": "iterate"},
    {"source": "reviewer", "target": "reporter", "when": "approve"},
]


def _manifest(**overrides):
    value = {
        "schema": "autogen-team.v1",
        "name": "build_loop",
        "conversation_pattern": "graph_flow",
        "graph": {"entry": "architect", "edges": LOOP_EDGES},
        "agents": [{"name": n, "system_prompt_ref": REF} for n in AGENTS],
        "memory_policy": "buffered",
        "max_messages": 30,
        "max_handoffs": 0,
        "max_tool_calls": 10,
        "termination_conditions": ["max_messages"],
        "entrypoint_command": ["python", "team.py"],
    }
    value.update(overrides)
    return FactoryAutoGenTeamManifestV1.model_validate(value, context={"allowed_tools": ()})


def test_graph_flow_manifest_validates():
    manifest = _manifest()
    assert manifest.conversation_pattern == "graph_flow"
    assert manifest.graph.entry == "architect"


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"graph": None}, "graph_flow requires a graph"),
        ({"conversation_pattern": "round_robin_group_chat"}, "graph is only valid with graph_flow"),
        ({"graph": {"entry": "ghost", "edges": LOOP_EDGES}}, "unknown graph node"),
        ({"graph": {"entry": "architect", "edges": LOOP_EDGES + [{"source": "builder", "target": "ghost"}]}}, "unknown graph node"),
        ({"graph": {"entry": "architect", "edges": LOOP_EDGES + [LOOP_EDGES[0]]}}, "duplicate graph edge"),
        ({"graph": {"entry": "architect", "edges": LOOP_EDGES[:3] + [{"source": "reviewer", "target": "builder", "when": "iterate"}, {"source": "reviewer", "target": "reporter"}]}}, "must not mix"),
        ({"agents": [{"name": n, "system_prompt_ref": REF} for n in AGENTS[:4]],
          "graph": {"entry": "architect", "edges": LOOP_EDGES[:3] + [{"source": "reviewer", "target": "builder"}]}}, "leaf"),
        ({"graph": {"entry": "builder", "edges": LOOP_EDGES}}, "unreachable"),
    ],
)
def test_invalid_graphs_are_rejected(overrides, message):
    with pytest.raises(ValidationError, match=message):
        _manifest(**overrides)


@pytest.mark.parametrize(
    "text,approve,iterate",
    [
        ("APPROVE", True, False),
        ("approve.\nall good", True, False),
        ("**ITERATE**\nfix the import", False, True),
        ("ITERATE: tests fail instead of APPROVE", False, False),
        ("I think we should APPROVE", False, False),
        ("", False, False),
    ],
)
def test_verdict_reads_only_the_first_line(text, approve, iterate):
    message = TextMessage(content=text, source="reviewer")
    assert verdict_condition("APPROVE")(message) is approve
    assert verdict_condition("ITERATE")(message) is iterate


def _agent(name, replies):
    return AssistantAgent(name, model_client=build_replay_model_client(list(replies)))


@pytest.mark.asyncio
async def test_iterate_loops_back_to_builder_then_approve_exits():
    manifest = _manifest()
    agents = [
        _agent("architect", ["plan: build hello"]),
        _agent("builder", ["built v1", "built v2"]),
        _agent("tester", ["2 failed", "all passed"]),
        _agent("reviewer", ["ITERATE\nfix failures", "APPROVE"]),
        _agent("reporter", ["summary"]),
    ]
    team = build_graph_flow(graph=manifest.graph, participants=agents,
                            termination=MaxMessageTermination(30), max_turns=30)

    result = await team.run(task="build hello")

    speakers = [m.source for m in result.messages if m.source != "user"]
    assert speakers == ["architect", "builder", "tester", "reviewer", "builder", "tester", "reviewer", "reporter"]


@pytest.mark.parametrize(
    "extra_edges,message",
    [
        ([{"source": "architect", "target": "architect"}], "cycle without a conditional edge"),
        (
            [
                {"source": "builder", "target": "architect"},
            ],
            "cycle without a conditional edge",
        ),
        ([{"source": "architect", "target": "tester"}], "join"),
    ],
)
def test_always_cycles_and_unconditional_joins_are_rejected(extra_edges, message):
    graph = {"entry": "architect", "edges": LOOP_EDGES + extra_edges}
    with pytest.raises(ValidationError, match=message):
        _manifest(graph=graph)


def test_conditional_loop_back_edge_is_not_a_cycle_or_join():
    manifest = _manifest()
    assert len(manifest.graph.edges) == 5


def _result(sources, stop_reason="Digraph execution is complete"):
    return TaskResult(
        messages=[TextMessage(content="x", source=name) for name in sources],
        stop_reason=stop_reason,
    )


def test_natural_graph_completion_at_a_leaf_is_graph_completed():
    manifest = _manifest()
    reason = _session_termination_reason(
        _result(["user", "architect", "builder", "tester", "reviewer", "reporter"]),
        termination_conditions=manifest.termination_conditions,
        max_messages=manifest.max_messages,
        graph=manifest.graph,
    )
    assert reason == "graph_completed"


def test_natural_graph_completion_away_from_a_leaf_is_graph_stalled():
    manifest = _manifest()
    reason = _session_termination_reason(
        _result(["user", "architect", "builder", "tester", "reviewer"]),
        termination_conditions=manifest.termination_conditions,
        max_messages=manifest.max_messages,
        graph=manifest.graph,
    )
    assert reason == "graph_stalled"


def test_graph_flow_message_ceiling_takes_precedence_over_graph_reasons():
    manifest = _manifest()
    reason = _session_termination_reason(
        _result(["user", "architect", "builder"], stop_reason="Maximum number of messages 3 reached"),
        termination_conditions=manifest.termination_conditions,
        max_messages=3,
        graph=manifest.graph,
    )
    assert reason == "max_messages"


def test_non_graph_patterns_keep_the_declared_termination_error():
    with pytest.raises(ValueError, match="without a declared termination condition"):
        _session_termination_reason(
            _result(["user", "a"]),
            termination_conditions=("max_tool_calls",),
            max_messages=30,
        )
