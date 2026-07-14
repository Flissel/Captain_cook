"""End-to-end smoke test for the unit-U11 integration
(agenten.orchestration.pipeline.build_pipeline): submits a synthetic
"Problem X" through the REAL wiring of every already-merged unit (U2-U5,
U7, U8, U10) over InMemoryEventBus + a real Blockchain, and asserts the
whole chain settles into a correct, fully-terminal ledger state.

Also covers the crash-recovery story (U9): a "crashed" first process that
only got as far as writing SubproblemProposed (blocks stuck at
Stage.VALIDATING, no Gatekeeper/Coordinator/Worker ever ran) is recovered
by building a FRESH pipeline against the SAME storage and calling
agenten.ledger_bridge.recovery.recover_on_startup against it -- proving
recovery integrates with the full wired-up pipeline, not just its own
isolated unit tests (see tests/ledger_bridge/test_recovery.py, which uses a
hand-rolled FakeLedgerQuery instead of a real Blockchain/pipeline).

Note on what recovery scenario this covers without unit U6 (Supervisor):
recover_on_startup's QUEUED/VALIDATING and ACCEPTED branches re-publish
SubproblemProposed/SubproblemAccepted, both of which the already-wired
Gatekeeper -> Coordinator -> Worker -> Recorder chain can carry all the way
to Stage.DONE on its own. Its ASSIGNED/IN_PROGRESS (LeaseExpired) branch
cannot be driven all the way to DONE without a SupervisorAgent deciding
retry-vs-escalate policy (see recorder.py's module docstring: "Deciding
*when* to give up retrying ... is the Supervisor's (U6) job") -- that half
is intentionally left as the TODO(U6) follow-up described in
agenten/orchestration/pipeline.py, not exercised here.
"""
import pytest

from blockchain.Blockchain_modell import Blockchain
from blockchain.storage import InMemoryStorage

from agenten.events.schemas import SubproblemProposed, make_meta, topic_for
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
# Failure path (waiting on U6 Supervisor)
# ----------------------------------------------------------------------
@pytest.mark.skip(reason="waiting on U6 Supervisor (agenten/supervision/supervisor.py)")
async def test_failure_path_retriable_failure_is_retried_to_done_via_supervisor():
    """Sketch of the failure-path smoke test to enable once unit U6 lands.

    Wiring to add in this test (mirroring the TODO(U6) markers in
    agenten/orchestration/pipeline.py's build_pipeline):
      supervisor = SupervisorAgent(bus, ledger_query, ...)
      bus.subscribe(topic_for(SubproblemFailed), supervisor.handle_subproblem_failed)
      bus.subscribe(topic_for(LeaseExpired), supervisor.handle_lease_expired)
    (its RetryRequested output feeds the already-subscribed
    coordinator.handle_retry_requested; its EscalateToRedecompose output
    feeds the already-subscribed decomposer.handle_escalate_to_redecompose.)

    Scenario to assert:
    1. Build the pipeline with a flaky worker whose execute() raises
       WorkerExecutionError(retriable=True) on the first call and succeeds
       on the second (register its capability tag in the registry and
       subscribe it to SubproblemAssigned, same as EchoWorker in
       build_pipeline).
    2. Submit a problem decomposed into 1 subproblem routed to that worker.
    3. Assert the block transitions ASSIGNED/IN_PROGRESS -> RETRYING on the
       first failure (recorder routes retriable failures to Stage.RETRYING,
       see recorder.py), the Supervisor publishes RetryRequested with its
       backoff, the Coordinator re-publishes SubproblemAssigned
       (RETRYING -> ASSIGNED), and the second attempt reaches Stage.DONE.
    4. Assert metadata["retry_history"] on the block records the first
       failure, and that last_error/retriable were cleared on reassignment.
    5. Separately: a worker that always fails should, after the
       Supervisor's max-retry policy, produce EscalateToRedecompose and
       drive the Decomposer's handle_escalate_to_redecompose (already wired
       with a real describe_subproblem lookup in build_pipeline).
    """
