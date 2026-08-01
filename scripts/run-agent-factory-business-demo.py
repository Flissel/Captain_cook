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
from agenten.agent_factory.codex_build_execution import FactoryCodexBuildInterrupted


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
    parser.add_argument("--hermes-reasoning-effort", required=True)
    parser.add_argument("--hermes-max-usd", default="0.10")
    parser.add_argument("--stop-before-quality-warden", action="store_true")
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
        hermes_reasoning_effort=args.hermes_reasoning_effort,
        hermes_maximum_total_cost_usd=Decimal(args.hermes_max_usd),
        maximum_dispatches=args.maximum_dispatches,
        stop_before_quality_warden=args.stop_before_quality_warden,
    )
    try:
        results = asyncio.run(
            run_business_demo_factory_jobs(settings, environment=os.environ)
        )
    except FactoryCodexBuildInterrupted as interruption:
        binding = interruption.authorization_binding
        if binding is None:
            raise RuntimeError(
                "Factory Codex interruption lacks Captain resume bindings"
            ) from interruption
        print(
            json.dumps(
                {
                    "schema": "captain.business-demo-factory-operator.v1",
                    "database": "captain_test",
                    "status": "codex_build_interrupted",
                    "exit_code": interruption.exit_code,
                    "reason": interruption.reason,
                    "checkpoint_ref": interruption.checkpoint_ref.model_dump(
                        mode="json"
                    ),
                    "terminal_receipt_ref": (
                        interruption.terminal_receipt_ref.model_dump(mode="json")
                    ),
                    "next_resume_ordinal": (
                        interruption.resume_ordinal + 1
                        if interruption.resume_ordinal < 2
                        else None
                    ),
                    "captain_authorization_binding": binding.as_dict(),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
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
