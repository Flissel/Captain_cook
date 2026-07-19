"""Release a Captain-approved evaluation manifest through a fenced planning boundary."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Sequence

import httpx

from agenten.evaluation.models import EvaluationManifest
from agenten.planning.captain_pipeline import CaptainPipeline
from agenten.planning.evaluation_bridge import (
    EvaluationBridgePolicy,
    release_accepted_evaluation,
)
from agenten.planning.gateway_client import GatewayPlanningClient
from agenten.planning.release import JsonDirectoryReleaseClient
from agenten.planning.run_store import JsonCaptainRunStore


class EvaluationReleaseConfigurationError(RuntimeError):
    """A release boundary lacks its explicit Captain-owned configuration."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="captain-evaluation-release")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--target", default="codex")
    parser.add_argument("--capability", action="append", dest="capabilities", required=True)
    parser.add_argument("--output", type=Path, default=Path("artifacts/captain-release"))
    parser.add_argument("--run-dir", type=Path, default=Path("artifacts/captain-runs"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--release-mode", choices=("json", "gateway"), default="json")
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8000")
    return parser


async def async_main(
    argv: Sequence[str] | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = EvaluationManifest.model_validate_json(args.manifest.read_bytes())
    except (OSError, ValueError):
        _print({"error": "invalid_manifest", "status": "failed"})
        return 1
    token = os.getenv("CAPTAIN_GATEWAY_TOKEN") if args.release_mode == "gateway" else None
    if args.release_mode == "gateway" and not token:
        raise EvaluationReleaseConfigurationError(
            "CAPTAIN_GATEWAY_TOKEN is required for gateway release mode"
        )

    async def release(http: httpx.AsyncClient | None):
        release_client = (
            GatewayPlanningClient(args.gateway_url, token, http)
            if args.release_mode == "gateway" and token is not None and http is not None
            else JsonDirectoryReleaseClient(args.output)
        )
        pipeline = CaptainPipeline(
            decompose=_unused_decompose,
            align=_unused_align,
            enrich=_unused_enrich,
            release_client=release_client,
            run_store=JsonCaptainRunStore(args.run_dir),
            target=args.target,
        )
        return await release_accepted_evaluation(
            manifest,
            policy=EvaluationBridgePolicy(
                target=args.target,
                capability_tags=tuple(args.capabilities),
                allowed_targets=frozenset({args.target}),
                allowed_capability_tags=frozenset(args.capabilities),
            ),
            pipeline=pipeline,
            run_id=args.run_id,
        )

    if args.release_mode == "gateway" and http_client is None:
        async with httpx.AsyncClient(timeout=30) as owned_client:
            compiled = await release(owned_client)
    else:
        compiled = await release(http_client)
    _print({"run_id": args.run_id, "status": "released", "batch_ids": [batch.batch_id for batch in compiled.batches]})
    return 0


async def _unused_decompose(_: str):
    raise AssertionError("evaluation release must not invoke LLM decomposition")


async def _unused_align(*_: object):
    raise AssertionError("evaluation release must not invoke LLM alignment")


async def _unused_enrich(*_: object):
    raise AssertionError("evaluation release must not invoke LLM enrichment")


def _print(value: dict[str, object]) -> None:
    print(json.dumps(value, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
