"""Unit tests for agenten.supervision.supervisor.SupervisorAgent.

Uses InMemoryEventBus end-to-end: publish SubproblemFailed/LeaseExpired/
SubproblemAssigned events into the supervisor and assert on the
RetryRequested / EscalateToRedecompose / CircuitStateChanged events it
publishes back out.
"""
import pytest

from agenten.events.schemas import (
    CircuitStateChanged,
    EscalateToRedecompose,
    LeaseExpired,
    RetryRequested,
    SubproblemAssigned,
    SubproblemFailed,
    make_meta,
    topic_for,
)
from agenten.runtime.event_bus import InMemoryEventBus
from agenten.supervision.supervisor import CIRCUIT_ROOT_ID, SupervisorAgent


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


def assigned_event(subproblem_id="sp-1", agent_type="researcher", agent_key="w-1"):
    return SubproblemAssigned(
        meta=make_meta(correlation_id=subproblem_id, root_problem_id="root-1"),
        subproblem_id=subproblem_id,
        agent_type=agent_type,
        agent_key=agent_key,
        lease_expires_at=10_000.0,
    )


CIRCUIT_EVENTS = (RetryRequested, EscalateToRedecompose, CircuitStateChanged)


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
async def test_escalation_evicts_attempt_state():
    """After escalation the subproblem's attempt count is dropped, so a
    re-decomposed reuse of the same id starts fresh instead of instantly
    re-escalating — and the dict can't grow without bound.
    """
    bus, recorded = make_bus_with_recorder(RetryRequested, EscalateToRedecompose)
    supervisor = SupervisorAgent(bus, max_retries=1)

    await supervisor.handle_failed(failed_event())  # retry (attempt 0)
    await supervisor.handle_failed(failed_event())  # escalate
    assert len(recorded[EscalateToRedecompose]) == 1
    assert supervisor._attempts == {}

    # Same id fails again after re-decomposition: retried, not escalated.
    await supervisor.handle_failed(failed_event())
    assert len(recorded[RetryRequested]) == 2
    assert len(recorded[EscalateToRedecompose]) == 1


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
    bus, recorded = make_bus_with_recorder(*CIRCUIT_EVENTS)
    supervisor = SupervisorAgent(bus, max_retries=5, backoff_base=2.0)

    await supervisor.handle_lease_expired(lease_expired_event())

    assert len(recorded[RetryRequested]) == 1
    assert recorded[RetryRequested][0].delay_seconds == 1.0
    assert recorded[EscalateToRedecompose] == []


@pytest.mark.asyncio
async def test_circuit_opens_after_threshold_failures():
    bus, recorded = make_bus_with_recorder(*CIRCUIT_EVENTS)
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
    # which already exceeds the 0.5 threshold, so the circuit trips open
    # on this very first sample.
    await supervisor.handle_lease_expired(
        lease_expired_event(subproblem_id="sp-1", agent_type="researcher")
    )

    changes = recorded[CircuitStateChanged]
    assert len(changes) == 1
    assert changes[0].agent_type == "researcher"
    assert changes[0].state == "open"
    assert changes[0].meta.root_problem_id == CIRCUIT_ROOT_ID
    assert await supervisor.circuit_state("researcher") == "open"


@pytest.mark.asyncio
async def test_circuit_stays_closed_when_failures_do_not_exceed_threshold():
    bus, recorded = make_bus_with_recorder(*CIRCUIT_EVENTS)
    clock = FakeClock()
    supervisor = SupervisorAgent(
        bus,
        max_retries=100,
        circuit_window=4,
        circuit_failure_threshold=0.5,
        now=clock,
    )

    # Interleave successes so the failure fraction never exceeds 0.5.
    await supervisor.record_success("researcher")
    await supervisor.handle_lease_expired(
        lease_expired_event(subproblem_id="sp-1", agent_type="researcher")
    )
    await supervisor.record_success("researcher")
    await supervisor.handle_lease_expired(
        lease_expired_event(subproblem_id="sp-2", agent_type="researcher")
    )

    assert recorded[CircuitStateChanged] == []
    assert await supervisor.circuit_state("researcher") == "closed"


@pytest.mark.asyncio
async def test_circuit_moves_to_half_open_after_timeout():
    bus, recorded = make_bus_with_recorder(*CIRCUIT_EVENTS)
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
async def test_success_during_half_open_closes_circuit():
    bus, recorded = make_bus_with_recorder(*CIRCUIT_EVENTS)
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
    clock.advance(61.0)
    assert await supervisor.circuit_state("researcher") == "half_open"

    await supervisor.record_success("researcher")
    assert recorded[CircuitStateChanged][-1].state == "closed"
    assert await supervisor.circuit_state("researcher") == "closed"


@pytest.mark.asyncio
async def test_full_circuit_cycle_closed_open_half_open_closed():
    bus, recorded = make_bus_with_recorder(*CIRCUIT_EVENTS)
    clock = FakeClock()
    supervisor = SupervisorAgent(
        bus,
        max_retries=100,
        circuit_window=4,
        circuit_failure_threshold=0.5,
        circuit_half_open_after_seconds=60.0,
        now=clock,
    )

    assert await supervisor.circuit_state("researcher") == "closed"
    await supervisor.handle_lease_expired(
        lease_expired_event(subproblem_id="sp-1", agent_type="researcher")
    )
    clock.advance(61.0)
    # The next failure arrives after the timeout: circuit first moves to
    # half_open, then the fresh failure (on a cleared window) re-opens it.
    await supervisor.handle_lease_expired(
        lease_expired_event(subproblem_id="sp-2", agent_type="researcher")
    )
    states = [c.state for c in recorded[CircuitStateChanged]]
    assert states == ["open", "half_open", "open"]

    # Recover: timeout again, then a success closes it for good.
    clock.advance(61.0)
    await supervisor.record_success("researcher")
    states = [c.state for c in recorded[CircuitStateChanged]]
    assert states == ["open", "half_open", "open", "half_open", "closed"]
    assert await supervisor.circuit_state("researcher") == "closed"


@pytest.mark.asyncio
async def test_half_open_trial_window_is_cleared_of_stale_failures():
    """Entering half_open clears the rolling window, so the trial is judged
    on fresh outcomes only — a success during probation closes the circuit
    even though the window was previously full of failures.
    """
    bus, recorded = make_bus_with_recorder(*CIRCUIT_EVENTS)
    clock = FakeClock()
    supervisor = SupervisorAgent(
        bus,
        max_retries=100,
        circuit_window=8,
        circuit_failure_threshold=0.5,
        circuit_half_open_after_seconds=60.0,
        now=clock,
    )

    for i in range(4):
        await supervisor.handle_lease_expired(
            lease_expired_event(subproblem_id=f"sp-{i}", agent_type="researcher")
        )
    assert await supervisor.circuit_state("researcher") == "open"

    clock.advance(61.0)
    assert await supervisor.circuit_state("researcher") == "half_open"
    await supervisor.record_success("researcher")
    assert await supervisor.circuit_state("researcher") == "closed"


@pytest.mark.asyncio
async def test_failed_event_drives_circuit_via_assignment_mapping():
    """SubproblemFailed lacks agent_type, but a prior SubproblemAssigned
    lets the supervisor attribute the failure to the assigned agent_type.
    """
    bus, recorded = make_bus_with_recorder(*CIRCUIT_EVENTS)
    supervisor = SupervisorAgent(bus, max_retries=100, circuit_failure_threshold=0.5)

    await supervisor.handle_assigned(assigned_event(subproblem_id="sp-1", agent_type="writer"))
    await supervisor.handle_failed(failed_event(subproblem_id="sp-1"))

    changes = recorded[CircuitStateChanged]
    assert len(changes) == 1
    assert changes[0].agent_type == "writer"
    assert changes[0].state == "open"


@pytest.mark.asyncio
async def test_failed_event_without_known_assignment_moves_no_circuit():
    bus, recorded = make_bus_with_recorder(*CIRCUIT_EVENTS)
    supervisor = SupervisorAgent(bus, max_retries=100, circuit_failure_threshold=0.5)

    await supervisor.handle_failed(failed_event(subproblem_id="sp-unseen"))

    assert len(recorded[RetryRequested]) == 1  # retry logic unaffected
    assert recorded[CircuitStateChanged] == []


@pytest.mark.asyncio
async def test_circuit_state_is_per_agent_type():
    bus, recorded = make_bus_with_recorder(*CIRCUIT_EVENTS)
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
