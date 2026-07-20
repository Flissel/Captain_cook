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
way InMemoryEventBus does. There is no "callable subscribed to a topic" concept
in autogen_core to hang a handler off of. Each business-logic unit instead
defines its own thin `RoutedAgent` subclass with `@event`/`@rpc`-decorated
handler methods, registers it against the runtime via
`RoutedAgent.register(...)`, and adds a `TypeSubscription(topic_type=topic,
agent_type=that_agent_type)` (see `agenten.runtime.bootstrap.subscribe_type`
and the final integration in unit U11).
"""
import asyncio
from typing import Any, Protocol
from uuid import UUID

from autogen_core import (
    AgentRuntime,
    MessageContext,
    RoutedAgent,
    SingleThreadedAgentRuntime,
    TopicId,
    TypeSubscription,
    message_handler,
)
from pydantic import BaseModel, ConfigDict

from agenten.agent_runtime.contracts import AgentRuntimeCommand, AgentRuntimeResult
from agenten.runtime.event_bus import EventBus

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
    and calls runtime.publish_message(). AutoGen Core subscribes agent TYPES to topics via
    TypeSubscription, not arbitrary callables. Each business-logic unit
    implements its own thin RoutedAgent adapter (see sibling units) and
    registers a TypeSubscription directly against the runtime instead.
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


LIVE_DEMO_RUNTIME_TOPIC = "captain-live-demo-runtime"


class RuntimeCommandExecutor(Protocol):
    async def execute(self, command: AgentRuntimeCommand) -> AgentRuntimeResult: ...


class LiveDemoRuntimeMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    correlation_id: UUID
    hermes_evidence_id: UUID
    command: AgentRuntimeCommand


class _OneShotAgent(RoutedAgent):
    def __init__(self, executor: RuntimeCommandExecutor,
                 completion: asyncio.Future[AgentRuntimeResult], deliveries: list[int]) -> None:
        super().__init__("Captain live-demo one-shot runtime relay")
        self._executor = executor
        self._completion = completion
        self._deliveries = deliveries

    @message_handler
    async def on_runtime_message(self, message: LiveDemoRuntimeMessage,
                                 ctx: MessageContext) -> None:
        del ctx
        self._deliveries.append(1)
        try:
            result = await self._executor.execute(message.command)
        except BaseException as exc:
            if not self._completion.done():
                self._completion.set_exception(exc)
            return
        if not self._completion.done():
            self._completion.set_result(result)


class AutoGenOneShotRuntimeRelay:
    """Register, publish, drain, and close one real AutoGen runtime."""

    def __init__(self, executor: RuntimeCommandExecutor) -> None:
        self._executor = executor

    async def deliver(self, message: LiveDemoRuntimeMessage) -> tuple[AgentRuntimeResult, int]:
        completion: asyncio.Future[AgentRuntimeResult] = asyncio.get_running_loop().create_future()
        deliveries: list[int] = []
        runtime = SingleThreadedAgentRuntime()
        bus = AutoGenEventBus(runtime)
        registered = await _OneShotAgent.register(
            runtime, "captain-live-demo-one-shot",
            lambda: _OneShotAgent(self._executor, completion, deliveries),
        )
        await runtime.add_subscription(TypeSubscription(
            topic_type=LIVE_DEMO_RUNTIME_TOPIC, agent_type=registered.type
        ))
        runtime.start()
        try:
            await bus.publish(LIVE_DEMO_RUNTIME_TOPIC, message)
            await runtime.stop_when_idle()
            return await completion, len(deliveries)
        finally:
            await runtime.close()
