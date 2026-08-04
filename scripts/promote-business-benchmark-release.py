from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from uuid import UUID

import httpx


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from agenten.agent_factory.workflow_promotion import promote_release_workflow


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Promote one release-mode benchmark workflow through Captain Gateway."
    )
    parser.add_argument("--job-id", type=UUID, required=True)
    parser.add_argument("--occurred-at", required=True)
    return parser


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def main() -> int:
    args = _parser().parse_args()
    try:
        occurred_at = datetime.fromisoformat(
            args.occurred_at.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise SystemExit("promotion occurrence time is invalid") from exc
    if (
        occurred_at.tzinfo is None
        or occurred_at.utcoffset() != timezone.utc.utcoffset(occurred_at)
    ):
        raise SystemExit("promotion occurrence time must be UTC")

    gateway_url = _required_env("CAPTAIN_BENCHMARK_GATEWAY_URL")
    captain_token = _required_env("CAPTAIN_GATEWAY_TOKEN")
    with httpx.Client(
        base_url=gateway_url,
        headers={"Authorization": f"Bearer {captain_token}"},
        timeout=30.0,
    ) as client:
        result = promote_release_workflow(
            client=client,
            job_id=args.job_id,
            occurred_at=occurred_at,
        )

    print(
        json.dumps(
            {
                "schema": "captain.workflow-promotion-result.v1",
                "job_id": str(result.job_id),
                "promotion_event_id": str(result.promotion_event_id),
                "replayed": result.replayed,
                "status": result.status.value,
                "phase": result.phase.value,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
