"""Integration tests for `agenten.llm.decompose.make_llm_decompose`.

Drives a real `DecomposerAgent` (agenten/decomposition/decomposer.py) end
to end, using `build_replay_model_client` (agenten/llm/model_client.py) so
no network access or API key is required -- the replay client just plays
back canned structured-output JSON, exercising the actual
`autogen_agentchat`/`autogen_ext` structured-output code path rather than
mocking it away.
"""
import pytest

from agenten.decomposition.budget import DecompositionBudget
from agenten.decomposition.decomposer import DecomposerAgent
from agenten.events.schemas import ProblemSubmitted, SubproblemProposed, make_meta, topic_for
from agenten.llm.decompose import DecomposeResponse, SubproblemCandidate, make_llm_decompose
from agenten.llm.model_client import build_replay_model_client
from agenten.runtime.event_bus import InMemoryEventBus

KNOWN_TAGS = ["agri", "logistics", "manufacturing"]


def make_problem_submitted(problem_id="p1", description="Solve world hunger", budget=None):
    meta = make_meta(correlation_id=problem_id, root_problem_id=problem_id)
    return ProblemSubmitted(meta=meta, problem_id=problem_id, description=description, budget=budget)


def collect_proposed(bus: InMemoryEventBus):
    received = []

    async def handler(event):
        received.append(event)

    bus.subscribe(topic_for(SubproblemProposed), handler)
    return received


@pytest.mark.asyncio
async def test_normal_decomposition_produces_valid_subproblems():
    """A realistic multi-subproblem response drives DecomposerAgent to
    publish matching SubproblemProposed events, with valid capability tags
    drawn from the injected known_capability_tags set."""
    response = DecomposeResponse(
        subproblems=[
            SubproblemCandidate(description="Increase crop yields", capability_tags=["agri"], atomic=False),
            SubproblemCandidate(
                description="Build distribution network", capability_tags=["logistics"], atomic=False
            ),
            SubproblemCandidate(
                description="Manufacture storage silos", capability_tags=["manufacturing"], atomic=True
            ),
        ]
    ).model_dump_json()

    model_client = build_replay_model_client([response])
    llm_decompose = make_llm_decompose(model_client, known_capability_tags=KNOWN_TAGS)

    bus = InMemoryEventBus()
    received = collect_proposed(bus)
    budget = DecompositionBudget(max_depth=4, max_fanout_per_node=6)
    agent = DecomposerAgent(bus=bus, budget=budget, llm_decompose=llm_decompose)

    event = make_problem_submitted(description="Solve world hunger across the region")
    await agent.handle_problem_submitted(event)

    assert len(received) == 3
    descriptions = {e.description for e in received}
    assert descriptions == {
        "Increase crop yields",
        "Build distribution network",
        "Manufacture storage silos",
    }
    for child in received:
        assert child.depth == 1
        assert set(child.capability_tags).issubset(set(KNOWN_TAGS))
    atomic_by_desc = {e.description: e.atomic for e in received}
    assert atomic_by_desc["Manufacture storage silos"] is True
    assert atomic_by_desc["Increase crop yields"] is False


@pytest.mark.asyncio
async def test_atomic_leaf_case():
    """A response marking its single candidate atomic=True must come
    through DecomposerAgent as a leaf, unaffected by the progress
    invariant even though it restates/extends the parent description."""
    response = DecomposeResponse(
        subproblems=[
            SubproblemCandidate(
                description="Ship the final crate to the warehouse dock",
                capability_tags=["logistics"],
                atomic=True,
            )
        ]
    ).model_dump_json()

    model_client = build_replay_model_client([response])
    llm_decompose = make_llm_decompose(model_client, known_capability_tags=KNOWN_TAGS)

    bus = InMemoryEventBus()
    received = collect_proposed(bus)
    budget = DecompositionBudget(max_depth=4, max_fanout_per_node=6)
    agent = DecomposerAgent(bus=bus, budget=budget, llm_decompose=llm_decompose)

    event = make_problem_submitted(description="Ship crate")
    await agent.handle_problem_submitted(event)

    assert len(received) == 1
    assert received[0].atomic is True
    assert received[0].capability_tags == ["logistics"]


@pytest.mark.asyncio
async def test_malformed_response_raises_instead_of_silently_returning_empty():
    """A non-JSON / schema-invalid LLM response must propagate as an
    exception through the full DecomposerAgent call -- not be swallowed
    into an empty candidate list."""
    model_client = build_replay_model_client(["this is not valid JSON at all"])
    llm_decompose = make_llm_decompose(model_client, known_capability_tags=KNOWN_TAGS)

    bus = InMemoryEventBus()
    collect_proposed(bus)
    budget = DecompositionBudget(max_depth=4, max_fanout_per_node=6)
    agent = DecomposerAgent(bus=bus, budget=budget, llm_decompose=llm_decompose)

    event = make_problem_submitted(description="Solve world hunger")

    with pytest.raises(Exception):
        await agent.handle_problem_submitted(event)


@pytest.mark.asyncio
async def test_known_capability_tags_are_caller_injected_not_hardcoded():
    """Two different callers with two different tag vocabularies must both
    work -- make_llm_decompose must not hardcode a fixed tag set."""
    custom_tags = ["welding", "painting"]
    response = DecomposeResponse(
        subproblems=[
            SubproblemCandidate(description="Weld the frame", capability_tags=["welding"], atomic=True),
        ]
    ).model_dump_json()

    model_client = build_replay_model_client([response])
    llm_decompose = make_llm_decompose(model_client, known_capability_tags=custom_tags)

    bus = InMemoryEventBus()
    received = collect_proposed(bus)
    budget = DecompositionBudget(max_depth=4, max_fanout_per_node=6)
    agent = DecomposerAgent(bus=bus, budget=budget, llm_decompose=llm_decompose)

    event = make_problem_submitted(description="Build a car frame")
    await agent.handle_problem_submitted(event)

    assert len(received) == 1
    assert received[0].capability_tags == ["welding"]
