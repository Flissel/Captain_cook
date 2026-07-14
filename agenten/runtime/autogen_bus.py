"""AutoGenEventBus: adapter over autogen_core's pub/sub for the EventBus port.

This is the ONLY file in the supply-chain subsystem that is allowed to
import autogen_core for the purpose of implementing agenten.runtime.event_bus.EventBus.
Business-logic units keep depending on the EventBus ABC (or InMemoryEventBus
for tests) so they stay importable with zero AutoGen installed; only code
that actually wires up the real runtime (this module, and
agenten/runtime/bootstrap.py) needs autogen-core present.

AutoGen Core's runtime is a topic/subscription broadcast system: publishing
targets a `TopicId` (a `(type, source)` pair), and agents are routed to a
topic by *type* via a `TypeSubscription` that maps `(topic_type, source) ->
agent instance keyed by source` -- not by registering ad-hoc callables the
way InMemoryEventBus does. That's why `subscribe()` below cannot be
implemented faithfully: there is no "callable subscribed to a topic" concept
in autogen_core to hang a handler off of. Each business-logic unit instead
defines its own thin `RoutedAgent` subclass with `@event`/`@rpc`-decorated
handler methods, registers it against the runtime via
`RoutedAgent.register(...)`, and adds a `TypeSubscription(topic_type=topic,
agent_type=that_agent_type)` (see `agenten.runtime.bootstrap.subscribe_type`
and the final integration in unit U11).
"""
from typing import Any

from autogen_core import AgentRuntime, TopicId

from agenten.runtime.event_bus import EventBus, Handler

# TopicId is a (type, source) pair; TypeSubscription routes by source to a
# per-source agent instance ("A topic_id with type `t1` and source `s1` will
# be handled by an agent of type `a1` with key `s1`"). Publishers in this
# codebase reason about correlation via EventMeta.root_problem_id, so we use
# that as the topic source when present -- keeping each root problem's event
# stream routed to its own agent instance/key -- falling back to a fixed
# source for events that (unusually) carry no meta.
DEFAULT_TOPIC_SOURCE = "default"


class AutoGenEventBus(EventBus):
    """Adapter over autogen_core's pub/sub. publish() maps topic -> TopicId
    and calls runtime.publish_message(). subscribe() is intentionally NOT
    supported here -- AutoGen Core subscribes agent TYPES to topics via
    TypeSubscription, not arbitrary callables. Each business-logic unit
    implements its own thin RoutedAgent adapter (see sibling units) and
    registers a TypeSubscription directly against the runtime instead;
    subscribe() exists only so this class satisfies the EventBus interface,
    and raises NotImplementedError with a message pointing at that pattern.
    """

    def __init__(self, runtime: AgentRuntime) -> None:
        self._runtime = runtime

    async def publish(self, topic: str, event: Any) -> None:
        meta = getattr(event, "meta", None)
        root_problem_id = getattr(meta, "root_problem_id", None) if meta is not None else None
        # Use `is not None` rather than truthiness: an event whose
        # root_problem_id happens to be an empty string is a distinct
        # (if unusual) correlation key from an event with no meta at all,
        # and must not silently collapse onto the shared default source.
        source = root_problem_id if root_problem_id is not None else DEFAULT_TOPIC_SOURCE
        topic_id = TopicId(type=topic, source=source)
        await self._runtime.publish_message(event, topic_id)

    def subscribe(self, topic: str, handler: Handler) -> None:
        raise NotImplementedError(
            "AutoGenEventBus.subscribe: register a TypeSubscription against the runtime for your RoutedAgent instead"
        )
