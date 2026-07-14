"""Unit tests for ConstitutionGatekeeper (unit U2).

Uses InMemoryEventBus (real, from unit U0) plus a hand-rolled fake
LedgerQuery and fake llm_judge callables — no autogen_core, no network,
no real ledger required.
"""
import asyncio
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
from agenten.runtime.event_bus import InMemoryEventBus


class FakeBlock:
    """Minimal stand-in for blockchain.Blockchain_modell.Block: only the
    attributes ConstitutionGatekeeper reads (data / metadata)."""

    def __init__(self, index: int, data: Dict, metadata: Optional[Dict] = None):
        self.index = index
        self.data = data
        self.metadata = metadata or {}


class FakeLedgerQuery(LedgerQuery):
    """In-memory fake: blocks are pre-seeded per stage by the test."""

    def __init__(self):
        self._blocks_by_stage: Dict[Stage, List[FakeBlock]] = {stage: [] for stage in Stage}

    def seed(self, stage: Stage, block: FakeBlock) -> None:
        self._blocks_by_stage[stage].append(block)

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
    parent_id: Optional[str] = None,
    description: str = "Preheat the oven to 220C for the bread.",
    capability_tags: Optional[List[str]] = None,
    root_problem_id: str = "root-1",
    depth: int = 1,
) -> SubproblemProposed:
    return SubproblemProposed(
        meta=make_meta(correlation_id=subproblem_id, root_problem_id=root_problem_id),
        subproblem_id=subproblem_id,
        parent_id=parent_id,
        depth=depth,
        description=description,
        capability_tags=capability_tags if capability_tags is not None else ["baking"],
        atomic=True,
    )


class Recorder:
    """Captures every event published to a topic."""

    def __init__(self):
        self.events = []

    async def __call__(self, event) -> None:
        self.events.append(event)


def wire_bus() -> (InMemoryEventBus, Recorder, Recorder):
    bus = InMemoryEventBus()
    accepted_recorder = Recorder()
    rejected_recorder = Recorder()
    bus.subscribe(topic_for(SubproblemAccepted), accepted_recorder)
    bus.subscribe(topic_for(SubproblemRejected), rejected_recorder)
    return bus, accepted_recorder, rejected_recorder


@pytest.mark.asyncio
async def test_accept_path_no_llm_judge():
    bus, accepted, rejected = wire_bus()
    ruleset = make_ruleset()
    ledger_query = FakeLedgerQuery()
    gatekeeper = ConstitutionGatekeeper(bus=bus, ruleset=ruleset, ledger_query=ledger_query)

    event = make_proposed()
    await gatekeeper.handle_subproblem_proposed(event)

    assert len(accepted.events) == 1
    assert len(rejected.events) == 0
    result = accepted.events[0]
    assert result.subproblem_id == "sp-1"
    assert result.block_index is None
    assert result.meta.constitution_version == "test-v1"


@pytest.mark.asyncio
async def test_accept_path_with_passing_llm_judge():
    bus, accepted, rejected = wire_bus()
    ruleset = make_ruleset()
    ledger_query = FakeLedgerQuery()

    async def always_pass(description: str, rs: ConstitutionRuleset) -> bool:
        return True

    gatekeeper = ConstitutionGatekeeper(
        bus=bus, ruleset=ruleset, ledger_query=ledger_query, llm_judge=always_pass
    )

    event = make_proposed()
    await gatekeeper.handle_subproblem_proposed(event)

    assert len(accepted.events) == 1
    assert len(rejected.events) == 0


@pytest.mark.asyncio
async def test_rejects_when_description_empty_malformed():
    bus, accepted, rejected = wire_bus()
    gatekeeper = ConstitutionGatekeeper(bus=bus, ruleset=make_ruleset(), ledger_query=FakeLedgerQuery())

    event = make_proposed(description="   ")
    await gatekeeper.handle_subproblem_proposed(event)

    assert len(accepted.events) == 0
    assert len(rejected.events) == 1
    assert rejected.events[0].reason == "malformed"


@pytest.mark.asyncio
async def test_rejects_when_capability_tags_empty_malformed():
    bus, accepted, rejected = wire_bus()
    gatekeeper = ConstitutionGatekeeper(bus=bus, ruleset=make_ruleset(), ledger_query=FakeLedgerQuery())

    event = make_proposed(capability_tags=[])
    await gatekeeper.handle_subproblem_proposed(event)

    assert len(accepted.events) == 0
    assert len(rejected.events) == 1
    assert rejected.events[0].reason == "malformed"


@pytest.mark.asyncio
async def test_rejects_when_not_minimal_longer_than_parent():
    bus, accepted, rejected = wire_bus()
    ledger_query = FakeLedgerQuery()
    # Seed a parent block somewhere in the ledger with a short description.
    ledger_query.seed(
        Stage.ACCEPTED,
        FakeBlock(index=1, data={"subproblem_id": "parent-1", "description": "Bake bread."}),
    )
    gatekeeper = ConstitutionGatekeeper(bus=bus, ruleset=make_ruleset(), ledger_query=ledger_query)

    event = make_proposed(
        parent_id="parent-1",
        description="Bake bread." + (" and also do many, many more additional things than the parent" * 3),
    )
    await gatekeeper.handle_subproblem_proposed(event)

    assert len(accepted.events) == 0
    assert len(rejected.events) == 1
    assert rejected.events[0].reason == "malformed"


@pytest.mark.asyncio
async def test_rejects_duplicate_pending_subproblem():
    bus, accepted, rejected = wire_bus()
    ledger_query = FakeLedgerQuery()
    ledger_query.seed(
        Stage.VALIDATING,
        FakeBlock(
            index=2,
            data={
                "subproblem_id": "sp-other",
                "root_problem_id": "root-1",
                "description": "Preheat the oven to 220C for the bread.",
            },
        ),
    )
    gatekeeper = ConstitutionGatekeeper(bus=bus, ruleset=make_ruleset(), ledger_query=ledger_query)

    event = make_proposed(
        subproblem_id="sp-1",
        root_problem_id="root-1",
        description="  PREHEAT   the oven to 220C for the bread.  ",
    )
    await gatekeeper.handle_subproblem_proposed(event)

    assert len(accepted.events) == 0
    assert len(rejected.events) == 1
    assert rejected.events[0].reason == "duplicate"


@pytest.mark.asyncio
async def test_duplicate_check_ignores_other_root_problems():
    bus, accepted, rejected = wire_bus()
    ledger_query = FakeLedgerQuery()
    ledger_query.seed(
        Stage.VALIDATING,
        FakeBlock(
            index=2,
            data={
                "subproblem_id": "sp-other",
                "root_problem_id": "root-DIFFERENT",
                "description": "Preheat the oven to 220C for the bread.",
            },
        ),
    )
    gatekeeper = ConstitutionGatekeeper(bus=bus, ruleset=make_ruleset(), ledger_query=ledger_query)

    event = make_proposed(
        subproblem_id="sp-1",
        root_problem_id="root-1",
        description="Preheat the oven to 220C for the bread.",
    )
    await gatekeeper.handle_subproblem_proposed(event)

    assert len(accepted.events) == 1
    assert len(rejected.events) == 0


@pytest.mark.asyncio
async def test_rejects_on_quality_bar_when_llm_judge_returns_false():
    bus, accepted, rejected = wire_bus()

    async def always_fail(description: str, rs: ConstitutionRuleset) -> bool:
        return False

    gatekeeper = ConstitutionGatekeeper(
        bus=bus, ruleset=make_ruleset(), ledger_query=FakeLedgerQuery(), llm_judge=always_fail
    )

    event = make_proposed()
    await gatekeeper.handle_subproblem_proposed(event)

    assert len(accepted.events) == 0
    assert len(rejected.events) == 1
    assert rejected.events[0].reason == "quality_bar"


@pytest.mark.asyncio
async def test_llm_timeout_rejects_conservatively():
    bus, accepted, rejected = wire_bus()

    async def hangs_forever(description: str, rs: ConstitutionRuleset) -> bool:
        await asyncio.sleep(10)
        return True  # pragma: no cover - never reached

    gatekeeper = ConstitutionGatekeeper(
        bus=bus,
        ruleset=make_ruleset(),
        ledger_query=FakeLedgerQuery(),
        llm_judge=hangs_forever,
        llm_timeout_seconds=0.05,
    )

    event = make_proposed()
    await gatekeeper.handle_subproblem_proposed(event)

    assert len(accepted.events) == 0
    assert len(rejected.events) == 1
    assert rejected.events[0].reason == "quality_bar"


@pytest.mark.asyncio
async def test_llm_exception_rejects_conservatively():
    bus, accepted, rejected = wire_bus()

    async def explodes(description: str, rs: ConstitutionRuleset) -> bool:
        raise RuntimeError("judge is on fire")

    gatekeeper = ConstitutionGatekeeper(
        bus=bus, ruleset=make_ruleset(), ledger_query=FakeLedgerQuery(), llm_judge=explodes
    )

    event = make_proposed()
    await gatekeeper.handle_subproblem_proposed(event)

    assert len(accepted.events) == 0
    assert len(rejected.events) == 1
    assert rejected.events[0].reason == "quality_bar"


@pytest.mark.asyncio
async def test_meta_stamps_constitution_version_on_rejection_too():
    bus, accepted, rejected = wire_bus()
    ruleset = make_ruleset(version="my-special-version")
    gatekeeper = ConstitutionGatekeeper(bus=bus, ruleset=ruleset, ledger_query=FakeLedgerQuery())

    event = make_proposed(description="")
    await gatekeeper.handle_subproblem_proposed(event)

    assert rejected.events[0].meta.constitution_version == "my-special-version"
