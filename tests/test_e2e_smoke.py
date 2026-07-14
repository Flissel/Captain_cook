"""End-to-end smoke test for the unit-U11 integration
(agenten.orchestration.pipeline.build_pipeline): submits a synthetic
"Problem X" through the REAL wiring of every already-merged unit (U2-U8,
U10) over InMemoryEventBus + a real Blockchain, and asserts the
whole chain settles into a correct, fully-terminal ledger state.

Also covers the crash-recovery story (U9): a "crashed" first process that
only got as far as writing SubproblemProposed (blocks stuck at
Stage.VALIDATING, no Gatekeeper/Coordinator/Worker ever ran) is recovered
by building a FRESH pipeline against the SAME storage and calling
agenten.ledger_bridge.recovery.recover_on_startup against it -- proving
recovery integrates with the full wired-up pipeline, not just its own
isolated unit tests (see tests/ledger_bridge/test_recovery.py, which uses a
hand-rolled FakeLedgerQuery instead of a real Blockchain/pipeline).

The failure path (Supervisor, unit U6: retriable failure -> Stage.RETRYING
-> RetryRequested -> reassignment -> Stage.DONE, and escalation to the
Decomposer once retries are exhausted) is covered by the two
test_failure_path_* tests at the bottom of this module, against the same
full build_pipeline() wiring.
"""
import pytest

from blockchain.Blockchain_modell import Blockchain
from blockchain.storage import InMemoryStorage

from agenten.events.schemas import (
    EscalateToRedecompose,
    RetryRequested,
    SubproblemAssigned,
    SubproblemProposed,
    make_meta,
    topic_for,
)
from agenten.ledger_bridge.query import InProcessBudgetLedger
from agenten.ledger_bridge.recorder import LedgerRecorderAgent
from agenten.ledger_bridge.recovery import recover_on_startup
from agenten.ledger_bridge.stage_machine import TERMINAL_STAGES, Stage
from agenten.orchestration.pipeline import (
    PipelineBootError,
    _validate_capability_registry_at_boot,
    build_pipeline,
)
from agenten.runtime.event_bus import InMemoryEventBus
from agenten.spawning.capability_registry import CapabilityRegistry
from agenten.supervision.supervisor import SupervisorAgent
from agenten.workers.base import WorkerAgent, WorkerExecutionError

pytestmark = pytest.mark.asyncio


# ----------------------------------------------------------------------
# Canned LLM stand-ins (deterministic, no network/model dependency).
# ----------------------------------------------------------------------
async def canned_llm_decompose(description, depth):
    """Splits any depth-0 problem into exactly 2 atomic, echo-routable
    subproblems; returns nothing at any deeper depth (nothing in this
    suite re-decomposes).
    """
    if depth != 0:
        return []
    return [
        {
            "description": f"Research phase for: {description}",
            "capability_tags": ["echo"],
            "atomic": True,
        },
        {
            "description": f"Execution phase for: {description}",
            "capability_tags": ["echo"],
            "atomic": True,
        },
    ]


async def canned_llm_judge(description, ruleset):
    """Always accepts -- layer-1 deterministic checks already ran first."""
    return True


def make_pipeline(blockchain=None, **kwargs):
    kwargs.setdefault("llm_decompose", canned_llm_decompose)
    kwargs.setdefault("llm_judge", canned_llm_judge)
    if blockchain is not None:
        kwargs["blockchain"] = blockchain
    return build_pipeline(**kwargs)


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------
async def test_happy_path_both_subproblems_reach_done():
    chain = Blockchain(storage=InMemoryStorage())
    pipeline = make_pipeline(blockchain=chain)
    await pipeline.start()

    problem_id = await pipeline.submit_problem("Problem X: launch the new product line")

    converged = await pipeline.wait_until_terminal(expected_subproblem_count=2, timeout=5.0)
    await pipeline.stop()

    assert converged, "pipeline did not reach a terminal ledger state within the timeout"
    assert pipeline.recorder.errors == []

    # -- both subproblems DONE --
    done_blocks = pipeline.ledger_query.blocks_in_stage(Stage.DONE)
    assert len(done_blocks) == 2
    for block in done_blocks:
        assert block.block_type == "subproblem"
        assert block.data["result"] == {"echo": block.data["description"]}
        assert block.data["agent_type"] == "echo_worker"

    # -- no stuck subproblem WIP: every non-terminal stage is either empty,
    # or (see module docstring / recorder.py) occupied only by the parent
    # "problem" block, which by U8's own design is written at
    # Stage.IN_PROGRESS and never transitioned again -- it is not part of
    # the subproblem lifecycle stage_machine.py's ALLOWED_TRANSITIONS
    # governs, so it is *expected* to still be sitting there, not a bug.
    for stage in Stage:
        if stage in TERMINAL_STAGES:
            continue
        for block in pipeline.ledger_query.blocks_in_stage(stage):
            assert block.block_type != "subproblem", (
                f"subproblem block {block.index} stuck in non-terminal stage {stage} at end of run"
            )

    # -- parent "problem" block's .children lists both subproblem indices --
    problem_blocks = chain.get_blocks_by_type("problem")
    assert len(problem_blocks) == 1
    problem_block = problem_blocks[0]
    assert problem_block.data["problem_id"] == problem_id
    assert sorted(problem_block.children) == sorted(b.index for b in done_blocks)


async def test_gatekeeper_rejects_malformed_candidate_without_blocking_the_valid_one():
    """Not required by the smoke-test spec, but cheap insurance that a
    Gatekeeper rejection (layer-1 deterministic check) doesn't wedge the
    rest of the pipeline -- one candidate is malformed (description too
    short), the other is fine and should still reach DONE.
    """

    async def mixed_decompose(description, depth):
        if depth != 0:
            return []
        return [
            {"description": "x", "capability_tags": ["echo"]},  # malformed: too short
            {"description": "A perfectly fine subproblem description", "capability_tags": ["echo"], "atomic": True},
        ]

    chain = Blockchain(storage=InMemoryStorage())
    pipeline = make_pipeline(blockchain=chain, llm_decompose=mixed_decompose)
    await pipeline.start()

    await pipeline.submit_problem("Problem Y: something else entirely here")
    converged = await pipeline.wait_until_terminal(expected_subproblem_count=1, timeout=5.0)
    await pipeline.stop()

    assert converged
    assert pipeline.ledger_query.count_in_stage(Stage.DONE) == 1
    assert pipeline.ledger_query.count_in_stage(Stage.REJECTED) == 1


async def test_boot_validation_rejects_capability_registered_to_unknown_agent_type():
    """CapabilityRegistry is "validated at boot, not trusted blindly" per
    its own module docstring -- prove the validator build_pipeline calls
    at the end of construction actually enforces that (a capability tag
    resolving to an agent type nothing was constructed/subscribed for
    fails fast) instead of silently building a pipeline that would
    dispatch into a void at runtime.
    """
    registry = CapabilityRegistry()
    registry.register("ghost", "no_such_worker_type")
    with pytest.raises(PipelineBootError):
        _validate_capability_registry_at_boot(registry, subscribed_worker_types=set())


async def test_submit_problem_on_unstarted_pipeline_raises():
    """Publishing into a never-started pipeline enqueues ledger writes into
    the Recorder's inbox that nothing ever drains -- the submission would
    return a problem_id for work that silently never happens. submit_problem
    must refuse loudly instead.
    """
    pipeline = make_pipeline(blockchain=Blockchain(storage=InMemoryStorage()))
    with pytest.raises(RuntimeError, match="not started"):
        await pipeline.submit_problem("Problem W: submitted before start()")

    # ...and again after stop(): the writer loop is gone, same silent-limbo
    # failure mode.
    await pipeline.start()
    await pipeline.stop()
    with pytest.raises(RuntimeError, match="not started"):
        await pipeline.submit_problem("Problem W2: submitted after stop()")


async def test_wait_until_terminal_counts_blockless_budget_rejections():
    """Budget-exhaustion rejections are terminal but write NO ledger block
    (recorder.py's documented design choice), so wait_until_terminal must
    count them via the pipeline's own budget_rejections counter or a caller
    passing expected_subproblem_count = number-proposed waits forever on a
    pipeline that has fully settled: with max_total_subproblems=1 and 2
    proposals, exactly 1 reaches DONE and 1 is budget-rejected blocklessly.
    """
    from agenten.decomposition.budget import DecompositionBudget

    pipeline = make_pipeline(
        blockchain=Blockchain(storage=InMemoryStorage()),
        default_budget=DecompositionBudget(max_total_subproblems=1),
    )
    await pipeline.start()
    await pipeline.submit_problem("Problem V: two proposals, budget for one")
    converged = await pipeline.wait_until_terminal(expected_subproblem_count=2, timeout=5.0)
    await pipeline.stop()

    assert converged, "wait_until_terminal must count the blockless budget rejection as terminal"
    assert pipeline.budget_rejections == 1
    assert pipeline.ledger_query.count_in_stage(Stage.DONE) == 1
    # The rejected proposal got no block at all (design choice, see
    # recorder.py), so REJECTED-stage count stays 0.
    assert pipeline.ledger_query.count_in_stage(Stage.REJECTED) == 0


# ----------------------------------------------------------------------
# Crash recovery (U9), integrated against the full wired-up pipeline.
# ----------------------------------------------------------------------
async def test_crash_recovery_republishes_stuck_subproblems_to_completion():
    storage = InMemoryStorage()
    chain = Blockchain(storage=storage)

    # --- Simulated crash: a first process only had the Ledger Recorder
    # running (the sole ledger writer -- see recorder.py) and crashed right
    # after two SubproblemProposed events were recorded. No
    # Gatekeeper/Coordinator/Worker ever ran, so both subproblems are stuck
    # at Stage.VALIDATING with no further progress possible until someone
    # re-derives the event that should still be pending for them.
    crashed_bus = InMemoryEventBus()
    budget_ledger = InProcessBudgetLedger(chain)
    crashed_recorder = LedgerRecorderAgent(crashed_bus, chain, budget_ledger)
    await crashed_recorder.start()
    for subproblem_id, description in [
        ("crash-sp-1", "Stuck subproblem one, never validated"),
        ("crash-sp-2", "Stuck subproblem two, never validated"),
    ]:
        await crashed_bus.publish(
            topic_for(SubproblemProposed),
            SubproblemProposed(
                meta=make_meta(correlation_id=subproblem_id, root_problem_id="crash-root"),
                subproblem_id=subproblem_id,
                parent_id=None,
                depth=1,
                description=description,
                capability_tags=["echo"],
            ),
        )
    await crashed_recorder.stop()

    assert crashed_recorder.query.count_in_stage(Stage.VALIDATING) == 2
    assert crashed_recorder.errors == []

    # --- "Restart": a fresh pipeline, fresh EventBus, fresh in-process
    # BudgetLedger/LedgerQuery -- none of the crashed process's in-memory
    # state survives -- constructed against a brand-new Blockchain object
    # pointed at the SAME underlying storage. LedgerRecorderAgent._rehydrate()
    # (called from its own __init__) reconstructs _subproblem_index /
    # _problem_index / cached budgets from the reloaded chain, exactly as
    # it would after a real process restart.
    #
    # Deliberately call recover_on_startup() BEFORE pipeline.start(): the
    # Ledger Recorder's writer-loop task (spawned by start()) is what
    # actually *drains* its inbox -- constructing it (which happens inside
    # build_pipeline already) is enough to safely *enqueue* into it.
    # Re-publishing everything recover_on_startup finds stuck, before any
    # background draining/cascading can race against that same scan, is
    # what makes "scan the ledger once, re-derive events for what's stuck"
    # a well-defined, deterministic startup step -- matching how a real
    # boot sequence would run recovery before opening the pipeline up to
    # concurrent processing, not interleaved with it.
    fresh_chain = Blockchain(storage=storage)
    fresh_pipeline = make_pipeline(blockchain=fresh_chain)

    summary = await recover_on_startup(fresh_pipeline.bus, fresh_pipeline.ledger_query)
    assert summary["queued_or_validating"] == 2
    assert summary["accepted"] == 0
    assert summary["lease_expired"] == 0
    assert summary["unhandled_stage_flagged"] == 0

    await fresh_pipeline.start()
    converged = await fresh_pipeline.wait_until_terminal(expected_subproblem_count=2, timeout=5.0)
    await fresh_pipeline.stop()

    assert converged, "recovered subproblems did not reach a terminal ledger state within the timeout"
    assert fresh_pipeline.recorder.errors == []
    assert fresh_pipeline.ledger_query.count_in_stage(Stage.DONE) == 2

    recovered_descriptions = {b.data["subproblem_id"] for b in fresh_pipeline.ledger_query.blocks_in_stage(Stage.DONE)}
    assert recovered_descriptions == {"crash-sp-1", "crash-sp-2"}


async def test_crash_recovery_on_fully_terminal_ledger_finds_nothing_to_recover():
    """The simplest recovery scenario: run the happy path to completion
    (everything terminal), then simulate a restart against the SAME
    storage and confirm recover_on_startup correctly reports there is
    nothing left to recover -- it must not fabricate work for blocks that
    already finished.
    """
    storage = InMemoryStorage()
    chain = Blockchain(storage=storage)
    pipeline = make_pipeline(blockchain=chain)
    await pipeline.start()
    await pipeline.submit_problem("Problem X: launch the new product line")
    assert await pipeline.wait_until_terminal(expected_subproblem_count=2, timeout=5.0)
    await pipeline.stop()
    assert pipeline.ledger_query.count_in_stage(Stage.DONE) == 2

    fresh_chain = Blockchain(storage=storage)
    fresh_pipeline = make_pipeline(blockchain=fresh_chain)
    summary = await recover_on_startup(fresh_pipeline.bus, fresh_pipeline.ledger_query)

    assert summary == {
        "queued_or_validating": 0,
        "accepted": 0,
        "lease_expired": 0,
        "stuck_retrying_flagged": 0,
        "lease_missing_flagged": 0,
        "unhandled_stage_flagged": 0,
    }
    assert fresh_pipeline.ledger_query.count_in_stage(Stage.DONE) == 2


async def test_crash_recovery_completes_block_stuck_at_queued_mid_handler():
    """Regression test for the sharpest crash window: the previous process
    died AFTER the Recorder's add_block persisted the subproblem block at
    Stage.QUEUED but BEFORE the same handler's QUEUED -> VALIDATING
    transition. On restart the Recorder rehydrates the block into its
    _subproblem_index, so the recovery-replayed SubproblemProposed hits the
    Recorder's dedup guard -- which must COMPLETE the interrupted
    transition (QUEUED -> VALIDATING) rather than plain-return, or the
    block is stuck at QUEUED forever: every Gatekeeper verdict no-ops
    against a non-VALIDATING block, and every future recovery pass would
    republish the same event into the same no-op.
    """
    storage = InMemoryStorage()
    chain = Blockchain(storage=storage)

    # Write exactly what the crashed process would have persisted: a
    # 'problem' block and a subproblem block frozen at QUEUED (add_block
    # done, transition not) -- same shapes recorder.py's
    # _write_problem_block/_apply_subproblem_proposed produce.
    from agenten.decomposition.budget import DecompositionBudget

    problem_block = chain.add_block(
        block_type="problem",
        data={"problem_id": "root-q", "description": "root problem root-q"},
        status=Stage.IN_PROGRESS.value,
        metadata={"budget_consumed": 1, "budget": DecompositionBudget().model_dump()},
    )
    chain.add_block(
        block_type="subproblem",
        data={
            "subproblem_id": "queued-sp-1",
            "description": "Subproblem interrupted mid-handler at QUEUED",
            "capability_tags": ["echo"],
            "parent_subproblem_id": None,
            "depth": 1,
            "root_problem_id": "root-q",
        },
        status=Stage.QUEUED.value,
        parent_index=problem_block.index,
    )

    fresh_chain = Blockchain(storage=storage)
    fresh_pipeline = make_pipeline(blockchain=fresh_chain)

    summary = await recover_on_startup(fresh_pipeline.bus, fresh_pipeline.ledger_query)
    assert summary["queued_or_validating"] == 1

    await fresh_pipeline.start()
    converged = await fresh_pipeline.wait_until_terminal(expected_subproblem_count=1, timeout=5.0)
    await fresh_pipeline.stop()

    assert converged, "QUEUED-stuck block was not driven to a terminal stage by recovery replay"
    assert fresh_pipeline.recorder.errors == []
    done_blocks = fresh_pipeline.ledger_query.blocks_in_stage(Stage.DONE)
    assert [b.data["subproblem_id"] for b in done_blocks] == ["queued-sp-1"]
    # And crucially: no duplicate block was written for the replayed event.
    subproblem_blocks = fresh_pipeline.blockchain.get_blocks_by_type("subproblem")
    assert len(subproblem_blocks) == 1


# ----------------------------------------------------------------------
# Failure path (U6 Supervisor, wired into build_pipeline)
# ----------------------------------------------------------------------
class FlakyWorker(WorkerAgent):
    """Raises WorkerExecutionError(retriable=True) for the first
    ``fail_times`` execute() calls, then succeeds -- the failure-path
    counterpart of EchoWorker. Test-local on purpose: no production module
    needs a deliberately failing worker.
    """

    agent_type = "flaky_worker"
    capability_tags = ["flaky"]

    def __init__(self, *args, fail_times: int = 1, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_times = fail_times
        self.calls = 0

    async def execute(self, subproblem_id, description):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise WorkerExecutionError(f"transient flake #{self.calls}", retriable=True)
        return {"flaky": description}


def install_flaky_worker(pipeline, fail_times: int) -> FlakyWorker:
    """Register a FlakyWorker on an already-built pipeline, the same way
    build_pipeline registers EchoWorker: capability tag in the registry +
    a SubproblemAssigned subscription. Subscribing after build_pipeline is
    what keeps the Supervisor's handle_assigned ahead of the worker in
    InMemoryEventBus's delivery order (see build_pipeline's
    subscription-order note).
    """
    worker = FlakyWorker(pipeline.bus, pipeline.tools, fail_times=fail_times)
    pipeline.capability_registry.register("flaky", worker.agent_type)
    pipeline.bus.subscribe(topic_for(SubproblemAssigned), worker.handle_subproblem_assigned)
    pipeline.workers[worker.agent_type] = worker
    return worker


def subscribe_recording(pipeline, event_type):
    """Record every event of ``event_type`` published on the pipeline bus."""
    events = []

    async def record(event):
        events.append(event)

    pipeline.bus.subscribe(topic_for(event_type), record)
    return events


async def flaky_decompose(description, depth):
    if depth != 0:
        return []
    return [
        {
            "description": f"Flaky phase for: {description}",
            "capability_tags": ["flaky"],
            "atomic": True,
        }
    ]


def make_failure_supervisor(bus, **kwargs):
    """Supervisor tuned for the failure-path tests, constructed against the
    same bus the pipeline will use (build_pipeline requires that for an
    injected supervisor):

    - backoff_initial_delay_seconds=0.01: the Coordinator really does
      asyncio.sleep(delay_seconds) before reassigning, and the first-retry
      delay equals the initial delay (base ** 0 == 1.0), so scale the
      whole backoff curve down to keep the tests fast.
    - circuit_failure_threshold=1.0: a single failure in an empty rolling
      window is failure fraction 1.0, which would trip the default 0.5
      breaker and turn our deliberate one-off failures into the
      Coordinator's 30-second circuit-open deferral. Circuit behavior has
      its own unit tests (tests/test_supervisor.py); these tests are about
      the retry/escalation path.
    """
    kwargs.setdefault("backoff_initial_delay_seconds", 0.01)
    kwargs.setdefault("circuit_failure_threshold", 1.0)
    return SupervisorAgent(bus, **kwargs)


async def test_failure_path_retriable_failure_is_retried_to_done_via_supervisor():
    """One retriable failure, then success: the Supervisor (wired in
    build_pipeline) answers the SubproblemFailed with RetryRequested, the
    Coordinator reassigns after the backoff, and the second attempt reaches
    Stage.DONE -- with the first failure preserved in retry_history and the
    last_error/retriable convenience fields cleared on reassignment.
    """
    chain = Blockchain(storage=InMemoryStorage())
    bus = InMemoryEventBus()
    pipeline = make_pipeline(
        blockchain=chain,
        llm_decompose=flaky_decompose,
        bus=bus,
        supervisor=make_failure_supervisor(bus),
    )
    worker = install_flaky_worker(pipeline, fail_times=1)
    retry_events = subscribe_recording(pipeline, RetryRequested)
    escalate_events = subscribe_recording(pipeline, EscalateToRedecompose)

    await pipeline.start()
    await pipeline.submit_problem("Problem F: needs one retry to land")
    converged = await pipeline.wait_until_terminal(expected_subproblem_count=1, timeout=5.0)
    await pipeline.stop()

    assert converged, "retried subproblem did not reach a terminal ledger state within the timeout"
    assert pipeline.recorder.errors == []
    assert worker.calls == 2  # first attempt failed, second succeeded
    assert escalate_events == []

    # The Supervisor answered the failure with exactly one RetryRequested,
    # carrying its (scaled) first-attempt backoff delay.
    assert len(retry_events) == 1
    assert retry_events[0].delay_seconds == pytest.approx(0.01)

    # Second attempt ended at DONE...
    done_blocks = pipeline.ledger_query.blocks_in_stage(Stage.DONE)
    assert len(done_blocks) == 1
    block = done_blocks[0]
    assert block.data["agent_type"] == "flaky_worker"
    assert block.data["result"] == {"flaky": block.data["subproblem_id"]}
    assert retry_events[0].subproblem_id == block.data["subproblem_id"]

    # ...via RETRYING: retry_history is only ever written on the recorder's
    # RETRYING path (_apply_retry_transition), and recorder.errors == []
    # above rules out any illegal-transition shortcut around it
    # (ASSIGNED -> ASSIGNED without the RETRYING hop would have errored).
    history = block.metadata["retry_history"]
    assert len(history) == 1
    assert history[0]["source"] == "subproblem_failed"
    assert "transient flake #1" in history[0]["error"]
    # The reassignment cleared the "current trouble" convenience fields --
    # a block that went on to reach DONE no longer advertises a last_error.
    assert "last_error" not in block.metadata
    assert "retriable" not in block.metadata


async def test_failure_path_exhausted_retries_escalate_to_redecompose():
    """A worker that always fails: after max_retries the Supervisor gives
    up and publishes EscalateToRedecompose, which drives the Decomposer's
    handle_escalate_to_redecompose through build_pipeline's ledger-backed
    describe_subproblem lookup -- the replacement subproblem (routed to the
    always-healthy echo worker) then reaches DONE.
    """
    decompose_calls = []

    async def flaky_then_echo_decompose(description, depth):
        decompose_calls.append((description, depth))
        if depth == 0:
            return [
                {
                    "description": f"Flaky phase for: {description}",
                    "capability_tags": ["flaky"],
                    "atomic": True,
                }
            ]
        # Re-decomposition of the escalated subproblem: hand the work to
        # the echo worker instead. Kept SHORTER than the parent flaky
        # subproblem's description -- the Gatekeeper rejects a child longer
        # than its parent as "not a minimal decomposition".
        return [
            {
                "description": "Replacement echo step for problem G",
                "capability_tags": ["echo"],
                "atomic": True,
            }
        ]

    chain = Blockchain(storage=InMemoryStorage())
    bus = InMemoryEventBus()
    pipeline = make_pipeline(
        blockchain=chain,
        llm_decompose=flaky_then_echo_decompose,
        bus=bus,
        supervisor=make_failure_supervisor(bus, max_retries=1),
    )
    worker = install_flaky_worker(pipeline, fail_times=10_000)  # never succeeds
    retry_events = subscribe_recording(pipeline, RetryRequested)
    escalate_events = subscribe_recording(pipeline, EscalateToRedecompose)

    await pipeline.start()
    await pipeline.submit_problem("Problem G: flaky work that needs re-planning")
    # Terminal outcomes: only the replacement (echo) subproblem reaches
    # DONE. The escalated flaky block itself deliberately stays at
    # Stage.RETRYING -- see below.
    converged = await pipeline.wait_until_terminal(expected_subproblem_count=1, timeout=5.0)
    await pipeline.stop()

    assert converged, "replacement subproblem did not reach a terminal ledger state within the timeout"
    assert pipeline.recorder.errors == []
    assert worker.calls == 2  # initial attempt + exactly max_retries=1 retry
    assert len(retry_events) == 1

    # The Supervisor escalated exactly once, for the flaky subproblem.
    assert len(escalate_events) == 1
    assert "exceeded max_retries" in escalate_events[0].reason

    # The Decomposer was re-driven via the ledger-backed describe_subproblem
    # lookup: its second call got the flaky subproblem's own description at
    # its recorded depth (children of a root problem sit at depth 1).
    flaky_description = "Flaky phase for: Problem G: flaky work that needs re-planning"
    assert decompose_calls[0] == ("Problem G: flaky work that needs re-planning", 0)
    assert (flaky_description, 1) in decompose_calls

    # The replacement subproblem reached DONE via the echo worker...
    done_blocks = pipeline.ledger_query.blocks_in_stage(Stage.DONE)
    assert len(done_blocks) == 1
    assert done_blocks[0].data["agent_type"] == "echo_worker"
    assert done_blocks[0].data["description"] == "Replacement echo step for problem G"

    # ...while the escalated flaky block stays parked at Stage.RETRYING
    # with its full failure forensics: escalation is terminal for the
    # Supervisor (the Decomposer re-plans), but the Recorder has no
    # EscalateToRedecompose handler, so nothing further touches the block.
    retrying_blocks = pipeline.ledger_query.blocks_in_stage(Stage.RETRYING)
    assert len(retrying_blocks) == 1
    flaky_block = retrying_blocks[0]
    assert flaky_block.data["description"] == flaky_description
    assert escalate_events[0].subproblem_id == flaky_block.data["subproblem_id"]
    assert len(flaky_block.metadata["retry_history"]) == 2  # both failed attempts
