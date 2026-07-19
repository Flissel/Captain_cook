"""Captain-owned startup recovery command for Gateway delivery claims."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path

import httpx

from agenten.delivery.gateway_client import GatewayDeliveryClient
from agenten.delivery.recovery import GatewayRecoveryService
from agenten.delivery.worker_recovery import LocalCodexWorkerRecoveryDirector


logger = logging.getLogger(__name__)


class GatewayRecoveryConfigurationError(RuntimeError):
    """Recovery cannot safely connect with the configured Captain identity."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="captain-recover-gateway",
        description="Run one Captain-owned, fail-closed Gateway recovery pass.",
    )
    parser.add_argument(
        "--gateway-url",
        default=os.getenv("CAPTAIN_GATEWAY_URL", "http://127.0.0.1:8090"),
        help="authenticated Captain Gateway base URL",
    )
    parser.add_argument(
        "--codex-state-dir",
        type=Path,
        default=Path(os.getenv("CAPTAIN_CODEX_SESSION_STATE_DIR", ".captain-cook/codex-sessions")),
        help="host-local directory containing <session-id>.json process evidence",
    )
    return parser


async def async_main(
    argv: Sequence[str] | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
    now: Callable[[], datetime] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    token = os.getenv("CAPTAIN_GATEWAY_TOKEN")
    if not token:
        raise GatewayRecoveryConfigurationError(
            "CAPTAIN_GATEWAY_TOKEN is required for Gateway recovery"
        )
    clock = now or (lambda: datetime.now(timezone.utc))

    async def recover(client: httpx.AsyncClient) -> int:
        gateway = GatewayDeliveryClient(args.gateway_url, token, client)
        director = LocalCodexWorkerRecoveryDirector(
            client=gateway,
            state_dir=args.codex_state_dir,
        )
        outcome = await GatewayRecoveryService(
            gateway,
            prepare_for_requeue=director.ready_for_requeue,
        ).recover_expired_pass(clock())
        print(
            json.dumps(
                {
                    "recovered_batch_ids": [
                        decision.batch_id for decision in outcome.recovered
                    ],
                    "deferred_batch_ids": list(outcome.deferred_batch_ids),
                },
                sort_keys=True,
            )
        )
        return 0

    if http_client is not None:
        return await recover(http_client)
    async with httpx.AsyncClient() as owned_client:
        return await recover(owned_client)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return asyncio.run(async_main(argv))
    except Exception:
        logger.exception("Captain Gateway recovery failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
