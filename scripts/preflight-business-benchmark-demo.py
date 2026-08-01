"""Resolve the real Gateway benchmark scope without executing a provider case."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from pathlib import Path
import sys
from uuid import UUID


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from agenten.agent_factory.business_benchmark_live import (  # noqa: E402
    LiveBusinessBenchmarkSettings,
    load_production_business_benchmark_composition,
)
from agenten.agent_factory.business_benchmark_demo_provisioning import (  # noqa: E402
    assert_local_captain_test_dsn,
)
from gateway.business_benchmark_demo import (  # noqa: E402
    resolve_current_factory_attempts,
)


def _with_current_factory_attempts(
    environment: Mapping[str, str],
) -> dict[str, str]:
    """Bind preflight to Captain's current attempts without executing effects."""

    resolved = dict(environment)
    profiles = ("CLAIMS", "RENEWAL")
    required = (
        "TEST_MARIADB_DSN",
        *(f"CAPTAIN_BENCHMARK_{profile}_JOB_ID" for profile in profiles),
    )
    if any(not resolved.get(name, "").strip() for name in required):
        return resolved
    dsn = resolved["TEST_MARIADB_DSN"].strip()
    assert_local_captain_test_dsn(dsn)
    job_ids = tuple(
        UUID(resolved[f"CAPTAIN_BENCHMARK_{profile}_JOB_ID"])
        for profile in profiles
    )
    current_attempts = resolve_current_factory_attempts(dsn, job_ids)
    for profile in profiles:
        job_id = UUID(resolved[f"CAPTAIN_BENCHMARK_{profile}_JOB_ID"])
        attempt = current_attempts[job_id]
        if isinstance(attempt, bool) or not 1 <= attempt <= 5:
            raise ValueError("current Captain Factory attempt is invalid")
        resolved[f"CAPTAIN_BENCHMARK_{profile}_ATTEMPT"] = str(attempt)
    return resolved
from agenten.agent_factory.business_benchmark_production import (  # noqa: E402
    BusinessBenchmarkProductionScopeError,
)


async def preflight(
    environment: Mapping[str, str],
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> dict[str, object]:
    """Build and preflight the default composition, never its effectful run method."""

    resolved_environment = _with_current_factory_attempts(environment)
    settings = LiveBusinessBenchmarkSettings.from_environment(
        resolved_environment,
        repository_root=repository_root,
    )
    composition = load_production_business_benchmark_composition(
        settings,
        environment=resolved_environment,
    )
    try:
        try:
            scopes = await composition.preflight(
                settings,
                resolved_environment,
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
