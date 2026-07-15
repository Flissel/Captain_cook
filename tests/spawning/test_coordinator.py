"""Unit tests for agenten/spawning/coordinator.py (unit U4).

Uses InMemoryEventBus (U0) to capture published events, a hand-rolled fake
LedgerQuery (in-memory dict of fake blocks) standing in for the real
ledger read-side (unit U8), and a real CapabilityRegistry (U0).
"""
import asyncio
from typing import Any, Dict, List, Optional

import pytest

from agenten.events.schemas import (
    CircuitStateChanged,
    RetryRequested,
    SubproblemAccepted,
    SubproblemAssigned,
    SubproblemUnroutable,
    make_meta,
    topic_for,
)
from agenten.ledger_bridge.stage_machine import LedgerQuery, Stage
from agenten.runtime.event_bus import InMemoryEventBus
from agenten.spawning.capability_registry import CapabilityRegistry
from agenten.spawning.coordinator import (
    BACKPRESSURE_RETRY_DELAY_SECONDS,
    CIRCUIT_OPEN_RETRY_DELAY_SECONDS,
    SpawnCoordinatorAgent,
)


class FakeBlock:
    """Minimal stand-in for blockchain.Blockchain_modell.Block: only the
    attributes SpawnCoordinatorAgent actually reads.
    """

    def __init__(self, index: int, stage: Stage, data: Dict[str, Any]):
        self.index = index
        self.stage = stage
        self.data = data


class FakeLedgerQuery(LedgerQuery):
    """In-memory LedgerQuery backed by a dict of FakeBlocks, keyed by
    block_index. Stage membership is derived from FakeBlock.stage so tests
    can freely move a block between stages.
    """

    def __init__(self):
        self._blocks: Dict[int, FakeBlock] = {}

    def add(self, index: int, stage: Stage, data: Dict[str, Any]) -> FakeBlock:
        block = FakeBlock(index=index, stage=stage, data=data)
        self._blocks[index] = block
        return block

    def count_in_stage(self, stage: Stage) -> int:
        return len(self.blocks_in_stage(stage))

    def blocks_in_stage(self, stage: Stage) -> List[FakeBlock]:
        return [b for b in self._blocks.values() if b.stage == stage]

    def get_block(self, index: int) -> Optional[FakeBlock]:
        return self._blocks.get(index)


def make_accepted_event(subproblem_id: str, block_index: Optional[int], root_problem_id: str = "root-1") -> SubproblemAccepted:
    return SubproblemAccepted(
        meta=make_meta(correlation_id=subproblem_id, root_problem_id=root_problem_id),
        subproblem_id=subproblem_id,
        block_index=block_index,
    )


class Collector:
    """Subscribes to a bus topic and records every event delivered to it."""

    def __init__(self, bus: InMemoryEventBus, topic: str):
        self.events: List[Any] = []
        bus.subscribe(topic, self._handle)

    async def _handle(self, event: Any) -> None:
        self.events.append(event)


def make_coordinator(ledger_query: FakeLedgerQuery, **kwargs):
    bus = InMemoryEventBus()
    registry = CapabilityRegistry()
    registry.register("research", "ResearchAgent")
    coordinator = SpawnCoordinatorAgent(bus=bus, registry=registry, ledger_query=ledger_query, **kwargs)
    assigned = Collector(bus, topic_for(SubproblemAssigned))
    retried = Collector(bus, topic_for(RetryRequested))
    return coordinator, assigned, retried


@pytest.mark.asyncio
async def test_normal_assignment_publishes_subproblem_assigned():
    ledger = FakeLedgerQuery()
    ledger.add(1, Stage.ACCEPTED, {"subproblem_id": "sp-1", "capability_tags": ["research"]})
    coordinator, assigned, retried = make_coordinator(ledger, now=lambda: 1000.0, lease_duration_seconds=60.0)

    await coordinator.handle_subproblem_accepted(make_accepted_event("sp-1", block_index=1))

    assert len(assigned.events) == 1
    event = assigned.events[0]
    assert event.subproblem_id == "sp-1"
    assert event.agent_type == "ResearchAgent"
    assert event.agent_key == "sp-1"  # subproblem_id doubles as agent_key
    assert event.lease_expires_at == 1060.0
    assert retried.events == []


@pytest.mark.asyncio
async def test_block_index_none_is_ignored():
    ledger = FakeLedgerQuery()
    coordinator, assigned, retried = make_coordinator(ledger)

    await coordinator.handle_subproblem_accepted(make_accepted_event("sp-1", block_index=None))

    assert assigned.events == []
    assert retried.events == []


@pytest.mark.asyncio
async def test_unroutable_capability_publishes_durable_outcome(caplog):
    ledger = FakeLedgerQuery()
    ledger.add(1, Stage.ACCEPTED, {"subproblem_id": "sp-1", "capability_tags": ["no-such-capability"]})
    coordinator, assigned, retried = make_coordinator(ledger)
    unroutable = Collector(coordinator._bus, topic_for(SubproblemUnroutable))

    with caplog.at_level("ERROR"):
        await coordinator.handle_subproblem_accepted(make_accepted_event("sp-1", block_index=1))

    assert assigned.events == []
    assert retried.events == []
    assert len(unroutable.events) == 1
    assert unroutable.events[0].subproblem_id == "sp-1"
    assert unroutable.events[0].capability_tags == ["no-such-capability"]
    assert "No capable agent type" in unroutable.events[0].error
    assert any("No capable agent type" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_backpressure_triggers_retry_requested():
    ledger = FakeLedgerQuery()
    # Two ResearchAgent blocks already in flight, cap is 2 -> at capacity.
    ledger.add(10, Stage.ASSIGNED, {"subproblem_id": "existing-1", "agent_type": "ResearchAgent"})
    ledger.add(11, Stage.IN_PROGRESS, {"subproblem_id": "existing-2", "agent_type": "ResearchAgent"})
    ledger.add(1, Stage.ACCEPTED, {"subproblem_id": "sp-1", "capability_tags": ["research"]})
    coordinator, assigned, retried = make_coordinator(ledger, max_in_flight_per_type=2)

    await coordinator.handle_subproblem_accepted(make_accepted_event("sp-1", block_index=1))

    assert assigned.events == []
    assert len(retried.events) == 1
    retry_event = retried.events[0]
    assert retry_event.subproblem_id == "sp-1"
    assert retry_event.delay_seconds == BACKPRESSURE_RETRY_DELAY_SECONDS


@pytest.mark.asyncio
async def test_open_circuit_triggers_retry_requested():
    ledger = FakeLedgerQuery()
    ledger.add(1, Stage.ACCEPTED, {"subproblem_id": "sp-1", "capability_tags": ["research"]})
    coordinator, assigned, retried = make_coordinator(ledger)

    await coordinator.handle_circuit_state_changed(
        CircuitStateChanged(
            meta=make_meta(correlation_id="ResearchAgent", root_problem_id="root-1"),
            agent_type="ResearchAgent",
            state="open",
        )
    )
    await coordinator.handle_subproblem_accepted(make_accepted_event("sp-1", block_index=1))

    assert assigned.events == []
    assert len(retried.events) == 1
    retry_event = retried.events[0]
    assert retry_event.subproblem_id == "sp-1"
    assert retry_event.delay_seconds == CIRCUIT_OPEN_RETRY_DELAY_SECONDS


@pytest.mark.asyncio
async def test_retry_requested_reattempts_and_succeeds_once_capacity_frees():
    ledger = FakeLedgerQuery()
    block = ledger.add(1, Stage.ACCEPTED, {"subproblem_id": "sp-1", "capability_tags": ["research"]})
    coordinator, assigned, retried = make_coordinator(ledger, max_in_flight_per_type=0)

    # First attempt: capacity is 0, so it defers.
    await coordinator.handle_subproblem_accepted(make_accepted_event("sp-1", block_index=1))
    assert assigned.events == []
    assert len(retried.events) == 1
    retry_event = retried.events[0]

    # Capacity frees up before the retry fires.
    coordinator._max_in_flight_per_type = 5
    await coordinator.handle_retry_requested(
        RetryRequested(
            meta=make_meta(correlation_id="sp-1", root_problem_id="root-1"),
            subproblem_id=retry_event.subproblem_id,
            delay_seconds=0.0,
        )
    )

    assert len(assigned.events) == 1
    assert assigned.events[0].subproblem_id == "sp-1"


@pytest.mark.asyncio
async def test_retry_requested_with_no_matching_block_is_skipped(caplog):
    ledger = FakeLedgerQuery()
    coordinator, assigned, retried = make_coordinator(ledger)

    with caplog.at_level("WARNING"):
        await coordinator.handle_retry_requested(
            RetryRequested(
                meta=make_meta(correlation_id="sp-missing", root_problem_id="root-1"),
                subproblem_id="sp-missing",
                delay_seconds=0.0,
            )
        )

    assert assigned.events == []
    assert retried.events == []


@pytest.mark.asyncio
async def test_duplicate_event_delivery_is_a_no_op():
    ledger = FakeLedgerQuery()
    ledger.add(1, Stage.ACCEPTED, {"subproblem_id": "sp-1", "capability_tags": ["research"]})
    coordinator, assigned, retried = make_coordinator(ledger)

    event = make_accepted_event("sp-1", block_index=1)
    await coordinator.handle_subproblem_accepted(event)
    await coordinator.handle_subproblem_accepted(event)  # same event_id, redelivered

    assert len(assigned.events) == 1


@pytest.mark.asyncio
async def test_import_with_or_without_autogen_core():
    """The module must import cleanly either way, and its soft-import
    degradation must be self-consistent: RoutedSpawnCoordinatorAgent is a
    real adapter class exactly when autogen_core resolved, and None (not a
    half-defined class, not an ImportError) when it didn't.
    """
    import importlib.util

    from agenten.spawning import coordinator as coordinator_module

    if importlib.util.find_spec("autogen_core") is not None:
        assert coordinator_module.autogen_core is not None
        assert coordinator_module.RoutedSpawnCoordinatorAgent is not None
    else:
        assert coordinator_module.autogen_core is None
        assert coordinator_module.RoutedSpawnCoordinatorAgent is None
