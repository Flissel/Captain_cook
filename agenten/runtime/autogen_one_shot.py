"""One-shot delivery of a Captain runtime command through real AutoGen routing."""

from __future__ import annotations

import asyncio
from typing import Protocol
from uuid import UUID

from autogen_core import MessageContext, RoutedAgent, message_handler
from pydantic import BaseModel, ConfigDict

from agenten.agent_runtime.contracts import AgentRuntimeCommand, AgentRuntimeResult
from agenten.runtime.bootstrap import build_runtime_and_bus, subscribe_type


LIVE_DEMO_RUNTIME_TOPIC = "captain-live-demo-runtime"


class RuntimeCommandExecutor(Protocol):
    async def execute(self, command: AgentRuntimeCommand) -> AgentRuntimeResult: ...


class LiveDemoRuntimeMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    correlation_id: UUID
    hermes_evidence_id: UUID
    command: AgentRuntimeCommand


class _OneShotAgent(RoutedAgent):
    def __init__(
        self,
        executor: RuntimeCommandExecutor,
        completion: asyncio.Future[AgentRuntimeResult],
        deliveries: list[int],
    ) -> None:
        super().__init__("Captain live-demo one-shot runtime relay")
        self._executor = executor
        self._completion = completion
        self._deliveries = deliveries

    @message_handler
    async def on_runtime_message(
        self, message: LiveDemoRuntimeMessage, ctx: MessageContext
    ) -> None:
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

    async def deliver(
        self, message: LiveDemoRuntimeMessage
    ) -> tuple[AgentRuntimeResult, int]:
        loop = asyncio.get_running_loop()
        completion: asyncio.Future[AgentRuntimeResult] = loop.create_future()
        deliveries: list[int] = []
        runtime, bus = build_runtime_and_bus()
        registered = await _OneShotAgent.register(
            runtime,
            "captain-live-demo-one-shot",
            lambda: _OneShotAgent(self._executor, completion, deliveries),
        )
        await subscribe_type(runtime, LIVE_DEMO_RUNTIME_TOPIC, registered.type)
        runtime.start()
        try:
            await bus.publish(LIVE_DEMO_RUNTIME_TOPIC, message)
            await runtime.stop_when_idle()
            return await completion, len(deliveries)
        finally:
            await runtime.close()
