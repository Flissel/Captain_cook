"""Real-runtime integration test for the unit-U11 integration point.

`agenten/orchestration/pipeline.py`'s `build_pipeline()` wires the full
business-logic pipeline over `InMemoryEventBus` on purpose (see that
module's docstring for why `AutoGenEventBus` is not a drop-in replacement
for the whole pipeline today). What IS proven end-to-end against the real,
installed `autogen_core` here is the lower-level building block a future
full AutoGen-Core wiring would be assembled from: a real `TypeSubscription`
registration + delivery round trip, for one of THIS unit's own agent types
(`EchoWorker`, via `agenten.workers.base.make_routed_agent_class`) rather
than the toy `RecordingAgent` unit U1's own
`tests/test_autogen_bus_integration.py` uses -- reusing that same,
already-established pattern (`build_runtime_and_bus()` +
`subscribe_type()` + `runtime.start()` / `stop_when_idle()` / `close()`),
not reinventing it.

`WorkerAgent` (agenten/workers/base.py) has no `bus.subscribe(...)` call of
its own anywhere in its business logic -- only `bus.publish(...)` calls
(for `WorkerHeartbeat`/`SubproblemCompleted`/`SubproblemFailed`) -- so,
unlike `LedgerRecorderAgent` (see pipeline.py's docstring), it IS safe to
hand an `AutoGenEventBus` straight to a plain `EchoWorker` instance: this
test does exactly that, addresses it via a real `TypeSubscription`, and
confirms both the inbound `SubproblemAssigned` delivery AND the outbound
`SubproblemCompleted` publish (captured by a second, toy `RoutedAgent`)
round-trip correctly through the real runtime.

Requires the real `autogen-core` package installed (see requirements.txt);
skips cleanly if it isn't available, same as tests/test_autogen_bus_integration.py.
"""
import pytest

autogen_core = pytest.importorskip("autogen_core")

from autogen_core import AgentId, MessageContext, RoutedAgent, message_handler  # noqa: E402

from agenten.events.schemas import (  # noqa: E402
    SubproblemAssigned,
    SubproblemCompleted,
    make_meta,
    topic_for,
)
from agenten.runtime.bootstrap import build_runtime_and_bus, subscribe_type  # noqa: E402
from agenten.tools.base import ToolRegistry  # noqa: E402
from agenten.workers.base import make_routed_agent_class  # noqa: E402
from agenten.workers.echo_worker import EchoWorker  # noqa: E402


class _RecordingAgent(RoutedAgent):
    """Trivial RoutedAgent that records every SubproblemCompleted it's
    delivered -- proves the EchoWorker's *outbound* publish (via
    AutoGenEventBus) round-trips through the real runtime too, not just
    inbound delivery to the worker.
    """

    def __init__(self) -> None:
        super().__init__("recording agent")
        self.received: list[SubproblemCompleted] = []

    @message_handler
    async def on_subproblem_completed(self, message: SubproblemCompleted, ctx: MessageContext) -> None:
        self.received.append(message)


@pytest.mark.asyncio
async def test_echo_worker_routed_agent_real_autogen_delivery_round_trip():
    runtime, bus = build_runtime_and_bus()

    worker = EchoWorker(bus, ToolRegistry(), echo_delay_seconds=0.0)
    RoutedEchoWorker = make_routed_agent_class(worker)
    assert RoutedEchoWorker is not None, "make_routed_agent_class must return a real class when autogen_core is installed"

    worker_agent_type = await RoutedEchoWorker.register(runtime, worker.agent_type, lambda: RoutedEchoWorker())
    await subscribe_type(runtime, topic_for(SubproblemAssigned), worker_agent_type.type)

    recorder_agent_type = await _RecordingAgent.register(runtime, "recorder", lambda: _RecordingAgent())
    await subscribe_type(runtime, topic_for(SubproblemCompleted), recorder_agent_type.type)

    runtime.start()
    try:
        root_problem_id = "root-1"
        event = SubproblemAssigned(
            meta=make_meta(correlation_id="sp-1", root_problem_id=root_problem_id),
            subproblem_id="sp-1",
            agent_type=worker.agent_type,
            agent_key="sp-1",
            lease_expires_at=0.0,
        )
        await bus.publish(topic_for(SubproblemAssigned), event)
        await runtime.stop_when_idle()

        # AutoGenEventBus.publish keys TopicId.source off
        # event.meta.root_problem_id (see agenten/runtime/autogen_bus.py),
        # so both the worker instance that handled the assignment and the
        # recorder instance that received its completion are addressed by
        # "root-1" -- not by agent_key ("sp-1"), which is only meaningful
        # for InMemoryEventBus-based wiring / the future full
        # RoutedAgent-per-subproblem production design this test's module
        # docstring describes.
        recorder_instance = await runtime.try_get_underlying_agent_instance(
            AgentId(recorder_agent_type.type, key=root_problem_id), type=_RecordingAgent
        )
        assert len(recorder_instance.received) == 1
        completed = recorder_instance.received[0]
        assert isinstance(completed, SubproblemCompleted)
        assert completed.subproblem_id == "sp-1"
        # EchoWorker.execute() with no description_resolver configured
        # falls back to the bare subproblem_id (see WorkerAgent's
        # DescriptionResolver docstring).
        assert completed.result == {"echo": "sp-1"}
    finally:
        await runtime.close()
