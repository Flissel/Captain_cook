"""Command-line entry point for the standalone Captain planner."""

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import List, Optional, Sequence

import httpx
from autogen_core.models import ChatCompletionClient

from agenten.llm.model_client import build_model_client
from agenten.planning.factory import build_captain_pipeline
from agenten.planning.gateway_client import GatewayPlanningClient


logger = logging.getLogger(__name__)


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
        help="configured executor target label; the LLM does not choose it",
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
        help="publication boundary; json remains the offline default",
    )
    parser.add_argument(
        "--gateway-url",
        default="http://127.0.0.1:8000",
        help="ledger gateway base URL used only in gateway mode",
    )
    return parser


async def async_main(
    argv: Optional[Sequence[str]] = None,
    *,
    model_client: Optional[ChatCompletionClient] = None,
    http_client: httpx.AsyncClient | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    project_description = args.project.read_text(encoding="utf-8")
    client = model_client if model_client is not None else build_model_client(model=args.model)
    owned_http: httpx.AsyncClient | None = None
    gateway: GatewayPlanningClient | None = None
    if args.release_mode == "gateway":
        if http_client is None:
            owned_http = httpx.AsyncClient()
            http_client = owned_http
        gateway = GatewayPlanningClient(args.gateway_url, http_client)

    try:
        pipeline = build_captain_pipeline(
            model_client=client,
            output_dir=args.output,
            target=args.target,
            known_capability_tags=list(args.capabilities),
            release_client=gateway,
            capability_resolver=gateway,
        )
        result = await pipeline.run(project_description)
    finally:
        if owned_http is not None:
            await owned_http.aclose()
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "release_mode": args.release_mode,
                "released_batches": [batch.batch_id for batch in result.batches],
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
