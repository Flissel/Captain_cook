"""Command-line entry point for the standalone Captain planner."""

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import List, Optional, Sequence

from autogen_core.models import ChatCompletionClient

from agenten.llm.model_client import build_model_client
from agenten.planning.factory import build_captain_pipeline


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
    return parser


async def async_main(
    argv: Optional[Sequence[str]] = None,
    *,
    model_client: Optional[ChatCompletionClient] = None,
) -> int:
    args = build_parser().parse_args(argv)
    project_description = args.project.read_text(encoding="utf-8")
    client = model_client if model_client is not None else build_model_client(model=args.model)
    pipeline = build_captain_pipeline(
        model_client=client,
        output_dir=args.output,
        target=args.target,
        known_capability_tags=list(args.capabilities),
    )
    result = await pipeline.run(project_description)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
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
