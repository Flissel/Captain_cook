"""Constructs the real AutoGen Core runtime + AutoGenEventBus pairing.

This module deliberately stops short of registering any agent types or
TypeSubscriptions: at unit-U1 time the full roster of agent types (Captain,
decomposition workers, ledger bridge, supervision, etc.) doesn't exist yet
-- each of those lands in its own parallel unit. Wiring them all onto one
shared runtime is unit U11's job, once every unit's RoutedAgent subclasses
are available to import. `subscribe_type` below is the helper U11 calls in a
loop (one call per (topic, agent_type) pair) to build that wiring; it is
exercised here only by this unit's own tests, against a toy agent, to prove
the real autogen_core APIs behave as documented.
"""
from typing import Tuple

from autogen_core import AgentRuntime, SingleThreadedAgentRuntime, TypeSubscription

from agenten.runtime.autogen_bus import AutoGenEventBus


def build_runtime_and_bus() -> Tuple[SingleThreadedAgentRuntime, AutoGenEventBus]:
    """Constructs and returns (runtime, AutoGenEventBus(runtime)) using
    autogen_core.SingleThreadedAgentRuntime. Does NOT register any
    TypeSubscriptions or agent factories -- that happens per-agent-type in
    the final integration unit (U11) once all agent types are known.

    Lifecycle note for callers: `SingleThreadedAgentRuntime.publish_message`
    (invoked by `AutoGenEventBus.publish`) only enqueues the message --
    nothing is delivered to subscribers until `await runtime.start()` has
    been called to run the processing loop. Callers should register their
    agent factories and TypeSubscriptions, call `await runtime.start()`,
    and later `await runtime.stop_when_idle()` (or `stop()`) to tear down.
    """
    runtime = SingleThreadedAgentRuntime()
    bus = AutoGenEventBus(runtime)
    return runtime, bus


async def subscribe_type(runtime: AgentRuntime, topic: str, agent_type: str) -> None:
    """Registers a TypeSubscription mapping `topic` (a topic *type* string,
    e.g. the output of agenten.events.topic_for) to `agent_type` (the string
    name an agent class was registered under via `RoutedAgent.register` /
    `runtime.register_factory`). U11 calls this once per (topic, agent_type)
    pair after all agent types have been registered on `runtime`.
    """
    await runtime.add_subscription(TypeSubscription(topic_type=topic, agent_type=agent_type))
