"""EventBus port: the seam that keeps business logic decoupled from any
particular pub/sub implementation (AutoGen Core's Topic/Subscription model
today, potentially something else later).

Business-logic units (constitution, decomposition, spawning, workers,
supervision, ledger_bridge) depend ONLY on this ABC — never on
autogen_core directly — so they stay importable and unit-testable with
zero AutoGen installed, via InMemoryEventBus.
"""
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Any, Awaitable, Callable, Dict, List

Handler = Callable[[Any], Awaitable[None]]


class EventBus(ABC):
    @abstractmethod
    async def publish(self, topic: str, event: Any) -> None:
        """Publish an event onto a topic. Delivery is at-least-once, not
        exactly-once — handlers must be idempotent w.r.t. EventMeta.event_id.
        """

    @abstractmethod
    def subscribe(self, topic: str, handler: Handler) -> None:
        """Register a handler for a topic.

        NOTE: AutoGen Core subscribes agent TYPES to topics, not arbitrary
        callables — AutoGenEventBus (unit U1) cannot implement this
        faithfully and raises NotImplementedError; real AutoGen wiring
        happens via each unit's RoutedAgent adapter + TypeSubscription
        instead (see agenten/orchestration/pipeline.py, unit U11).
        InMemoryEventBus is the one implementation where subscribe() is
        the real mechanism, e.g. for tests.
        """


class InMemoryEventBus(EventBus):
    """Sequential, deterministic in-process bus. Used for unit tests and as
    the default single-process runtime until an AutoGen-backed bus is wired
    up in unit U11.
    """

    def __init__(self):
        self._handlers: Dict[str, List[Handler]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Handler) -> None:
        self._handlers[topic].append(handler)

    async def publish(self, topic: str, event: Any) -> None:
        # Snapshot the handler list: a handler that publishes further events
        # (the common case here) must not see itself re-entered mid-iteration
        # if something subscribes concurrently.
        for handler in list(self._handlers.get(topic, [])):
            await handler(event)
