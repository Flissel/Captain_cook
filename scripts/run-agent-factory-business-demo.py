from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
from uuid import UUID

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from gateway.agent_factory_live_operator import (
    FactoryLiveOperatorSettings,
    run_business_demo_factory_jobs,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resume two Captain business-demo Factory jobs.",
    )
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--python-executable", type=Path, required=True)
    parser.add_argument("--hermes-python-executable", type=Path, required=True)
    parser.add_argument("--job-id", type=UUID, action="append", required=True)
    parser.add_argument("--maximum-dispatches", type=int, default=12)
    parser.add_argument("--hermes-provider", default="openai-api")
    parser.add_argument("--hermes-model", required=True)
    parser.add_argument("--hermes-max-usd", default="0.10")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if len(args.job_id) != 2:
        raise SystemExit("exactly two --job-id values are required")
    settings = FactoryLiveOperatorSettings(
        workspace_root=args.workspace_root,
        python_executable=args.python_executable,
        hermes_python_executable=args.hermes_python_executable,
        test_mariadb_dsn=os.environ.get("TEST_MARIADB_DSN", ""),
        job_ids=(args.job_id[0], args.job_id[1]),
        hermes_provider=args.hermes_provider,
        hermes_model=args.hermes_model,
        hermes_maximum_total_cost_usd=Decimal(args.hermes_max_usd),
        maximum_dispatches=args.maximum_dispatches,
    )
    results = asyncio.run(
        run_business_demo_factory_jobs(settings, environment=os.environ)
    )
    print(
        json.dumps(
            {
                "schema": "captain.business-demo-factory-operator.v1",
                "database": "captain_test",
                "results": [
                    item.model_dump(mode="json", by_alias=True) for item in results
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
