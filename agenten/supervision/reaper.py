"""Reaper agent: lease/heartbeat watchdog (unit U7).

AutoGen's own runtime does not persist in-flight work across restarts, and
nothing else in the pipeline notices when a worker agent dies (crash, OOM,
host restart) between being assigned a subproblem and ever publishing
anything back — the ledger record is then stuck at Stage.ASSIGNED (or
Stage.IN_PROGRESS, if it managed to start before dying) forever, with no
other signal that anything went wrong. The ReaperAgent closes that gap by
periodically polling the ledger's read side for blocks whose lease has
expired and publishing a `LeaseExpired` event for each one, so a downstream
retry/escalation unit (see agenten/events/schemas.py: RetryRequested,
EscalateToRedecompose) can act on it.

This module has no AutoGen import — it depends only on the EventBus port
and the LedgerQuery port (both from unit U0), so it is importable and
unit-testable with zero AutoGen installed.
"""
import asyncio
import time
from typing import Callable, List, Set, Tuple

from agenten.events.schemas import LeaseExpired, make_meta, topic_for
from agenten.ledger_bridge.stage_machine import LedgerQuery, Stage
from agenten.runtime.event_bus import EventBus

_LEASE_STAGES = (Stage.ASSIGNED, Stage.IN_PROGRESS)


class ReaperAgent:
    """Polls the ledger for stuck subproblems and announces expired leases.

    Idempotency has two layers of defense: consumers are expected to
    de-dupe on `EventMeta.event_id` per the shared contract (delivery is
    at-least-once, never exactly-once — see EventBus.publish), but this
    agent additionally tracks which (block index, lease deadline) pairs it
    has already flagged so a stuck block does not get a fresh
    `LeaseExpired` event blasted onto the bus on every single poll for as
    long as it stays stuck. Keying on the lease deadline too (not just the
    block index) matters because the underlying ledger reuses a block's
    index across retries (see Blockchain.update_task_status) rather than
    appending a new block per transition: if a subproblem is reassigned
    with a fresh lease after a retry and *that* lease also expires, it's a
    new incident and must be re-flagged, not silently swallowed because
    the block index was already in the seen set from the earlier expiry.
    """

    def __init__(
        self,
        bus: EventBus,
        ledger_query: LedgerQuery,
        poll_interval_seconds: float = 15.0,
        now: Callable[[], float] = time.time,
    ):
        self.bus = bus
        self.ledger_query = ledger_query
        self.poll_interval_seconds = poll_interval_seconds
        self.now = now
        self._reaped_leases: Set[Tuple[int, float]] = set()

    async def scan_once(self) -> List[LeaseExpired]:
        """Scan for blocks with an expired lease and publish LeaseExpired
        for each newly-discovered one. Returns the events actually
        published this call (empty if nothing newly expired)."""
        current_time = self.now()
        published: List[LeaseExpired] = []

        for stage in _LEASE_STAGES:
            for block in self.ledger_query.blocks_in_stage(stage):
                lease_expires_at = block.metadata.get("lease_expires_at")
                if lease_expires_at is None or lease_expires_at >= current_time:
                    continue
                lease_key = (block.index, lease_expires_at)
                if lease_key in self._reaped_leases:
                    continue

                subproblem_id = block.data.get("subproblem_id")
                agent_type = block.data.get("agent_type")
                agent_key = block.data.get("agent_key")

                # Prefer an explicit root_problem_id if the writer stashed
                # one on the block; fall back to the subproblem_id itself.
                # NOTE: this fallback is a known limitation — without a
                # true root_problem_id we can't distinguish "top-level
                # problem" from "subproblem" for correlation purposes
                # upstream of this event, so consumers that need the real
                # root should look it up via the subproblem_id instead of
                # trusting root_problem_id blindly in that case.
                root_problem_id = (
                    block.data.get("root_problem_id")
                    or block.metadata.get("root_problem_id")
                    or subproblem_id
                )

                event = LeaseExpired(
                    meta=make_meta(
                        correlation_id=subproblem_id,
                        root_problem_id=root_problem_id,
                    ),
                    subproblem_id=subproblem_id,
                    agent_type=agent_type,
                    agent_key=agent_key,
                )

                await self.bus.publish(topic_for(LeaseExpired), event)
                self._reaped_leases.add(lease_key)
                published.append(event)

        return published

    async def run_forever(self) -> None:
        """Poll forever until the enclosing task is cancelled.

        Intended usage: `task = asyncio.create_task(reaper.run_forever())`
        and later `task.cancel()`. asyncio.CancelledError is allowed to
        propagate normally (not swallowed), so cancellation actually stops
        the loop instead of being silently absorbed.
        """
        while True:
            await self.scan_once()
            await asyncio.sleep(self.poll_interval_seconds)


try:
    import autogen_core  # noqa: F401
except ImportError:
    autogen_core = None

# Integration note (unit U11 wires this up for real): ReaperAgent.run_forever()
# is a polling loop, not a message handler, so there is no natural
# RoutedAgent/@message_handler adapter for it the way there is for
# event-driven units. Once the real AutoGen runtime exists, a ReaperAgent
# instance would simply be constructed with the live AutoGenEventBus-backed
# EventBus and LedgerQuery, and scheduled as a background task at process
# startup, e.g.:
#
#     reaper = ReaperAgent(bus=autogen_event_bus, ledger_query=ledger_query)
#     reaper_task = asyncio.create_task(reaper.run_forever())
#
# alongside (not instead of) the runtime's own agent registration, and
# cancelled on graceful shutdown via `reaper_task.cancel()`.
