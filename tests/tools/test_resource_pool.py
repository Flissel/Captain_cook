"""Tests for BoundedTool, the concurrency limiter over Tool.run()."""
import asyncio
from typing import Any

import pytest

from agenten.tools.base import Tool
from agenten.tools.resource_pool import BoundedTool


class _RecordingTool(Tool):
    """Fake inner tool that sleeps briefly and records peak concurrency."""

    name = "recording_tool"

    def __init__(self, sleep_s: float = 0.05):
        self.sleep_s = sleep_s
        self.active = 0
        self.peak_active = 0
        self.calls = 0

    async def run(self, *args, **kwargs) -> Any:  # noqa: ANN401 (matches Tool signature)
        self.calls += 1
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            await asyncio.sleep(self.sleep_s)
        finally:
            self.active -= 1
        return "ok"


class _AlwaysRaisesTool(Tool):
    """Fake inner tool whose .run() always raises."""

    name = "always_raises_tool"

    def __init__(self):
        self.calls = 0

    async def run(self, *args, **kwargs) -> Any:  # noqa: ANN401
        self.calls += 1
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_concurrency_is_bounded():
    inner = _RecordingTool(sleep_s=0.05)
    max_concurrency = 3
    bounded = BoundedTool(inner, max_concurrency=max_concurrency)

    total_calls = 10
    results = await asyncio.gather(*(bounded.run() for _ in range(total_calls)))

    assert results == ["ok"] * total_calls
    assert inner.calls == total_calls
    assert inner.peak_active <= max_concurrency
    # Sanity: with 10 calls vs a concurrency cap of 3, the cap should
    # actually have been hit (not just trivially satisfied).
    assert inner.peak_active == max_concurrency


@pytest.mark.asyncio
async def test_exception_propagates_and_releases_semaphore():
    inner = _AlwaysRaisesTool()
    max_concurrency = 2
    bounded = BoundedTool(inner, max_concurrency=max_concurrency)

    # Call .run() max_concurrency times *in a row* (sequentially awaited).
    # If an exception ever failed to release the semaphore, one of these
    # would hang forever waiting to acquire it -- so simply completing
    # this loop (under a timeout) is the assertion that nothing leaked.
    async def run_and_expect_raise():
        with pytest.raises(RuntimeError, match="boom"):
            await bounded.run()

    await asyncio.wait_for(
        asyncio.gather(*(run_and_expect_raise() for _ in range(max_concurrency))),
        timeout=5,
    )

    # A subsequent call should also complete promptly (semaphore not stuck).
    with pytest.raises(RuntimeError, match="boom"):
        await asyncio.wait_for(bounded.run(), timeout=5)

    assert inner.calls == max_concurrency + 1


def test_default_name_delegates_to_inner_tool():
    inner = _RecordingTool()
    bounded = BoundedTool(inner, max_concurrency=1)
    assert bounded.name == inner.name


def test_explicit_name_overrides_inner_tool_name():
    inner = _RecordingTool()
    bounded = BoundedTool(inner, max_concurrency=1, name="custom_name")
    assert bounded.name == "custom_name"


@pytest.mark.parametrize("max_concurrency", [0, -1])
def test_rejects_non_positive_max_concurrency(max_concurrency):
    # max_concurrency=0 would make asyncio.Semaphore(0) permanently
    # unacquirable, silently deadlocking every future .run() call rather
    # than raising -- reject it up front instead.
    inner = _RecordingTool()
    with pytest.raises(ValueError):
        BoundedTool(inner, max_concurrency=max_concurrency)
