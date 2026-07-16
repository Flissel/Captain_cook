"""Real-runtime integration test for agenten.runtime.autogen_bus.AutoGenEventBus.

Exercises the actual, installed `autogen_core` package end-to-end (no
mocking of autogen_core internals): registers a trivial `RoutedAgent` via
`TypeSubscription`, publishes an event through `AutoGenEventBus`, and
asserts the agent receives it through a real
`SingleThreadedAgentRuntime.start()` / `stop_when_idle()` / `close()`
lifecycle.

This is also the regression test for the bug that motivated migrating
`agenten/events/schemas.py` and `agenten/decomposition/budget.py` from
frozen stdlib dataclasses to frozen Pydantic v2 `BaseModel`s: autogen_core's
default message serializer rejects dataclasses with Optional/Union fields
or nested dataclass fields ("...not supported. To use a union/nested
types, use a Pydantic model"). Every event nests `EventMeta`, so
`test_routed_agent_accepts_pydantic_event_as_declared_message_type` below
proves a `RoutedAgent` handler can now be declared directly against a
frozen event type (`ProblemSubmitted`) without a custom serializer or a
Pydantic wrapper.

Requires the real `autogen-core` package installed (see requirements.txt);
skips cleanly if it isn't available so the rest of the suite stays runnable
without AutoGen installed.
"""
import pytest

autogen_core = pytest.importorskip("autogen_core")

from autogen_core import AgentId, MessageContext, RoutedAgent, TopicId, message_handler  # noqa: E402

from agenten.decomposition.budget import DecompositionBudget  # noqa: E402
from agenten.events.schemas import ProblemSubmitted, make_meta, topic_for  # noqa: E402
from agenten.runtime.autogen_bus import DEFAULT_TOPIC_SOURCE  # noqa: E402
from agenten.runtime.bootstrap import build_runtime_and_bus, subscribe_type  # noqa: E402
from agenten.runtime import event_bus as event_bus_module  # noqa: E402


def make_problem_submitted(problem_id="p1", root_problem_id=None, description="test problem"):
    root = problem_id if root_problem_id is None else root_problem_id
    return ProblemSubmitted(
        meta=make_meta(correlation_id=problem_id, root_problem_id=root),
        problem_id=problem_id,
        description=description,
        budget=DecompositionBudget(),
    )


class RecordingAgent(RoutedAgent):
    """Trivial RoutedAgent whose handler is declared directly against the
    frozen, nested-Pydantic ProblemSubmitted event type -- the exact shape
    that used to be rejected by autogen_core's dataclass serializer before
    the schemas.py -> Pydantic migration.
    """

    def __init__(self) -> None:
        super().__init__("recording agent")
        self.received: list[ProblemSubmitted] = []

    @message_handler
    async def on_problem_submitted(self, message: ProblemSubmitted, ctx: MessageContext) -> None:
        self.received.append(message)


@pytest.mark.asyncio
async def test_routed_agent_accepts_pydantic_event_as_declared_message_type():
    """The core regression test: registering a RoutedAgent handler typed
    against ProblemSubmitted (a frozen Pydantic BaseModel nesting EventMeta,
    with an Optional[DecompositionBudget] field) must NOT raise autogen_core's
    "use a Pydantic model" serializer error, and the event must be delivered
    end-to-end through AutoGenEventBus + SingleThreadedAgentRuntime.
    """
    runtime, bus = build_runtime_and_bus()

    agent_type = await RecordingAgent.register(runtime, "recorder", lambda: RecordingAgent())
    topic = topic_for(ProblemSubmitted)
    await subscribe_type(runtime, topic, agent_type.type)

    runtime.start()
    try:
        event = make_problem_submitted(problem_id="p1")
        await bus.publish(topic, event)
        await runtime.stop_when_idle()

        # TypeSubscription routes topic_id (type, source) -> an agent
        # instance keyed by `source`; AutoGenEventBus.publish used
        # event.meta.root_problem_id ("p1") as that source, so the agent
        # instance that received the message is keyed the same way.
        agent_id = AgentId(agent_type.type, key="p1")
        agent_instance = await runtime.try_get_underlying_agent_instance(
            agent_id, type=RecordingAgent
        )
        assert len(agent_instance.received) == 1
        assert agent_instance.received[0] == event
        assert isinstance(agent_instance.received[0], ProblemSubmitted)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_topic_source_derives_from_root_problem_id():
    """AutoGenEventBus.publish must key TopicId.source off
    event.meta.root_problem_id, including the `is not None` (not
    truthiness) edge case for an empty-string root_problem_id, per the
    docstring/comment in autogen_bus.py.
    """
    runtime, bus = build_runtime_and_bus()

    published: list[TopicId] = []
    orig_publish_message = runtime.publish_message

    async def capturing_publish_message(message, topic_id, **kwargs):
        published.append(topic_id)
        return await orig_publish_message(message, topic_id, **kwargs)

    runtime.publish_message = capturing_publish_message
    topic = topic_for(ProblemSubmitted)

    # Normal case: non-empty root_problem_id is used verbatim as the source.
    await bus.publish(topic, make_problem_submitted(problem_id="p1", root_problem_id="root-1"))
    assert published[-1].source == "root-1"

    # Edge case: an explicit empty-string root_problem_id is a distinct
    # correlation key from "no meta at all" and must NOT collapse onto
    # DEFAULT_TOPIC_SOURCE.
    await bus.publish(topic, make_problem_submitted(problem_id="p2", root_problem_id=""))
    assert published[-1].source == ""
    assert published[-1].source != DEFAULT_TOPIC_SOURCE

    # No meta at all: falls back to DEFAULT_TOPIC_SOURCE.
    class _NoMeta:
        pass

    await bus.publish(topic, _NoMeta())
    assert published[-1].source == DEFAULT_TOPIC_SOURCE


def test_autogen_bus_is_publish_only():
    """AutoGen uses TypeSubscription and exposes no callable subscription API."""
    _runtime, bus = build_runtime_and_bus()

    assert isinstance(bus, event_bus_module.EventBus)
    assert not hasattr(bus, "subscribe")


@pytest.mark.asyncio
async def test_in_memory_bus_is_subscribable_and_delivers_handlers():
    subscribable_bus_type = getattr(event_bus_module, "SubscribableEventBus", None)
    assert subscribable_bus_type is not None
    bus = event_bus_module.InMemoryEventBus()
    received = []

    async def handler(event):
        received.append(event)

    assert isinstance(bus, subscribable_bus_type)
    bus.subscribe("some.topic", handler)
    await bus.publish("some.topic", "payload")
    assert received == ["payload"]
