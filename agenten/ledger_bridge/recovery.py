"""Startup recovery: the ledger-driven replacement for "the event bus
remembers what was in flight".

AutoGen's own distributed runtime does NOT persist its task queue across a
restart (in-memory only — see GitHub issue #5327). The blockchain ledger is
this system's actual durability boundary: every meaningful transition is a
Block before anything downstream is allowed to act on it. That means the
correct way to resume after a crash/restart is never "replay whatever the
bus buffered" (there is nothing to replay - InMemoryEventBus/AutoGenEventBus
hold no durable state) but "ask the ledger what is stuck in a non-terminal
stage, and re-publish the event that should still be pending for it".

This module has no AutoGen import — it is a plain async function, called
once at process startup by the integration unit (U11), operating purely
over the EventBus/LedgerQuery ports so it stays importable and unit-testable
without autogen_core installed.
"""
import time
from typing import Callable, Dict

from agenten.events.schemas import (
    LeaseExpired,
    SubproblemAccepted,
    SubproblemProposed,
    make_meta,
    topic_for,
)
from agenten.ledger_bridge.stage_machine import TERMINAL_STAGES, LedgerQuery, Stage
from agenten.runtime.event_bus import EventBus

# Stages this routine actively re-derives an event for. Anything else that
# is non-terminal (computed generically below via TERMINAL_STAGES, so this
# stays correct even if stage_machine.py grows new non-terminal stages we
# don't yet know how to recover, e.g. Stage.VERIFYING today) is scanned but
# not touched, and is flagged in the summary instead of silently ignored —
# recovery must never fabricate behavior for a stage it doesn't understand,
# but it also must never pretend nothing was there.
_QUEUED_OR_VALIDATING = {Stage.QUEUED, Stage.VALIDATING}
_ASSIGNED_OR_IN_PROGRESS = {Stage.ASSIGNED, Stage.IN_PROGRESS}


def _recovery_attempt(block) -> int:
    """How many times this block has already been through a recovery pass.

    U9 has no ledger write access, so it cannot increment this counter
    itself — it only reads whatever the Ledger Recorder (U8) has already
    stamped into block.metadata, and stamps that number into the attempt
    field of the event it re-publishes.
    """
    return block.metadata.get("recovery_attempts", 0)


def _meta_for(block, ts: float):
    subproblem_id = block.data.get("subproblem_id")
    root_problem_id = block.data.get("root_problem_id", "")
    return make_meta(
        correlation_id=subproblem_id,
        root_problem_id=root_problem_id,
        attempt=_recovery_attempt(block),
        ts=ts,
    )


async def recover_on_startup(
    bus: EventBus,
    ledger_query: LedgerQuery,
    now: Callable[[], float] = time.time,
) -> Dict[str, int]:
    """Scan every non-terminal stage and re-publish whatever event is needed
    to get each stuck block moving again. See module docstring for why this
    has to be ledger-driven rather than bus-driven.

    Returns a summary dict counting how many blocks were recovered/flagged
    per bucket, useful for startup logging and for the e2e crash-recovery
    integration test.
    """
    summary: Dict[str, int] = {
        "queued_or_validating": 0,
        "accepted": 0,
        "lease_expired": 0,
        "stuck_retrying_flagged": 0,
        "lease_missing_flagged": 0,
        "unhandled_stage_flagged": 0,
    }

    non_terminal_stages = [stage for stage in Stage if stage not in TERMINAL_STAGES]
    recovery_ts = now()

    for stage in non_terminal_stages:
        blocks = ledger_query.blocks_in_stage(stage)

        if stage in _QUEUED_OR_VALIDATING:
            for block in blocks:
                event = SubproblemProposed(
                    meta=_meta_for(block, recovery_ts),
                    subproblem_id=block.data.get("subproblem_id"),
                    parent_id=block.data.get("parent_subproblem_id"),
                    depth=block.data.get("depth", 0),
                    description=block.data.get("description"),
                    capability_tags=block.data.get("capability_tags", []),
                    atomic=block.data.get("atomic", False),
                )
                await bus.publish(topic_for(SubproblemProposed), event)
                summary["queued_or_validating"] += 1

        elif stage is Stage.ACCEPTED:
            for block in blocks:
                event = SubproblemAccepted(
                    meta=_meta_for(block, recovery_ts),
                    subproblem_id=block.data.get("subproblem_id"),
                    block_index=block.index,
                )
                await bus.publish(topic_for(SubproblemAccepted), event)
                summary["accepted"] += 1

        elif stage in _ASSIGNED_OR_IN_PROGRESS:
            for block in blocks:
                if block.block_type != "subproblem":
                    # The root "problem" block itself is written at
                    # Stage.IN_PROGRESS and, by the Ledger Recorder's own
                    # design (see recorder.py's _write_problem_block), never
                    # transitions again -- it sits at IN_PROGRESS for the
                    # entire lifetime of the ledger, on purpose, and never
                    # carries a lease. Without this guard every recovery
                    # pass against any ledger that has ever seen a
                    # ProblemSubmitted would permanently flag it as
                    # "lease_missing" (see the branch below), which isn't a
                    # real anomaly and would drown out genuine signal.
                    # Only actual subproblem blocks are lease-bearing work
                    # this routine needs to reason about here.
                    continue
                lease_expires_at = block.metadata.get("lease_expires_at")
                if lease_expires_at is None:
                    # A block genuinely shouldn't reach ASSIGNED/IN_PROGRESS
                    # without a lease being stamped by the Coordinator/
                    # Ledger Recorder - this is a data anomaly, not the
                    # normal "still in flight" case. We still must not
                    # fabricate a LeaseExpired for it (we don't know who,
                    # if anyone, holds it), but per this module's rule of
                    # never silently dropping stuck non-terminal blocks
                    # (see RETRYING/unhandled-stage handling below), flag
                    # it for operator visibility instead of vanishing it.
                    summary["lease_missing_flagged"] += 1
                elif lease_expires_at < recovery_ts:
                    event = LeaseExpired(
                        meta=_meta_for(block, recovery_ts),
                        subproblem_id=block.data.get("subproblem_id"),
                        agent_type=block.data.get("agent_type"),
                        agent_key=block.data.get("agent_key"),
                    )
                    await bus.publish(topic_for(LeaseExpired), event)
                    summary["lease_expired"] += 1
                # else: the lease has not expired yet. A live worker may
                # genuinely still be working this subproblem - recovery
                # must not double-assign work that's still legitimately in
                # flight, so we leave it alone and let the Reaper's normal
                # watchdog poll handle it if it later expires.

        elif stage is Stage.RETRYING:
            # RETRYING is transient Coordinator bookkeeping: it means a
            # RetryRequested was already handled and the block is on its
            # way back to ASSIGNED through the normal flow (see
            # ALLOWED_TRANSITIONS: RETRYING -> {ASSIGNED, FAILED}).
            # Recovery doesn't need to (and can't, since there's no event
            # that means "resume retrying") re-derive anything for it. But
            # a block that is STILL sitting in RETRYING at startup means
            # that normal flow never completed - either the Coordinator
            # crashed mid-transition or there's a bug elsewhere - so we
            # flag it in the summary rather than silently ignoring it.
            summary["stuck_retrying_flagged"] += len(blocks)

        else:
            # Any other non-terminal stage stage_machine.py knows about
            # that this routine doesn't (e.g. Stage.VERIFYING) has no
            # recovery behavior defined here. Rather than silently drop
            # blocks stuck there, flag them for operator visibility.
            summary["unhandled_stage_flagged"] += len(blocks)

    return summary
