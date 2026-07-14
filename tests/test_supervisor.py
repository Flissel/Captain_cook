"""Unit tests for agenten.supervision.supervisor.SupervisorAgent.

Uses InMemoryEventBus end-to-end: publish SubproblemFailed/LeaseExpired
events into the supervisor and assert on the RetryRequested /
EscalateToRedecompose / CircuitStateChanged events it publishes back out.
"""
import pytest

from agenten.events.schemas import (
    CircuitStateChanged,
    EscalateToRedecompose,
    LeaseExpired,
    RetryRequested,
    SubproblemFailed,
    make_meta,
    topic_for,
)
from agenten.runtime.event_bus import InMemoryEventBus
from agenten.supervision.supervisor import SupervisorAgent


def make_bus_with_recorder(*event_types):
    """InMemoryEventBus wired to record every published instance of each
    event type into a dict of lists, keyed by event type.
    """
    bus = InMemoryEventBus()
    recorded = {event_type: [] for event_type in event_types}

    for event_type in event_types:

        async def handler(event, _bucket=recorded[event_type]):
            _bucket.append(event)

        bus.subscribe(topic_for(event_type), handler)

    return bus, recorded


def failed_event(subproblem_id="sp-1", retriable=True, error="boom"):
    return SubproblemFailed(
        meta=make_meta(correlation_id=subproblem_id, root_problem_id="root-1"),
        subproblem_id=subproblem_id,
        error=error,
        retriable=retriable,
    )


def lease_expired_event(subproblem_id="sp-1", agent_type="researcher", agent_key="w-1"):
    return LeaseExpired(
        meta=make_meta(correlation_id=subproblem_id, root_problem_id="root-1"),
        subproblem_id=subproblem_id,
        agent_type=agent_type,
        agent_key=agent_key,
    )


class FakeClock:
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.mark.asyncio
async def test_retry_with_exponential_backoff_delays():
    bus, recorded = make_bus_with_recorder(RetryRequested, EscalateToRedecompose)
    supervisor = SupervisorAgent(bus, max_retries=5, backoff_base=2.0)

    for _ in range(3):
        await supervisor.handle_failed(failed_event())

    retries = recorded[RetryRequested]
    assert len(retries) == 3
    assert [r.delay_seconds for r in retries] == [1.0, 2.0, 4.0]
    assert all(r.subproblem_id == "sp-1" for r in retries)
    assert recorded[EscalateToRedecompose] == []


@pytest.mark.asyncio
async def test_escalation_after_max_retries():
    bus, recorded = make_bus_with_recorder(RetryRequested, EscalateToRedecompose)
    supervisor = SupervisorAgent(bus, max_retries=3, backoff_base=2.0)

    # Three failures get retried (attempts 0, 1, 2)...
    for _ in range(3):
        await supervisor.handle_failed(failed_event())
    assert len(recorded[RetryRequested]) == 3
    assert recorded[EscalateToRedecompose] == []

    # ...the fourth failure exceeds max_retries and escalates instead.
    await supervisor.handle_failed(failed_event())
    assert len(recorded[RetryRequested]) == 3
    assert len(recorded[EscalateToRedecompose]) == 1
    escalation = recorded[EscalateToRedecompose][0]
    assert escalation.subproblem_id == "sp-1"
    assert "max_retries" in escalation.reason


@pytest.mark.asyncio
async def test_default_max_retries_comes_from_app_settings():
    from config.app_settings import RETRY_POLICY

    bus, recorded = make_bus_with_recorder(RetryRequested, EscalateToRedecompose)
    supervisor = SupervisorAgent(bus)  # no explicit max_retries

    for _ in range(RETRY_POLICY["max_retries"]):
        await supervisor.handle_failed(failed_event())
    assert len(recorded[RetryRequested]) == RETRY_POLICY["max_retries"]

    await supervisor.handle_failed(failed_event())
    assert len(recorded[EscalateToRedecompose]) == 1


@pytest.mark.asyncio
async def test_non_retriable_escalates_immediately():
    bus, recorded = make_bus_with_recorder(RetryRequested, EscalateToRedecompose)
    supervisor = SupervisorAgent(bus, max_retries=5)

    await supervisor.handle_failed(failed_event(retriable=False, error="fatal"))

    assert recorded[RetryRequested] == []
    assert len(recorded[EscalateToRedecompose]) == 1
    escalation = recorded[EscalateToRedecompose][0]
    assert escalation.subproblem_id == "sp-1"
    assert "non-retriable" in escalation.reason


@pytest.mark.asyncio
async def test_lease_expired_drives_retry_like_a_retriable_failure():
    bus, recorded = make_bus_with_recorder(
        RetryRequested, EscalateToRedecompose, CircuitStateChanged
    )
    supervisor = SupervisorAgent(bus, max_retries=5, backoff_base=2.0)

    await supervisor.handle_lease_expired(lease_expired_event())

    assert len(recorded[RetryRequested]) == 1
    assert recorded[RetryRequested][0].delay_seconds == 1.0
    assert recorded[EscalateToRedecompose] == []


@pytest.mark.asyncio
async def test_circuit_opens_after_threshold_failures():
    bus, recorded = make_bus_with_recorder(
        RetryRequested, EscalateToRedecompose, CircuitStateChanged
    )
    clock = FakeClock()
    supervisor = SupervisorAgent(
        bus,
        max_retries=100,
        circuit_window=4,
        circuit_failure_threshold=0.5,
        circuit_half_open_after_seconds=60.0,
        now=clock,
    )

    # A single lease-expiry failure gives a failure fraction of 1/1 = 1.0,
    # which already exceeds the 0.5 threshold, so the circuit should trip
    # open on this very first sample.
    await supervisor.handle_lease_expired(
        lease_expired_event(subproblem_id="sp-1", agent_type="researcher")
    )

    changes = recorded[CircuitStateChanged]
    assert len(changes) == 1
    assert changes[0].agent_type == "researcher"
    assert changes[0].state == "open"
    assert await supervisor.circuit_state("researcher") == "open"


@pytest.mark.asyncio
async def test_circuit_stays_closed_when_failures_do_not_exceed_threshold():
    bus, recorded = make_bus_with_recorder(
        RetryRequested, EscalateToRedecompose, CircuitStateChanged
    )
    clock = FakeClock()
    supervisor = SupervisorAgent(
        bus,
        max_retries=100,
        circuit_window=4,
        circuit_failure_threshold=0.5,
        now=clock,
    )

    # Interleave successes so the failure fraction never exceeds 0.5.
    supervisor.record_success("researcher")
    await supervisor.handle_lease_expired(
        lease_expired_event(subproblem_id="sp-1", agent_type="researcher")
    )
    supervisor.record_success("researcher")
    await supervisor.handle_lease_expired(
        lease_expired_event(subproblem_id="sp-2", agent_type="researcher")
    )

    assert recorded[CircuitStateChanged] == []
    assert await supervisor.circuit_state("researcher") == "closed"


@pytest.mark.asyncio
async def test_circuit_moves_to_half_open_after_timeout():
    bus, recorded = make_bus_with_recorder(
        RetryRequested, EscalateToRedecompose, CircuitStateChanged
    )
    clock = FakeClock()
    supervisor = SupervisorAgent(
        bus,
        max_retries=100,
        circuit_window=4,
        circuit_failure_threshold=0.5,
        circuit_half_open_after_seconds=60.0,
        now=clock,
    )

    await supervisor.handle_lease_expired(
        lease_expired_event(subproblem_id="sp-1", agent_type="researcher")
    )
    assert recorded[CircuitStateChanged][-1].state == "open"

    # Not enough time has passed yet: still open.
    clock.advance(30.0)
    assert await supervisor.circuit_state("researcher") == "open"
    assert recorded[CircuitStateChanged][-1].state == "open"

    # Past the half-open timeout: transitions (and publishes) on next access.
    clock.advance(31.0)
    assert await supervisor.circuit_state("researcher") == "half_open"
    assert recorded[CircuitStateChanged][-1].state == "half_open"
    assert recorded[CircuitStateChanged][-1].agent_type == "researcher"


@pytest.mark.asyncio
async def test_circuit_state_is_per_agent_type():
    bus, recorded = make_bus_with_recorder(
        RetryRequested, EscalateToRedecompose, CircuitStateChanged
    )
    supervisor = SupervisorAgent(bus, max_retries=100, circuit_failure_threshold=0.5)

    await supervisor.handle_lease_expired(
        lease_expired_event(subproblem_id="sp-1", agent_type="researcher")
    )

    assert await supervisor.circuit_state("researcher") == "open"
    assert await supervisor.circuit_state("writer") == "closed"


def test_import_without_autogen_core(monkeypatch):
    """SupervisorRoutedAgent must be None (not a hard import error) when
    autogen_core is unavailable — simulate that regardless of whatever is
    actually installed in this environment.
    """
    import builtins
    import importlib
    import sys

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "autogen_core" or name.startswith("autogen_core."):
            raise ImportError(f"simulated missing module: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "agenten.supervision.supervisor", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    try:
        module = importlib.import_module("agenten.supervision.supervisor")
    finally:
        monkeypatch.setattr(builtins, "__import__", real_import)
        monkeypatch.delitem(sys.modules, "agenten.supervision.supervisor", raising=False)

    assert module.autogen_core is None
    assert module.SupervisorRoutedAgent is None
