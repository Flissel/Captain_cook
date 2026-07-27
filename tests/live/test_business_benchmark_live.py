"""Explicit provider-live gate for business benchmark execution."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import pytest

from agenten.agent_factory.business_benchmark_live import (
    run_provider_business_benchmarks,
)


pytestmark = pytest.mark.live


async def _runtime_health(url: str) -> bool:
    request = Request(url.rstrip("/") + "/health", method="GET")
    try:
        with urlopen(request, timeout=5) as response:  # noqa: S310 - explicit live gate
            return 200 <= response.status < 300
    except (OSError, URLError):
        return False


@pytest.mark.asyncio
async def test_provider_backed_business_benchmark_preflight() -> None:
    """Missing live prerequisites are failures, never skips or mock success."""

    # The default production composition loader is deliberately fail-closed with
    # TODO_TOOL.v1 until the adapter/capability bridge exists. Once integrated,
    # this same call performs health preflight and requires the complete
    # 30/30/60 finalized receipt scope plus Captain summaries/evidence.
    await run_provider_business_benchmarks(
        os.environ,
        repository_root=Path.cwd(),
    )
