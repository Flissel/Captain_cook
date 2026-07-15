"""Best-effort asynchronous mirroring that never blocks ledger writes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any


logger = logging.getLogger(__name__)
MirrorHandler = Callable[[dict[str, Any]], Awaitable[None]]


class MirrorQueue:
    def __init__(self, handler: MirrorHandler):
        self._handler = handler
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="minibook-mirror")

    async def stop(self) -> None:
        if self._worker is None:
            return
        await self._queue.join()
        self._worker.cancel()
        try:
            await self._worker
        except asyncio.CancelledError:
            pass
        self._worker = None

    def enqueue_nowait(self, block: dict[str, Any]) -> None:
        self._queue.put_nowait(block)

    async def _run(self) -> None:
        while True:
            block = await self._queue.get()
            try:
                await self._handler(block)
            except Exception:
                logger.exception("Minibook mirror failed for ledger block %s", block.get("index"))
            finally:
                self._queue.task_done()
