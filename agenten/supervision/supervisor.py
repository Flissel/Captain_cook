"""Supervisor agent: retry/backoff and per-agent-type circuit breaking for
the event-driven supply-chain pipeline.

Circuit-outcome attribution: `SubproblemFailed` carries no `agent_type`
field, so on its own it cannot be attributed to a worker type. Two signals
compensate:

- `LeaseExpired` (from the reaper, unit U7) carries `agent_type` directly
  and is the primary circuit signal.
- `handle_assigned` records a subproblem_id -> agent_type mapping from
  `SubproblemAssigned` (published by the spawn coordinator, unit U4), which
  lets `handle_failed` attribute a later `SubproblemFailed` to the agent
  type that was working on it. If no assignment was observed for a
  subproblem (e.g. supervisor restarted in between), the failure still
  drives retry/backoff/escalation but moves no circuit — attribution is
  best-effort by design.

Wiring these handlers to bus subscriptions / AutoGen TypeSubscriptions is
the orchestration unit's (U11's) job; this module only exposes the
handlers.

Scope note on state: circuit-breaker, attempt-count, and assignment state
here is in-memory and single-process by design. A distributed deployment
(multiple supervisor replicas) would need shared state (e.g. Redis) to
agree on circuit state and attempt counts across processes — that's out of
scope for this unit and deliberately not built here.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Optional

from agenten.events.schemas import (
    CircuitState,
    CircuitStateChanged,
    EscalateToRedecompose,
    EventMeta,
    LeaseExpired,
    RetryRequested,
    SubproblemAssigned,
    SubproblemFailed,
    make_meta,
    topic_for,
)
from agenten.runtime.event_bus import EventBus
from config.app_settings import RETRY_POLICY

# CircuitStateChanged is about an agent_type, not any one problem, so its
# meta has no real root problem to point at. Use this sentinel rather than
# smuggling the agent_type into root_problem_id, which would corrupt any
# downstream correlation-by-root-problem.
CIRCUIT_ROOT_ID = "__circuit__"


@dataclass
class _CircuitInfo:
    """Rolling per-agent_type circuit-breaker bookkeeping.

    `outcomes` is a required argument: the only constructor is
    `SupervisorAgent._circuit_info`, which supplies a deque bounded to
    `circuit_window` — no default here, so nothing can accidentally build
    an unbounded window.
    """

    outcomes: Deque[bool]  # True == failure
    state: CircuitState = "closed"
    opened_at: Optional[float] = None


class SupervisorAgent:
    """Tracks retry attempts per subproblem and circuit-breaker state per
    agent_type, in-memory, driven purely by events published onto an
    `EventBus`. Has no AutoGen dependency of its own — see
    `SupervisorRoutedAgent` at the bottom of this module for the thin
    AutoGen Core adapter (only importable if `autogen_core` is installed).

    Circuit state machine: closed -> open when the failure fraction in the
    rolling window exceeds `circuit_failure_threshold`; open -> half_open
    after `circuit_half_open_after_seconds` (checked lazily on next access,
    no background timer); half_open -> closed on a success, half_open ->
    open on a failure. Every transition publishes `CircuitStateChanged` and
    clears the rolling window so the next state is judged on fresh data,
    not outcomes accumulated under the previous state.
    """

    def __init__(
        self,
        bus: EventBus,
        max_retries: Optional[int] = None,
        backoff_base: float = 2.0,
        circuit_failure_threshold: float = 0.5,
        circuit_window: int = 20,
        circuit_half_open_after_seconds: float = 60.0,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._bus = bus
        self._max_retries = (
            max_retries if max_retries is not None else RETRY_POLICY["max_retries"]
        )
        self._backoff_base = backoff_base
        self._circuit_failure_threshold = circuit_failure_threshold
        self._circuit_window = circuit_window
        self._circuit_half_open_after_seconds = circuit_half_open_after_seconds
        self._now = now

        # subproblem_id -> number of retries already granted for it.
        # Entries are evicted on escalation (terminal for this supervisor).
        self._attempts: Dict[str, int] = {}
        # subproblem_id -> agent_type, learned from SubproblemAssigned so
        # SubproblemFailed (which lacks agent_type) can be attributed.
        # Evicted on escalation alongside the attempt count.
        self._assigned_agent_type: Dict[str, str] = {}
        # agent_type -> rolling outcome window + circuit state.
        self._circuits: Dict[str, _CircuitInfo] = {}

    # ------------------------------------------------------------------
    # Retry / backoff / escalation (shared by handle_failed and
    # handle_lease_expired).
    # ------------------------------------------------------------------

    async def _handle_retriable_outcome(
        self,
        subproblem_id: str,
        retriable: bool,
        error: str,
        meta: EventMeta,
    ) -> None:
        attempt = self._attempts.get(subproblem_id, 0)

        def child_meta(next_attempt: int) -> EventMeta:
            return make_meta(
                correlation_id=subproblem_id,
                root_problem_id=meta.root_problem_id,
                attempt=next_attempt,
                constitution_version=meta.constitution_version,
            )

        if not retriable or attempt >= self._max_retries:
            if not retriable:
                reason = f"non-retriable failure for subproblem {subproblem_id}: {error}"
            else:
                reason = (
                    f"subproblem {subproblem_id} exceeded max_retries "
                    f"({self._max_retries}): {error}"
                )
            # Escalation is terminal from this supervisor's point of view
            # (the decomposer re-plans); drop per-subproblem state so these
            # dicts don't grow without bound in a long-lived process.
            self._attempts.pop(subproblem_id, None)
            self._assigned_agent_type.pop(subproblem_id, None)
            escalate = EscalateToRedecompose(
                meta=child_meta(attempt),
                subproblem_id=subproblem_id,
                reason=reason,
            )
            await self._bus.publish(topic_for(EscalateToRedecompose), escalate)
            return

        delay = self._backoff_base ** attempt
        self._attempts[subproblem_id] = attempt + 1
        retry = RetryRequested(
            meta=child_meta(attempt + 1),
            subproblem_id=subproblem_id,
            delay_seconds=delay,
        )
        await self._bus.publish(topic_for(RetryRequested), retry)

    async def handle_assigned(self, event: SubproblemAssigned) -> None:
        """Record which agent_type is working a subproblem, so a later
        `SubproblemFailed` (which has no agent_type field) can be attributed
        to it for circuit purposes.
        """
        self._assigned_agent_type[event.subproblem_id] = event.agent_type

    async def handle_failed(self, event: SubproblemFailed) -> None:
        """Retry/backoff/escalate accounting for a subproblem failure, plus
        a circuit failure outcome for the agent_type last seen assigned to
        this subproblem (best-effort: no-op if no assignment was observed —
        see module docstring).
        """
        # Look the assignment up BEFORE retry handling: escalation evicts it.
        agent_type = self._assigned_agent_type.get(event.subproblem_id)
        await self._handle_retriable_outcome(
            subproblem_id=event.subproblem_id,
            retriable=event.retriable,
            error=event.error,
            meta=event.meta,
        )
        if agent_type is not None:
            await self._record_circuit_outcome(agent_type, failure=True)

    async def handle_lease_expired(self, event: LeaseExpired) -> None:
        """A lease expiry is treated exactly like a retriable
        `SubproblemFailed` for retry/backoff/escalation purposes, and it
        carries `agent_type` directly, so it always drives circuit state.
        """
        await self._handle_retriable_outcome(
            subproblem_id=event.subproblem_id,
            retriable=True,
            error=f"lease expired for agent_type={event.agent_type} agent_key={event.agent_key}",
            meta=event.meta,
        )
        await self._record_circuit_outcome(event.agent_type, failure=True)

    # ------------------------------------------------------------------
    # Circuit breaker.
    # ------------------------------------------------------------------

    async def record_success(self, agent_type: str) -> None:
        """Record a success outcome for `agent_type`'s rolling window. A
        success while the circuit is half_open closes it (publishing
        `CircuitStateChanged(state="closed")`).

        This is a plain method, not an event handler: `SupervisorAgent`
        does not subscribe to `SubproblemCompleted` in this unit's scope.
        Wiring `record_success` to fire on `SubproblemCompleted` is the
        integration unit's (U11's) job. It is async because closing the
        circuit publishes an event.
        """
        await self._record_circuit_outcome(agent_type, failure=False)

    async def _record_circuit_outcome(self, agent_type: str, failure: bool) -> None:
        info = self._circuit_info(agent_type)
        # Time-check on access: a stale "open" circuit past its timeout
        # becomes "half_open" before this outcome is evaluated.
        await self._maybe_open_to_half_open(agent_type, info)

        info.outcomes.append(failure)

        if failure:
            if (
                info.state in ("closed", "half_open")
                and self._failure_fraction(info) > self._circuit_failure_threshold
            ):
                await self._set_circuit_state(agent_type, info, "open")
        elif info.state == "half_open":
            # Probe succeeded while on probation: close the circuit.
            await self._set_circuit_state(agent_type, info, "closed")

    async def circuit_state(self, agent_type: str) -> CircuitState:
        """Current circuit state for `agent_type`, applying the half-open
        timeout check on access (see class docstring: no background timer).
        Async because that check can publish `CircuitStateChanged`.
        """
        info = self._circuit_info(agent_type)
        await self._maybe_open_to_half_open(agent_type, info)
        return info.state

    def _circuit_info(self, agent_type: str) -> _CircuitInfo:
        info = self._circuits.get(agent_type)
        if info is None:
            info = _CircuitInfo(outcomes=deque(maxlen=self._circuit_window))
            self._circuits[agent_type] = info
        return info

    def _failure_fraction(self, info: _CircuitInfo) -> float:
        if not info.outcomes:
            return 0.0
        return sum(1 for outcome in info.outcomes if outcome) / len(info.outcomes)

    async def _maybe_open_to_half_open(self, agent_type: str, info: _CircuitInfo) -> None:
        if info.state != "open" or info.opened_at is None:
            return
        if self._now() - info.opened_at >= self._circuit_half_open_after_seconds:
            await self._set_circuit_state(agent_type, info, "half_open")

    async def _set_circuit_state(
        self, agent_type: str, info: _CircuitInfo, state: CircuitState
    ) -> None:
        info.state = state
        info.opened_at = self._now() if state == "open" else None
        # Fresh window per state: the new state is judged on outcomes that
        # happen under it, not on the stale outcomes that caused (or
        # preceded) the transition. Without this, a half_open trial would
        # be instantly re-tripped by the very failures that opened the
        # circuit in the first place.
        info.outcomes.clear()
        changed = CircuitStateChanged(
            meta=make_meta(
                correlation_id=agent_type,
                root_problem_id=CIRCUIT_ROOT_ID,
            ),
            agent_type=agent_type,
            state=state,
        )
        await self._bus.publish(topic_for(CircuitStateChanged), changed)


# ----------------------------------------------------------------------
# Optional AutoGen Core adapter. Gated so this module imports cleanly with
# autogen_core absent (e.g. in the unit-test environment for this unit).
# ----------------------------------------------------------------------
try:
    import autogen_core
    from autogen_core import MessageContext, RoutedAgent, message_handler
except ImportError:  # pragma: no cover - exercised by "no autogen_core" tests
    autogen_core = None


if autogen_core is not None:  # pragma: no cover - requires autogen_core installed

    class SupervisorRoutedAgent(RoutedAgent):
        """Thin AutoGen Core adapter forwarding message handlers into a
        plain `SupervisorAgent`. Real topic subscriptions (TypeSubscription)
        are wired up by the orchestration unit (U11), not here.
        """

        def __init__(self, supervisor: SupervisorAgent, description: str = "Supervisor agent") -> None:
            super().__init__(description)
            self._supervisor = supervisor

        @message_handler
        async def on_subproblem_assigned(
            self, message: SubproblemAssigned, ctx: MessageContext
        ) -> None:
            await self._supervisor.handle_assigned(message)

        @message_handler
        async def on_subproblem_failed(
            self, message: SubproblemFailed, ctx: MessageContext
        ) -> None:
            await self._supervisor.handle_failed(message)

        @message_handler
        async def on_lease_expired(self, message: LeaseExpired, ctx: MessageContext) -> None:
            await self._supervisor.handle_lease_expired(message)

else:
    SupervisorRoutedAgent = None
