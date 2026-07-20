"""Async reader for the Gateway-owned, redacted Minibook projection feed."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agenten.delivery.minibook_events import MinibookProjectionEvent


class ProjectionFeedError(RuntimeError):
    """The projection feed failed without retaining response or credential data."""


class _ProjectionFeedPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    events: tuple[MinibookProjectionEvent, ...]
    cursor: str = Field(min_length=1)
    has_more: bool


class GatewayProjectionFeedClient:
    """Read the public Gateway feed through an injected asynchronous HTTP client."""

    def __init__(
        self,
        base_url: str,
        token: str,
        client: httpx.AsyncClient,
        *,
        page_size: int = 100,
    ) -> None:
        if not base_url.strip():
            raise ValueError("gateway base_url must not be empty")
        if not token:
            raise ValueError("gateway token must not be empty")
        if page_size < 1:
            raise ValueError("projection page_size must be positive")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = client
        self._page_size = page_size

    async def events_for_correlation(
        self, correlation_id: UUID
    ) -> tuple[MinibookProjectionEvent, ...]:
        cursor: str | None = None
        seen: set[str] = set()
        matched: list[MinibookProjectionEvent] = []
        while True:
            params: dict[str, Any] = {"limit": self._page_size}
            if cursor is not None:
                params["cursor"] = cursor
            try:
                response = await self._client.request(
                    "GET",
                    f"{self._base_url}/api/v1/projections/minibook/events",
                    headers={"Authorization": f"Bearer {self._token}"},
                    params=params,
                )
            except httpx.HTTPError:
                raise ProjectionFeedError("projection feed could not reach the gateway") from None
            if response.status_code != 200:
                raise ProjectionFeedError(
                    f"projection feed failed with gateway status {response.status_code}"
                )
            try:
                page = _ProjectionFeedPage.model_validate(response.json())
            except (ValueError, ValidationError):
                raise ProjectionFeedError("projection feed returned an invalid page") from None
            matched.extend(
                event for event in page.events if event.correlation_id == correlation_id
            )
            if not page.has_more:
                return tuple(matched)
            if page.cursor == cursor or page.cursor in seen:
                raise ProjectionFeedError("projection feed repeated a cursor")
            seen.add(page.cursor)
            cursor = page.cursor
