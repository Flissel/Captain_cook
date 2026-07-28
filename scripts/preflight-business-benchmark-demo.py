"""Resolve the real Gateway benchmark scope without executing a provider case."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from agenten.agent_factory.business_benchmark_live import (  # noqa: E402
    LiveBusinessBenchmarkSettings,
    load_production_business_benchmark_composition,
)
from agenten.agent_factory.business_benchmark_production import (  # noqa: E402
    BusinessBenchmarkProductionScopeError,
)


async def preflight(
    environment: Mapping[str, str],
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> dict[str, object]:
    """Build and preflight the default composition, never its effectful run method."""

    settings = LiveBusinessBenchmarkSettings.from_environment(
        environment,
        repository_root=repository_root,
    )
    composition = load_production_business_benchmark_composition(
        settings,
        environment=environment,
    )
    try:
        try:
            scopes = await composition.preflight(
                settings,
                environment,
                repository_root=repository_root,
            )
        except BusinessBenchmarkProductionScopeError:
            return {
                "schema": "captain.business-benchmark-default-preflight.v1",
                "status": "factory_dispatch_required",
                "database": "captain_test",
                "production_scope_resolvable": False,
            }
        return {
            "schema": "captain.business-benchmark-default-preflight.v1",
            "status": "resolvable",
            "database": "captain_test",
            "production_scope_resolvable": True,
            "jobs": [
                {
                    "job_id": str(scope.job_id),
                    "candidate_id": scope.candidate_id,
                    "attempt": scope.attempt,
                }
                for scope in scopes
            ],
        }
    finally:
        closer = getattr(composition, "aclose", None)
        if callable(closer):
            await closer()


def main() -> int:
    result = asyncio.run(preflight(dict(os.environ)))
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
