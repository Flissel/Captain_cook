"""Explicit provider-live gate for business benchmark execution."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import pytest

from agenten.agent_factory.business_benchmark_live import (
    LiveBusinessBenchmarkPreflight,
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

    await LiveBusinessBenchmarkPreflight(
        health_check=_runtime_health,
        # TODO_TOOL.v1: production_adapter_bundle/capability bridges are not
        # integrated on this branch. Supplying None is the honest production
        # state and the preflight must fail before a provider effect.
        runtime_bundle=None,
    ).validate_environment(os.environ, repository_root=Path.cwd())
