"""Spawn Coordinator: turns a ledger-finalized accepted subproblem into a
concrete worker assignment.

Resolves the subproblem's ``capability_tags`` (read off the ledger block
the Ledger Recorder, unit U8, wrote) to an agent TYPE via
``CapabilityRegistry``, applies backpressure/circuit-breaker checks against
the ledger's in-flight counts, and either publishes ``SubproblemAssigned``
or defers the attempt via ``RetryRequested``.

A ``RetryRequested`` with a positive delay is scheduled as a background
task instead of sleeping inside the handler. On ``InMemoryEventBus`` a
handler runs nested in whoever published, which is usually the Ledger
Recorder's writer loop: an inline backoff would stall every ledger write and
serialize all subproblems behind one retry. ``cancel_pending_retries`` (called
by the pipeline's ``stop``) and ``wait_for_pending_retries`` manage them.

``agent_key = subproblem_id`` is the detail that matters most downstream:
it is what gives AutoGen's ``AgentId(agent_type, agent_key)`` addressing an
on-demand, per-subproblem agent *instance* rather than one shared instance
per agent type (unit U11 wires the ``RoutedAgent`` adapter below into real
AutoGen Core pub/sub).

This module has no hard dependency on ``autogen_core`` — the business logic
in ``SpawnCoordinatorAgent`` is plain asyncio/dataclasses so it stays
importable and unit-testable without AutoGen installed. The optional
``RoutedSpawnCoordinatorAgent`` adapter at the bottom is gated behind an
``ImportError``-tolerant import.
"""
import asyncio
import logging
import time
from collections import deque
from typing import Callable, Deque, Dict, List, Set

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
from agenten.runtime.event_bus import EventBus
from agenten.spawning.capability_registry import CapabilityRegistry, NoCapableAgentType

logger = logging.getLogger(__name__)

# Backoff used when we defer an assignment for backpressure/circuit reasons
# rather than an actual worker failure. Backpressure gets a short retry
# (capacity likely frees up soon); an open circuit gets a longer one (the
# whole agent type is unhealthy, no point hammering it).
BACKPRESSURE_RETRY_DELAY_SECONDS = 5.0
CIRCUIT_OPEN_RETRY_DELAY_SECONDS = 30.0

# Stages that count as "an agent is already occupied with this subproblem"
# for the purposes of the max_in_flight_per_type cap.
_IN_FLIGHT_STAGES = (Stage.ASSIGNED, Stage.IN_PROGRESS)

# How many recent EventMeta.event_ids to remember for duplicate-delivery
# detection (see _already_processed below). Bounded so a long-running
# coordinator doesn't grow this set without limit.
_PROCESSED_EVENT_ID_HISTORY = 10_000


class SpawnCoordinatorAgent:
    """AutoGen-agnostic business logic for the spawn-coordination step of
    the pipeline. See ``RoutedSpawnCoordinatorAgent`` below for the AutoGen
    Core adapter that forwards into an instance of this class.
    """

    def __init__(
        self,
        bus: EventBus,
        registry: CapabilityRegistry,
        ledger_query: LedgerQuery,
        max_in_flight_per_type: int = 20,
        lease_duration_seconds: float = 120.0,
        now: Callable[[], float] = time.time,
    ):
        self._bus = bus
        self._registry = registry
        self._ledger_query = ledger_query
        self._max_in_flight_per_type = max_in_flight_per_type
        self._lease_duration_seconds = lease_duration_seconds
        self._now = now

        # Per-agent_type circuit-breaker state, updated by
        # handle_circuit_state_changed. Single-process, in-memory only: a
        # multi-process/distributed deployment (multiple coordinator
        # replicas) would need this shared (e.g. Redis) since each replica
        # would otherwise have its own, possibly stale, view of which agent
        # types are open. Out of scope for this unit.
        self._circuit_state: Dict[str, str] = {}
        # agent_type -> earliest time one probe may be assigned while the
        # circuit is open; removed once that probe is sent, so the rest wait
        # for the supervisor's verdict on it.
        self._circuit_probe_at: Dict[str, float] = {}

        # EventBus.publish documents at-least-once (not exactly-once)
        # delivery, and requires handlers to be idempotent w.r.t.
        # EventMeta.event_id. Without this guard, redelivery of the same
        # SubproblemAccepted/RetryRequested could resolve+publish a second
        # SubproblemAssigned for a subproblem that's already been assigned,
        # double-counting backpressure and tripping the ledger's
        # ASSIGNED -> ASSIGNED illegal-transition check downstream.
        self._processed_event_ids: Set[str] = set()
        self._processed_event_id_order: Deque[str] = deque(maxlen=_PROCESSED_EVENT_ID_HISTORY)

        # Retries waiting out their backoff. Held strongly so the event loop
        # cannot garbage-collect a pending task; removed when it finishes.
        self._pending_retries: Set["asyncio.Task[None]"] = set()

    async def handle_subproblem_accepted(self, event: SubproblemAccepted) -> None:
        if self._already_processed(event.meta.event_id):
            return
        if event.block_index is None:
            # Only the Ledger Recorder's re-published SubproblemAccepted
            # (with a real block_index once the block is actually written)
            # is actionable. The Gatekeeper's earlier tentative verdict
            # carries block_index=None and is not finalized in the ledger
            # yet — ignore it defensively in case both ever reach us; in
            # the intended wiring only the Recorder's event reaches this
            # handler.
            logger.debug(
                "Ignoring SubproblemAccepted with block_index=None for subproblem_id=%s (not yet finalized)",
                event.subproblem_id,
            )
            return
        await self._attempt_assignment(
            subproblem_id=event.subproblem_id,
            block_index=event.block_index,
            root_problem_id=event.meta.root_problem_id,
            attempt=event.meta.attempt,
        )

    async def handle_retry_requested(self, event: RetryRequested) -> None:
        if self._already_processed(event.meta.event_id):
            return
        if event.delay_seconds <= 0:
            await self._retry(event)
            return
        task = asyncio.ensure_future(self._retry_after_delay(event))
        self._pending_retries.add(task)
        task.add_done_callback(self._pending_retries.discard)

    @property
    def pending_retry_count(self) -> int:
        return len(self._pending_retries)

    async def wait_for_pending_retries(self) -> None:
        """Wait until no retry is waiting out its backoff, including retries
        that the awaited ones schedule in turn."""
        while self._pending_retries:
            await asyncio.gather(*list(self._pending_retries), return_exceptions=True)

    async def cancel_pending_retries(self) -> None:
        """Cancel every retry still waiting out its backoff (shutdown)."""
        pending = list(self._pending_retries)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _retry_after_delay(self, event: RetryRequested) -> None:
        await asyncio.sleep(event.delay_seconds)
        try:
            await self._retry(event)
        except Exception:  # noqa: BLE001 - a background task has no caller to report to
            logger.exception("Delayed retry for subproblem_id=%s failed", event.subproblem_id)

    async def _retry(self, event: RetryRequested) -> None:
        block = self._find_block_for_subproblem(event.subproblem_id)
        if block is None:
            logger.warning(
                "RetryRequested for subproblem_id=%s but no matching ledger block was found; skipping",
                event.subproblem_id,
            )
            return

        await self._attempt_assignment(
            subproblem_id=event.subproblem_id,
            block_index=block.index,
            root_problem_id=event.meta.root_problem_id,
            attempt=event.meta.attempt,
        )

    async def handle_circuit_state_changed(self, event: CircuitStateChanged) -> None:
        self._circuit_state[event.agent_type] = event.state
        if event.state == "open" and event.probe_after_seconds is not None:
            self._circuit_probe_at[event.agent_type] = self._now() + event.probe_after_seconds
        else:
            self._circuit_probe_at.pop(event.agent_type, None)

    # -- internals ---------------------------------------------------------

    def _already_processed(self, event_id: str) -> bool:
        """Returns False (and records event_id) the first time it's called
        with a given event_id. Returns True on every later call with that
        same event_id, so at-least-once bus redelivery of a
        SubproblemAccepted/RetryRequested is a no-op the second time.
        Bounded history (see _PROCESSED_EVENT_ID_HISTORY): a duplicate
        arriving after more than that many other events have been processed
        would not be caught, which is an acceptable tradeoff for a simple
        in-memory guard versus persisting a full dedup log.
        """
        if event_id in self._processed_event_ids:
            logger.debug("Ignoring duplicate delivery of event_id=%s", event_id)
            return True
        if len(self._processed_event_id_order) == self._processed_event_id_order.maxlen:
            oldest = self._processed_event_id_order.popleft()
            self._processed_event_ids.discard(oldest)
        self._processed_event_ids.add(event_id)
        self._processed_event_id_order.append(event_id)
        return False

    async def _attempt_assignment(
        self, subproblem_id: str, block_index: int, root_problem_id: str, attempt: int
    ) -> None:
        """Shared resolve + backpressure + circuit-check + publish logic
        used by both handle_subproblem_accepted and (after its delay)
        handle_retry_requested.
        """
        block = self._ledger_query.get_block(block_index)
        if block is None:
            logger.warning(
                "No ledger block at index=%s for subproblem_id=%s; skipping assignment",
                block_index,
                subproblem_id,
            )
            return

        capability_tags: List[str] = list((block.data or {}).get("capability_tags", []))

        try:
            agent_type = self._registry.resolve(capability_tags)
        except NoCapableAgentType as exc:
            logger.error(
                "No capable agent type for subproblem_id=%s capability_tags=%s: %s",
                subproblem_id,
                capability_tags,
                exc,
            )
            await self._bus.publish(
                topic_for(SubproblemUnroutable),
                SubproblemUnroutable(
                    meta=make_meta(
                        correlation_id=subproblem_id,
                        root_problem_id=root_problem_id,
                        attempt=attempt,
                    ),
                    subproblem_id=subproblem_id,
                    capability_tags=capability_tags,
                    error=f"No capable agent type: {exc}",
                ),
            )
            return

        if self._circuit_state.get(agent_type) == "open" and not self._take_circuit_probe(agent_type):
            await self._defer(
                subproblem_id=subproblem_id,
                root_problem_id=root_problem_id,
                attempt=attempt,
                delay_seconds=CIRCUIT_OPEN_RETRY_DELAY_SECONDS,
                reason=f"circuit open for agent_type={agent_type!r}",
            )
            return

        in_flight = self._in_flight_count(agent_type)
        if in_flight >= self._max_in_flight_per_type:
            await self._defer(
                subproblem_id=subproblem_id,
                root_problem_id=root_problem_id,
                attempt=attempt,
                delay_seconds=BACKPRESSURE_RETRY_DELAY_SECONDS,
                reason=f"at capacity for agent_type={agent_type!r} ({in_flight}/{self._max_in_flight_per_type})",
            )
            return

        lease_expires_at = self._now() + self._lease_duration_seconds
        assigned = SubproblemAssigned(
            meta=make_meta(correlation_id=subproblem_id, root_problem_id=root_problem_id, attempt=attempt),
            subproblem_id=subproblem_id,
            agent_type=agent_type,
            # subproblem_id doubles as the agent_key: this is what gives
            # AutoGen's AgentId(agent_type, agent_key) an on-demand agent
            # *instance* per subproblem rather than one shared instance per
            # agent type.
            agent_key=subproblem_id,
            lease_expires_at=lease_expires_at,
        )
        await self._bus.publish(topic_for(SubproblemAssigned), assigned)

    def _take_circuit_probe(self, agent_type: str) -> bool:
        probe_at = self._circuit_probe_at.get(agent_type)
        if probe_at is None or self._now() < probe_at:
            return False
        del self._circuit_probe_at[agent_type]
        logger.info("Circuit open for agent_type=%r: sending one probe assignment", agent_type)
        return True

    async def _defer(
        self, subproblem_id: str, root_problem_id: str, attempt: int, delay_seconds: float, reason: str
    ) -> None:
        logger.info("Deferring assignment for subproblem_id=%s: %s", subproblem_id, reason)
        # NOTE: RetryRequested is normally a failure-driven signal ("the
        # worker failed, try again"). We deliberately reuse it here as a
        # generic backpressure/circuit-breaker deferral signal too, so the
        # existing retry loop (a sibling Supervisor unit) re-triggers a
        # later attempt without introducing a second, near-duplicate event
        # type for "try again later, but not because of a failure".
        retry = RetryRequested(
            meta=make_meta(correlation_id=subproblem_id, root_problem_id=root_problem_id, attempt=attempt),
            subproblem_id=subproblem_id,
            delay_seconds=delay_seconds,
        )
        await self._bus.publish(topic_for(RetryRequested), retry)

    def _in_flight_count(self, agent_type: str) -> int:
        """Number of blocks currently ASSIGNED or IN_PROGRESS for the given
        agent_type. ledger_query.count_in_stage(stage) alone isn't
        agent-type-specific, so this filters blocks_in_stage(...) locally
        by block.data.get("agent_type") instead.
        """
        count = 0
        for stage in _IN_FLIGHT_STAGES:
            for block in self._ledger_query.blocks_in_stage(stage):
                if (block.data or {}).get("agent_type") == agent_type:
                    count += 1
        return count

    def _find_block_for_subproblem(self, subproblem_id: str):
        """RetryRequested only carries a subproblem_id, not a block_index,
        so look the block back up by scanning every stage for a matching
        subproblem_id — including RETRYING (a failure-driven RetryRequested
        from the Supervisor arrives after the Recorder has moved the block
        there) and the terminal stages (defensively, in case a stale/
        duplicate RetryRequested arrives after the subproblem has already
        finished). O(active blocks); the ledger read-side is expected to be
        indexed per unit (see agenten/ledger_bridge/stage_machine.py's
        LedgerQuery docstring), so this stays cheap in practice.
        """
        for stage in Stage:
            for block in self._ledger_query.blocks_in_stage(stage):
                if (block.data or {}).get("subproblem_id") == subproblem_id:
                    return block
        return None


# --- Optional AutoGen Core adapter ----------------------------------------
#
# Kept import-tolerant so agenten/spawning/coordinator.py (and therefore
# SpawnCoordinatorAgent) stays usable in environments without autogen_core
# installed, e.g. this repo's current test environment. Real Topic/
# TypeSubscription wiring for RoutedSpawnCoordinatorAgent happens in unit
# U11 (agenten/orchestration/pipeline.py) — this class only proves the
# forwarding seam.
try:
    import autogen_core
    from autogen_core import MessageContext, RoutedAgent, message_handler
except ImportError:  # pragma: no cover - exercised by environments without autogen_core
    autogen_core = None
    MessageContext = None  # type: ignore
    RoutedAgent = None  # type: ignore
    message_handler = None  # type: ignore


if autogen_core is not None:

    class RoutedSpawnCoordinatorAgent(RoutedAgent):
        """Thin AutoGen Core RoutedAgent adapter that forwards each message
        type into the corresponding SpawnCoordinatorAgent handler. Holds no
        business logic of its own.
        """

        def __init__(self, description: str, coordinator: SpawnCoordinatorAgent):
            super().__init__(description)
            self._coordinator = coordinator

        @message_handler
        async def on_subproblem_accepted(self, message: SubproblemAccepted, ctx: MessageContext) -> None:
            await self._coordinator.handle_subproblem_accepted(message)

        @message_handler
        async def on_retry_requested(self, message: RetryRequested, ctx: MessageContext) -> None:
            await self._coordinator.handle_retry_requested(message)

        @message_handler
        async def on_circuit_state_changed(self, message: CircuitStateChanged, ctx: MessageContext) -> None:
            await self._coordinator.handle_circuit_state_changed(message)

else:
    RoutedSpawnCoordinatorAgent = None  # type: ignore
