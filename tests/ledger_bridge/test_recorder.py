"""Unit tests for the Ledger Recorder (unit U8) — the sole ledger writer.

No pytest-asyncio in this environment, so async test bodies are driven
explicitly via `asyncio.run(...)` from plain synchronous `test_*`
functions.

These tests deliberately exercise the REAL `blockchain.Blockchain_modell`
classes (not fakes) per the unit's own instructions: this is the one unit
whose job is to prove the real ledger behaves correctly under the event
sequences every other unit will throw at it.
"""
import asyncio
import json
import os
import tempfile
import time

from blockchain.Blockchain_modell import Block, Blockchain
from blockchain.storage import InMemoryStorage, JSONFileStorage

from agenten.decomposition.budget import DecompositionBudget
from agenten.events.schemas import (
    LeaseExpired,
    ProblemSubmitted,
    SubproblemAccepted,
    SubproblemAssigned,
    SubproblemCompleted,
    SubproblemFailed,
    SubproblemProposed,
    SubproblemRejected,
    SubproblemUnroutable,
    WorkerHeartbeat,
    make_meta,
    topic_for,
)
from agenten.ledger_bridge.query import InProcessBudgetLedger
from agenten.ledger_bridge import recorder as recorder_module
from agenten.ledger_bridge.recorder import LedgerRecorderAgent, RECORDER_TOPICS
from agenten.ledger_bridge.stage_machine import Stage
from agenten.runtime.event_bus import InMemoryEventBus


def run(coro):
    return asyncio.run(coro)


def make_recorder(blockchain=None, budget_ledger=None, default_budget=None):
    blockchain = blockchain if blockchain is not None else Blockchain(storage=InMemoryStorage())
    budget_ledger = budget_ledger if budget_ledger is not None else InProcessBudgetLedger(blockchain)
    bus = InMemoryEventBus()
    kwargs = {}
    if default_budget is not None:
        kwargs["default_budget"] = default_budget
    recorder = LedgerRecorderAgent(bus, blockchain, budget_ledger, **kwargs)
    recorder_module.subscribe_recorder(bus, recorder)
    return bus, blockchain, budget_ledger, recorder


def test_recorder_subscription_is_explicit_and_complete():
    blockchain = Blockchain(storage=InMemoryStorage())
    budget_ledger = InProcessBudgetLedger(blockchain)
    bus = InMemoryEventBus()

    recorder = LedgerRecorderAgent(bus, blockchain, budget_ledger)

    assert dict(bus._handlers) == {}
    subscribe_recorder = getattr(recorder_module, "subscribe_recorder", None)
    assert subscribe_recorder is not None
    subscribe_recorder(bus, recorder)
    assert set(bus._handlers) == set(RECORDER_TOPICS)
    assert all(len(handlers) == 1 for handlers in bus._handlers.values())


def test_subscribe_recorder_rejects_publish_only_bus():
    class PublishOnlyBus:
        async def publish(self, topic, event):
            return None

    blockchain = Blockchain(storage=InMemoryStorage())
    budget_ledger = InProcessBudgetLedger(blockchain)
    recorder = LedgerRecorderAgent(InMemoryEventBus(), blockchain, budget_ledger)
    subscribe_recorder = getattr(recorder_module, "subscribe_recorder", None)
    assert subscribe_recorder is not None

    try:
        subscribe_recorder(PublishOnlyBus(), recorder)
    except TypeError as exc:
        assert "SubscribableEventBus" in str(exc)
    else:
        raise AssertionError("publish-only bus was accepted")


async def submit_problem(bus, problem_id="root-1", budget=None):
    await bus.publish(
        topic_for(ProblemSubmitted),
        ProblemSubmitted(
            meta=make_meta(correlation_id=problem_id, root_problem_id=problem_id),
            problem_id=problem_id,
            description=f"root problem {problem_id}",
            budget=budget,
        ),
    )


async def propose(bus, subproblem_id, root_problem_id="root-1", parent_id=None, depth=1):
    await bus.publish(
        topic_for(SubproblemProposed),
        SubproblemProposed(
            meta=make_meta(correlation_id=subproblem_id, root_problem_id=root_problem_id),
            subproblem_id=subproblem_id,
            parent_id=parent_id,
            depth=depth,
            description=f"subproblem {subproblem_id}",
            capability_tags=["research"],
        ),
    )


async def accept(bus, subproblem_id, root_problem_id="root-1"):
    await bus.publish(
        topic_for(SubproblemAccepted),
        SubproblemAccepted(
            meta=make_meta(correlation_id=subproblem_id, root_problem_id=root_problem_id),
            subproblem_id=subproblem_id,
            block_index=None,  # Gatekeeper never has ledger write access
        ),
    )


async def assign(bus, subproblem_id, root_problem_id="root-1", agent_type="worker", agent_key="w-1"):
    await bus.publish(
        topic_for(SubproblemAssigned),
        SubproblemAssigned(
            meta=make_meta(correlation_id=subproblem_id, root_problem_id=root_problem_id),
            subproblem_id=subproblem_id,
            agent_type=agent_type,
            agent_key=agent_key,
            lease_expires_at=time.time() + 60,
        ),
    )


async def heartbeat(bus, subproblem_id, root_problem_id="root-1", agent_type="worker", agent_key="w-1"):
    await bus.publish(
        topic_for(WorkerHeartbeat),
        WorkerHeartbeat(
            meta=make_meta(correlation_id=subproblem_id, root_problem_id=root_problem_id),
            subproblem_id=subproblem_id,
            agent_type=agent_type,
            agent_key=agent_key,
        ),
    )


async def complete(bus, subproblem_id, root_problem_id="root-1", result=None):
    await bus.publish(
        topic_for(SubproblemCompleted),
        SubproblemCompleted(
            meta=make_meta(correlation_id=subproblem_id, root_problem_id=root_problem_id),
            subproblem_id=subproblem_id,
            result=result or {"answer": 42},
        ),
    )


async def mark_unroutable(
    bus,
    subproblem_id,
    root_problem_id="root-1",
    capability_tags=None,
    error="No capable agent type",
):
    await bus.publish(
        topic_for(SubproblemUnroutable),
        SubproblemUnroutable(
            meta=make_meta(correlation_id=subproblem_id, root_problem_id=root_problem_id),
            subproblem_id=subproblem_id,
            capability_tags=capability_tags or ["missing-capability"],
            error=error,
        ),
    )


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------
def test_happy_path_reaches_done():
    async def scenario():
        bus, blockchain, budget_ledger, recorder = make_recorder()
        await recorder.start()

        await submit_problem(bus, "root-1")
        await propose(bus, "s1", root_problem_id="root-1", parent_id=None)
        await accept(bus, "s1")
        await assign(bus, "s1")
        await heartbeat(bus, "s1")
        await complete(bus, "s1", result={"answer": 42})

        await recorder.stop()

        idx = recorder._subproblem_index["s1"]
        block = blockchain.get_block(idx)
        assert block.status == Stage.DONE.value
        assert block.data["result"] == {"answer": 42}
        assert block.data["agent_type"] == "worker"
        assert block.data["agent_key"] == "w-1"
        assert "lease_expires_at" in block.metadata
        assert recorder.query.count_in_stage(Stage.DONE) == 1
        assert recorder.errors == []

    run(scenario())


def test_unroutable_subproblem_is_persisted_as_terminal_failure():
    async def scenario():
        bus, blockchain, _budget_ledger, recorder = make_recorder()
        await recorder.start()
        await submit_problem(bus, "root-1")
        await propose(bus, "s1")
        await accept(bus, "s1")
        await mark_unroutable(
            bus,
            "s1",
            capability_tags=["missing-capability"],
            error="No capable agent type for ['missing-capability']",
        )
        await recorder.stop()
        return blockchain, recorder

    blockchain, recorder = run(scenario())
    block = blockchain.get_block(recorder._subproblem_index["s1"])
    assert block.status == Stage.FAILED.value
    assert block.metadata["failure_reason"] == "No capable agent type for ['missing-capability']"
    assert block.metadata["unroutable_capability_tags"] == ["missing-capability"]
    assert block.metadata["retriable"] is False
    assert recorder.errors == []


def test_subproblem_accepted_is_republished_with_real_block_index():
    async def scenario():
        bus, blockchain, budget_ledger, recorder = make_recorder()
        republished = []

        async def collector(event):
            republished.append(event)

        bus.subscribe(topic_for(SubproblemAccepted), collector)
        await recorder.start()

        await submit_problem(bus, "root-1")
        await propose(bus, "s1")
        await accept(bus, "s1")
        await recorder.stop()

        # collector also receives the Gatekeeper's own SubproblemAccepted
        # (block_index=None) plus the Recorder's re-publish (real index).
        real_index_events = [e for e in republished if e.block_index is not None]
        assert len(real_index_events) == 1
        idx = recorder._subproblem_index["s1"]
        assert real_index_events[0].block_index == idx
        assert real_index_events[0].subproblem_id == "s1"

    run(scenario())


def test_gatekeeper_verdict_after_budget_rejection_is_a_graceful_noop():
    async def scenario():
        bus, blockchain, budget_ledger, recorder = make_recorder(
            default_budget=DecompositionBudget(max_total_subproblems=0)
        )
        await recorder.start()

        await submit_problem(bus, "root-1")
        await propose(bus, "s1")
        # A redundant Gatekeeper verdict for a subproblem the Recorder
        # itself already rejected for budget reasons must not raise.
        await accept(bus, "s1")
        await recorder.stop()

        assert "s1" not in recorder._subproblem_index
        assert recorder.errors == []

    run(scenario())


# ----------------------------------------------------------------------
# Budget exhaustion
# ----------------------------------------------------------------------
def test_budget_exhaustion_produces_rejection_and_no_block():
    async def scenario():
        bus, blockchain, budget_ledger, recorder = make_recorder(
            default_budget=DecompositionBudget(max_total_subproblems=1)
        )
        rejections = []

        async def collector(event):
            rejections.append(event)

        bus.subscribe(topic_for(SubproblemRejected), collector)
        await recorder.start()

        await submit_problem(bus, "root-1")
        await recorder._inbox.join()  # ensure the "problem" block write has landed
        blocks_before = len(blockchain.chain)

        await propose(bus, "s1")  # consumes the only budget slot
        await propose(bus, "s2")  # must be rejected

        await recorder.stop()

        assert "s1" in recorder._subproblem_index
        assert "s2" not in recorder._subproblem_index
        assert len(rejections) == 1
        assert rejections[0].subproblem_id == "s2"
        assert rejections[0].reason == "budget_exceeded"
        # exactly one new block written (s1's), none for s2
        assert len(blockchain.chain) == blocks_before + 1

    run(scenario())


# ----------------------------------------------------------------------
# Illegal transitions are caught, not silently accepted
# ----------------------------------------------------------------------
def test_illegal_transition_is_caught_not_silently_accepted():
    async def scenario():
        bus, blockchain, budget_ledger, recorder = make_recorder()
        await recorder.start()

        await submit_problem(bus, "root-1")
        await propose(bus, "s1")  # block ends up in VALIDATING
        # Skip SubproblemAccepted entirely and try to assign directly —
        # VALIDATING -> ASSIGNED is not a legal transition.
        await assign(bus, "s1")
        await recorder.stop()

        idx = recorder._subproblem_index["s1"]
        block = blockchain.get_block(idx)
        assert block.status == Stage.VALIDATING.value  # unchanged
        assert len(recorder.errors) == 1
        assert isinstance(recorder.errors[0], ValueError)

    run(scenario())


def test_subproblem_failed_from_terminal_stage_is_surfaced_loudly():
    async def scenario():
        bus, blockchain, budget_ledger, recorder = make_recorder()
        await recorder.start()

        await submit_problem(bus, "root-1")
        await propose(bus, "s1")
        await accept(bus, "s1")
        await assign(bus, "s1")
        await heartbeat(bus, "s1")
        await complete(bus, "s1")  # -> DONE (terminal)

        # A SubproblemFailed arriving after DONE cannot legally transition
        # (DONE is terminal); must be caught, not silently swallowed, and
        # must not corrupt the DONE status already recorded.
        await bus.publish(
            topic_for(SubproblemFailed),
            SubproblemFailed(
                meta=make_meta(correlation_id="s1", root_problem_id="root-1"),
                subproblem_id="s1",
                error="stale failure report",
            ),
        )
        await recorder.stop()

        idx = recorder._subproblem_index["s1"]
        block = blockchain.get_block(idx)
        assert block.status == Stage.DONE.value
        assert len(recorder.errors) == 1

    run(scenario())


def test_lease_expired_transitions_in_progress_to_retrying():
    """A lease expiring almost always means transient infra failure (worker
    crashed, host restarted) — LeaseExpired carries no `retriable` field, so
    the Recorder treats it as retriable by default and routes to RETRYING,
    not the terminal FAILED. Whether to eventually give up is the
    Supervisor's (U6) retry-count/backoff decision, not this unit's — see
    the module docstring's "Stage-machine note".
    """

    async def scenario():
        bus, blockchain, budget_ledger, recorder = make_recorder()
        await recorder.start()

        await submit_problem(bus, "root-1")
        await propose(bus, "s1")
        await accept(bus, "s1")
        await assign(bus, "s1")
        await heartbeat(bus, "s1")  # -> IN_PROGRESS

        await bus.publish(
            topic_for(LeaseExpired),
            LeaseExpired(
                meta=make_meta(correlation_id="s1", root_problem_id="root-1"),
                subproblem_id="s1",
                agent_type="worker",
                agent_key="w-1",
            ),
        )
        await recorder.stop()

        idx = recorder._subproblem_index["s1"]
        block = blockchain.get_block(idx)
        assert block.status == Stage.RETRYING.value
        assert "lease_expired" in block.metadata["last_error"]
        assert block.metadata["retriable"] is True
        assert recorder.errors == []

    run(scenario())


def test_subproblem_failed_retriable_transitions_to_retrying_not_failed():
    """SubproblemFailed(retriable=True) must land in the non-terminal
    RETRYING stage, not FAILED — Stage.FAILED is reserved for genuinely
    terminal (retriable=False) failures. This is the ownership fix: only
    this Recorder (the sole ledger writer) can legally make this call, so
    it belongs here rather than deferred to U6/U9.
    """

    async def scenario():
        bus, blockchain, budget_ledger, recorder = make_recorder()
        await recorder.start()

        await submit_problem(bus, "root-1")
        await propose(bus, "s1")
        await accept(bus, "s1")
        await assign(bus, "s1")
        await heartbeat(bus, "s1")  # -> IN_PROGRESS

        await bus.publish(
            topic_for(SubproblemFailed),
            SubproblemFailed(
                meta=make_meta(correlation_id="s1", root_problem_id="root-1"),
                subproblem_id="s1",
                error="worker raised a transient exception",
                retriable=True,
            ),
        )
        await recorder.stop()

        idx = recorder._subproblem_index["s1"]
        block = blockchain.get_block(idx)
        assert block.status == Stage.RETRYING.value
        assert block.metadata["last_error"] == "worker raised a transient exception"
        assert block.metadata["retriable"] is True
        assert "failure_reason" not in block.metadata
        assert recorder.errors == []

    run(scenario())


def test_subproblem_failed_non_retriable_still_transitions_to_failed():
    """retriable=False is genuinely terminal — this path is unchanged."""

    async def scenario():
        bus, blockchain, budget_ledger, recorder = make_recorder()
        await recorder.start()

        await submit_problem(bus, "root-1")
        await propose(bus, "s1")
        await accept(bus, "s1")
        await assign(bus, "s1")
        await heartbeat(bus, "s1")  # -> IN_PROGRESS

        await bus.publish(
            topic_for(SubproblemFailed),
            SubproblemFailed(
                meta=make_meta(correlation_id="s1", root_problem_id="root-1"),
                subproblem_id="s1",
                error="worker raised a fatal, non-retriable exception",
                retriable=False,
            ),
        )
        await recorder.stop()

        idx = recorder._subproblem_index["s1"]
        block = blockchain.get_block(idx)
        assert block.status == Stage.FAILED.value
        assert block.metadata["failure_reason"] == "worker raised a fatal, non-retriable exception"
        assert block.metadata["retriable"] is False
        assert "last_error" not in block.metadata
        assert recorder.errors == []

    run(scenario())


def test_retrying_block_moves_back_to_assigned_via_existing_handler():
    """Once a block lands in RETRYING (via a retriable SubproblemFailed or
    a LeaseExpired), no new `handle_retry_requested` handler is needed to
    get it re-assigned: RETRYING -> ASSIGNED is already a legal transition
    (ALLOWED_TRANSITIONS[Stage.RETRYING] includes Stage.ASSIGNED), and the
    existing `_apply_subproblem_assigned` handler already applies it the
    moment the Coordinator re-publishes SubproblemAssigned after a
    RetryRequested-driven backoff.
    """

    async def scenario():
        bus, blockchain, budget_ledger, recorder = make_recorder()
        await recorder.start()

        await submit_problem(bus, "root-1")
        await propose(bus, "s1")
        await accept(bus, "s1")
        await assign(bus, "s1")
        await heartbeat(bus, "s1")  # -> IN_PROGRESS

        await bus.publish(
            topic_for(SubproblemFailed),
            SubproblemFailed(
                meta=make_meta(correlation_id="s1", root_problem_id="root-1"),
                subproblem_id="s1",
                error="transient",
                retriable=True,
            ),
        )
        # Drain (without stopping) to confirm it actually landed in
        # RETRYING before re-assigning.
        await recorder._inbox.join()
        idx = recorder._subproblem_index["s1"]
        retrying_status = blockchain.get_block(idx).status

        # Re-assign, as the Coordinator would after a RetryRequested backoff.
        await assign(bus, "s1", agent_type="worker", agent_key="w-2")
        await recorder.stop()

        block = blockchain.get_block(idx)
        return retrying_status, block, recorder.errors

    retrying_status, block, errors = run(scenario())
    assert retrying_status == Stage.RETRYING.value
    assert block.status == Stage.ASSIGNED.value
    assert block.data["agent_type"] == "worker"
    assert block.data["agent_key"] == "w-2"
    # The stale retry-tracking fields from the resolved RETRYING detour
    # must not linger once the block is actively (re)assigned again — see
    # test_successful_retry_clears_stale_retry_metadata below for the full
    # DONE-after-retry case.
    assert "last_error" not in block.metadata
    assert "retriable" not in block.metadata
    assert block.metadata["retry_history"] == [{"error": "transient", "source": "subproblem_failed"}]
    assert errors == []


def test_successful_retry_clears_stale_retry_metadata():
    """Regression test: once a block retries (RETRYING) and then goes on
    to actually finish (DONE), `last_error`/`retriable` from the resolved
    retry must not still be sitting on the block — a reader checking
    `metadata.get("retriable")`/`"last_error" in metadata` to find blocks
    currently in trouble would otherwise misreport a fully successful DONE
    block as failed. The full retry history is still preserved, just in
    the append-only `retry_history` list instead of the "current status"
    fields.
    """

    async def scenario():
        bus, blockchain, budget_ledger, recorder = make_recorder()
        await recorder.start()

        await submit_problem(bus, "root-1")
        await propose(bus, "s1")
        await accept(bus, "s1")
        await assign(bus, "s1")
        await heartbeat(bus, "s1")  # -> IN_PROGRESS

        await bus.publish(
            topic_for(SubproblemFailed),
            SubproblemFailed(
                meta=make_meta(correlation_id="s1", root_problem_id="root-1"),
                subproblem_id="s1",
                error="transient",
                retriable=True,
            ),
        )  # -> RETRYING
        await assign(bus, "s1", agent_type="worker", agent_key="w-2")  # -> ASSIGNED
        await heartbeat(bus, "s1", agent_type="worker", agent_key="w-2")  # -> IN_PROGRESS
        await complete(bus, "s1", result={"answer": 7})  # -> DONE
        await recorder.stop()

        idx = recorder._subproblem_index["s1"]
        return blockchain.get_block(idx), recorder.errors

    block, errors = run(scenario())
    assert block.status == Stage.DONE.value
    assert block.data["result"] == {"answer": 7}
    assert "last_error" not in block.metadata
    assert "retriable" not in block.metadata
    assert block.metadata["retry_history"] == [{"error": "transient", "source": "subproblem_failed"}]
    assert errors == []


def test_repeated_lease_expired_while_already_retrying_is_not_an_error():
    """A second LeaseExpired for the same subproblem while it's still
    sitting in RETRYING (awaiting Coordinator reassignment) is expected,
    normal traffic during the retry-backoff window, not corruption —
    RETRYING -> RETRYING must not be logged as an illegal transition.
    """

    async def scenario():
        bus, blockchain, budget_ledger, recorder = make_recorder()
        await recorder.start()

        await submit_problem(bus, "root-1")
        await propose(bus, "s1")
        await accept(bus, "s1")
        await assign(bus, "s1")
        await heartbeat(bus, "s1")  # -> IN_PROGRESS

        await bus.publish(
            topic_for(LeaseExpired),
            LeaseExpired(
                meta=make_meta(correlation_id="s1", root_problem_id="root-1"),
                subproblem_id="s1",
                agent_type="worker",
                agent_key="w-1",
            ),
        )  # -> RETRYING
        await bus.publish(
            topic_for(LeaseExpired),
            LeaseExpired(
                meta=make_meta(correlation_id="s1", root_problem_id="root-1"),
                subproblem_id="s1",
                agent_type="worker",
                agent_key="w-1",
            ),
        )  # still RETRYING — must be a graceful no-op, not an error
        await recorder.stop()

        idx = recorder._subproblem_index["s1"]
        return blockchain.get_block(idx), recorder.errors

    block, errors = run(scenario())
    assert block.status == Stage.RETRYING.value
    assert len(block.metadata["retry_history"]) == 2
    assert errors == []


# ----------------------------------------------------------------------
# CQRS read side
# ----------------------------------------------------------------------
def test_count_and_blocks_in_stage_reflect_writes():
    async def scenario():
        bus, blockchain, budget_ledger, recorder = make_recorder()
        await recorder.start()

        await submit_problem(bus, "root-1")
        await propose(bus, "s1")
        await propose(bus, "s2")
        await accept(bus, "s1")  # s1 -> ACCEPTED, s2 stays VALIDATING
        await recorder.stop()

        assert recorder.query.count_in_stage(Stage.VALIDATING) == 1
        assert recorder.query.count_in_stage(Stage.ACCEPTED) == 1
        blocks = recorder.query.blocks_in_stage(Stage.ACCEPTED)
        assert len(blocks) == 1
        assert blocks[0].data["subproblem_id"] == "s1"
        assert recorder.query.get_block(blocks[0].index) is blocks[0]

    run(scenario())


# ----------------------------------------------------------------------
# Real JSONFileStorage round trip (not just InMemoryStorage)
# ----------------------------------------------------------------------
def test_json_file_storage_round_trip():
    async def scenario(path):
        blockchain = Blockchain(storage=JSONFileStorage(path))
        budget_ledger = InProcessBudgetLedger(blockchain)
        bus = InMemoryEventBus()
        recorder = LedgerRecorderAgent(bus, blockchain, budget_ledger)
        recorder_module.subscribe_recorder(bus, recorder)
        await recorder.start()

        await submit_problem(bus, "root-1")
        await propose(bus, "s1")
        await accept(bus, "s1")
        await assign(bus, "s1")
        await heartbeat(bus, "s1")
        await complete(bus, "s1", result={"ok": True})
        await recorder.stop()

        return recorder._subproblem_index["s1"]

    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "chain.json")
        idx = run(scenario(path))

        assert os.path.exists(path)
        with open(path) as f:
            raw = json.load(f)
        by_index = {b["index"]: b for b in raw}
        assert by_index[idx]["status"] == Stage.DONE.value
        assert by_index[idx]["data"]["result"] == {"ok": True}

        # Simulate a process restart: reload from the same file on disk.
        reloaded = Blockchain(storage=JSONFileStorage(path))
        assert len(reloaded.chain) == len(raw)
        reloaded_block = reloaded.get_block(idx)
        assert reloaded_block.status == Stage.DONE.value

        # And prove InProcessBudgetLedger rehydrates from that same file.
        rehydrated_budget = InProcessBudgetLedger(reloaded)
        assert rehydrated_budget.consumed("root-1") == 1


# ----------------------------------------------------------------------
# Concurrency: the race this unit exists to close
# ----------------------------------------------------------------------
def test_naive_unserialized_writes_can_corrupt_the_chain():
    """Control test: WITHOUT the Recorder's single-writer-loop
    serialization, concurrent handlers that each yield control (`await`)
    between deciding to write and actually mutating `Blockchain.chain` /
    `parent.children` can interleave and corrupt the ledger — duplicate
    `.index` values, a `parent.children` list that doesn't match how many
    writes actually happened. This is the exact race
    `LedgerRecorderAgent`'s writer loop closes; this test proves the race
    is real, not hypothetical.
    """

    async def naive_add_child(blockchain, parent_index, i):
        # Mirrors what a handler would do if it wrote to the ledger
        # directly instead of enqueuing onto the Recorder's single writer
        # loop: it reads shared state, yields control (as any real
        # handler eventually does, e.g. while awaiting an upstream
        # validation call), then mutates shared state assuming nothing
        # changed underneath it.
        index = len(blockchain.chain)
        previous_hash = blockchain.chain[-1].hash
        await asyncio.sleep(0)  # yield: lets sibling coroutines interleave here
        block = Block(
            index=index,
            block_type="subproblem",
            data={"i": i},
            status=Stage.QUEUED.value,
            previous_hash=previous_hash,
            parent_index=parent_index,
        )
        parent = blockchain.chain[parent_index]
        parent.children.append(block.index)
        parent.hash = parent.compute_hash()
        blockchain.chain.append(block)
        blockchain._save()

    async def scenario():
        blockchain = Blockchain(storage=InMemoryStorage())
        parent = blockchain.add_block(block_type="problem", data={"problem_id": "root-1"}, status="in_progress")
        n = 20
        await asyncio.gather(*[naive_add_child(blockchain, parent.index, i) for i in range(n)])
        return blockchain, parent

    blockchain, parent = run(scenario())
    indices = [b.index for b in blockchain.chain if b.block_type == "subproblem"]
    # The race manifests as duplicate `.index` values assigned to
    # different Block objects (multiple coroutines read the same
    # `len(chain)` before any of them appended) — proving concurrent,
    # unserialized writers CAN corrupt this ledger.
    assert len(set(indices)) < len(indices), (
        "expected the naive unserialized writer to produce duplicate block "
        "indices under concurrency; if this fails the race stopped "
        "reproducing and this control test should be revisited"
    )


def test_recorder_concurrent_proposals_no_lost_children():
    """The actual regression test: firing many `SubproblemProposed`
    handler calls concurrently (`asyncio.gather`) for children of the same
    parent must not lose any child and must not corrupt the parent's
    `.children` list — because every one of them is funneled through the
    Recorder's single writer-loop task, so the underlying `Blockchain`
    writes are never actually concurrent no matter how concurrently the
    handlers themselves were invoked.
    """

    async def scenario():
        bus, blockchain, budget_ledger, recorder = make_recorder(
            default_budget=DecompositionBudget(max_total_subproblems=1000)
        )
        await recorder.start()
        await submit_problem(bus, "root-1")
        # let the ProblemSubmitted write land before firing children
        await recorder._inbox.join()

        n = 30
        await asyncio.gather(*[propose(bus, f"s{i}", root_problem_id="root-1") for i in range(n)])
        await recorder.stop()
        return blockchain, recorder, n

    blockchain, recorder, n = run(scenario())

    assert recorder.errors == []
    assert len(recorder._subproblem_index) == n

    problem_index = recorder._problem_index["root-1"]
    parent_block = blockchain.get_block(problem_index)
    assert len(parent_block.children) == n
    assert len(set(parent_block.children)) == n  # no duplicates

    seen_ids = set()
    for subproblem_id, idx in recorder._subproblem_index.items():
        block = blockchain.get_block(idx)
        assert block is not None
        assert block.index == idx  # index integrity: no collisions
        assert block.status == Stage.VALIDATING.value
        assert block.parent_index == problem_index
        assert idx in parent_block.children
        seen_ids.add(subproblem_id)

    assert seen_ids == {f"s{i}" for i in range(n)}
    assert recorder.query.count_in_stage(Stage.VALIDATING) == n


# ----------------------------------------------------------------------
# Regression tests for bugs found (and fixed) during code review
# ----------------------------------------------------------------------
def test_start_after_stop_resumes_processing():
    """stop() leaves `_stop_requested=True`; start() must reset it, or the
    writer loop exits again after at most one processed item.
    """

    async def scenario():
        bus, blockchain, budget_ledger, recorder = make_recorder()
        await recorder.start()
        await submit_problem(bus, "root-1")
        await recorder.stop()

        # Resume on the same recorder instance (a real caller doing e.g.
        # a brief pause/resume within one process lifetime).
        await recorder.start()
        await propose(bus, "s1")
        await accept(bus, "s1")
        await assign(bus, "s1")
        await recorder.stop()
        return recorder, blockchain

    recorder, blockchain = run(scenario())
    assert recorder.errors == []
    idx = recorder._subproblem_index["s1"]
    assert blockchain.get_block(idx).status == Stage.ASSIGNED.value


def test_recorder_rehydrates_indices_from_preexisting_chain():
    """Simulates a restart: a Blockchain already has 'problem'/'subproblem'
    blocks from a previous process. A freshly constructed
    LedgerRecorderAgent on top of it must recognize those subproblem_ids
    instead of treating every at-least-once-redelivered event for them as
    'unknown subproblem_id' and dropping it.
    """
    blockchain = Blockchain(storage=InMemoryStorage())
    budget = DecompositionBudget(max_total_subproblems=50)
    problem_block = blockchain.add_block(
        block_type="problem",
        data={"problem_id": "root-1", "description": "root"},
        status=Stage.IN_PROGRESS.value,
        metadata={"budget_consumed": 1, "budget": budget.model_dump()},
    )
    sub_block = blockchain.add_block(
        block_type="subproblem",
        data={"subproblem_id": "s1", "root_problem_id": "root-1", "description": "..."},
        status=Stage.ASSIGNED.value,
        parent_index=problem_block.index,
        metadata={"lease_expires_at": time.time() + 60},
    )

    budget_ledger = InProcessBudgetLedger(blockchain)
    bus = InMemoryEventBus()
    recorder = LedgerRecorderAgent(bus, blockchain, budget_ledger)
    recorder_module.subscribe_recorder(bus, recorder)

    # The rehydrated indices must be usable immediately, with no events
    # replayed yet.
    assert recorder._problem_index["root-1"] == problem_block.index
    assert recorder._subproblem_index["s1"] == sub_block.index
    assert recorder.query.count_in_stage(Stage.ASSIGNED) == 1

    async def scenario():
        await recorder.start()
        # A redelivered WorkerHeartbeat for the subproblem that already
        # existed before this process started must be applied, not
        # dropped as "unknown subproblem_id".
        await heartbeat(bus, "s1", root_problem_id="root-1")
        await complete(bus, "s1", root_problem_id="root-1", result={"answer": 1})
        await recorder.stop()

    run(scenario())
    assert recorder.errors == []
    assert blockchain.get_block(sub_block.index).status == Stage.DONE.value


def test_subproblem_completed_before_first_heartbeat_still_reaches_done():
    """A fast worker can finish before its first WorkerHeartbeat ever
    bumped the block from ASSIGNED to IN_PROGRESS. ALLOWED_TRANSITIONS has
    no ASSIGNED -> VERIFYING edge, so the Recorder must advance through
    IN_PROGRESS on the fly instead of losing the completion.
    """

    async def scenario():
        bus, blockchain, budget_ledger, recorder = make_recorder()
        await recorder.start()
        await submit_problem(bus, "root-1")
        await propose(bus, "s1")
        await accept(bus, "s1")
        await assign(bus, "s1")
        # No heartbeat this time — go straight to completed.
        await complete(bus, "s1", result={"answer": 7})
        await recorder.stop()
        return blockchain, recorder

    blockchain, recorder = run(scenario())
    idx = recorder._subproblem_index["s1"]
    block = blockchain.get_block(idx)
    assert block.status == Stage.DONE.value
    assert block.data["result"] == {"answer": 7}
    assert recorder.errors == []


def test_stale_failed_after_done_does_not_corrupt_the_done_block():
    """A slow worker's failure report can race a faster success path and
    arrive after the block is already DONE. It must not silently overwrite
    `data['result']`/turn a successful block into one that looks failed —
    it's recorded as a late event breadcrumb instead.
    """

    async def scenario():
        bus, blockchain, budget_ledger, recorder = make_recorder()
        await recorder.start()
        await submit_problem(bus, "root-1")
        await propose(bus, "s1")
        await accept(bus, "s1")
        await assign(bus, "s1")
        await complete(bus, "s1", result={"answer": 99})  # -> DONE

        await bus.publish(
            topic_for(SubproblemFailed),
            SubproblemFailed(
                meta=make_meta(correlation_id="s1", root_problem_id="root-1"),
                subproblem_id="s1",
                error="stale failure report",
            ),
        )
        await recorder.stop()
        return blockchain, recorder

    blockchain, recorder = run(scenario())
    idx = recorder._subproblem_index["s1"]
    block = blockchain.get_block(idx)
    assert block.status == Stage.DONE.value
    assert block.data["result"] == {"answer": 99}
    assert "failure_reason" not in block.metadata
    assert block.metadata["late_events"][0]["kind"] == "subproblem_failed"
    assert len(recorder.errors) == 1


def test_stale_assigned_does_not_overwrite_agent_identity_of_in_progress_block():
    """A duplicate/out-of-order SubproblemAssigned arriving after a
    subproblem is already IN_PROGRESS under a different agent must not
    silently swap agent_type/agent_key — the Reaper (U7) trusts those
    fields to know who currently owns the lease.
    """

    async def scenario():
        bus, blockchain, budget_ledger, recorder = make_recorder()
        await recorder.start()
        await submit_problem(bus, "root-1")
        await propose(bus, "s1")
        await accept(bus, "s1")
        await assign(bus, "s1", agent_type="worker", agent_key="real-owner")
        await heartbeat(bus, "s1")  # -> IN_PROGRESS

        # A stale re-assignment for a different agent, arriving late.
        await assign(bus, "s1", agent_type="worker", agent_key="stale-imposter")
        await recorder.stop()
        return blockchain, recorder

    blockchain, recorder = run(scenario())
    idx = recorder._subproblem_index["s1"]
    block = blockchain.get_block(idx)
    assert block.status == Stage.IN_PROGRESS.value
    assert block.data["agent_key"] == "real-owner"
    assert len(recorder.errors) == 1


def test_budget_consumed_is_crash_safe_even_without_problem_submitted():
    """A SubproblemProposed can legitimately arrive before/without its
    root's ProblemSubmitted ever being observed by this process. The
    Recorder must still synthesize a 'problem' block so budget
    consumption is persisted crash-safely, not just tracked in memory.
    """

    async def scenario():
        blockchain = Blockchain(storage=InMemoryStorage())
        budget_ledger = InProcessBudgetLedger(blockchain)
        bus = InMemoryEventBus()
        recorder = LedgerRecorderAgent(
            bus, blockchain, budget_ledger, default_budget=DecompositionBudget(max_total_subproblems=5)
        )
        recorder_module.subscribe_recorder(bus, recorder)
        await recorder.start()
        # No submit_problem() call at all.
        await propose(bus, "s1", root_problem_id="orphan-root")
        await recorder.stop()
        return blockchain, recorder

    blockchain, recorder = run(scenario())
    assert "orphan-root" in recorder._problem_index
    problem_block = blockchain.get_block(recorder._problem_index["orphan-root"])
    assert problem_block.metadata["budget_consumed"] == 1

    # And it truly is crash-safe: a fresh InProcessBudgetLedger built on
    # top of the same blockchain rehydrates the same count.
    rehydrated = InProcessBudgetLedger(blockchain)
    assert rehydrated.consumed("orphan-root") == 1
