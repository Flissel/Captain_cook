from __future__ import annotations

import asyncio

from agenten.runtime.event_bus import EventBus

from .models import DeliveryEvent


class DeliveryEventPublisher:
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    def publish(self, event: DeliveryEvent) -> None:
        asyncio.run(self._bus.publish("delivery.events", event))
