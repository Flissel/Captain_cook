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


@pytest.mark.asyncio
async def test_duplicate_sibling_in_same_batch_is_rejected():
    """Two identical children proposed before the Recorder drains must not both pass.

    The Recorder only enqueues its VALIDATING write, so the ledger cannot see
    sibling one when sibling two is judged. The Gatekeeper must remember its
    own verdicts to close that window.
    """
    bus, accepted, rejected = wire_bus()
    gatekeeper = ConstitutionGatekeeper(
        bus=bus, ruleset=make_ruleset(), ledger_query=FakeLedgerQuery()
    )

    await gatekeeper.handle_subproblem_proposed(
        make_proposed(subproblem_id="sp-1", description="Knead the dough for ten minutes.")
    )
    await gatekeeper.handle_subproblem_proposed(
        make_proposed(subproblem_id="sp-2", description="Knead the dough for ten minutes.")
    )

    assert [event.subproblem_id for event in accepted.events] == ["sp-1"]
    assert [event.subproblem_id for event in rejected.events] == ["sp-2"]
    assert rejected.events[0].reason == "duplicate"


@pytest.mark.asyncio
async def test_in_flight_memory_ban_is_released_by_eviction_not_the_ledger():
    """The in-process memory must not become a permanent duplicate ban.

    This used to be ledger-driven: seeding the subproblem into a DONE block
    released the ban (see git history / test_prune_does_not_query_the_ledger
    for why that scan is gone). Release is now purely capacity-driven: once
    enough later acceptances evict the oldest entry from the bounded map, an
    identical description is accepted again — even though the ledger here
    never learns about sp-1 at all.
    """
    bus, accepted, rejected = wire_bus()
    gatekeeper = ConstitutionGatekeeper(
        bus=bus, ruleset=make_ruleset(), ledger_query=FakeLedgerQuery()
    )

    await gatekeeper.handle_subproblem_proposed(
        make_proposed(subproblem_id="sp-1", description="Knead the dough for ten minutes.")
    )

    # Fill the bounded map past its limit so sp-1 is evicted as the oldest
    # entry. The ledger is never seeded with anything here — if release
    # were still ledger-driven, sp-1 would remain a permanent ban.
    for index in range(gatekeeper.IN_FLIGHT_MEMORY_LIMIT):
        await gatekeeper.handle_subproblem_proposed(
            make_proposed(
                subproblem_id=f"filler-{index}",
                description=f"Prepare baking step number {index}.",
            )
        )
    assert "sp-1" not in gatekeeper._accepted_in_flight

    await gatekeeper.handle_subproblem_proposed(
        make_proposed(subproblem_id="sp-2", description="Knead the dough for ten minutes.")
    )

    assert accepted.events[-1].subproblem_id == "sp-2"
    assert rejected.events == []


@pytest.mark.asyncio
async def test_identical_descriptions_under_different_roots_are_both_accepted():
    """The in-flight memory must stay scoped to one root problem."""
    bus, accepted, rejected = wire_bus()
    gatekeeper = ConstitutionGatekeeper(
        bus=bus, ruleset=make_ruleset(), ledger_query=FakeLedgerQuery()
    )

    await gatekeeper.handle_subproblem_proposed(
        make_proposed(
            subproblem_id="sp-1",
            root_problem_id="root-1",
            description="Knead the dough for ten minutes.",
        )
    )
    await gatekeeper.handle_subproblem_proposed(
        make_proposed(
            subproblem_id="sp-2",
            root_problem_id="root-2",
            description="Knead the dough for ten minutes.",
        )
    )

    assert [event.subproblem_id for event in accepted.events] == ["sp-1", "sp-2"]
    assert rejected.events == []


@pytest.mark.asyncio
async def test_in_flight_memory_is_bounded_and_evicts_oldest_first():
    """The memory must not grow without bound when the ledger never catches up.

    A Recorder that fails to persist an acceptance would otherwise leave an
    entry in place for the life of the process, permanently banning that
    description under that root.
    """
    bus, accepted, rejected = wire_bus()
    gatekeeper = ConstitutionGatekeeper(
        bus=bus, ruleset=make_ruleset(), ledger_query=FakeLedgerQuery()
    )

    for index in range(gatekeeper.IN_FLIGHT_MEMORY_LIMIT + 1):
        await gatekeeper.handle_subproblem_proposed(
            make_proposed(
                subproblem_id=f"sp-{index}",
                description=f"Prepare baking step number {index}.",
            )
        )

    assert len(gatekeeper._accepted_in_flight) == gatekeeper.IN_FLIGHT_MEMORY_LIMIT
    assert "sp-0" not in gatekeeper._accepted_in_flight
    assert rejected.events == []


@pytest.mark.asyncio
async def test_prune_does_not_query_the_ledger():
    """Pruning must not scan the ledger: it ran on every proposal during a batch."""
    bus, accepted, rejected = wire_bus()

    class CountingLedgerQuery(FakeLedgerQuery):
        def __init__(self):
            super().__init__()
            self.stage_queries = 0

        def blocks_in_stage(self, stage):
            self.stage_queries += 1
            return super().blocks_in_stage(stage)

    ledger = CountingLedgerQuery()
    gatekeeper = ConstitutionGatekeeper(
        bus=bus, ruleset=make_ruleset(), ledger_query=ledger
    )

    await gatekeeper.handle_subproblem_proposed(
        make_proposed(subproblem_id="sp-1", description="Knead the dough for ten minutes.")
    )
    first = ledger.stage_queries
    await gatekeeper.handle_subproblem_proposed(
        make_proposed(subproblem_id="sp-2", description="Shape the loaf and score it.")
    )

    # Only the single VALIDATING lookup per proposal, never a sweep of all stages.
    assert ledger.stage_queries - first == 1


@pytest.mark.asyncio
async def test_ledger_outage_during_pending_lookup_does_not_break_in_flight_dedup():
    """`_collect_pending_descriptions` catches ledger failures and falls back to
    an empty VALIDATING-stage list rather than propagating.

    That fallback is behaviourally meaningful, not just defensive: in-flight
    dedup must survive a ledger outage instead of collapsing with it. A gate
    whose duplicate check blew up on every proposal the moment the ledger was
    unreachable would be strictly worse than one that degrades to
    in-process-only duplicate detection.
    """
    bus, accepted, rejected = wire_bus()

    class RaisingLedgerQuery(FakeLedgerQuery):
        def blocks_in_stage(self, stage):
            raise RuntimeError("ledger unavailable")

    gatekeeper = ConstitutionGatekeeper(
        bus=bus, ruleset=make_ruleset(), ledger_query=RaisingLedgerQuery()
    )

    await gatekeeper.handle_subproblem_proposed(
        make_proposed(subproblem_id="sp-1", description="Knead the dough for ten minutes.")
    )
    await gatekeeper.handle_subproblem_proposed(
        make_proposed(subproblem_id="sp-2", description="Knead the dough for ten minutes.")
    )

    assert [event.subproblem_id for event in accepted.events] == ["sp-1"]
    assert [event.subproblem_id for event in rejected.events] == ["sp-2"]
    assert rejected.events[0].reason == "duplicate"


@pytest.mark.asyncio
async def test_redelivery_of_an_already_accepted_subproblem_does_not_shrink_the_map():
    """The event bus is at-least-once, not exactly-once: `handle_subproblem_proposed`
    can run twice for the same `subproblem_id`. Re-accepting an id already present
    in `_accepted_in_flight` must not count as growth — it is a same-size overwrite,
    not an insert — so it must not trigger an eviction. Evicting anyway would drop
    an unrelated, still-relevant entry and silently shrink the map below its limit.
    """
    bus, accepted, rejected = wire_bus()
    gatekeeper = ConstitutionGatekeeper(
        bus=bus, ruleset=make_ruleset(), ledger_query=FakeLedgerQuery()
    )

    for index in range(gatekeeper.IN_FLIGHT_MEMORY_LIMIT):
        await gatekeeper.handle_subproblem_proposed(
            make_proposed(
                subproblem_id=f"sp-{index}",
                description=f"Prepare baking step number {index}.",
            )
        )
    assert len(gatekeeper._accepted_in_flight) == gatekeeper.IN_FLIGHT_MEMORY_LIMIT
    assert "sp-0" in gatekeeper._accepted_in_flight

    # Redeliver an already-accepted, non-oldest subproblem — same id, same event.
    await gatekeeper.handle_subproblem_proposed(
        make_proposed(subproblem_id="sp-5", description="Prepare baking step number 5.")
    )

    assert len(gatekeeper._accepted_in_flight) == gatekeeper.IN_FLIGHT_MEMORY_LIMIT
    assert "sp-0" in gatekeeper._accepted_in_flight
    assert rejected.events == []
