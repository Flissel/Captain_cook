"""Unit tests for agenten.decomposition.decomposer.DecomposerAgent.

Uses InMemoryEventBus (from U0) plus a fake llm_decompose coroutine — no
AutoGen import is exercised anywhere in this test module.
"""
import logging

import pytest

from agenten.decomposition.budget import DecompositionBudget
from agenten.decomposition.decomposer import DecomposerAgent
from agenten.events.schemas import (
    EscalateToRedecompose,
    ProblemSubmitted,
    SubproblemProposed,
    make_meta,
    topic_for,
)
from agenten.runtime.event_bus import InMemoryEventBus


def make_problem_submitted(problem_id="p1", description="Solve world hunger", budget=None):
    meta = make_meta(correlation_id=problem_id, root_problem_id=problem_id)
    return ProblemSubmitted(
        meta=meta,
        problem_id=problem_id,
        description=description,
        budget=budget,
    )


def collect_proposed(bus: InMemoryEventBus):
    received = []

    async def handler(event):
        received.append(event)

    bus.subscribe(topic_for(SubproblemProposed), handler)
    return received


@pytest.mark.asyncio
async def test_normal_decomposition_publishes_children():
    bus = InMemoryEventBus()
    received = collect_proposed(bus)
    budget = DecompositionBudget(max_depth=4, max_fanout_per_node=6)

    async def llm_decompose(description, depth):
        return [
            {"description": "grow more food", "capability_tags": ["agri"], "atomic": False},
            {"description": "distribute food", "capability_tags": ["logistics"], "atomic": True},
        ]

    agent = DecomposerAgent(bus=bus, budget=budget, llm_decompose=llm_decompose)
    event = make_problem_submitted(description="Solve world hunger problem")

    await agent.handle_problem_submitted(event)

    assert len(received) == 2
    descriptions = {e.description for e in received}
    assert descriptions == {"grow more food", "distribute food"}
    for child in received:
        # Children are one level deeper than the problem they came from.
        assert child.depth == 1
        assert child.parent_id is None
        assert child.meta.correlation_id == child.subproblem_id
        assert child.meta.root_problem_id == event.meta.root_problem_id
        assert child.meta.attempt == 0
    # subproblem_ids are unique
    assert len({e.subproblem_id for e in received}) == 2
    atomic_by_desc = {e.description: e.atomic for e in received}
    assert atomic_by_desc["grow more food"] is False
    assert atomic_by_desc["distribute food"] is True


@pytest.mark.asyncio
async def test_depth_cap_forces_atomic():
    bus = InMemoryEventBus()
    received = collect_proposed(bus)
    # max_depth=0 means the root problem (depth=0) is already at the cap.
    budget = DecompositionBudget(max_depth=0, max_fanout_per_node=6)

    async def llm_decompose(description, depth):
        # LLM insists these are NOT atomic and are not shorter than the
        # parent description either -- but the depth cap should still
        # force atomic=True and thus bypass the progress-invariant drop.
        return [
            {
                "description": description + " (a much longer restated version)",
                "capability_tags": [],
                "atomic": False,
            }
        ]

    agent = DecomposerAgent(bus=bus, budget=budget, llm_decompose=llm_decompose)
    event = make_problem_submitted(description="short")

    await agent.handle_problem_submitted(event)

    assert len(received) == 1
    assert received[0].atomic is True
    assert received[0].depth == 1


@pytest.mark.asyncio
async def test_fanout_cap_truncates_and_logs(caplog):
    bus = InMemoryEventBus()
    received = collect_proposed(bus)
    budget = DecompositionBudget(max_depth=4, max_fanout_per_node=2)

    async def llm_decompose(description, depth):
        return [
            {"description": f"child {i}", "capability_tags": [], "atomic": True}
            for i in range(5)
        ]

    agent = DecomposerAgent(bus=bus, budget=budget, llm_decompose=llm_decompose)
    event = make_problem_submitted(description="Solve world hunger problem")

    with caplog.at_level(logging.WARNING, logger="agenten.decomposition.decomposer"):
        await agent.handle_problem_submitted(event)

    assert len(received) == 2
    assert any("truncat" in rec.message.lower() for rec in caplog.records)


@pytest.mark.asyncio
async def test_progress_invariant_drops_non_shrinking_non_atomic_child(caplog):
    bus = InMemoryEventBus()
    received = collect_proposed(bus)
    budget = DecompositionBudget(max_depth=4, max_fanout_per_node=6)

    parent_description = "short problem"

    async def llm_decompose(description, depth):
        return [
            # Not atomic, and longer than the parent -- must be dropped.
            {
                "description": description + " with extra restated words that make it longer",
                "capability_tags": [],
                "atomic": False,
            },
            # A legitimately shorter, non-atomic child -- must be kept.
            {"description": "short", "capability_tags": [], "atomic": False},
        ]

    agent = DecomposerAgent(bus=bus, budget=budget, llm_decompose=llm_decompose)
    event = make_problem_submitted(description=parent_description)

    with caplog.at_level(logging.WARNING, logger="agenten.decomposition.decomposer"):
        await agent.handle_problem_submitted(event)

    assert len(received) == 1
    assert received[0].description == "short"
    assert any("dropping" in rec.message.lower() for rec in caplog.records)


@pytest.mark.asyncio
async def test_progress_invariant_keeps_atomic_child_even_if_longer():
    bus = InMemoryEventBus()
    received = collect_proposed(bus)
    budget = DecompositionBudget(max_depth=4, max_fanout_per_node=6)

    async def llm_decompose(description, depth):
        return [
            {
                "description": description + " restated as an atomic leaf with more detail",
                "capability_tags": [],
                "atomic": True,
            }
        ]

    agent = DecomposerAgent(bus=bus, budget=budget, llm_decompose=llm_decompose)
    event = make_problem_submitted(description="short")

    await agent.handle_problem_submitted(event)

    assert len(received) == 1
    assert received[0].atomic is True


@pytest.mark.asyncio
async def test_uses_event_budget_override_when_present():
    bus = InMemoryEventBus()
    received = collect_proposed(bus)
    default_budget = DecompositionBudget(max_depth=4, max_fanout_per_node=6)
    override_budget = DecompositionBudget(max_depth=0, max_fanout_per_node=6)

    async def llm_decompose(description, depth):
        return [{"description": "x", "capability_tags": [], "atomic": False}]

    agent = DecomposerAgent(bus=bus, budget=default_budget, llm_decompose=llm_decompose)
    event = make_problem_submitted(description="Solve world hunger problem", budget=override_budget)

    await agent.handle_problem_submitted(event)

    # override budget's max_depth=0 should force atomic despite the
    # constructor-level default_budget allowing depth 4.
    assert received[0].atomic is True


@pytest.mark.asyncio
async def test_escalate_to_redecompose_uses_describe_subproblem_lookup():
    bus = InMemoryEventBus()
    received = collect_proposed(bus)
    budget = DecompositionBudget(max_depth=4, max_fanout_per_node=6)

    async def llm_decompose(description, depth):
        assert description == "failed subproblem description"
        assert depth == 2
        return [{"description": "retry piece", "capability_tags": [], "atomic": True}]

    async def describe_subproblem(subproblem_id):
        assert subproblem_id == "sp-123"
        return "failed subproblem description", 2

    agent = DecomposerAgent(
        bus=bus,
        budget=budget,
        llm_decompose=llm_decompose,
        describe_subproblem=describe_subproblem,
    )
    meta = make_meta(correlation_id="sp-123", root_problem_id="root-1")
    event = EscalateToRedecompose(meta=meta, subproblem_id="sp-123", reason="too many failures")

    await agent.handle_escalate_to_redecompose(event)

    assert len(received) == 1
    child = received[0]
    assert child.parent_id == "sp-123"
    assert child.depth == 3
    assert child.meta.root_problem_id == "root-1"


@pytest.mark.asyncio
async def test_escalate_to_redecompose_without_lookup_raises():
    bus = InMemoryEventBus()
    budget = DecompositionBudget()

    async def llm_decompose(description, depth):
        return []

    agent = DecomposerAgent(bus=bus, budget=budget, llm_decompose=llm_decompose)
    meta = make_meta(correlation_id="sp-1", root_problem_id="root-1")
    event = EscalateToRedecompose(meta=meta, subproblem_id="sp-1")

    with pytest.raises(RuntimeError):
        await agent.handle_escalate_to_redecompose(event)


@pytest.mark.asyncio
async def test_capability_tags_none_does_not_crash():
    bus = InMemoryEventBus()
    received = collect_proposed(bus)
    budget = DecompositionBudget()

    async def llm_decompose(description, depth):
        return [{"description": "x", "capability_tags": None, "atomic": True}]

    agent = DecomposerAgent(bus=bus, budget=budget, llm_decompose=llm_decompose)
    event = make_problem_submitted(description="Solve world hunger problem")

    await agent.handle_problem_submitted(event)

    assert received[0].capability_tags == []
