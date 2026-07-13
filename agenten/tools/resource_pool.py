"""Concurrency limiter for tools that wrap a scarce underlying resource.

On-demand agent spawning (see the Captain/armada architecture) can create
many agent instances in a very short window. If several of those instances
share a ``Tool`` that wraps something expensive and stateful -- e.g.
``agenten/tools/internet_search.py``'s ``InternetSearchTool``, which drives a
real Selenium browser session -- the number of *agent instances* is the
wrong thing to bound: two different budgets are in play.

Subproblem-count budgeting (how many agents get spawned in the first place)
is a separate concern handled elsewhere in the fleet. ``BoundedTool`` bounds
the other axis directly: no matter how many agents hold a reference to the
same tool, only ``max_concurrency`` calls to its ``.run()`` are ever in
flight at once. Everything past that limit simply waits its turn.
"""
from typing import Any, Optional

import asyncio

from .base import Tool


class BoundedTool(Tool):
    """Wraps another ``Tool``, bounding how many of its ``.run()`` calls can
    be in flight concurrently -- independent of how many agent instances
    exist, which is the whole point (agent-instance-count bounding happens
    elsewhere; this bounds the scarce underlying resource directly).
    """

    def __init__(self, inner: Tool, max_concurrency: int, name: Optional[str] = None):
        if max_concurrency < 1:
            raise ValueError(f"max_concurrency must be >= 1, got {max_concurrency}")
        self.inner = inner
        self.name = name or inner.name
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        async with self._semaphore:
            return await self.inner.run(*args, **kwargs)
