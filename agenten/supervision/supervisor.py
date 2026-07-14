"""Supervisor agent: retry/backoff and per-agent-type circuit breaking for
the event-driven supply-chain pipeline.

Scope note on circuit tracking: `SubproblemFailed` carries no `agent_type`
field, so a failure reported through `handle_failed` alone cannot be
attributed to a specific worker type — true per-agent_type circuit coverage
from that path would need `agent_type` to be threaded through from
`SubproblemAssigned` (unit U4), which is out of scope here. This unit
therefore drives the circuit breaker from `handle_lease_expired`, whose
`LeaseExpired` event DOES carry `agent_type`/`agent_key`, and treats that as
the primary circuit signal. `handle_failed` still does full retry/backoff/
escalate accounting, it just doesn't move any circuit's needle.

Scope note on state: circuit-breaker and attempt-count state here is
in-memory and single-process by design. A distributed deployment (multiple
supervisor replicas) would need shared state (e.g. Redis) to agree on
circuit state and attempt counts across processes — that's out of scope for
this unit and deliberately not built here.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, Optional

from agenten.events.schemas import (
    CircuitState,
    CircuitStateChanged,
    EscalateToRedecompose,
    EventMeta,
    LeaseExpired,
    RetryRequested,
    SubproblemFailed,
    make_meta,
    topic_for,
)
from agenten.runtime.event_bus import EventBus
from config.app_settings import RETRY_POLICY


@dataclass
class _CircuitInfo:
    """Rolling per-agent_type circuit-breaker bookkeeping."""

    outcomes: Deque[bool] = field(default_factory=deque)  # True == failure
    state: CircuitState = "closed"
    opened_at: Optional[float] = None


class SupervisorAgent:
    """Tracks retry attempts per subproblem and circuit-breaker state per
    agent_type, in-memory, driven purely by events published onto an
    `EventBus`. Has no AutoGen dependency of its own — see
    `SupervisorRoutedAgent` at the bottom of this module for the thin
    AutoGen Core adapter (only importable if `autogen_core` is installed).
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
        self._attempts: Dict[str, int] = {}
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

        if not retriable or attempt >= self._max_retries:
            if not retriable:
                reason = f"non-retriable failure for subproblem {subproblem_id}: {error}"
            else:
                reason = (
                    f"subproblem {subproblem_id} exceeded max_retries "
                    f"({self._max_retries}): {error}"
                )
            escalate = EscalateToRedecompose(
                meta=make_meta(
                    correlation_id=subproblem_id,
                    root_problem_id=meta.root_problem_id,
                    attempt=attempt,
                    constitution_version=meta.constitution_version,
                ),
                subproblem_id=subproblem_id,
                reason=reason,
            )
            await self._bus.publish(topic_for(EscalateToRedecompose), escalate)
            return

        delay = self._backoff_base ** attempt
        self._attempts[subproblem_id] = attempt + 1
        retry = RetryRequested(
            meta=make_meta(
                correlation_id=subproblem_id,
                root_problem_id=meta.root_problem_id,
                attempt=attempt + 1,
                constitution_version=meta.constitution_version,
            ),
            subproblem_id=subproblem_id,
            delay_seconds=delay,
        )
        await self._bus.publish(topic_for(RetryRequested), retry)

    async def handle_failed(self, event: SubproblemFailed) -> None:
        """Retry/backoff/escalate accounting for a subproblem failure.

        Does NOT touch any circuit-breaker state: `SubproblemFailed` has no
        `agent_type`, so there is nothing to attribute a circuit outcome to
        here (see module docstring). Circuit tracking lives in
        `handle_lease_expired`.
        """
        await self._handle_retriable_outcome(
            subproblem_id=event.subproblem_id,
            retriable=event.retriable,
            error=event.error,
            meta=event.meta,
        )

    async def handle_lease_expired(self, event: LeaseExpired) -> None:
        """A lease expiry is treated exactly like a retriable
        `SubproblemFailed` for retry/backoff/escalation purposes, AND it is
        the primary signal driving per-agent_type circuit-breaker state
        (see module docstring for why `handle_failed` can't do this).
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

    def record_success(self, agent_type: str) -> None:
        """Record a success outcome for `agent_type`'s rolling window.

        This is a plain, synchronous method (it cannot publish events, since
        `EventBus.publish` is async) — it only feeds the rolling window that
        future failure-fraction checks read from. It is not an event
        handler: `SupervisorAgent` does not subscribe to `SubproblemCompleted`
        in this unit's scope. Wiring `record_success` to fire on
        `SubproblemCompleted` is the integration unit's (U11's) job.
        """
        info = self._circuit_info(agent_type)
        info.outcomes.append(False)

    async def _record_circuit_outcome(self, agent_type: str, failure: bool) -> None:
        info = self._circuit_info(agent_type)
        # Time-check on access: a stale "open" circuit past its timeout
        # becomes "half_open" before we evaluate/record this outcome.
        await self._maybe_open_to_half_open(agent_type, info)

        info.outcomes.append(failure)

        if failure and info.state in ("closed", "half_open"):
            fraction = self._failure_fraction(info)
            if fraction > self._circuit_failure_threshold:
                await self._set_circuit_state(agent_type, info, "open")

    async def circuit_state(self, agent_type: str) -> CircuitState:
        """Current circuit state for `agent_type`, applying the half-open
        timeout check on access (see module docstring: no background timer).
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
        changed = CircuitStateChanged(
            meta=make_meta(
                correlation_id=agent_type,
                root_problem_id=agent_type,
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
        async def on_subproblem_failed(
            self, message: SubproblemFailed, ctx: MessageContext
        ) -> None:
            await self._supervisor.handle_failed(message)

        @message_handler
        async def on_lease_expired(self, message: LeaseExpired, ctx: MessageContext) -> None:
            await self._supervisor.handle_lease_expired(message)

else:
    SupervisorRoutedAgent = None
