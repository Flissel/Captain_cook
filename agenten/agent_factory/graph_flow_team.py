"""Build an AutoGen GraphFlow from a declarative factory graph manifest."""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from typing import Literal

from autogen_agentchat.base import ChatAgent, TerminationCondition
from autogen_agentchat.messages import BaseChatMessage
from autogen_agentchat.teams import DiGraphBuilder, GraphFlow

from agenten.agent_factory.candidate_evaluation import FactoryGraphSpecV1

_TOKENS: dict[str, Literal["APPROVE", "ITERATE"]] = {
    "approve": "APPROVE",
    "iterate": "ITERATE",
}


def verdict_condition(token: Literal["APPROVE", "ITERATE"]) -> Callable[[BaseChatMessage], bool]:
    """Match only when the first line of the message is exactly the verdict token."""

    def condition(message: BaseChatMessage) -> bool:
        lines = message.to_text().strip().splitlines()
        return bool(lines) and lines[0].strip().strip("*`_.: ").upper() == token

    return condition


def build_graph_flow(
    *,
    graph: FactoryGraphSpecV1,
    participants: Sequence[ChatAgent],
    termination: TerminationCondition | None,
    max_turns: int,
) -> GraphFlow:
    by_name = {agent.name: agent for agent in participants}
    builder = DiGraphBuilder()
    for agent in participants:
        builder.add_node(agent)
    incoming = Counter(edge.target for edge in graph.edges)
    for edge in graph.edges:
        condition = None if edge.when == "always" else verdict_condition(_TOKENS[edge.when])
        if incoming[edge.target] > 1:
            builder.add_edge(
                by_name[edge.source], by_name[edge.target], condition=condition,
                activation_group=f"{edge.source}->{edge.target}", activation_condition="any",
            )
        else:
            builder.add_edge(by_name[edge.source], by_name[edge.target], condition=condition)
    builder.set_entry_point(by_name[graph.entry])
    return GraphFlow(
        participants=builder.get_participants(),
        graph=builder.build(),
        termination_condition=termination,
        max_turns=max_turns,
    )
