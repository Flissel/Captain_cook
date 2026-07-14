"""Unit tests for agenten/ledger_bridge/recovery.py.

Uses a hand-rolled fake LedgerQuery (in-memory list of fake Block-like
objects) + the real InMemoryEventBus, so these run without autogen_core
and without a real blockchain/ledger implementation (U8 not required).
"""
from typing import Dict, List, Optional

import pytest

from agenten.events.schemas import LeaseExpired, SubproblemAccepted, SubproblemProposed, topic_for
from agenten.ledger_bridge.recovery import recover_on_startup
from agenten.ledger_bridge.stage_machine import Stage
from agenten.runtime.event_bus import InMemoryEventBus


class FakeBlock:
    """Minimal stand-in matching the real Block's shape
    (index, block_type, data, status, metadata, parent_index, children).
    """

    def __init__(
        self,
        index: int,
        status: str,
        data: Optional[dict] = None,
        metadata: Optional[dict] = None,
        block_type: str = "subproblem",
        parent_index: Optional[int] = None,
    ):
        self.index = index
        self.block_type = block_type
        self.data = data or {}
        self.status = status
        self.metadata = metadata or {}
        self.parent_index = parent_index
        self.children: List[int] = []


class FakeLedgerQuery:
    """In-memory LedgerQuery: blocks_in_stage/get_block over a plain list.

    Not a subclass of the real LedgerQuery ABC on purpose (duck-typed), to
    keep this test importable even if stage_machine.py's ABC surface
    shifts slightly.
    """

    def __init__(self, blocks: List[FakeBlock]):
        self._blocks = blocks

    def count_in_stage(self, stage: Stage) -> int:
        return len(self.blocks_in_stage(stage))

    def blocks_in_stage(self, stage: Stage) -> List[FakeBlock]:
        return [b for b in self._blocks if b.status == stage.value]

    def get_block(self, index: int) -> Optional[FakeBlock]:
        for b in self._blocks:
            if b.index == index:
                return b
        return None


def _collect(bus: InMemoryEventBus, event_type) -> List:
    received = []

    async def handler(event):
        received.append(event)

    bus.subscribe(topic_for(event_type), handler)
    return received


def make_subproblem_data(**overrides) -> dict:
    data = {
        "subproblem_id": "sp-1",
        "description": "do the thing",
        "capability_tags": ["tag-a"],
        "parent_subproblem_id": "sp-0",
        "depth": 1,
        "root_problem_id": "root-1",
    }
    data.update(overrides)
    return data


@pytest.mark.asyncio
async def test_queued_republishes_subproblem_proposed():
    bus = InMemoryEventBus()
    received = _collect(bus, SubproblemProposed)

    block = FakeBlock(index=1, status=Stage.QUEUED.value, data=make_subproblem_data())
    ledger = FakeLedgerQuery([block])

    summary = await recover_on_startup(bus, ledger)

    assert len(received) == 1
    event = received[0]
    assert event.subproblem_id == "sp-1"
    assert event.parent_id == "sp-0"
    assert event.depth == 1
    assert event.description == "do the thing"
    assert event.capability_tags == ["tag-a"]
    assert event.meta.correlation_id == "sp-1"
    assert event.meta.root_problem_id == "root-1"
    assert event.meta.attempt == 0
    assert summary["queued_or_validating"] == 1


@pytest.mark.asyncio
async def test_validating_republishes_subproblem_proposed_with_recovery_attempt():
    bus = InMemoryEventBus()
    received = _collect(bus, SubproblemProposed)

    block = FakeBlock(
        index=2,
        status=Stage.VALIDATING.value,
        data=make_subproblem_data(subproblem_id="sp-2"),
        metadata={"recovery_attempts": 3},
    )
    ledger = FakeLedgerQuery([block])

    summary = await recover_on_startup(bus, ledger)

    assert len(received) == 1
    assert received[0].meta.attempt == 3
    assert summary["queued_or_validating"] == 1


@pytest.mark.asyncio
async def test_accepted_republishes_subproblem_accepted_with_block_index():
    bus = InMemoryEventBus()
    received = _collect(bus, SubproblemAccepted)

    block = FakeBlock(index=42, status=Stage.ACCEPTED.value, data=make_subproblem_data(subproblem_id="sp-3"))
    ledger = FakeLedgerQuery([block])

    summary = await recover_on_startup(bus, ledger)

    assert len(received) == 1
    event = received[0]
    assert event.subproblem_id == "sp-3"
    assert event.block_index == 42
    assert summary["accepted"] == 1


@pytest.mark.asyncio
async def test_assigned_with_expired_lease_publishes_lease_expired():
    bus = InMemoryEventBus()
    received = _collect(bus, LeaseExpired)

    block = FakeBlock(
        index=5,
        status=Stage.ASSIGNED.value,
        data=make_subproblem_data(subproblem_id="sp-4", agent_type="worker", agent_key="w-1"),
        metadata={"lease_expires_at": 100.0},
    )
    ledger = FakeLedgerQuery([block])

    summary = await recover_on_startup(bus, ledger, now=lambda: 200.0)

    assert len(received) == 1
    event = received[0]
    assert event.subproblem_id == "sp-4"
    assert event.agent_type == "worker"
    assert event.agent_key == "w-1"
    assert summary["lease_expired"] == 1


@pytest.mark.asyncio
async def test_in_progress_with_expired_lease_publishes_lease_expired():
    bus = InMemoryEventBus()
    received = _collect(bus, LeaseExpired)

    block = FakeBlock(
        index=6,
        status=Stage.IN_PROGRESS.value,
        data=make_subproblem_data(subproblem_id="sp-5", agent_type="worker", agent_key="w-2"),
        metadata={"lease_expires_at": 50.0},
    )
    ledger = FakeLedgerQuery([block])

    summary = await recover_on_startup(bus, ledger, now=lambda: 51.0)

    assert len(received) == 1
    assert summary["lease_expired"] == 1


@pytest.mark.asyncio
async def test_unexpired_lease_is_left_alone():
    bus = InMemoryEventBus()
    received = _collect(bus, LeaseExpired)

    block = FakeBlock(
        index=7,
        status=Stage.ASSIGNED.value,
        data=make_subproblem_data(subproblem_id="sp-6", agent_type="worker", agent_key="w-3"),
        metadata={"lease_expires_at": 1000.0},
    )
    ledger = FakeLedgerQuery([block])

    summary = await recover_on_startup(bus, ledger, now=lambda: 500.0)

    assert received == []
    assert summary["lease_expired"] == 0


@pytest.mark.asyncio
async def test_retrying_is_flagged_and_nothing_republished():
    bus = InMemoryEventBus()
    proposed = _collect(bus, SubproblemProposed)
    accepted = _collect(bus, SubproblemAccepted)
    lease_expired = _collect(bus, LeaseExpired)

    block = FakeBlock(index=8, status=Stage.RETRYING.value, data=make_subproblem_data(subproblem_id="sp-7"))
    ledger = FakeLedgerQuery([block])

    summary = await recover_on_startup(bus, ledger)

    assert proposed == []
    assert accepted == []
    assert lease_expired == []
    assert summary["stuck_retrying_flagged"] == 1


@pytest.mark.asyncio
async def test_terminal_stage_blocks_are_never_touched():
    bus = InMemoryEventBus()
    proposed = _collect(bus, SubproblemProposed)
    accepted = _collect(bus, SubproblemAccepted)
    lease_expired = _collect(bus, LeaseExpired)

    blocks = [
        FakeBlock(index=9, status=Stage.DONE.value, data=make_subproblem_data(subproblem_id="sp-done")),
        FakeBlock(index=10, status=Stage.FAILED.value, data=make_subproblem_data(subproblem_id="sp-failed")),
        FakeBlock(index=11, status=Stage.REJECTED.value, data=make_subproblem_data(subproblem_id="sp-rejected")),
    ]
    ledger = FakeLedgerQuery(blocks)

    summary = await recover_on_startup(bus, ledger)

    assert proposed == accepted == lease_expired == []
    assert summary == {
        "queued_or_validating": 0,
        "accepted": 0,
        "lease_expired": 0,
        "stuck_retrying_flagged": 0,
        "lease_missing_flagged": 0,
        "unhandled_stage_flagged": 0,
    }


@pytest.mark.asyncio
async def test_verifying_stage_is_flagged_not_silently_dropped():
    """Stage.VERIFYING is non-terminal but has no dedicated recovery bucket
    in the U9 contract (no ledger-recoverable event is defined for it) —
    it must still show up in the summary rather than vanish.
    """
    bus = InMemoryEventBus()
    block = FakeBlock(index=12, status=Stage.VERIFYING.value, data=make_subproblem_data(subproblem_id="sp-8"))
    ledger = FakeLedgerQuery([block])

    summary = await recover_on_startup(bus, ledger)

    assert summary["unhandled_stage_flagged"] == 1


@pytest.mark.asyncio
async def test_summary_counts_across_mixed_blocks():
    bus = InMemoryEventBus()

    blocks = [
        FakeBlock(index=1, status=Stage.QUEUED.value, data=make_subproblem_data(subproblem_id="a")),
        FakeBlock(index=2, status=Stage.VALIDATING.value, data=make_subproblem_data(subproblem_id="b")),
        FakeBlock(index=3, status=Stage.VALIDATING.value, data=make_subproblem_data(subproblem_id="c")),
        FakeBlock(index=4, status=Stage.ACCEPTED.value, data=make_subproblem_data(subproblem_id="d")),
        FakeBlock(
            index=5,
            status=Stage.ASSIGNED.value,
            data=make_subproblem_data(subproblem_id="e", agent_type="t", agent_key="k"),
            metadata={"lease_expires_at": 1.0},
        ),
        FakeBlock(
            index=6,
            status=Stage.IN_PROGRESS.value,
            data=make_subproblem_data(subproblem_id="f", agent_type="t", agent_key="k"),
            metadata={"lease_expires_at": 1.0},
        ),
        FakeBlock(
            index=7,
            status=Stage.ASSIGNED.value,
            data=make_subproblem_data(subproblem_id="g", agent_type="t", agent_key="k"),
            metadata={"lease_expires_at": 99999.0},
        ),
        FakeBlock(index=8, status=Stage.RETRYING.value, data=make_subproblem_data(subproblem_id="h")),
        FakeBlock(index=9, status=Stage.DONE.value, data=make_subproblem_data(subproblem_id="i")),
    ]
    ledger = FakeLedgerQuery(blocks)

    summary = await recover_on_startup(bus, ledger, now=lambda: 500.0)

    assert summary == {
        "queued_or_validating": 3,
        "accepted": 1,
        "lease_expired": 2,
        "stuck_retrying_flagged": 1,
        "lease_missing_flagged": 0,
        "unhandled_stage_flagged": 0,
    }


@pytest.mark.asyncio
async def test_missing_lease_is_flagged_not_silently_dropped():
    """An ASSIGNED/IN_PROGRESS block with no lease_expires_at at all is a
    data anomaly (Coordinator/Ledger Recorder should always stamp one) —
    it must not be silently skipped, and must not have a LeaseExpired
    fabricated for it either (we don't know who, if anyone, holds it).
    """
    bus = InMemoryEventBus()
    lease_expired = _collect(bus, LeaseExpired)

    block = FakeBlock(
        index=13,
        status=Stage.ASSIGNED.value,
        data=make_subproblem_data(subproblem_id="sp-9", agent_type="t", agent_key="k"),
        metadata={},
    )
    ledger = FakeLedgerQuery([block])

    summary = await recover_on_startup(bus, ledger)

    assert lease_expired == []
    assert summary["lease_missing_flagged"] == 1
    assert summary["lease_expired"] == 0


@pytest.mark.asyncio
async def test_republished_events_carry_the_injected_clock_not_wall_time():
    """meta.ts on every re-published event must come from the injected
    `now` clock, not real wall-clock time, so recovery is deterministic
    and testable (and so a simulated/replayed clock is honored).
    """
    bus = InMemoryEventBus()
    proposed = _collect(bus, SubproblemProposed)
    accepted = _collect(bus, SubproblemAccepted)
    lease_expired = _collect(bus, LeaseExpired)

    blocks = [
        FakeBlock(index=20, status=Stage.QUEUED.value, data=make_subproblem_data(subproblem_id="a")),
        FakeBlock(index=21, status=Stage.ACCEPTED.value, data=make_subproblem_data(subproblem_id="b")),
        FakeBlock(
            index=22,
            status=Stage.ASSIGNED.value,
            data=make_subproblem_data(subproblem_id="c", agent_type="t", agent_key="k"),
            metadata={"lease_expires_at": 1.0},
        ),
    ]
    ledger = FakeLedgerQuery(blocks)

    fixed_ts = 123456.0
    await recover_on_startup(bus, ledger, now=lambda: fixed_ts)

    assert proposed[0].meta.ts == fixed_ts
    assert accepted[0].meta.ts == fixed_ts
    assert lease_expired[0].meta.ts == fixed_ts
