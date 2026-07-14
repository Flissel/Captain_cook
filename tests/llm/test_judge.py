"""Integration tests for `agenten.llm.judge.make_llm_judge`.

Drives a real `ConstitutionGatekeeper` (agenten/constitution/gatekeeper.py)
end to end, using `build_replay_model_client` (agenten/llm/model_client.py)
so no network access or API key is required -- the replay client just plays
back canned structured-output JSON, exercising the actual
`autogen_core.models` structured-output call path rather than mocking it
away.
"""
from typing import Dict, List, Optional

import pytest

from agenten.constitution.gatekeeper import ConstitutionGatekeeper
from agenten.constitution.ruleset import ConstitutionRuleset
from agenten.decomposition.budget import DecompositionBudget
from agenten.events.schemas import (
    SubproblemAccepted,
    SubproblemProposed,
    SubproblemRejected,
    make_meta,
    topic_for,
)
from agenten.ledger_bridge.stage_machine import LedgerQuery, Stage
from agenten.llm.judge import JudgeVerdict, make_llm_judge
from agenten.llm.model_client import build_replay_model_client
from agenten.runtime.event_bus import InMemoryEventBus


class FakeBlock:
    def __init__(self, index: int, data: Dict, metadata: Optional[Dict] = None):
        self.index = index
        self.data = data
        self.metadata = metadata or {}


class FakeLedgerQuery(LedgerQuery):
    def __init__(self):
        self._blocks_by_stage: Dict[Stage, List[FakeBlock]] = {stage: [] for stage in Stage}

    def count_in_stage(self, stage: Stage) -> int:
        return len(self._blocks_by_stage[stage])

    def blocks_in_stage(self, stage: Stage) -> List[FakeBlock]:
        return list(self._blocks_by_stage[stage])

    def get_block(self, index: int) -> Optional[FakeBlock]:
        for blocks in self._blocks_by_stage.values():
            for block in blocks:
                if block.index == index:
                    return block
        return None


def make_ruleset(**overrides) -> ConstitutionRuleset:
    fields = dict(
        version="test-v1",
        scope_statement="Only accept subproblems about baking bread.",
        quality_rubric="Must be specific and verifiable.",
        prohibited_topics=["weapons"],
        default_budget=DecompositionBudget(),
    )
    fields.update(overrides)
    return ConstitutionRuleset(**fields)


def make_proposed(
    subproblem_id: str = "sp-1",
    description: str = "Preheat the oven to 220C for the bread.",
    capability_tags: Optional[List[str]] = None,
    root_problem_id: str = "root-1",
) -> SubproblemProposed:
    return SubproblemProposed(
        meta=make_meta(correlation_id=subproblem_id, root_problem_id=root_problem_id),
        subproblem_id=subproblem_id,
        parent_id=None,
        depth=1,
        description=description,
        capability_tags=capability_tags if capability_tags is not None else ["baking"],
        atomic=True,
    )


class Recorder:
    def __init__(self):
        self.events = []

    async def __call__(self, event) -> None:
        self.events.append(event)


def wire_bus():
    bus = InMemoryEventBus()
    accepted = Recorder()
    rejected = Recorder()
    bus.subscribe(topic_for(SubproblemAccepted), accepted)
    bus.subscribe(topic_for(SubproblemRejected), rejected)
    return bus, accepted, rejected


@pytest.mark.asyncio
async def test_judge_accepts_when_response_says_accept():
    response = JudgeVerdict(accept=True, reason="in scope and meets the rubric").model_dump_json()
    model_client = build_replay_model_client([response])
    llm_judge = make_llm_judge(model_client)

    bus, accepted, rejected = wire_bus()
    gatekeeper = ConstitutionGatekeeper(
        bus=bus, ruleset=make_ruleset(), ledger_query=FakeLedgerQuery(), llm_judge=llm_judge
    )

    await gatekeeper.handle_subproblem_proposed(make_proposed())

    assert len(accepted.events) == 1
    assert len(rejected.events) == 0


@pytest.mark.asyncio
async def test_judge_rejects_when_response_says_reject():
    response = JudgeVerdict(accept=False, reason="out of scope").model_dump_json()
    model_client = build_replay_model_client([response])
    llm_judge = make_llm_judge(model_client)

    bus, accepted, rejected = wire_bus()
    gatekeeper = ConstitutionGatekeeper(
        bus=bus, ruleset=make_ruleset(), ledger_query=FakeLedgerQuery(), llm_judge=llm_judge
    )

    await gatekeeper.handle_subproblem_proposed(make_proposed())

    assert len(accepted.events) == 0
    assert len(rejected.events) == 1
    assert rejected.events[0].reason == "quality_bar"


@pytest.mark.asyncio
async def test_malformed_judge_response_is_rejected_conservatively_by_gatekeeper():
    """A non-JSON LLM response makes make_llm_judge's callable raise;
    ConstitutionGatekeeper's own exception handling (not duplicated here)
    must catch that and reject conservatively -- proving the two layers
    compose correctly rather than crashing the whole pipeline."""
    model_client = build_replay_model_client(["not valid json"])
    llm_judge = make_llm_judge(model_client)

    bus, accepted, rejected = wire_bus()
    gatekeeper = ConstitutionGatekeeper(
        bus=bus, ruleset=make_ruleset(), ledger_query=FakeLedgerQuery(), llm_judge=llm_judge
    )

    await gatekeeper.handle_subproblem_proposed(make_proposed())

    assert len(accepted.events) == 0
    assert len(rejected.events) == 1
    assert rejected.events[0].reason == "quality_bar"


@pytest.mark.asyncio
async def test_llm_judge_callable_raises_directly_on_malformed_response():
    """Exercises agenten.llm.judge in isolation (not just through the
    gatekeeper's exception-swallowing wrapper) to prove it genuinely raises
    rather than returning some default truthy/falsy value."""
    model_client = build_replay_model_client(["not valid json"])
    llm_judge = make_llm_judge(model_client)

    with pytest.raises(Exception):
        await llm_judge("Preheat the oven.", make_ruleset())
