"""Ledger Recorder — the sole ledger writer — unit U8.

Every other unit that thinks it wants to write to the blockchain is wrong:
it publishes an event instead, and `LedgerRecorderAgent` is the only thing
in the whole fleet that ever calls `Blockchain.add_block` /
`Blockchain.update_task_status`.

Why a single writer matters
----------------------------
`Blockchain.add_block` mutates a *parent* block's `.children` list
in-place (`parent_block.children.append(new_block.index)`) and then
re-saves the *entire* chain. If two coroutines did that concurrently for
siblings of the same parent, one append can be lost (classic
read-modify-write race on the shared list + the "last save wins" file
write), silently corrupting the ledger's tree structure. `asyncio` doesn't
preempt a coroutine mid-`await`, but `add_block`/`update_task_status` are
plain synchronous calls — the race is real the moment two *different*
event handlers (e.g. two `SubproblemProposed` for siblings, handled by two
concurrently-running `asyncio.gather`'d coroutines) both reach a
`blockchain.add_block(..., parent_index=X)` call before either one's
callback stack unwinds past an `await` that would otherwise let the event
loop interleave them.

The fix: every public `handle_xxx` method below is a pure "enqueue and
return" — it does nothing but `await self._inbox.put((self._apply_xxx,
event))`. The actual mutation (`_apply_xxx`, `_transition`, `Blockchain.add_block`/
`update_task_status`) only ever runs inside `_writer_loop`, a single
`asyncio.Task` started by `start()` that pulls one item off `self._inbox`
at a time and fully finishes applying it (including any nested
`self._bus.publish(...)` this unit itself does, e.g. re-publishing
`SubproblemAccepted` or publishing `SubproblemRejected` for budget
exhaustion) before pulling the next. No two ledger writes are ever
in flight at once, no matter how many `handle_xxx` calls race each other
concurrently upstream. This is also exactly why `InProcessBudgetLedger`
(query.py) can get away with an unlocked in-memory counter: `try_reserve`
is only ever called from inside this same writer-loop turn.

Stage-machine note (retriable failures route to RETRYING, not FAILED)
----------------------------------------------------------------------
`Stage.FAILED` is *terminal* in the frozen `agenten.ledger_bridge.stage_machine`
state machine (`TERMINAL_STAGES` includes it, `ALLOWED_TRANSITIONS[Stage.FAILED]`
is empty), while `Stage.RETRYING` is not: `ALLOWED_TRANSITIONS[Stage.ASSIGNED]`
and `ALLOWED_TRANSITIONS[Stage.IN_PROGRESS]` both already include
`Stage.RETRYING` as a legal target alongside `Stage.FAILED`, and
`ALLOWED_TRANSITIONS[Stage.RETRYING]` permits `Stage.ASSIGNED` — the state
machine was designed with exactly this "retriable failure" path in mind.

Because this Recorder is the *sole* ledger writer (see above), it is also
the only unit that can legally make the FAILED-vs-RETRYING call — no other
unit is allowed to touch `.status` at all. So `_apply_subproblem_failed`
routes `SubproblemFailed(retriable=True)` to `Stage.RETRYING` (reserving
`Stage.FAILED` for `retriable=False`, genuinely terminal failures), and
`_apply_lease_expired` treats a lease expiry as inherently retriable by
default and also routes to `Stage.RETRYING` (a lease expiring almost
always means transient infra failure — worker crashed, host restarted —
and `LeaseExpired` carries no `retriable` field to say otherwise; see
`agenten.events.schemas.LeaseExpired`). Once a block is in `RETRYING`, no
new handler is needed to get it re-assigned: the existing
`handle_subproblem_assigned` / `_apply_subproblem_assigned` path already
performs `RETRYING -> ASSIGNED` (a transition `validate_transition`
already permits) the moment the Coordinator re-publishes
`SubproblemAssigned` after a `RetryRequested`-driven backoff. Deciding
*when* to give up retrying (retry-count/backoff policy, publishing
`EscalateToRedecompose`) is the Supervisor's (U6) job, not this unit's —
this unit only records what happened.

Robustness note
----------------
An illegal transition (or any other exception) raised while applying one
queued item is never allowed to kill `_writer_loop` — that would silently
stop the *only* ledger writer in the whole system. Instead it is logged
loudly via `logging` and appended to `self.errors` (tests and operators
can inspect this), and the loop moves on to the next item. The offending
block is left exactly as it was; it is not left half-written, because the
Blockchain writes below always finish mutating in-memory fields *before*
calling `validate_transition`/`update_task_status`, so a failed transition
either fully applies or doesn't touch `.status` at all (see `_transition`).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from blockchain.Blockchain_modell import Block, Blockchain

from agenten.decomposition.budget import BudgetLedger, DecompositionBudget
from agenten.events.schemas import (
    LeaseExpired,
    ProblemSubmitted,
    SubproblemAccepted,
    SubproblemAssigned,
    SubproblemCompleted,
    SubproblemFailed,
    SubproblemProposed,
    SubproblemRejected,
    WorkerHeartbeat,
    make_meta,
    topic_for,
)
from agenten.ledger_bridge.query import LedgerQueryImpl, build_status_index
from agenten.ledger_bridge.stage_machine import TERMINAL_STAGES, Stage, validate_transition
from agenten.runtime.event_bus import EventBus

logger = logging.getLogger(__name__)

# Sentinel pushed onto the inbox to ask the writer loop to stop after
# draining everything queued ahead of it.
_STOP = object()

# Canonical (event_type, handle_method_name) pairs — the single source of
# truth both `LedgerRecorderAgent._subscribe()` (real `EventBus` wiring)
# and `RECORDER_TOPICS` (module-level list unit U11 uses to wire up the
# AutoGen `TypeSubscription`s for `LedgerRecorderRoutedAgent`) are derived
# from, so the two can never silently drift out of sync (e.g. a 10th event
# type added to one list but not the other).
_SUBSCRIPTION_SPEC: List[Tuple[type, str]] = [
    (ProblemSubmitted, "handle_problem_submitted"),
    (SubproblemProposed, "handle_subproblem_proposed"),
    (SubproblemAccepted, "handle_subproblem_accepted"),
    (SubproblemRejected, "handle_subproblem_rejected"),
    (SubproblemAssigned, "handle_subproblem_assigned"),
    (WorkerHeartbeat, "handle_worker_heartbeat"),
    (SubproblemCompleted, "handle_subproblem_completed"),
    (SubproblemFailed, "handle_subproblem_failed"),
    (LeaseExpired, "handle_lease_expired"),
]

RECORDER_TOPICS: List[str] = [topic_for(event_type) for event_type, _ in _SUBSCRIPTION_SPEC]


class LedgerRecorderAgent:
    """Sole ledger writer. See module docstring for the concurrency story."""

    def __init__(
        self,
        bus: EventBus,
        blockchain: Blockchain,
        budget_ledger: BudgetLedger,
        default_budget: DecompositionBudget = DecompositionBudget(),
    ):
        self._bus = bus
        self._blockchain = blockchain
        self._budget_ledger = budget_ledger
        self._default_budget = default_budget

        self._inbox: "asyncio.Queue[Any]" = asyncio.Queue()
        self._writer_task: Optional[asyncio.Task] = None

        # subproblem_id -> block index, and root_problem_id -> "problem"
        # block index. Maintained ONLY inside the writer loop.
        self._subproblem_index: Dict[str, int] = {}
        self._problem_index: Dict[str, int] = {}
        self._problem_budgets: Dict[str, DecompositionBudget] = {}

        # Best-effort at-least-once dedup: EventBus explicitly documents
        # delivery as at-least-once, not exactly-once.
        self._seen_event_ids: Set[str] = set()

        # Errors caught while applying a queued item (illegal transitions,
        # etc.) — never raised out of the writer loop, but never silently
        # dropped either. Tests and operators can inspect this.
        self.errors: List[BaseException] = []

        # Set by stop(); the writer loop only exits once this is true AND
        # the inbox is observed empty (see _writer_loop / stop docstring
        # for why "empty at the moment STOP is dequeued" is not enough).
        self._stop_requested = False

        # Seed the read-side index from whatever is already on `blockchain`
        # (e.g. this process is starting up on top of a ledger a previous
        # process crashed while writing) instead of only ever reflecting
        # blocks written during this process's lifetime.
        self.query = LedgerQueryImpl(blockchain, status_index=build_status_index(blockchain))
        self._rehydrate()

        self._subscribe()

    def _rehydrate(self) -> None:
        """Rebuild `_subproblem_index` / `_problem_index` / cached
        per-root budgets from an already-populated `blockchain` on
        construction.

        Without this, a `LedgerRecorderAgent` constructed on top of a
        ledger from a previous process (the normal crash/restart case —
        this class has no in-memory-only assumption anywhere else, see
        `InProcessBudgetLedger._rehydrate` and `query.build_status_index`
        doing the equivalent for the budget ledger and the CQRS read
        side) would start with empty indices and treat every subsequent
        at-least-once-redelivered event for an already-existing
        subproblem as "unknown subproblem_id", permanently dropping the
        update instead of applying it.
        """
        for block in self._blockchain.chain:
            if block.block_type == "problem":
                problem_id = block.data.get("problem_id")
                if not problem_id:
                    continue
                self._problem_index[problem_id] = block.index
                raw_budget = block.metadata.get("budget")
                if raw_budget:
                    try:
                        self._problem_budgets[problem_id] = DecompositionBudget(**raw_budget)
                    except Exception:  # pragma: no cover - defensive against malformed persisted data
                        logger.exception(
                            "LedgerRecorder: failed to rehydrate budget for root_problem_id=%s", problem_id
                        )
            elif block.block_type == "subproblem":
                subproblem_id = block.data.get("subproblem_id")
                if subproblem_id:
                    self._subproblem_index[subproblem_id] = block.index

    # ------------------------------------------------------------------
    # wiring
    # ------------------------------------------------------------------
    def _subscribe(self) -> None:
        for event_type, handler_name in _SUBSCRIPTION_SPEC:
            self._bus.subscribe(topic_for(event_type), getattr(self, handler_name))

    async def start(self) -> None:
        """Spawn the single writer-loop task, if not already running."""
        if self._writer_task is None or self._writer_task.done():
            # Must reset before creating the task: stop() leaves this True,
            # and _writer_loop exits the instant it sees `_stop_requested
            # and inbox empty` — without this reset, a start() following a
            # prior stop() would process at most one item and silently
            # exit again.
            self._stop_requested = False
            self._writer_task = asyncio.create_task(self._writer_loop())

    async def stop(self) -> None:
        """Drain everything already queued, then stop the writer task.

        Naively, "push a `_STOP` sentinel, `await self._inbox.join()`"
        would be enough — except some of the events this unit applies
        (re-publishing `SubproblemAccepted` with a real `block_index`,
        publishing `SubproblemRejected` for budget exhaustion) are
        published back onto the SAME topics this agent is itself
        subscribed to. On `InMemoryEventBus` (and, unavoidably, on any
        topic-based pub/sub including AutoGen's `TypeSubscription`), that
        self-published event is delivered straight back to this agent and
        lands as a brand new item in `self._inbox` — potentially *after*
        the `_STOP` sentinel is already queued. A single "exit as soon as
        I see STOP" loop would abandon that trailing item forever and
        `join()` would then deadlock (its internal unfinished-task count
        never reaches zero). So instead: `stop()` just sets a flag and
        nudges the loop awake; the loop itself is the one that decides
        it's safe to exit, by re-checking "stop requested AND inbox
        observed empty" after fully finishing each item (including any
        nested self-publishes that item triggered) — see `_writer_loop`.

        Scope of the guarantee: this closes the loop reliably for events
        *this agent itself* publishes as a side effect of applying
        something already in its own queue. It does NOT guarantee
        delivery of events published by OTHER agents concurrently in
        flight at the moment `stop()` is called — e.g. another task
        already inside `await bus.publish(...)` for `SubproblemAssigned`
        whose corresponding `await self._inbox.put(...)` simply hasn't
        run yet when this agent's writer loop observes "stop requested
        and empty" and returns. Once `_writer_task` finishes, nothing
        services `self._inbox` anymore; any such item is queued but
        never drained. This is inherent to a per-agent `stop()` with no
        cross-agent quiescence barrier (`EventBus`, unit U0, has no
        concept of "pause all producers") — orderly shutdown of the full
        pipeline (unit U11's job) must stop routing new work to this
        agent's subscribed topics before calling `recorder.stop()`, not
        rely on `stop()` alone to catch events still arriving from
        elsewhere.
        """
        if self._writer_task is None:
            return
        self._stop_requested = True
        # Wake the loop even if it's currently blocked on an empty queue
        # (get() would otherwise never return and it'd never re-check
        # `_stop_requested`).
        await self._inbox.put(_STOP)
        await self._writer_task  # the loop returns on its own once truly drained
        self._writer_task = None

    # ------------------------------------------------------------------
    # public handlers — enqueue only, per the module docstring
    #
    # Each puts the *bound `_apply_xxx` method itself* (not a string tag
    # looked up in a separately-maintained dispatch table) onto the
    # inbox, so there is exactly one place — this pairing — that says
    # "this event type maps to this handler"; a typo or a forgotten
    # update when adding a 10th event type would be a straightforward
    # NameError/AttributeError at edit time instead of a silently-dropped
    # event at runtime.
    # ------------------------------------------------------------------
    async def handle_problem_submitted(self, event: ProblemSubmitted) -> None:
        await self._inbox.put((self._apply_problem_submitted, event))

    async def handle_subproblem_proposed(self, event: SubproblemProposed) -> None:
        await self._inbox.put((self._apply_subproblem_proposed, event))

    async def handle_subproblem_accepted(self, event: SubproblemAccepted) -> None:
        await self._inbox.put((self._apply_subproblem_accepted, event))

    async def handle_subproblem_rejected(self, event: SubproblemRejected) -> None:
        await self._inbox.put((self._apply_subproblem_rejected, event))

    async def handle_subproblem_assigned(self, event: SubproblemAssigned) -> None:
        await self._inbox.put((self._apply_subproblem_assigned, event))

    async def handle_worker_heartbeat(self, event: WorkerHeartbeat) -> None:
        await self._inbox.put((self._apply_worker_heartbeat, event))

    async def handle_subproblem_completed(self, event: SubproblemCompleted) -> None:
        await self._inbox.put((self._apply_subproblem_completed, event))

    async def handle_subproblem_failed(self, event: SubproblemFailed) -> None:
        await self._inbox.put((self._apply_subproblem_failed, event))

    async def handle_lease_expired(self, event: LeaseExpired) -> None:
        await self._inbox.put((self._apply_lease_expired, event))

    # ------------------------------------------------------------------
    # writer loop — the ONLY place blockchain mutation happens
    # ------------------------------------------------------------------
    async def _writer_loop(self) -> None:
        while True:
            item = await self._inbox.get()
            apply_fn = None
            try:
                if item is _STOP:
                    # Just a wakeup nudge from stop() — nothing to apply.
                    # Whether we actually exit is decided in `finally`
                    # below (only once truly drained), not here.
                    continue
                apply_fn, event = item
                event_id = getattr(getattr(event, "meta", None), "event_id", None)
                if event_id is not None and event_id in self._seen_event_ids:
                    logger.debug(
                        "LedgerRecorder: dropping duplicate delivery of %s (event_id=%s)",
                        apply_fn.__name__,
                        event_id,
                    )
                    continue
                if event_id is not None:
                    self._seen_event_ids.add(event_id)
                await apply_fn(event)
            except Exception as exc:  # noqa: BLE001 - the writer loop must never die
                logger.exception(
                    "LedgerRecorder: failed to apply %s: %s", apply_fn.__name__ if apply_fn else item, exc
                )
                self.errors.append(exc)
            finally:
                self._inbox.task_done()
                # Only exit once stop() has been called AND, at this
                # precise point (right after fully finishing one item,
                # including any nested self-publishes it triggered — see
                # stop()'s docstring), the inbox is observed empty. Any
                # item a self-publish added is already sitting in the
                # queue by now and will be picked up on the next
                # iteration instead of being abandoned.
                if self._stop_requested and self._inbox.empty():
                    return

    # ------------------------------------------------------------------
    # internal: persistence helpers
    # ------------------------------------------------------------------
    def _touch(self, index: int) -> None:
        """Persist an in-place `.data`/`.metadata` mutation that did not
        change `.status`, by round-tripping through the public
        `update_task_status` (recomputes hash + saves) with the block's
        current status unchanged. Keeps us inside `Blockchain`'s public
        API instead of reaching for its private `_save()`.
        """
        block = self._blockchain.get_block(index)
        if block is None:  # pragma: no cover - defensive
            return
        self._blockchain.update_task_status(index, block.status)

    def _can_transition(self, index: int, target: Stage) -> Optional[Block]:
        """Pure check (no mutation): returns the block if `.status ->
        target` is currently legal, else logs+records the error and
        returns None.

        Callers that need to write `.data`/`.metadata` fields that are
        part of a block's *core identity* (who owns it, what its result
        is — `agent_type`/`agent_key`/`lease_expires_at`, `result`) MUST
        call this first and skip the mutation entirely if it returns
        None. `Block` objects are long-lived, mutable, in-place-shared
        Python objects held directly in `blockchain.chain` — mutating
        `.data`/`.metadata` before knowing the transition is legal would
        leak into the ledger on the *next* unrelated save regardless of
        whether `_transition` itself calls `_touch`, silently
        overwriting core fields on a block a redundant/out-of-order/
        duplicate event was never actually allowed to touch (e.g. a
        stale `SubproblemAssigned` redelivery landing after the
        subproblem is already `IN_PROGRESS` must not stomp on
        `agent_type`/`agent_key` for the agent that's actually doing the
        work).
        """
        block = self._blockchain.get_block(index)
        if block is None:
            logger.error("LedgerRecorder: transition to %s failed, no block at index %s", target, index)
            self.errors.append(IndexError(f"no block at index {index}"))
            return None
        current = Stage(block.status)
        try:
            validate_transition(current, target)
        except ValueError as exc:
            logger.error(
                "LedgerRecorder: illegal transition %s -> %s for block %s: %s", current, target, index, exc
            )
            self.errors.append(exc)
            return None
        return block

    def _transition(self, index: int, target: Stage) -> bool:
        """Attempt `block.status -> target` via the shared state machine.

        Returns True/False instead of letting `ValueError` propagate: a
        rejected transition is recorded in `self.errors` and logged, never
        silently ignored, but it also never takes down the writer loop
        (see module docstring).

        Every `_apply_xxx` caller is expected to validate via
        `_can_transition` (or an equivalent guard like
        `_require_validating_block`) *before* writing any `.data`/
        `.metadata` field, so by the time this method is reached the
        transition normally succeeds; on the rare/defensive path where it
        still doesn't, `_touch` is called to make sure nothing this
        method itself touched is left dangling unsaved — it is not a
        mechanism callers should rely on for flushing writes made ahead
        of an unvalidated transition (see `_can_transition`'s docstring
        and `_record_late_event` for the actual pattern for that).
        """
        block = self._can_transition(index, target)
        if block is None:
            self._touch(index)
            return False
        current = Stage(block.status)
        self._blockchain.update_task_status(index, target.value)
        self.query._index_move(current, target, index)
        return True

    def _persist_budget_consumed(self, root_problem_id: str) -> None:
        idx = self._problem_index.get(root_problem_id)
        if idx is None:
            return
        block = self._blockchain.get_block(idx)
        if block is None:  # pragma: no cover - defensive
            return
        block.metadata["budget_consumed"] = self._budget_ledger.consumed(root_problem_id)
        self._touch(idx)

    def _require_block(self, subproblem_id: str, event_kind: str) -> Optional[Tuple[int, Block]]:
        """Shared lookup for the handlers that require a subproblem to
        already have a block (SubproblemAssigned/Completed/Failed,
        LeaseExpired): logs+records an error and returns None if we have
        no block for `subproblem_id` at all (unlike the Accepted/Rejected
        Gatekeeper-verdict path, an unknown id here is always suspicious,
        never an expected "already handled elsewhere" case).
        """
        idx = self._subproblem_index.get(subproblem_id)
        if idx is None:
            logger.error("LedgerRecorder: %s for unknown subproblem_id=%s", event_kind, subproblem_id)
            self.errors.append(KeyError(subproblem_id))
            return None
        block = self._blockchain.get_block(idx)
        if block is None:  # pragma: no cover - defensive, index should always be valid
            return None
        return idx, block

    def _require_validating_block(self, subproblem_id: str, event_kind: str) -> Optional[Tuple[int, Block]]:
        """Shared lookup for the Gatekeeper-verdict handlers
        (SubproblemAccepted/Rejected): a missing block or a block that's
        no longer VALIDATING is a graceful no-op (see module docstring —
        e.g. the block was already budget-rejected, or this is a
        redundant/out-of-order verdict redelivery), not an error.
        """
        idx = self._subproblem_index.get(subproblem_id)
        if idx is None:
            logger.debug(
                "LedgerRecorder: %s for %s with no known block (already budget-rejected or duplicate); no-op",
                event_kind,
                subproblem_id,
            )
            return None
        block = self._blockchain.get_block(idx)
        if block is None or Stage(block.status) != Stage.VALIDATING:
            logger.debug(
                "LedgerRecorder: ignoring redundant/out-of-order %s for %s (current status=%s)",
                event_kind,
                subproblem_id,
                block.status if block else None,
            )
            return None
        return idx, block

    def _record_late_event(self, index: int, kind: str, detail: Dict[str, Any]) -> None:
        """Append-only forensic breadcrumb for an event that could not
        legally apply because the block had already moved past where it
        was aimed (e.g. a stale `SubproblemFailed`/`LeaseExpired` arriving
        after the subproblem is already `DONE` — at-least-once delivery
        and out-of-order arrival are both expected, see module docstring).

        Deliberately does NOT touch `data`/`metadata` fields that
        describe the block's actual, real outcome (`result`,
        `failure_reason`, `agent_type`/`agent_key`, ...) — only ever
        appends to a side list, so a reader can never mistake a late or
        duplicate report for what actually happened to this block. A
        `DONE` block that later receives a stale failure report stays
        legibly `DONE`; the stale report is visible in
        `metadata['late_events']` instead of overwriting
        `metadata['failure_reason']`.
        """
        block = self._blockchain.get_block(index)
        if block is None:  # pragma: no cover - defensive
            return
        block.metadata.setdefault("late_events", []).append({"kind": kind, **detail})
        self._touch(index)

    # ------------------------------------------------------------------
    # internal: one _apply_xxx per event kind — the numbered behaviors
    # from the shared cross-unit spec, applied inside the writer loop
    # ------------------------------------------------------------------
    def _write_problem_block(self, problem_id: str, description: str, budget: DecompositionBudget) -> Block:
        block = self._blockchain.add_block(
            block_type="problem",
            data={"problem_id": problem_id, "description": description},
            status=Stage.IN_PROGRESS.value,
            metadata={"budget_consumed": 0, "budget": budget.model_dump()},
        )
        self._problem_index[problem_id] = block.index
        self._problem_budgets[problem_id] = budget
        self.query._index_add(Stage.IN_PROGRESS, block.index)
        return block

    async def _apply_problem_submitted(self, event: ProblemSubmitted) -> None:
        if event.problem_id in self._problem_index:
            logger.debug("LedgerRecorder: duplicate ProblemSubmitted for %s, ignoring", event.problem_id)
            return
        budget = event.budget if event.budget is not None else self._default_budget
        self._write_problem_block(event.problem_id, event.description, budget)

    def _ensure_problem_block(self, root_problem_id: str) -> DecompositionBudget:
        """Returns the budget in effect for `root_problem_id`, writing a
        minimal 'problem' block for it first if `ProblemSubmitted` was
        never observed.

        Without this, `_persist_budget_consumed` has nowhere to persist
        `try_reserve`'s reservation for an orphaned root (see
        `_apply_subproblem_proposed`'s parent-lookup fallback below) —
        the in-memory `BudgetLedger` counter would still enforce the cap
        correctly for the lifetime of this process, but the crash-safety
        guarantee (`InProcessBudgetLedger` rehydrating `consumed()` from
        chain metadata) silently would not apply to that root across a
        restart. Synthesizing the block here means every root problem
        that ever gets a `SubproblemProposed` has a home for its budget
        bookkeeping, no exceptions.
        """
        if root_problem_id in self._problem_index:
            return self._problem_budgets.get(root_problem_id, self._default_budget)
        logger.warning(
            "LedgerRecorder: no 'problem' block for root_problem_id=%s (ProblemSubmitted never "
            "observed); synthesizing a minimal one so budget bookkeeping stays crash-safe",
            root_problem_id,
        )
        self._write_problem_block(
            root_problem_id, "(synthesized: ProblemSubmitted was never observed)", self._default_budget
        )
        return self._default_budget

    async def _apply_subproblem_proposed(self, event: SubproblemProposed) -> None:
        root_problem_id = event.meta.root_problem_id
        budget = self._ensure_problem_block(root_problem_id)

        reserved = self._budget_ledger.try_reserve(root_problem_id, budget, 1)
        if reserved == 0:
            await self._bus.publish(
                topic_for(SubproblemRejected),
                SubproblemRejected(
                    meta=make_meta(
                        correlation_id=event.subproblem_id,
                        root_problem_id=root_problem_id,
                        attempt=event.meta.attempt,
                        constitution_version=event.meta.constitution_version,
                    ),
                    subproblem_id=event.subproblem_id,
                    reason="budget_exceeded",
                    detail=(
                        f"root problem {root_problem_id!r} budget exhausted "
                        f"(max_total_subproblems={budget.max_total_subproblems}, "
                        f"consumed={self._budget_ledger.consumed(root_problem_id)})"
                    ),
                ),
            )
            # Design choice: no block is written for a budget-rejected
            # proposal. The rejection itself is fully captured in the
            # SubproblemRejected event; a subproblem that never got a
            # ledger slot needs no audit block, and it keeps
            # `_subproblem_index` a reliable "did we actually write a
            # block for this id" check for downstream Accepted/Rejected
            # verdicts (see _apply_subproblem_accepted/_rejected).
            return

        if event.parent_id is not None:
            parent_index = self._subproblem_index.get(event.parent_id)
            if parent_index is None:
                logger.warning(
                    "LedgerRecorder: SubproblemProposed %s references unknown parent_id %s; "
                    "writing as a root-level block",
                    event.subproblem_id,
                    event.parent_id,
                )
        else:
            # _ensure_problem_block above guarantees a 'problem' block
            # exists for root_problem_id by this point (synthesizing one
            # if ProblemSubmitted was never observed), so this is never
            # None.
            parent_index = self._problem_index[root_problem_id]

        block = self._blockchain.add_block(
            block_type="subproblem",
            data={
                "subproblem_id": event.subproblem_id,
                "description": event.description,
                "capability_tags": list(event.capability_tags),
                "parent_subproblem_id": event.parent_id,
                "depth": event.depth,
                "root_problem_id": root_problem_id,
            },
            status=Stage.QUEUED.value,
            parent_index=parent_index,
        )
        self._subproblem_index[event.subproblem_id] = block.index
        self.query._index_add(Stage.QUEUED, block.index)

        self._transition(block.index, Stage.VALIDATING)
        self._persist_budget_consumed(root_problem_id)

    async def _apply_subproblem_accepted(self, event: SubproblemAccepted) -> None:
        found = self._require_validating_block(event.subproblem_id, "SubproblemAccepted")
        if found is None:
            return
        idx, block = found

        ok = self._transition(idx, Stage.ACCEPTED)
        if not ok:
            return

        root_problem_id = block.data.get("root_problem_id", event.meta.root_problem_id)
        await self._bus.publish(
            topic_for(SubproblemAccepted),
            SubproblemAccepted(
                meta=make_meta(
                    correlation_id=event.subproblem_id,
                    root_problem_id=root_problem_id,
                    attempt=event.meta.attempt,
                    constitution_version=event.meta.constitution_version,
                ),
                subproblem_id=event.subproblem_id,
                block_index=idx,
            ),
        )

    async def _apply_subproblem_rejected(self, event: SubproblemRejected) -> None:
        found = self._require_validating_block(event.subproblem_id, "SubproblemRejected")
        if found is None:
            return
        idx, block = found
        block.metadata["rejection_reason"] = event.reason
        block.metadata["rejection_detail"] = event.detail
        self._transition(idx, Stage.REJECTED)

    async def _apply_subproblem_assigned(self, event: SubproblemAssigned) -> None:
        found = self._require_block(event.subproblem_id, "SubproblemAssigned")
        if found is None:
            return
        idx, _block = found
        # Validate BEFORE mutating: agent_type/agent_key/lease_expires_at
        # are core identity fields the Reaper (U7) trusts to know who
        # currently owns the lease. `Block` objects are mutable and
        # shared in-place with `blockchain.chain`, so writing these
        # first and validating after (as an earlier version of this
        # method did) would leak a stale/duplicate/out-of-order
        # SubproblemAssigned's agent identity into the ledger on the
        # very next unrelated save, even though the accompanying status
        # transition was correctly rejected.
        block = self._can_transition(idx, Stage.ASSIGNED)
        if block is None:
            return
        block.data["agent_type"] = event.agent_type
        block.data["agent_key"] = event.agent_key
        block.metadata["lease_expires_at"] = event.lease_expires_at
        # A (re)assignment — including the RETRYING -> ASSIGNED case after
        # a retriable SubproblemFailed/LeaseExpired — means whatever
        # `last_error`/`retriable` said about the *previous* attempt no
        # longer describes the block's current state. Pop them (rather
        # than leave them sitting on a block that goes on to reach DONE,
        # or fails again for a genuinely different reason) so a reader
        # checking `metadata.get("retriable")`/`"last_error" in metadata`
        # to find blocks currently in trouble is never misled by a
        # resolved, historical retry. The full history isn't lost — it's
        # already preserved append-only in `metadata["retry_history"]`
        # (see `_apply_retry_transition`).
        block.metadata.pop("last_error", None)
        block.metadata.pop("retriable", None)
        self._transition(idx, Stage.ASSIGNED)

    async def _apply_worker_heartbeat(self, event: WorkerHeartbeat) -> None:
        idx = self._subproblem_index.get(event.subproblem_id)
        if idx is None:
            return  # best-effort/optional per spec: unknown subproblem, ignore
        block = self._blockchain.get_block(idx)
        if block is None:
            return
        try:
            current = Stage(block.status)
        except ValueError:  # pragma: no cover - defensive
            return
        if current in TERMINAL_STAGES:
            return  # stale heartbeat after the subproblem already finished; ignore

        # Design choice: we record last-heartbeat-seen for observability,
        # but we deliberately do NOT auto-extend `lease_expires_at` here.
        # WorkerHeartbeat carries no new lease value, and inventing a fixed
        # extension duration would be an undocumented policy decision this
        # unit shouldn't make unilaterally; a real lease refresh should be
        # an explicit, Coordinator-issued value.
        block.metadata["last_heartbeat_at"] = event.meta.ts

        if current == Stage.ASSIGNED:
            self._transition(idx, Stage.IN_PROGRESS)
        else:
            self._touch(idx)

    async def _apply_subproblem_completed(self, event: SubproblemCompleted) -> None:
        found = self._require_block(event.subproblem_id, "SubproblemCompleted")
        if found is None:
            return
        idx, block = found
        current = Stage(block.status)

        # A worker can legitimately finish before its first heartbeat
        # ever bumped the block from ASSIGNED to IN_PROGRESS (fast
        # completion racing the heartbeat cadence) — ALLOWED_TRANSITIONS
        # has no ASSIGNED -> VERIFYING edge, so advance through
        # IN_PROGRESS first when that's where we still are, instead of
        # rejecting the completion and stranding the block at ASSIGNED
        # forever even though the worker really did finish.
        if current == Stage.ASSIGNED:
            self._transition(idx, Stage.IN_PROGRESS)

        # Validate BEFORE writing `data['result']` (same reasoning as
        # `_can_transition`'s docstring): a stale/duplicate completion
        # arriving after the block is already terminal (e.g. already
        # DONE from an earlier completion, or FAILED) must not overwrite
        # the real result — recorded as a late event instead.
        verifying_block = self._can_transition(idx, Stage.VERIFYING)
        if verifying_block is None:
            self._record_late_event(idx, "subproblem_completed", {})
            return
        verifying_block.data["result"] = dict(event.result)
        # Chained straight through VERIFYING -> DONE within the same
        # writer-loop turn: there's no separate verification agent in this
        # fleet yet, and doing both here (rather than stopping at
        # VERIFYING) keeps "completed" unambiguous for readers of the
        # ledger without waiting on an agent that doesn't exist.
        if self._transition(idx, Stage.VERIFYING):
            self._transition(idx, Stage.DONE)

    def _apply_retry_transition(self, idx: int, error: str, source: str) -> None:
        """Shared RETRYING-path handling for a retriable `SubproblemFailed`
        or a `LeaseExpired` (both route here — see module docstring).

        Appends `error` to the block's append-only `metadata["retry_history"]`
        forensic list (nothing about a retry is ever lost, mirroring
        `_record_late_event`'s append-only philosophy), and sets
        `last_error`/`retriable` as convenience "most recent attempt"
        fields — cleared by `_apply_subproblem_assigned` the moment the
        block is actually reassigned, so they never linger as stale
        current-state on a block that goes on to reach DONE or a later,
        different FAILED.

        A second retriable report arriving while the block is *already*
        RETRYING (e.g. two LeaseExpired events for the same still-
        unassigned lease before the Coordinator gets to it) is a graceful
        no-op status-wise: RETRYING -> RETRYING isn't an edge
        `ALLOWED_TRANSITIONS` defines, so routing it through
        `_can_transition` would log a spurious "illegal transition" error
        for what is actually a normal part of the retry-backoff window,
        not corruption. The report is still appended to `retry_history`
        (and `last_error` refreshed) — it's only the self.errors entry
        that's skipped.
        """
        block = self._blockchain.get_block(idx)
        if block is None:  # pragma: no cover - defensive
            return
        if Stage(block.status) == Stage.RETRYING:
            logger.debug(
                "LedgerRecorder: %s for %s while already RETRYING (awaiting reassignment); "
                "recording to retry_history without filing an error",
                source,
                idx,
            )
            block.metadata.setdefault("retry_history", []).append({"error": error, "source": source})
            block.metadata["last_error"] = error
            self._touch(idx)
            return
        validated = self._can_transition(idx, Stage.RETRYING)
        if validated is None:
            self._record_late_event(idx, source, {"error": error, "retriable": True})
            return
        validated.metadata.setdefault("retry_history", []).append({"error": error, "source": source})
        validated.metadata["last_error"] = error
        validated.metadata["retriable"] = True
        self._transition(idx, Stage.RETRYING)

    async def _apply_subproblem_failed(self, event: SubproblemFailed) -> None:
        found = self._require_block(event.subproblem_id, "SubproblemFailed")
        if found is None:
            return
        idx, _block = found
        # Retriable failures go to RETRYING (non-terminal — the block can
        # legally come back via RETRYING -> ASSIGNED once the Coordinator
        # re-publishes SubproblemAssigned after backoff, see
        # `_apply_retry_transition`); only genuinely terminal
        # (retriable=False) failures go to FAILED, unchanged from before.
        # Both targets are legal from ASSIGNED/IN_PROGRESS per
        # ALLOWED_TRANSITIONS — see the module docstring. Validate BEFORE
        # writing `failure_reason`/`retriable` (see _can_transition's
        # docstring / _record_late_event): a stale/duplicate failure
        # report arriving after the block is already terminal (e.g. DONE)
        # must not overwrite those fields and make a successfully
        # completed block look like it failed — it's recorded as a late
        # event instead. Either way this is surfaced loudly via
        # self.errors, never silently swallowed.
        if not event.retriable:
            block = self._can_transition(idx, Stage.FAILED)
            if block is None:
                self._record_late_event(idx, "subproblem_failed", {"error": event.error, "retriable": False})
                return
            block.metadata["failure_reason"] = event.error
            block.metadata["retriable"] = False
            self._transition(idx, Stage.FAILED)
            return
        self._apply_retry_transition(idx, event.error, "subproblem_failed")

    async def _apply_lease_expired(self, event: LeaseExpired) -> None:
        found = self._require_block(event.subproblem_id, "LeaseExpired")
        if found is None:
            return
        idx, _block = found
        # LeaseExpired carries no `retriable` field (unlike SubproblemFailed)
        # — a lease expiring almost always means transient infra failure
        # (worker crashed, host restarted), so treat it as retriable by
        # default and route to RETRYING, not FAILED. Deciding when to give
        # up retrying (retry-count/backoff, EscalateToRedecompose) is the
        # Supervisor's (U6) job, not this unit's — see module docstring.
        failure_reason = f"lease_expired (agent_type={event.agent_type}, agent_key={event.agent_key})"
        self._apply_retry_transition(idx, failure_reason, "lease_expired")


# ----------------------------------------------------------------------
# Optional AutoGen Core adapter. Must import cleanly with autogen_core
# absent (agenten/events/schemas.py and friends have zero AutoGen
# dependency by design; this module keeps that property too).
# ----------------------------------------------------------------------
try:
    import autogen_core
    from autogen_core import MessageContext, RoutedAgent, message_handler
except ImportError:  # pragma: no cover - exercised by the "no autogen_core" import check
    autogen_core = None

# RECORDER_TOPICS (the topics unit U11 needs to bind `TypeSubscription`s
# for) is defined once, near `_SUBSCRIPTION_SPEC` at the top of this
# module — see there.


if autogen_core is not None:

    class LedgerRecorderRoutedAgent(RoutedAgent):
        """Thin AutoGen Core `RoutedAgent` adapter around `LedgerRecorderAgent`.

        Forwards each supply-chain message type into the wrapped
        `LedgerRecorderAgent`'s `handle_xxx` (queue-and-return) method. The
        actual `TypeSubscription` registrations binding `RECORDER_TOPICS`
        to this agent's type are unit U11's job (see
        `agenten/orchestration/pipeline.py`); this class only needs to
        exist, be constructible, and correctly route messages once
        subscribed.
        """

        def __init__(
            self,
            bus: EventBus,
            blockchain: Blockchain,
            budget_ledger: BudgetLedger,
            default_budget: DecompositionBudget = DecompositionBudget(),
        ):
            super().__init__(description="Ledger Recorder (unit U8) — sole ledger writer")
            self._inner = LedgerRecorderAgent(bus, blockchain, budget_ledger, default_budget)

        @property
        def query(self) -> LedgerQueryImpl:
            return self._inner.query

        @property
        def errors(self) -> List[BaseException]:
            return self._inner.errors

        async def start(self) -> None:
            await self._inner.start()

        async def stop(self) -> None:
            await self._inner.stop()

        @message_handler
        async def on_problem_submitted(self, message: ProblemSubmitted, ctx: MessageContext) -> None:
            await self._inner.handle_problem_submitted(message)

        @message_handler
        async def on_subproblem_proposed(self, message: SubproblemProposed, ctx: MessageContext) -> None:
            await self._inner.handle_subproblem_proposed(message)

        @message_handler
        async def on_subproblem_accepted(self, message: SubproblemAccepted, ctx: MessageContext) -> None:
            await self._inner.handle_subproblem_accepted(message)

        @message_handler
        async def on_subproblem_rejected(self, message: SubproblemRejected, ctx: MessageContext) -> None:
            await self._inner.handle_subproblem_rejected(message)

        @message_handler
        async def on_subproblem_assigned(self, message: SubproblemAssigned, ctx: MessageContext) -> None:
            await self._inner.handle_subproblem_assigned(message)

        @message_handler
        async def on_worker_heartbeat(self, message: WorkerHeartbeat, ctx: MessageContext) -> None:
            await self._inner.handle_worker_heartbeat(message)

        @message_handler
        async def on_subproblem_completed(self, message: SubproblemCompleted, ctx: MessageContext) -> None:
            await self._inner.handle_subproblem_completed(message)

        @message_handler
        async def on_subproblem_failed(self, message: SubproblemFailed, ctx: MessageContext) -> None:
            await self._inner.handle_subproblem_failed(message)

        @message_handler
        async def on_lease_expired(self, message: LeaseExpired, ctx: MessageContext) -> None:
            await self._inner.handle_lease_expired(message)

else:  # pragma: no cover - exercised by the "no autogen_core" import check
    LedgerRecorderRoutedAgent = None  # type: ignore[assignment,misc]
