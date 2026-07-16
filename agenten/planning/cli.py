"""Command-line entry point for the standalone Captain planner."""

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import List, Optional, Sequence

import httpx
from autogen_core.models import ChatCompletionClient

from agenten.llm.model_client import build_model_client
from agenten.planning.autonomous import AutonomousCaptainPlanner, AutonomousPlanningResult
from agenten.planning.factory import build_captain_pipeline
from agenten.planning.gateway_client import GatewayPlanningClient
from agenten.planning.run_store import JsonCaptainRunStore


logger = logging.getLogger(__name__)


class GatewayPlanningConfigurationError(RuntimeError):
    """Gateway release mode is missing required non-secret configuration."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="captain-plan",
        description="Turn a project description into validated external work-batch contracts.",
    )
    parser.add_argument("project", type=Path, help="UTF-8 project description file")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/captain-release"),
        help="directory receiving separate batches/ and holdouts/ contracts",
    )
    parser.add_argument(
        "--target",
        default="external",
        help="default executor target label",
    )
    parser.add_argument(
        "--allowed-target",
        action="append",
        dest="allowed_targets",
        help="allowlisted executor target (repeat for a mixed-target DAG)",
    )
    parser.add_argument(
        "--capability",
        action="append",
        dest="capabilities",
        required=True,
        help="allowed capability tag (repeat for multiple tags)",
    )
    parser.add_argument("--model", help="override CAPTAIN_MODEL for this run")
    parser.add_argument(
        "--release-mode",
        choices=("json", "gateway"),
        default="json",
        help="publication boundary; json remains the deterministic offline default",
    )
    parser.add_argument(
        "--gateway-url",
        default="http://127.0.0.1:8000",
        help="Captain ledger gateway base URL used only in gateway mode",
    )
    parser.add_argument(
        "--run-id",
        help="durable id used to resume an interrupted gateway release",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=Path("artifacts/captain-runs"),
        help="directory containing atomic Captain run checkpoints",
    )
    return parser


async def async_main(
    argv: Optional[Sequence[str]] = None,
    *,
    model_client: Optional[ChatCompletionClient] = None,
    http_client: httpx.AsyncClient | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.run_id is not None and args.release_mode != "gateway":
        raise GatewayPlanningConfigurationError(
            "--run-id requires gateway release mode"
        )
    gateway_token: str | None = None
    if args.release_mode == "gateway":
        gateway_token = os.getenv("CAPTAIN_GATEWAY_TOKEN")
        if not gateway_token:
            raise GatewayPlanningConfigurationError(
                "CAPTAIN_GATEWAY_TOKEN is required for gateway release mode"
            )

    client = model_client if model_client is not None else build_model_client(model=args.model)

    async def run(http: httpx.AsyncClient | None) -> AutonomousPlanningResult:
        gateway = (
            GatewayPlanningClient(args.gateway_url, gateway_token, http)
            if args.release_mode == "gateway" and gateway_token is not None and http is not None
            else None
        )
        pipeline = build_captain_pipeline(
            model_client=client,
            output_dir=args.output,
            target=args.target,
            allowed_targets=list(args.allowed_targets) if args.allowed_targets else None,
            known_capability_tags=list(args.capabilities),
            release_client=gateway,
            capability_resolver=gateway,
            run_store=(
                JsonCaptainRunStore(args.run_dir)
                if gateway is not None and args.run_id is not None
                else None
            ),
        )
        return await AutonomousCaptainPlanner(
            pipeline=pipeline,
            output_dir=args.output,
        ).run(
            args.project,
            source_reference=args.project.name,
            release_compiled=gateway is not None,
            run_id=args.run_id,
        )

    if args.release_mode == "gateway" and http_client is None:
        async with httpx.AsyncClient() as owned_http_client:
            result = await run(owned_http_client)
    else:
        result = await run(http_client)

    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "canonical_plan_id": result.plan.plan_id,
                "released_batches": [
                    package.batch_id for package in result.plan.work_packages
                ],
                "worker_pool": list(result.plan.worker_pool),
            },
            ensure_ascii=False,
        )
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    try:
        return asyncio.run(async_main(argv))
    except Exception:
        logger.exception("Captain planning failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
