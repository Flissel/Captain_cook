"""Read-only verification facade for the Gateway-owned Minibook projection."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from agenten.delivery.minibook_events import MinibookProjectionEvent


class MinibookProjectionReadPort(Protocol):
    async def events_for_correlation(
        self,
        correlation_id: UUID,
    ) -> tuple[MinibookProjectionEvent, ...]: ...


class ReadOnlyMinibookProjectionAdapter:
    """Read correlated projection evidence without exposing any write method."""

    def __init__(self, feed: MinibookProjectionReadPort) -> None:
        if feed is None:
            raise ValueError("Minibook projection feed is required")
        self._feed = feed

    async def events_for_correlation(
        self,
        correlation_id: UUID,
    ) -> tuple[MinibookProjectionEvent, ...]:
        events = await self._feed.events_for_correlation(correlation_id)
        if not events:
            raise ValueError("Minibook projection has no correlated evidence")
        if any(not isinstance(event, MinibookProjectionEvent) for event in events):
            raise ValueError("Minibook projection returned an untyped event")
        if any(event.correlation_id != correlation_id for event in events):
            raise ValueError("Minibook projection returned a foreign correlation")
        return events
