"""build_pipeline(): the unit-U11 integration point.

Wires every already-merged unit (U2-U5, U7, U8, U10) onto a single shared
``EventBus`` and a single real ``Blockchain``, and returns a
``SupplyChainPipeline`` handle a caller (``CaptainAgent``,
``examples/armada_demo.py``, ``tests/test_e2e_smoke.py``) can submit a
problem through and inspect the resulting ledger with.

Bus choice: ``InMemoryEventBus`` is used here on purpose. It is the
deterministic, synchronous-per-handler, fast path every other unit's own
tests were written against (see e.g. ``tests/ledger_bridge/test_recorder.py``'s
module docstring), and it is a strict *port* implementation --
``agenten.runtime.event_bus.EventBus`` -- so nothing downstream needs to
change to run for real.

Swapping in ``AutoGenEventBus`` (``agenten.runtime.autogen_bus`` +
``agenten.runtime.bootstrap.build_runtime_and_bus()``) is the production
path, but it is NOT a drop-in replacement for the business-logic wiring
below: ``AutoGenEventBus.subscribe()`` intentionally raises
``NotImplementedError`` (AutoGen Core subscribes agent *types* to topics via
``TypeSubscription``, not arbitrary callables -- see that module's
docstring), while ``LedgerRecorderAgent.__init__`` unconditionally calls
``self._bus.subscribe(...)`` for nine event types. That means constructing
``LedgerRecorderAgent``/``LedgerRecorderRoutedAgent`` directly against an
``AutoGenEventBus`` today raises immediately at boot -- a real production
wiring needs each business-logic unit's already-defined ``RoutedAgent``
adapter (``LedgerRecorderRoutedAgent``, ``GatekeeperRoutedAgent``,
``RoutedSpawnCoordinatorAgent``, ``make_routed_agent_class(worker)``, ...)
registered on the runtime via ``RoutedAgent.register(...)`` plus one
``agenten.runtime.bootstrap.subscribe_type(runtime, topic, agent_type)``
call per (topic, agent_type) pair, NOT the ``bus.subscribe(topic, handler)``
calls this module uses for ``InMemoryEventBus``. That full AutoGen-Core
rewiring is future work; what IS proven end-to-end against the real,
installed ``autogen_core`` here is the lower-level building block it would
be assembled from: a real ``TypeSubscription`` registration + delivery
round trip (see ``tests/test_autogen_bus_integration.py``, unit U1's own
test -- reused as-is, not reinvented, by
``tests/test_pipeline_autogen_subscription.py``).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from blockchain.Blockchain_modell import Blockchain
from blockchain.storage import LedgerStorage

from agenten.constitution.gatekeeper import ConstitutionGatekeeper, LlmJudge
from agenten.constitution.ruleset import ConstitutionRuleset, load_constitution
from agenten.decomposition.budget import DecompositionBudget
from agenten.decomposition.decomposer import DecomposerAgent, DescribeSubproblem, LlmDecompose
from agenten.events.schemas import (
    CircuitStateChanged,
    EscalateToRedecompose,
    ProblemSubmitted,
    RetryRequested,
    SubproblemAccepted,
    SubproblemAssigned,
    SubproblemProposed,
    SubproblemRejected,
    make_meta,
    topic_for,
)
from agenten.ledger_bridge.query import InProcessBudgetLedger, LedgerQueryImpl
from agenten.ledger_bridge.recorder import LedgerRecorderAgent
from agenten.ledger_bridge.stage_machine import Stage
from agenten.runtime.event_bus import EventBus, InMemoryEventBus
from agenten.spawning.capability_registry import CapabilityRegistry
from agenten.spawning.coordinator import SpawnCoordinatorAgent
from agenten.supervision.reaper import ReaperAgent
from agenten.tools.base import ToolRegistry
from agenten.workers.base import WorkerAgent
from agenten.workers.echo_worker import DEFAULT_ECHO_DELAY_SECONDS, EchoWorker

logger = logging.getLogger(__name__)


class PipelineBootError(RuntimeError):
    """Raised when boot-time validation fails -- e.g. a capability tag the
    Gatekeeper/Decomposer might route through resolves (via
    ``CapabilityRegistry``) to an agent type that was never actually
    constructed and subscribed in this pipeline.

    Per ``agenten/spawning/capability_registry.py``'s module docstring:
    "Validated against the actually-registered runtime agent types at boot
    ... rather than trusted blindly, so a registered-but-never-deployed
    capability fails fast instead of dispatching into a void." This is that
    validation.
    """


def _make_ledger_describe_subproblem(ledger_query: LedgerQueryImpl) -> DescribeSubproblem:
    """Build the ``describe_subproblem`` callable ``DecomposerAgent`` needs
    for ``handle_escalate_to_redecompose`` (a subproblem_id -> (description,
    depth) lookup), backed by the ledger read-side.

    Not exercised by the happy-path smoke test (nothing publishes
    ``EscalateToRedecompose`` yet -- that is the Supervisor's, unit U6's,
    job), but wiring it now means U6 has a real callable to fire against on
    day one instead of a `None` that raises ``RuntimeError`` the first time
    anyone tries.
    """

    async def describe_subproblem(subproblem_id: str) -> Tuple[str, int]:
        block = ledger_query.find_block_by_subproblem_id(subproblem_id)
        if block is None:
            raise KeyError(f"No ledger block found for subproblem_id={subproblem_id!r}")
        return block.data.get("description", ""), int(block.data.get("depth", 0))

    return describe_subproblem


@dataclass
class SupplyChainPipeline:
    """Handle returned by ``build_pipeline()``. Holds every wired-up agent
    plus the shared bus/ledger, and a couple of small convenience methods
    for submitting a problem and waiting for it to settle -- callers are
    free to reach past these and talk to the individual agents/bus/ledger
    directly (e.g. the e2e test inspects ``ledger_query``/``blockchain``
    directly, and the crash-recovery test calls ``agenten.ledger_bridge.
    recovery.recover_on_startup`` against a *fresh* pipeline's bus/query).
    """

    bus: EventBus
    blockchain: Blockchain
    budget_ledger: InProcessBudgetLedger
    ledger_query: LedgerQueryImpl
    recorder: LedgerRecorderAgent
    ruleset: ConstitutionRuleset
    gatekeeper: ConstitutionGatekeeper
    decomposer: DecomposerAgent
    capability_registry: CapabilityRegistry
    coordinator: SpawnCoordinatorAgent
    tools: ToolRegistry
    workers: Dict[str, WorkerAgent]
    reaper: ReaperAgent
    default_budget: DecompositionBudget
    # Count of subproblems rejected for budget exhaustion. These are the
    # one terminal outcome with NO ledger block (the Recorder deliberately
    # writes no block for a budget-rejected proposal -- see
    # `_apply_subproblem_proposed`'s design-choice comment in recorder.py),
    # so `wait_until_terminal` must count them separately or a caller
    # passing expected_subproblem_count = number-proposed would wait
    # forever on a pipeline that has in fact fully settled. Maintained by
    # `_on_subproblem_rejected`, which build_pipeline subscribes to the
    # SubproblemRejected topic.
    budget_rejections: int = 0
    # Whether start() has been called (and stop() not yet called). Guards
    # submit_problem: publishing into a never-started pipeline enqueues
    # ledger writes into the Recorder's inbox that nothing will ever
    # drain -- the submission would "succeed" and then silently go
    # nowhere.
    started: bool = False

    async def start(self) -> None:
        """Spawn the Ledger Recorder's writer-loop task. Must be called
        (once) before submitting problems -- see
        ``agenten.ledger_bridge.recorder.LedgerRecorderAgent.start``'s
        docstring: every other agent's handlers only *enqueue* ledger
        writes, the writer loop is what actually applies them.
        """
        await self.recorder.start()
        self.started = True

    async def stop(self) -> None:
        """Drain everything already queued, then stop the writer-loop task.
        See ``LedgerRecorderAgent.stop``'s docstring for the draining
        guarantee and its limits.
        """
        self.started = False
        await self.recorder.stop()

    async def _on_subproblem_rejected(self, event: SubproblemRejected) -> None:
        """Track budget-exhaustion rejections (the blockless terminal
        outcome -- see the ``budget_rejections`` field comment). Gatekeeper
        rejections (malformed/duplicate/quality_bar/...) are NOT counted
        here: those blocks really exist and land in Stage.REJECTED, which
        ``wait_until_terminal`` already counts via the ledger -- counting
        them twice would overcount.
        """
        if event.reason == "budget_exceeded":
            self.budget_rejections += 1

    async def submit_problem(
        self,
        description: str,
        budget: Optional[DecompositionBudget] = None,
        problem_id: Optional[str] = None,
    ) -> str:
        """Publish a ``ProblemSubmitted`` event for a fresh root problem and
        return its ``problem_id``. Does not itself wait for the pipeline to
        settle -- see ``wait_until_terminal``.

        Raises RuntimeError if ``start()`` has not been awaited yet: a
        submission into a never-started pipeline would enqueue into the
        Ledger Recorder's inbox with nothing draining it, returning a
        problem_id for work that silently never happens.
        """
        if not self.started:
            raise RuntimeError(
                "SupplyChainPipeline.submit_problem: pipeline not started -- await pipeline.start() first"
            )
        problem_id = problem_id or str(uuid.uuid4())
        event = ProblemSubmitted(
            meta=make_meta(correlation_id=problem_id, root_problem_id=problem_id),
            problem_id=problem_id,
            description=description,
            budget=budget,
        )
        await self.bus.publish(topic_for(ProblemSubmitted), event)
        return problem_id

    async def wait_until_terminal(
        self,
        expected_subproblem_count: int,
        timeout: float = 5.0,
        poll_interval: float = 0.01,
    ) -> bool:
        """Poll until at least ``expected_subproblem_count`` subproblems
        have reached a terminal outcome, or ``timeout`` seconds elapse.
        Returns whether it converged in time.

        "Terminal outcome" = ledger blocks in DONE/FAILED/REJECTED *plus*
        ``budget_rejections`` -- budget-exhaustion rejections are terminal
        but blockless (see the field comment), so a caller passing
        expected_subproblem_count = number-of-proposals gets correct
        convergence even when some proposals were budget-rejected.

        This is a plain polling loop (not a bus-level "wait for quiescence"
        primitive -- ``EventBus`` has none) because the ledger is this
        system's actual durability/observability boundary (see
        ``agenten.ledger_bridge.recovery``'s module docstring for the same
        argument applied to crash recovery): asking "is everything I
        submitted done yet" is a ledger-read-side question, not a
        bus-introspection one.
        """
        deadline = time.monotonic() + timeout
        while True:
            terminal = (
                self.ledger_query.count_in_stage(Stage.DONE)
                + self.ledger_query.count_in_stage(Stage.FAILED)
                + self.ledger_query.count_in_stage(Stage.REJECTED)
                + self.budget_rejections
            )
            if terminal >= expected_subproblem_count:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(poll_interval)


def build_pipeline(
    *,
    llm_decompose: LlmDecompose,
    llm_judge: Optional[LlmJudge] = None,
    bus: Optional[EventBus] = None,
    blockchain: Optional[Blockchain] = None,
    storage: Optional[LedgerStorage] = None,
    blockchain_file_path: str = "blockchain.json",
    constitution_path: Optional[str] = None,
    default_budget: Optional[DecompositionBudget] = None,
    describe_subproblem: Optional[DescribeSubproblem] = None,
    tools: Optional[ToolRegistry] = None,
    echo_delay_seconds: float = DEFAULT_ECHO_DELAY_SECONDS,
    heartbeat_interval_seconds: float = 20.0,
    max_in_flight_per_type: int = 20,
    lease_duration_seconds: float = 120.0,
    reaper_poll_interval_seconds: float = 15.0,
    llm_timeout_seconds: float = 15.0,
) -> SupplyChainPipeline:
    """Construct and wire up the full supply-chain pipeline.

    ``llm_decompose`` is required (``DecomposerAgent`` has no usable default
    -- see its own constructor); ``llm_judge`` is optional (``None`` skips
    the Gatekeeper's semantic layer-2 check per
    ``ConstitutionGatekeeper``'s own documented escape hatch).

    Real LLM-backed implementations of both callables exist in
    ``agenten.llm``: ``make_llm_decompose(model_client, known_capability_tags)``
    and ``make_llm_judge(model_client)``, with ``model_client`` from
    ``agenten.llm.model_client.build_model_client()`` (OpenAI-backed) or
    ``build_replay_model_client(...)`` (deterministic/offline). They are
    not defaulted here because constructing a real model client requires
    an API key at build time; pass them in explicitly, e.g.::

        from agenten.llm.decompose import make_llm_decompose
        from agenten.llm.judge import make_llm_judge
        from agenten.llm.model_client import build_model_client

        client = build_model_client()
        pipeline = build_pipeline(
            llm_decompose=make_llm_decompose(client, ["echo", "test"]),
            llm_judge=make_llm_judge(client),
        )
    """
    bus = bus if bus is not None else InMemoryEventBus()

    if blockchain is not None:
        chain = blockchain
    elif storage is not None:
        chain = Blockchain(storage=storage)
    else:
        chain = Blockchain(file_path=blockchain_file_path)

    budget_ledger = InProcessBudgetLedger(chain)
    resolved_default_budget = default_budget if default_budget is not None else DecompositionBudget()

    # --- Ledger Recorder (U8) — the sole ledger writer. Subscribes its own
    # handle_xxx methods onto `bus` inside __init__ (see recorder.py); no
    # bus.subscribe(...) call needed here for it. Also builds+seeds
    # self.query (a LedgerQueryImpl), which every other agent below reads
    # the ledger through.
    recorder = LedgerRecorderAgent(bus, chain, budget_ledger, default_budget=resolved_default_budget)
    ledger_query = recorder.query

    # --- Constitution Gatekeeper (U2) — independent admission check.
    ruleset = load_constitution(constitution_path)
    gatekeeper = ConstitutionGatekeeper(
        bus, ruleset, ledger_query, llm_judge=llm_judge, llm_timeout_seconds=llm_timeout_seconds
    )
    bus.subscribe(topic_for(SubproblemProposed), gatekeeper.handle_subproblem_proposed)

    # --- Capability registry + worker fleet (U4 registry, U5 workers).
    # EchoWorker has no external dependencies (no Selenium/LLM) so it's the
    # one used for the smoke test; register both of its capability_tags
    # ("echo", "test") against its agent_type so a subproblem proposed with
    # either tag routes to it.
    capability_registry = CapabilityRegistry()
    tool_registry = tools if tools is not None else ToolRegistry()

    echo_worker = EchoWorker(
        bus,
        tool_registry,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        description_resolver=_make_ledger_description_resolver(ledger_query),
        echo_delay_seconds=echo_delay_seconds,
    )
    workers: Dict[str, WorkerAgent] = {echo_worker.agent_type: echo_worker}
    for tag in echo_worker.capability_tags:
        capability_registry.register(tag, echo_worker.agent_type)
    # `subscribed_worker_types` records which worker agent_types actually
    # got a bus.subscribe(...) for SubproblemAssigned -- the set boot
    # validation checks the CapabilityRegistry against (see
    # `_validate_capability_registry_at_boot`). Kept as its own explicit
    # set (rather than re-using `workers`' keys) so the validation is
    # anchored to the subscription actually happening, not to a dict entry
    # that could be added without one.
    subscribed_worker_types: set = set()
    bus.subscribe(topic_for(SubproblemAssigned), echo_worker.handle_subproblem_assigned)
    subscribed_worker_types.add(echo_worker.agent_type)

    # --- Spawn Coordinator (U4) — turns a ledger-finalized SubproblemAccepted
    # into a SubproblemAssigned addressed to a resolved worker agent type.
    coordinator = SpawnCoordinatorAgent(
        bus,
        capability_registry,
        ledger_query,
        max_in_flight_per_type=max_in_flight_per_type,
        lease_duration_seconds=lease_duration_seconds,
    )
    bus.subscribe(topic_for(SubproblemAccepted), coordinator.handle_subproblem_accepted)
    bus.subscribe(topic_for(RetryRequested), coordinator.handle_retry_requested)
    # TODO(U6): once agenten/supervision/supervisor.py lands, the Supervisor
    # is what actually publishes CircuitStateChanged based on failure-rate
    # tracking per agent_type; the Coordinator already has a handler ready
    # to consume it. Wired here now so no additional subscription needs to
    # be added later.
    bus.subscribe(topic_for(CircuitStateChanged), coordinator.handle_circuit_state_changed)

    # --- Decomposer (U3) — turns ProblemSubmitted / EscalateToRedecompose
    # into SubproblemProposed.
    resolved_describe_subproblem = (
        describe_subproblem if describe_subproblem is not None else _make_ledger_describe_subproblem(ledger_query)
    )
    decomposer = DecomposerAgent(
        bus, resolved_default_budget, llm_decompose, describe_subproblem=resolved_describe_subproblem
    )
    bus.subscribe(topic_for(ProblemSubmitted), decomposer.handle_problem_submitted)
    # TODO(U6): once agenten/supervision/supervisor.py lands, subscribe a
    # SupervisorAgent to SubproblemFailed/LeaseExpired here (it decides
    # retry-vs-escalate policy) and wire its RetryRequested back onto the
    # Coordinator (already subscribed above) and its EscalateToRedecompose
    # onto this handler:
    bus.subscribe(topic_for(EscalateToRedecompose), decomposer.handle_escalate_to_redecompose)

    # --- Reaper (U7) — lease/heartbeat watchdog. Poll-based, not
    # event-driven, so it is NOT subscribed onto `bus` the way the agents
    # above are; a caller runs `await reaper.scan_once()` directly (as this
    # module's own smoke test / examples/armada_demo.py do) or schedules
    # `asyncio.create_task(reaper.run_forever())` as a background task in a
    # long-lived process. LeaseExpired events it publishes feed the same
    # retry path SubproblemFailed(retriable=True) does (see recorder.py's
    # module docstring) -- again a U6 concern once retry-count/backoff
    # policy exists; today an expired lease routes a block to
    # Stage.RETRYING and simply waits there for a reassignment that (until
    # U6 lands) will never come. That's fine for the happy-path smoke test,
    # which never produces an expired lease.
    reaper = ReaperAgent(bus, ledger_query, poll_interval_seconds=reaper_poll_interval_seconds)

    _validate_capability_registry_at_boot(capability_registry, subscribed_worker_types)

    pipeline = SupplyChainPipeline(
        bus=bus,
        blockchain=chain,
        budget_ledger=budget_ledger,
        ledger_query=ledger_query,
        recorder=recorder,
        ruleset=ruleset,
        gatekeeper=gatekeeper,
        decomposer=decomposer,
        capability_registry=capability_registry,
        coordinator=coordinator,
        tools=tool_registry,
        workers=workers,
        reaper=reaper,
        default_budget=resolved_default_budget,
    )
    # Budget-exhaustion rejections are terminal but blockless (the Recorder
    # writes no block for them), so the pipeline handle itself tracks them
    # for wait_until_terminal -- see SupplyChainPipeline.budget_rejections.
    bus.subscribe(topic_for(SubproblemRejected), pipeline._on_subproblem_rejected)
    return pipeline


def _make_ledger_description_resolver(ledger_query: LedgerQueryImpl):
    """Build the ``description_resolver`` a ``WorkerAgent`` needs: given a
    ``SubproblemAssigned`` (which carries no description of its own, see
    ``agenten/workers/base.py``'s ``DescriptionResolver`` docstring), look
    the subproblem's real description up on the ledger block the Ledger
    Recorder already wrote for it.
    """

    async def resolve(event: SubproblemAssigned) -> str:
        block = ledger_query.find_block_by_subproblem_id(event.subproblem_id)
        if block is not None:
            description = block.data.get("description")
            if isinstance(description, str) and description:
                return description
        # Fall back to the bare subproblem_id (same default WorkerAgent
        # itself uses when no resolver is supplied at all) rather than
        # raising -- a missing ledger block here would be a boot-order bug
        # elsewhere, not something a worker should crash a whole
        # in-flight execution over.
        logger.warning(
            "No ledger block found for subproblem_id=%s while resolving its description; "
            "falling back to the bare subproblem_id",
            event.subproblem_id,
        )
        return event.subproblem_id

    return resolve


def _validate_capability_registry_at_boot(registry: CapabilityRegistry, subscribed_worker_types: set) -> None:
    """Fail fast if the ``CapabilityRegistry`` maps any capability tag to an
    agent type that never actually got a ``bus.subscribe(...)`` call for the
    ``SubproblemAssigned`` topic in this pipeline -- see
    ``agenten/spawning/capability_registry.py``'s module docstring:
    "Validated against the actually-registered runtime agent types at boot
    ... rather than trusted blindly, so a registered-but-never-deployed
    capability fails fast instead of dispatching into a void."

    ``subscribed_worker_types`` is deliberately the set build_pipeline
    populates at the exact point each worker's SubproblemAssigned
    subscription is made -- not a workers dict that could gain entries
    without a subscription -- so what this validates is the thing that
    actually matters at dispatch time: "publishing SubproblemAssigned for
    this agent_type reaches a handler."

    Scope caveat: this validates the ``InMemoryEventBus`` wiring this
    module builds, nothing more. A future AutoGen-Core production wiring
    (RoutedAgent adapters + TypeSubscriptions on a real runtime, per this
    module's docstring) MUST reimplement this check against the runtime's
    actual agent-type registrations/TypeSubscriptions -- this function has
    no visibility into those and would be silently meaningless if reused
    unchanged there.
    """
    missing = sorted(
        agent_type
        for agent_type in registry.registered_agent_types()
        if agent_type not in subscribed_worker_types
    )
    if missing:
        raise PipelineBootError(
            f"CapabilityRegistry references agent type(s) {missing!r} with no SubproblemAssigned "
            f"subscription in this pipeline (subscribed worker types: {sorted(subscribed_worker_types)!r}); "
            "boot validation failed."
        )
    if not registry.known_tags():
        logger.warning(
            "CapabilityRegistry has no capability tags registered at all -- every SubproblemProposed "
            "will fail SpawnCoordinatorAgent's capability resolution (NoCapableAgentType)."
        )
