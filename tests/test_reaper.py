"""Unit tests for agenten.supervision.reaper.ReaperAgent.

Uses a hand-rolled fake LedgerQuery (in-memory list of fake blocks with
controllable metadata["lease_expires_at"]) and the real InMemoryEventBus,
so these tests exercise the real publish path with zero AutoGen installed.
"""
import asyncio
import functools
from typing import Dict, List, Optional

import pytest

from agenten.events.schemas import LeaseExpired, topic_for
from agenten.ledger_bridge.stage_machine import LedgerQuery, Stage
from agenten.runtime.event_bus import InMemoryEventBus
from agenten.supervision.reaper import ReaperAgent


def async_test(coro_fn):
    """Run an async test function via asyncio.run(), so these tests work
    under plain pytest without depending on the pytest-asyncio plugin
    (not currently part of this repo's test setup)."""

    @functools.wraps(coro_fn)
    def wrapper(*args, **kwargs):
        asyncio.run(coro_fn(*args, **kwargs))

    return wrapper


class FakeBlock:
    def __init__(self, index: int, data: Dict, metadata: Optional[Dict] = None):
        self.index = index
        self.block_type = "task"
        self.data = data
        self.status = "assigned"
        self.metadata = metadata or {}


class FakeLedgerQuery(LedgerQuery):
    """In-memory stand-in for the real ledger read side. Blocks are bucketed
    by stage explicitly (rather than inferred from block.status) so tests
    can control exactly what blocks_in_stage() returns per stage."""

    def __init__(self):
        self._by_stage: Dict[Stage, List[FakeBlock]] = {}

    def put(self, stage: Stage, block: FakeBlock) -> None:
        self._by_stage.setdefault(stage, []).append(block)

    def count_in_stage(self, stage: Stage) -> int:
        return len(self._by_stage.get(stage, []))

    def blocks_in_stage(self, stage: Stage) -> List[FakeBlock]:
        return list(self._by_stage.get(stage, []))

    def get_block(self, index: int) -> Optional[FakeBlock]:
        for blocks in self._by_stage.values():
            for block in blocks:
                if block.index == index:
                    return block
        return None


def make_block(index: int, subproblem_id: str, lease_expires_at, root_problem_id=None) -> FakeBlock:
    data = {
        "subproblem_id": subproblem_id,
        "agent_type": "researcher",
        "agent_key": f"agent-{index}",
    }
    if root_problem_id is not None:
        data["root_problem_id"] = root_problem_id
    return FakeBlock(index=index, data=data, metadata={"lease_expires_at": lease_expires_at})


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@async_test
async def test_expired_lease_produces_exactly_one_lease_expired():
    ledger = FakeLedgerQuery()
    ledger.put(Stage.ASSIGNED, make_block(1, "sp-1", lease_expires_at=500.0))
    bus = InMemoryEventBus()
    received = []

    async def handler(event):
        received.append(event)

    bus.subscribe(topic_for(LeaseExpired), handler)
    reaper = ReaperAgent(bus=bus, ledger_query=ledger, now=FakeClock(1000.0))

    published = await reaper.scan_once()

    assert len(published) == 1
    event = published[0]
    assert isinstance(event, LeaseExpired)
    assert event.subproblem_id == "sp-1"
    assert event.agent_type == "researcher"
    assert event.agent_key == "agent-1"
    assert len(received) == 1


@async_test
async def test_non_expired_lease_produces_no_events():
    ledger = FakeLedgerQuery()
    ledger.put(Stage.ASSIGNED, make_block(1, "sp-1", lease_expires_at=2000.0))
    ledger.put(Stage.IN_PROGRESS, make_block(2, "sp-2", lease_expires_at=None))
    bus = InMemoryEventBus()
    reaper = ReaperAgent(bus=bus, ledger_query=ledger, now=FakeClock(1000.0))

    published = await reaper.scan_once()

    assert published == []


@async_test
async def test_already_reaped_lease_not_reflagged_on_second_scan():
    ledger = FakeLedgerQuery()
    ledger.put(Stage.ASSIGNED, make_block(1, "sp-1", lease_expires_at=500.0))
    bus = InMemoryEventBus()
    reaper = ReaperAgent(bus=bus, ledger_query=ledger, now=FakeClock(1000.0))

    first = await reaper.scan_once()
    second = await reaper.scan_once()

    assert len(first) == 1
    assert second == []


@async_test
async def test_root_problem_id_falls_back_to_subproblem_id():
    ledger = FakeLedgerQuery()
    ledger.put(Stage.ASSIGNED, make_block(1, "sp-1", lease_expires_at=500.0))
    bus = InMemoryEventBus()
    reaper = ReaperAgent(bus=bus, ledger_query=ledger, now=FakeClock(1000.0))

    published = await reaper.scan_once()

    assert published[0].meta.root_problem_id == "sp-1"
    assert published[0].meta.correlation_id == "sp-1"


@async_test
async def test_root_problem_id_uses_explicit_value_when_present():
    ledger = FakeLedgerQuery()
    ledger.put(
        Stage.IN_PROGRESS,
        make_block(1, "sp-1", lease_expires_at=500.0, root_problem_id="root-99"),
    )
    bus = InMemoryEventBus()
    reaper = ReaperAgent(bus=bus, ledger_query=ledger, now=FakeClock(1000.0))

    published = await reaper.scan_once()

    assert published[0].meta.root_problem_id == "root-99"


@async_test
async def test_run_forever_can_be_started_and_cancelled_cleanly():
    ledger = FakeLedgerQuery()
    bus = InMemoryEventBus()
    reaper = ReaperAgent(bus=bus, ledger_query=ledger, poll_interval_seconds=0.01, now=FakeClock(1000.0))

    task = asyncio.create_task(reaper.run_forever())
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@async_test
async def test_reexpiry_after_lease_renewal_on_same_block_index_is_reflagged():
    # The underlying ledger mutates a block in place across retries rather
    # than appending a new block (Blockchain.update_task_status), so the
    # same block.index can legitimately expire, get renewed with a fresh
    # lease, and then expire again. That must produce a second
    # LeaseExpired, not be swallowed by the dedup-by-index tracking.
    ledger = FakeLedgerQuery()
    block = make_block(1, "sp-1", lease_expires_at=500.0)
    ledger.put(Stage.ASSIGNED, block)
    bus = InMemoryEventBus()
    reaper = ReaperAgent(bus=bus, ledger_query=ledger, now=FakeClock(1000.0))

    first = await reaper.scan_once()
    assert len(first) == 1

    # Simulate a retry: same block index, renewed (still-expired-later) lease.
    block.metadata["lease_expires_at"] = 1500.0
    unchanged = await reaper.scan_once()
    assert unchanged == []

    # The renewed lease itself now expires.
    block.metadata["lease_expires_at"] = 1800.0
    reaper.now = FakeClock(2000.0)
    second = await reaper.scan_once()
    assert len(second) == 1


@async_test
async def test_scans_both_assigned_and_in_progress_stages():
    ledger = FakeLedgerQuery()
    ledger.put(Stage.ASSIGNED, make_block(1, "sp-1", lease_expires_at=500.0))
    ledger.put(Stage.IN_PROGRESS, make_block(2, "sp-2", lease_expires_at=500.0))
    bus = InMemoryEventBus()
    reaper = ReaperAgent(bus=bus, ledger_query=ledger, now=FakeClock(1000.0))

    published = await reaper.scan_once()

    subproblem_ids = {event.subproblem_id for event in published}
    assert subproblem_ids == {"sp-1", "sp-2"}
